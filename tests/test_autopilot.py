"""
Autopilot plugin — the auto-answer side of the awaiting-input loop.

Two layers under test:
  1. `match_unblock_rule` — the PURE decision (which curated rule, if any,
     fires for a rendered screen at a given mode). No daemon needed; this is
     also what `relaydeck autopilot test` runs.
  2. The handler — booted against a real PluginEventBus + a mocked
     orchestrator (the hitl-test pattern): an `agent.status_changed →
     awaiting-input` event renders the agent's screen and, for a benign
     match, sends the answer through the harness's send_input/send_message
     and emits `autopilot.unblocked`; an unknown prompt is left for a human
     (`autopilot.held`), never guessed at.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from relaydeck.plugin import Event, PluginContext, PluginEventBus
from plugins.autopilot.plugin import _DEFAULTS, _legacy_on_load, match_unblock_rule


# ── Pure matcher ───────────────────────────────────────────────────


def test_defaults_match_manifest():
    assert _DEFAULTS["mode"] == "benign"
    assert _DEFAULTS["auto_accept_terms"] is False


def test_press_enter_is_benign():
    r = match_unblock_rule("…done.\npress enter to continue", mode="benign")
    assert r is not None and r.name == "press-enter"
    assert r.action == {"key": "enter"}


def test_trust_workspace_is_benign():
    r = match_unblock_rule(
        "Do you trust the files in this folder? [y/N]", mode="benign"
    )
    assert r is not None and r.name == "trust-workspace"
    assert r.action == {"data": "y", "enter": True}


def test_generic_yn_default_is_never_auto_answered():
    """Safety: an unrecognized [Y/n]/[y/N] prompt is HELD, never blindly
    accepted — the shared matcher is case-insensitive so the default can't
    be told from the keys, and a wrong default could be destructive."""
    screen = "Continue with setup? [Y/n]"
    assert match_unblock_rule(screen, mode="benign") is None
    assert match_unblock_rule(screen, mode="all-known", allow_terms=True) is None


def test_update_deferral_is_all_known():
    screen = "Update available! Install now? [y/N]"
    assert match_unblock_rule(screen, mode="benign") is None
    r = match_unblock_rule(screen, mode="all-known")
    assert r is not None and r.name == "defer-update"
    assert r.action == {"data": "n", "enter": True}


def test_terms_gated_behind_allow_terms():
    screen = "Do you accept the terms of service? [y/N]"
    assert match_unblock_rule(screen, mode="all-known", allow_terms=False) is None
    r = match_unblock_rule(screen, mode="all-known", allow_terms=True)
    assert r is not None and r.name == "accept-terms"


def test_off_mode_never_matches():
    assert match_unblock_rule("press enter to continue", mode="off") is None


def test_unknown_prompt_does_not_match():
    screen = "Delete the production database? (type DELETE to confirm)"
    assert match_unblock_rule(screen, mode="all-known", allow_terms=True) is None


# ── Handler (booted plugin) ────────────────────────────────────────


class _FakeInstance:
    def __init__(self, screen: bytes):
        self._buf = screen
        self.inputs: list[bytes] = []
        self.messages: list[str] = []

    def get_pty_buffer(self) -> bytes:
        return self._buf

    def send_input(self, data: bytes) -> bool:
        self.inputs.append(data)
        return True

    def send_message(self, text: str) -> bool:
        self.messages.append(text)
        return True


@pytest.fixture
def ctx(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "runtime").mkdir(parents=True)
    orch = MagicMock()
    orch.get_agent.return_value = {"id": "alice", "workspace": "demo"}
    bus = PluginEventBus()
    return PluginContext(config_home=cfg, event_bus=bus, orchestrator=orch), orch, bus


def _capture(bus, etype: str):
    seen: list[Event] = []
    bus.subscribe(etype, lambda e: seen.append(e))
    return seen


def _awaiting(bus, agent_id="alice", to="awaiting-input", source="engine"):
    bus.emit(Event(
        type="agent.status_changed",
        data={"agent_id": agent_id, "from": "working", "to": to, "source": source},
        source_plugin="orchestrator",
    ))


def test_handler_auto_answers_press_enter(ctx):
    c, orch, bus = ctx
    fake = _FakeInstance(b"Setup complete.\npress enter to continue\n")
    orch.get_running_instance.return_value = fake
    _legacy_on_load(c)
    seen = _capture(bus, "autopilot.unblocked")
    _awaiting(bus)
    assert fake.inputs == [b"\r"]
    assert len(seen) == 1
    assert seen[0].data["rule"] == "press-enter"
    assert seen[0].data["agent_id"] == "alice"


def test_handler_trusts_workspace_via_send_message(ctx):
    c, orch, bus = ctx
    fake = _FakeInstance(b"Do you trust the files in this folder? [y/N]\n")
    orch.get_running_instance.return_value = fake
    _legacy_on_load(c)
    seen = _capture(bus, "autopilot.unblocked")
    _awaiting(bus)
    # enter=True actions route through send_message (submit semantics).
    assert fake.messages == ["y"]
    assert fake.inputs == []
    assert seen[0].data["rule"] == "trust-workspace"


def test_mode_off_sends_nothing(ctx, monkeypatch):
    monkeypatch.setenv("RELAYDECK_AUTOPILOT_MODE", "off")
    c, orch, bus = ctx
    fake = _FakeInstance(b"press enter to continue\n")
    orch.get_running_instance.return_value = fake
    _legacy_on_load(c)
    seen = _capture(bus, "autopilot.unblocked")
    _awaiting(bus)
    assert fake.inputs == []
    assert seen == []


def test_unknown_prompt_is_held_not_answered(ctx):
    c, orch, bus = ctx
    fake = _FakeInstance(b"Delete production DB? (type DELETE to confirm)\n")
    orch.get_running_instance.return_value = fake
    _legacy_on_load(c)
    held = _capture(bus, "autopilot.held")
    unblocked = _capture(bus, "autopilot.unblocked")
    _awaiting(bus)
    assert fake.inputs == [] and fake.messages == []
    assert len(held) == 1
    assert held[0].data["reason"] == "no_matching_rule"
    assert unblocked == []


def test_non_awaiting_status_is_ignored(ctx):
    c, orch, bus = ctx
    fake = _FakeInstance(b"press enter to continue\n")
    orch.get_running_instance.return_value = fake
    _legacy_on_load(c)
    seen = _capture(bus, "autopilot.unblocked")
    _awaiting(bus, to="working")
    assert seen == [] and fake.inputs == []


def test_cooldown_suppresses_rapid_reattempts(ctx):
    c, orch, bus = ctx
    fake = _FakeInstance(b"press enter to continue\n")
    orch.get_running_instance.return_value = fake
    _legacy_on_load(c)
    seen = _capture(bus, "autopilot.unblocked")
    _awaiting(bus)            # answers once
    _awaiting(bus)            # within cooldown — suppressed
    assert len(seen) == 1
    assert fake.inputs == [b"\r"]
