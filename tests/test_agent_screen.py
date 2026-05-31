"""
`relaydeck agent screen` — rendered PTY snapshot via pyte.

Covers:
  - the pure render() helper across plain text, ANSI color, and
    cursor-positioning sequences
  - trailing-blank-line trimming
  - the endpoint resolves 404 / 409 for unknown / not-running
    agents
  - basic render through the real endpoint for a live instance
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relaydeck.screen import render


# ── Pure render() helper ──────────────────────────────────────────


def test_render_plain_text_is_unchanged():
    """ASCII text with no escapes renders identically."""
    out = render(b"hello\nworld\n")
    assert out == "hello\nworld"


def test_render_strips_ansi_color_codes():
    """SGR codes shouldn't appear in the rendered text — pyte
    consumes them and just sets cell attributes."""
    body = b"\x1b[31mred\x1b[0m \x1b[1mbold\x1b[0m\n"
    out = render(body)
    assert out == "red bold"


def test_render_resolves_cursor_positioning():
    """When the harness moves the cursor and overwrites cells,
    the rendered output reflects the FINAL state, not the byte
    history. This is the whole point of the emulator."""
    # Position to row 1 col 1, write "hi", move to row 1 col 1
    # again, overwrite with "OK".
    body = b"\x1b[1;1Hhi\x1b[1;1HOK\n"
    out = render(body)
    # The terminal's row 1 should be "OK", not "hiOK" or "OK\nhi".
    assert out.splitlines()[0].strip() == "OK"


def test_render_trims_trailing_blank_lines():
    """A short TUI shouldn't come back as 50 lines of whitespace
    padding."""
    out = render(b"one\n")
    # No trailing newlines, no blank padding.
    assert out == "one"


def test_render_rows_padding_is_honored():
    """Tall buffers don't lose history — pyte's default behavior
    is to keep recent rows in the visible grid."""
    body = ("\n".join(f"line-{i}" for i in range(40)) + "\n").encode()
    out = render(body, rows=50)
    assert "line-39" in out
    # Earliest lines may or may not be present depending on the
    # rendered scrollback; we don't assert on those — we just
    # confirm the most-recent line landed.


def test_render_handles_empty_buffer():
    assert render(b"") == ""


def test_render_tolerates_private_device_status_report():
    """opencode (and other modern TUIs) emit the *private* Device Status
    Report `CSI ? n` (e.g. DECXCPR `\\x1b[?6n`) on startup to probe the
    terminal. pyte's parser dispatches private CSI sequences with
    `private=True`, but its base `report_device_status(mode)` doesn't accept
    that kwarg — so the whole feed() raised TypeError and `agent screen`
    500'd. The tolerant Screen must swallow it and keep rendering."""
    body = b"\x1b[?6nbefore\x1b[?25h after\n"
    out = render(body)
    # No exception, and surrounding text still rendered.
    assert "before" in out
    assert "after" in out


# ── Endpoint behavior ────────────────────────────────────────────


def _make_app(tmp_path: Path):
    from relaydeck.transports.api import create_app
    from relaydeck.orchestrator import get_orchestrator
    import relaydeck.orchestrator as _orch_mod

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch


def test_screen_endpoint_404_for_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _orch = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/no-such/screen")
    assert r.status_code == 404


def test_screen_endpoint_409_for_stopped_agent(tmp_path, monkeypatch):
    """Agent exists but isn't running — no live PTY to snapshot.
    409 (conflict) is the honest signal: the resource is there
    but the operation can't be fulfilled in this state."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    with TestClient(app) as c:
        r = c.get("/api/agents/alice/screen")
    assert r.status_code == 409
    assert "not running" in r.json()["detail"].lower()


def test_screen_endpoint_renders_live_instance(tmp_path, monkeypatch):
    """Smoke: with a fake running instance whose `get_pty_buffer`
    returns a known byte sequence, the endpoint produces the
    expected rendered text. Bypasses the WS / real PTY plumbing —
    we're testing the integration of the endpoint with pyte."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)

    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    class _FakeInstance:
        def get_pty_buffer(self) -> bytes:
            return b"hello\nworld\n"

    # Patch the orchestrator's running-instance lookup.
    monkeypatch.setattr(
        orch, "get_running_instance",
        lambda agent_id: _FakeInstance() if agent_id == "alice" else None,
    )

    with TestClient(app) as c:
        r = c.get("/api/agents/alice/screen")
    assert r.status_code == 200, r.text
    assert "hello" in r.text
    assert "world" in r.text
