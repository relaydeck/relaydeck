"""
Opt-in transcript persistence — recover an exited agent's last screen.

The companion to `agent result` for crashes: when RELAYDECK_TRANSCRIPT_BYTES>0,
the daemon snapshots the tail of an agent's PTY buffer on exit, so its last
screen survives the process. Default off (no behaviour change). Covers the
snapshot helper, get_transcript, and the HTTP 404/200.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import relaydeck.orchestrator as _orch_mod
from relaydeck.orchestrator import get_orchestrator


def _orch(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    return get_orchestrator(home)


def test_no_persist_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAYDECK_TRANSCRIPT_BYTES", raising=False)
    orch = _orch(tmp_path, monkeypatch)
    agent = MagicMock()
    agent.get_pty_buffer.return_value = b"hello world"
    orch._persist_transcript("alice", agent)
    assert orch.get_transcript("alice") is None


def test_persist_snapshots_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_TRANSCRIPT_BYTES", "8")
    orch = _orch(tmp_path, monkeypatch)
    agent = MagicMock()
    agent.get_pty_buffer.return_value = b"0123456789ABCDEF"
    orch._persist_transcript("alice", agent)
    # Last 8 bytes captured.
    assert orch.get_transcript("alice") == "89ABCDEF"


def test_persist_handles_empty_buffer(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_TRANSCRIPT_BYTES", "100")
    orch = _orch(tmp_path, monkeypatch)
    agent = MagicMock()
    agent.get_pty_buffer.return_value = b""
    orch._persist_transcript("bob", agent)
    assert orch.get_transcript("bob") is None


def test_transcript_endpoint_404_then_200(tmp_path, monkeypatch):
    from relaydeck.transports.api import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RELAYDECK_TRANSCRIPT_BYTES", "50")
    orch = _orch(tmp_path, monkeypatch)
    app = create_app(tmp_path / ".relaydeck")
    app.state.orchestrator = orch

    with TestClient(app) as c:
        assert c.get("/api/agents/ghost/transcript").status_code == 404
        agent = MagicMock()
        agent.get_pty_buffer.return_value = b"final screen contents"
        orch._persist_transcript("worker", agent)
        r = c.get("/api/agents/worker/transcript")
        assert r.status_code == 200, r.text
        assert r.json()["transcript"] == "final screen contents"
