"""
Cross-harness semantic-status engine.

Covers the pure state machine (`derive_status`), the prompt matcher
(`screen_matches_any`), the producer-arbitration rule (`may_write`), the
per-harness awaiting-input detector on the harness base, and the
`mark_agent_viewed` read-transition on the orchestrator.

These pin the behavior that makes semantic status reliable for EVERY harness
(not just Claude Code's vendor hook): screen-activity → working, settle →
complete-unread, prompt-on-screen → awaiting-input, view → idle, and "defer to
a fresh vendor hook but reclaim a stale one".
"""

from __future__ import annotations

import pytest

from relaydeck import semantic_engine as se
from relaydeck.semantic_engine import (
    _Track,
    derive_status,
    may_write,
    screen_matches_any,
)


# ── screen_matches_any ────────────────────────────────────────────


def test_match_anchored_yes_no_prompt():
    assert screen_matches_any("Proceed? [y/N]", (r"\[y/n\]\s*$",))


def test_match_is_case_insensitive():
    assert screen_matches_any("AWAITING YOUR INPUT", (r"awaiting .*input",))


def test_no_match_on_plain_prose():
    # A sentence merely mentioning approval shouldn't trip an anchored prompt.
    pats = (r"\[y/n\]\s*$", r"\bapprove\b.*\?\s*$")
    assert not screen_matches_any("I will approve the change and move on.", pats)


def test_match_only_considers_screen_tail():
    # An 'approve?' far up the scrollback (>12 non-empty lines back) is stale.
    body = "\n".join(f"line {i}" for i in range(40))
    screen = "Allow command? \n" + body
    assert not screen_matches_any(screen, (r"\ballow command\?",))


def test_empty_inputs_never_match():
    assert not screen_matches_any("", (r".",))
    assert not screen_matches_any("something", ())


def test_bad_regex_is_skipped_not_raised():
    # A malformed pattern must not blow up detection for the rest.
    assert screen_matches_any("Proceed? [y/N]", (r"(", r"\[y/n\]\s*$"))


# ── may_write (producer arbitration) ──────────────────────────────


def test_engine_defers_to_fresh_hook():
    # A vendor hook set the status 5s ago — engine stays out of the way.
    assert may_write("hook", at=100.0, now=105.0) is False


def test_engine_reclaims_stale_hook():
    # The hook went silent 60s ago (> STALE_S) — engine self-heals.
    assert may_write("hook", at=100.0, now=160.0) is True


def test_engine_owns_unforced_field():
    assert may_write(None, at=None, now=100.0) is True


def test_engine_overwrites_its_own_writes():
    assert may_write(se.ENGINE_SOURCE, at=100.0, now=101.0) is True


def test_engine_defers_to_hitl_and_manual_when_fresh():
    assert may_write("hitl", at=100.0, now=101.0) is False
    assert may_write("manual", at=100.0, now=101.0) is False


def test_viewer_source_is_not_authoritative():
    # A read-transition shouldn't lock the engine out.
    assert may_write("viewer", at=100.0, now=101.0) is True


# ── derive_status (the state machine) ─────────────────────────────


def _settle(track, h, t):
    """Advance past the settle window with no screen change."""
    return derive_status(track, screen_hash=h, awaiting=False, now=t)


def test_changing_screen_is_working():
    tr = _Track()
    # first sample establishes baseline (no edge)
    assert derive_status(tr, screen_hash="a", awaiting=False, now=0.0) is None
    # screen changed → working
    assert derive_status(tr, screen_hash="b", awaiting=False, now=1.0) == "working"
    # still changing → no repeated event
    assert derive_status(tr, screen_hash="c", awaiting=False, now=2.0) is None


def test_work_then_settle_is_complete_unread():
    tr = _Track()
    derive_status(tr, screen_hash="a", awaiting=False, now=0.0)
    derive_status(tr, screen_hash="b", awaiting=False, now=1.0)  # working
    derive_status(tr, screen_hash="c", awaiting=False, now=4.0)  # still working
    # First settle after spawn = idle baseline (not a phantom completion).
    assert _settle(tr, "c", 9.0) == "idle"
    # A *subsequent* real work burst that settles → complete-unread.
    assert derive_status(tr, screen_hash="d", awaiting=False, now=10.0) == "working"
    derive_status(tr, screen_hash="e", awaiting=False, now=13.0)
    assert _settle(tr, "e", 18.0) == "complete-unread"


