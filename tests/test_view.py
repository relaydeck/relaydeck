"""
`relaydeck view` — the built-in textual TUI.

These tests cover the *pure* parts of the TUI module: the
detach-key state machine, the workspace grouping, and the
status-badge mapping. We don't boot the textual App or hit a
live daemon — those layers are integration concerns and would
make the suite flaky / require a TTY.

The TUI's correctness contract is:

  - `Ctrl+B` then `D` quits (tmux muscle memory)
  - `Ctrl+B` then any other key cancels (no accidental quit on
    `Ctrl+B Ctrl+B`)
  - Sidebar rows group by workspace, sorted deterministically
  - Every semantic-status value has a glyph + color so the
    badge never falls back to a generic "?" rendering
"""

from __future__ import annotations

import io
import threading

from relaydeck.transports.view import (
    SEMANTIC_STATUS_COLOR,
    SEMANTIC_STATUS_GLYPH,
    AgentRow,
    _consume_sse_response,
    _event_headers,
    detach_state,
    group_by_workspace,
    key_to_bytes,
    status_badge,
)

# ── detach_state ───────────────────────────────────────────────────


def test_ctrl_b_then_d_quits():
    """The contract: Ctrl+B arms the prefix; D commits the
    detach. Pinned because if a refactor breaks this, every
    tmux-trained operator gets stuck in the TUI."""
    prev, quit_now = detach_state("", "ctrl+b")
    assert prev == "prefix"
    assert quit_now is False

    prev, quit_now = detach_state(prev, "d")
    assert prev == ""
    assert quit_now is True


def test_ctrl_b_then_d_uppercase_also_quits():
    """Caps-lock shouldn't trap the user. D and d both detach."""
    prev, quit_now = detach_state("", "ctrl+b")
    prev, quit_now = detach_state(prev, "D")
    assert quit_now is True


def test_ctrl_b_then_other_key_cancels():
    """Pressing a non-D key after the prefix cancels — quitting
    only happens with explicit Ctrl+B D. This is the safety
    against `Ctrl+B Ctrl+B` (a common tmux nest-escape) silently
    quitting the relaydeck view."""
    prev, quit_now = detach_state("", "ctrl+b")
    prev, quit_now = detach_state(prev, "x")
    assert prev == ""
    assert quit_now is False


def test_bare_key_does_nothing():
    prev, quit_now = detach_state("", "a")
    assert prev == ""
    assert quit_now is False


def test_double_ctrl_b_resets_without_quit():
    """`Ctrl+B Ctrl+B` is the tmux escape for the literal prefix;
    in our TUI it should stay armed (still prefix state) so a
    follow-up D quits cleanly. NOT silent-quit."""
    prev, quit_now = detach_state("", "ctrl+b")
    prev, quit_now = detach_state(prev, "ctrl+b")
    # Whatever we choose, it must NOT be a quit. We choose
    # "cancel prefix" because that's more conservative.
    assert quit_now is False


# ── group_by_workspace ────────────────────────────────────────────


def _row(id_: str, ws: str, **kw) -> AgentRow:
    return AgentRow(
        id=id_, workspace=ws,
        status=kw.get("status", "running"),
        semantic_status=kw.get("semantic"),
        purpose=kw.get("purpose", ""),
    )


def test_group_by_workspace_sorts_keys_and_values():
    """Render order must be deterministic so the sidebar doesn't
    reshuffle on every refresh and the user doesn't lose their
    cursor position."""
    rows = [
        _row("zeta", "alpha"),
        _row("apple", "beta"),
        _row("banana", "alpha"),
        _row("cherry", "alpha"),
    ]
    grouped = group_by_workspace(rows)
    assert list(grouped.keys()) == ["alpha", "beta"]
    assert [r.id for r in grouped["alpha"]] == ["banana", "cherry", "zeta"]
    assert [r.id for r in grouped["beta"]] == ["apple"]


def test_group_by_workspace_empty():
    assert group_by_workspace([]) == {}


def test_group_by_workspace_single_workspace():
    rows = [_row("a", "ws"), _row("b", "ws")]
    grouped = group_by_workspace(rows)
    assert list(grouped.keys()) == ["ws"]
    assert len(grouped["ws"]) == 2


# ── status_badge ─────────────────────────────────────────────────


def test_status_badge_covers_every_semantic_state():
    """Every semantic_status value (incl. None) has a glyph+color
    so the sidebar never renders an empty cell or a generic '?'."""
    for state in ("working", "awaiting-input", "complete-unread", "idle", None):
        glyph, color = status_badge(state)
        assert glyph, f"missing glyph for {state}"
        assert color, f"missing color for {state}"


def test_status_badge_unknown_state_falls_back():
    """An out-of-band value (future state added without updating
    this map) renders as the None-default rather than crashing."""
    glyph, color = status_badge("super-mega-busy")
    assert glyph == "·"
    assert color == "dim"


