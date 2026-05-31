"""
Workspace-level health roll-up rules.

The precedence table is the contract every viewer (CLI list,
dashboard tile, future TUI sidebar) reads. Pin every entry so a
refactor can't quietly invert "errored beats awaiting-input"
and silently mislead operators about what needs attention.
"""

from __future__ import annotations

from relaydeck.workspace_health import HEALTH_LEVELS, HEALTH_STYLES, roll_up


def _a(status: str = "running", semantic: str | None = None) -> dict:
    return {"status": status, "semantic_status": semantic}


# ── Edge cases ───────────────────────────────────────────────────────


def test_empty_workspace_rolls_up_to_empty():
    assert roll_up([]) == "empty"


def test_only_stopped_agents_roll_up_to_stopped():
    assert roll_up([_a(status="stopped"), _a(status="stopped")]) == "stopped"


def test_running_without_semantic_status_is_idle():
    """A fresh agent (no hook event yet) is `running` at process
    level with `semantic_status=None`. For roll-up purposes this is
    idle — better than calling the workspace `stopped` when an
    agent is actually up."""
    assert roll_up([_a(status="running", semantic=None)]) == "idle"


# ── Precedence ───────────────────────────────────────────────────────


def test_errored_beats_everything():
    rows = [
        _a(status="errored"),
        _a(semantic="awaiting-input"),
        _a(semantic="working"),
        _a(semantic="idle"),
    ]
    assert roll_up(rows) == "errored"


def test_awaiting_input_beats_working_and_below():
    rows = [
        _a(semantic="working"),
        _a(semantic="awaiting-input"),
        _a(semantic="idle"),
    ]
    assert roll_up(rows) == "awaiting-input"


def test_working_beats_complete_unread_and_idle():
    rows = [_a(semantic="complete-unread"), _a(semantic="working"), _a(semantic="idle")]
    assert roll_up(rows) == "working"


def test_complete_unread_beats_idle():
    rows = [_a(semantic="idle"), _a(semantic="complete-unread")]
    assert roll_up(rows) == "complete-unread"


def test_all_idle_rolls_up_to_idle():
    rows = [_a(semantic="idle"), _a(semantic="idle")]
    assert roll_up(rows) == "idle"


# ── Constants are stable ────────────────────────────────────────────


def test_health_levels_constant_pinned():
    """The precedence order is user-visible — pinned so refactors
    can't quietly re-order errored vs awaiting-input or any other
    pair."""
    assert HEALTH_LEVELS == (
        "errored",
        "awaiting-input",
        "working",
        "complete-unread",
        "idle",
        "stopped",
        "empty",
    )


def test_health_styles_cover_every_level():
    """Every roll-up state must have a color so the CLI/dashboard
    never falls back to plain text and the visual language stays
    consistent."""
    for level in HEALTH_LEVELS:
        assert level in HEALTH_STYLES, f"missing style for {level}"