def test_brief_flicker_settles_to_idle_not_complete_unread():
    tr = _Track()
    derive_status(tr, screen_hash="a", awaiting=False, now=0.0)
    # consume the spawn baseline first
    assert _settle(tr, "a", 5.0) == "idle"
    # a sub-MIN_WORK_S flicker then quiet → idle, not complete-unread
    assert derive_status(tr, screen_hash="b", awaiting=False, now=6.0) == "working"
    assert _settle(tr, "b", 11.0) == "idle"


def test_awaiting_input_takes_priority_and_emits_once():
    tr = _Track()
    derive_status(tr, screen_hash="a", awaiting=False, now=0.0)
    assert derive_status(tr, screen_hash="b", awaiting=True, now=1.0) == "awaiting-input"
    # still awaiting → no repeat event
    assert derive_status(tr, screen_hash="b", awaiting=True, now=2.0) is None


def test_steady_state_returns_none():
    tr = _Track()
    derive_status(tr, screen_hash="a", awaiting=False, now=0.0)
    _settle(tr, "a", 5.0)  # baseline → idle
    # nothing changes, already settled
    assert _settle(tr, "a", 6.0) is None
    assert _settle(tr, "a", 99.0) is None


# ── harness base: detect_awaiting_input ───────────────────────────


def test_harness_base_detects_generic_prompt():
    from relaydeck.harness import HarnessAgent

    # Use the unbound method against a minimal stand-in carrying the patterns.
    class _Stub:
        AWAITING_INPUT_PATTERNS = HarnessAgent.AWAITING_INPUT_PATTERNS
        detect_awaiting_input = HarnessAgent.detect_awaiting_input

    stub = _Stub()
    assert stub.detect_awaiting_input("Continue? [y/N]") is True
    assert stub.detect_awaiting_input("just some output text") is False


def test_codex_adds_its_own_prompts():
    from plugins.harnesses.codex.agent import CodexAgent
    from relaydeck.harness import HarnessAgent

    # Codex extends, not replaces, the base patterns.
    assert set(HarnessAgent.AWAITING_INPUT_PATTERNS).issubset(
        set(CodexAgent.AWAITING_INPUT_PATTERNS)
    )
    assert len(CodexAgent.AWAITING_INPUT_PATTERNS) > len(
        HarnessAgent.AWAITING_INPUT_PATTERNS
    )


# ── orchestrator: mark_agent_viewed (read-transition) ─────────────


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    from pathlib import Path

    import relaydeck.orchestrator as orch_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True, exist_ok=True)
    orch_mod._orchestrator = None
    o = orch_mod.get_orchestrator(tmp_path / ".relaydeck")
    from relaydeck.db import open_db, upsert_agent

    conn = open_db(o.db_path)
    try:
        upsert_agent(conn, "alice", "pi", "alice", workspace="w")
    finally:
        conn.close()
    return o


def test_mark_viewed_clears_complete_unread(orch):
    orch.set_semantic_status("alice", "complete-unread", source="engine")
    assert orch.mark_agent_viewed("alice") is True
    assert orch.get_agent("alice")["semantic_status"] == "idle"


def test_mark_viewed_is_noop_when_working(orch):
    orch.set_semantic_status("alice", "working", source="engine")
    assert orch.mark_agent_viewed("alice") is False
    assert orch.get_agent("alice")["semantic_status"] == "working"


def test_mark_viewed_is_idempotent(orch):
    orch.set_semantic_status("alice", "complete-unread", source="engine")
    assert orch.mark_agent_viewed("alice") is True
    assert orch.mark_agent_viewed("alice") is False  # already idle


def test_viewed_transition_is_source_tagged_viewer(orch):
    from relaydeck.db import get_semantic_status, open_db

    orch.set_semantic_status("alice", "complete-unread", source="engine")
    orch.mark_agent_viewed("alice")
    conn = open_db(orch.db_path)
    try:
        status, _at, source = get_semantic_status(conn, "alice")
    finally:
        conn.close()
    assert status == "idle"
    assert source == "viewer"
