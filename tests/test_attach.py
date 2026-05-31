"""
Tests for `relaydeck attach`'s pure-Python helpers.

The real attach loop drives terminal modes + a live WebSocket; that
path is exercised manually. Here we cover the bits that are unit-testable without a
TTY:

  - detach-key parser (the muscle-memory surface)
  - detach-key label rendering (what the masthead prints)
  - status-event JSON decoding (what the operator sees when the
    PTY closes or the agent isn't running)
"""

from __future__ import annotations

import json

import pytest

from relaydeck.transports.attach import (
    _decode_status,
    _detach_label,
    _parse_detach_key,
    _parse_key,
)


def test_parse_default_detach_key_is_ctrl_b_then_d():
    prefix, mark = _parse_detach_key("ctrl-b,d")
    assert prefix == 0x02   # Ctrl-B
    assert mark == ord("d")


def test_parse_detach_key_custom_pair():
    """Operators who don't like tmux's Ctrl-B can pick a different
    prefix. `Ctrl-X` is a common alternative because most TUIs don't
    bind it."""
    prefix, mark = _parse_detach_key("ctrl-x,q")
    assert prefix == 0x18  # Ctrl-X
    assert mark == ord("q")


def test_parse_detach_key_rejects_wrong_arity():
    with pytest.raises(ValueError):
        _parse_detach_key("ctrl-b")        # missing mark
    with pytest.raises(ValueError):
        _parse_detach_key("ctrl-b,d,x")    # too many parts


def test_parse_key_rejects_unknown():
    with pytest.raises(ValueError):
        _parse_key("super-q")
    with pytest.raises(ValueError):
        _parse_key("F1")


def test_parse_key_accepts_single_printable():
    """A single letter / digit can be either the prefix or the mark
    (some users pick a printable detach prefix for ad-hoc sessions)."""
    assert _parse_key("a") == ord("a")
    assert _parse_key("7") == ord("7")


def test_detach_label_renders_ctrl_letter_friendly():
    """The masthead at attach start should show `Ctrl-B then D`, not
    `\\x02 then d` — operators read this once per session and we
    want it scannable."""
    assert _detach_label(0x02, ord("d")) == "Ctrl-B then D"
    assert _detach_label(0x18, ord("q")) == "Ctrl-X then Q"


def test_decode_status_renders_known_event_kind():
    """Status events that come back over the WS as JSON should
    surface their `event` field prominently. Operators care about
    `agent_not_running` and `pty_closed` specifically."""
    payload = json.dumps({"event": "pty_closed"}).encode()
    line = _decode_status(payload)
    assert "pty_closed" in line


def test_decode_status_includes_extras():
    payload = json.dumps({"event": "agent_not_running", "agent_id": "alice"}).encode()
    line = _decode_status(payload)
    assert "agent_not_running" in line
    assert "agent_id=alice" in line


def test_decode_status_tolerates_malformed_json():
    """A malformed status frame must not crash the attach loop.
    Worst case the operator sees the raw bytes."""
    line = _decode_status(b"not-json")
    # The line should be safely renderable (no exception). Beyond that
    # we don't pin the exact format — humans only see this in the rare
    # protocol-mismatch case.
    assert isinstance(line, str)
    assert line  # non-empty