def test_status_badge_glyph_color_maps_consistent():
    """Drift-pin: glyph table and color table must have the same
    keys — every state with a glyph must have a color, and vice
    versa. Otherwise a partial state ends up half-styled."""
    assert set(SEMANTIC_STATUS_GLYPH.keys()) == set(SEMANTIC_STATUS_COLOR.keys())


# ── AgentRow ─────────────────────────────────────────────────────


def test_agent_row_is_running_helper():
    """The `is_running` convenience hides the magic string
    "running" so the rest of the TUI never has to know it. If we
    ever add a "starting" state, only AgentRow needs to change."""
    assert _row("a", "ws", status="running").is_running is True
    assert _row("a", "ws", status="stopped").is_running is False
    assert _row("a", "ws", status="errored").is_running is False


# ── key_to_bytes ──────────────────────────────────────────────────


def test_key_to_bytes_printable_passes_through():
    """A bare letter, number, or punctuation lands as UTF-8.
    Harnesses are sensitive to byte-exact input — TUIs expecting
    `a` won't accept `A`."""
    assert key_to_bytes("a") == b"a"
    assert key_to_bytes("Z") == b"Z"
    assert key_to_bytes("1") == b"1"
    assert key_to_bytes("/") == b"/"


def test_key_to_bytes_character_overrides_key():
    """Shift+a gives key='a' but character='A' in textual. We
    prefer the character so the user actually sees `A` in the
    PTY — otherwise typing capital letters would be impossible."""
    assert key_to_bytes("a", character="A") == b"A"


def test_key_to_bytes_enter_is_cr():
    """relaydeck's send_message convention is `\\r` (CR) for "submit"
    to TUI children, not `\\n`. The view widget must follow the
    same rule or every keystroke fed through it would land as
    a literal line-feed glyph in pi/codex/claude-code."""
    assert key_to_bytes("enter") == b"\r"
    assert key_to_bytes("return") == b"\r"


def test_key_to_bytes_backspace_is_del():
    """\\x7f, not \\x08. Most modern terminals send DEL on
    backspace; harnesses expect that. \\x08 would land as
    Ctrl+H in vim, etc."""
    assert key_to_bytes("backspace") == b"\x7f"


def test_key_to_bytes_arrows():
    """ANSI CSI sequences for cursor movement. The whole point
    of having a TUI is that arrow keys work."""
    assert key_to_bytes("up") == b"\x1b[A"
    assert key_to_bytes("down") == b"\x1b[B"
    assert key_to_bytes("right") == b"\x1b[C"
    assert key_to_bytes("left") == b"\x1b[D"


def test_key_to_bytes_ctrl_letter():
    """Ctrl+letter chords map to the 0x01–0x1a control range.
    Ctrl+C must produce \\x03 so the harness's child gets an
    actual SIGINT (which is what users expect when they
    Ctrl+C inside a TUI)."""
    assert key_to_bytes("ctrl+c") == b"\x03"
    assert key_to_bytes("ctrl+a") == b"\x01"
    assert key_to_bytes("ctrl+z") == b"\x1a"


def test_key_to_bytes_function_keys():
    """F1-F4 use the SS3 prefix (`\\x1bO`); F5+ use CSI~ form.
    Different harnesses (claude-code's help, codex's status) rely
    on this — wrong sequence and the wrong action triggers."""
    assert key_to_bytes("f1") == b"\x1bOP"
    assert key_to_bytes("f5") == b"\x1b[15~"
    assert key_to_bytes("f12") == b"\x1b[24~"


def test_key_to_bytes_unknown_returns_none():
    """Keys we don't recognize return None so the caller can
    decide whether to drop them or apply a fallback. Defensive
    against textual key names changing version-to-version."""
    assert key_to_bytes("super+meta+galaxy") is None
    assert key_to_bytes("") is None


def test_key_to_bytes_tab_and_escape():
    """Both are single-byte. Listed explicitly to pin them
    against a refactor that might "helpfully" map tab to spaces
    or escape to nothing."""
    assert key_to_bytes("tab") == b"\t"
    assert key_to_bytes("escape") == b"\x1b"


# ── daemon event stream ───────────────────────────────────────────


class _Host:
    daemon_url = "http://127.0.0.1:8765"
    token = "tok"


def test_event_headers_include_bearer_token():
    """`relaydeck view` uses the same authenticated /api/events feed as
    the dashboard. A missing token here makes the TUI silently stale."""
    assert _event_headers(_Host()) == {
        "Accept": "text/event-stream",
        "Authorization": "Bearer tok",
    }


def test_consume_sse_response_parses_json_events():
    """The TUI event worker is intentionally tiny: it only needs to parse
    `data:` frames and ignore heartbeats without blocking Textual."""
    seen = []
    resp = io.BytesIO(
        b": heartbeat\n\n"
        b"data: {\"type\":\"agent.status_changed\",\"data\":{\"agent_id\":\"a\"}}\n\n"
        b"data: not-json\n\n"
        b"data: {\"type\":\"workspace.added\"}\n\n"
    )
    _consume_sse_response(resp, seen.append, threading.Event())
    assert [e["type"] for e in seen] == ["agent.status_changed", "workspace.added"]
