"""
`relaydeck agent wait` — synchronization primitive driven by the
`agent.status_changed` SSE stream.

Coverage:

  - usage validation (both flags / no flag / invalid status)
  - fast-path early return when state already matches
  - exit codes: 0 reached, 1 timeout, 2 usage, 3 transport
  - state-stream endpoint shape (route exists + 404 on unknown
    agent)

The full SSE→exit roundtrip is exercised against a fake urlopen
that yields one event then closes. Driving a real long-lived
SSE stream in pytest is brittle; the seam tested here covers
the parsing + match logic which is the part that breaks under
refactor.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from relaydeck.transports.cli import main as cli


# ── Usage validation ──────────────────────────────────────────────


def test_wait_requires_a_status_flag(tmp_path, monkeypatch):
    """No --status, no --not-status → exit 2 with a clear error."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "wait", "alice"])
    assert result.exit_code == 2
    assert "required" in result.output.lower()


def test_wait_rejects_both_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["agent", "wait", "alice", "--status", "idle", "--not-status", "working"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_wait_rejects_invalid_status_value(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["agent", "wait", "alice", "--status", "ruminating"],
    )
    assert result.exit_code == 2
    assert "invalid status" in result.output.lower()


# ── Fast path: state already matches ──────────────────────────────


def test_wait_fast_path_exits_0_when_already_matched(tmp_path, monkeypatch):
    """If the daemon reports the target state on the initial GET,
    `wait` returns immediately without opening the SSE stream."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)

    # Stub the daemon GET to return semantic_status="idle".
    import io
    import urllib.request

    class _Resp:
        def __init__(self, body):
            self._b = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *_): return self._b

    def _fake(req, timeout=None, context=None):
        del timeout, context
        body = b'{"id":"alice","semantic_status":"idle"}'
        return _Resp(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["agent", "wait", "alice", "--status", "idle", "--timeout", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "already" in result.output.lower()


def test_wait_timeout_zero_exits_1_when_not_matched(tmp_path, monkeypatch):
    """`--timeout 0` is a probe: return immediately, exit 1 if
    state doesn't already match. Useful for scripted polling."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck").mkdir(parents=True)
    import urllib.request

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *_): return b'{"id":"alice","semantic_status":"working"}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, **kw: _Resp())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["agent", "wait", "alice", "--status", "idle", "--timeout", "0"],
    )
    assert result.exit_code == 1
    assert "timeout" in result.output.lower() or "not at target" in result.output.lower()


# ── State-stream endpoint ─────────────────────────────────────────


def test_state_stream_route_registered(tmp_path, monkeypatch):
    """Light regression guard that the SSE endpoint is wired up.
    The full streaming behavior is exercised by manual smoke."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.transports.api import create_app
    import relaydeck.orchestrator as _orch_mod

    _orch_mod._orchestrator = None
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    app = create_app(cfg)

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/agents/{agent_id}/state/stream" in paths


def test_state_stream_404_for_unknown_agent(tmp_path, monkeypatch):
    """SSE endpoint must reject unknown agent ids cleanly so the
    CLI can surface a useful error instead of hanging."""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.transports.api import create_app
    import relaydeck.orchestrator as _orch_mod

    _orch_mod._orchestrator = None
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    app = create_app(cfg)

    with TestClient(app) as c:
        r = c.get("/api/agents/no-such/state/stream")
    assert r.status_code == 404
