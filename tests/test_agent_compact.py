"""
In-place context compaction (`agent compact`).

KV-safer than a fresh session: the harness summarizes-and-trims its
conversation without dying, so the prompt prefix stays stable. Covers the
harness `compact()` contract, the orchestrator `compact_agent` (running /
not-running / unsupported), the HTTP endpoint codes, the CLI, and the
manager's `compact` action.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.transports.cli as cli_mod
from relaydeck.harness.base import HarnessAgent
from relaydeck.transports.cli import _POST_OK, _POST_DAEMON_ERROR


def _make_app(tmp_path: Path):
    from relaydeck.transports.api import create_app
    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch


# ── Harness contract ───────────────────────────────────────────────


def test_harness_compact_sends_command():
    class _Stub:
        COMPACT_COMMAND = "/compact"

        def __init__(self):
            self.sent = []

        def send_message(self, m):
            self.sent.append(m)
            return True

    s = _Stub()
    assert HarnessAgent.compact(s) is True
    assert s.sent == ["/compact"]


def test_harness_compact_unset_is_false():
    class _Stub:
        COMPACT_COMMAND = ""

        def send_message(self, m):
            return True

    assert HarnessAgent.compact(_Stub()) is False


def test_claude_code_supports_compact():
    from plugins.harnesses.claude_code.agent import ClaudeCodeAgent
    assert ClaudeCodeAgent.COMPACT_COMMAND == "/compact"


# ── Orchestrator ───────────────────────────────────────────────────


def test_compact_agent_running_emits_event(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _app, orch = _make_app(tmp_path)

    inst = MagicMock()
    inst.COMPACT_COMMAND = "/compact"
    inst.compact.return_value = True
    with orch._lock:
        orch._instances["alice"] = inst

    q = orch.subscribe_events("*")
    result = orch.compact_agent("alice")
    orch.unsubscribe_events("*", q)

    assert result == {"ok": True, "reason": "sent", "command": "/compact"}
    inst.compact.assert_called_once()
    seen = []
    try:
        while True:
            seen.append(q.get_nowait())
    except Exception:
        pass
    assert any(e["type"] == "agent.compacted" for e in seen)


def test_compact_agent_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _app, orch = _make_app(tmp_path)
    assert orch.compact_agent("ghost")["reason"] == "not-running"


def test_compact_agent_unsupported_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _app, orch = _make_app(tmp_path)
    inst = MagicMock()
    inst.COMPACT_COMMAND = ""
    with orch._lock:
        orch._instances["bob"] = inst
    assert orch.compact_agent("bob")["reason"] == "unsupported"
    inst.compact.assert_not_called()


# ── HTTP endpoint ──────────────────────────────────────────────────


def test_compact_endpoint_404_and_409(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    orch.create_agent("worker", "claude-code", "Worker", workspace=None)
    with TestClient(app) as c:
        assert c.post("/api/agents/ghost/compact").status_code == 404
        # Created but not running → 409.
        assert c.post("/api/agents/worker/compact").status_code == 409


# ── CLI ────────────────────────────────────────────────────────────


def test_cli_compact_reports_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake(method, path, body=None, *, timeout=30.0):
        assert path == "/api/agents/w1/compact"
        return _POST_OK, {"ok": True, "command": "/compact", "agent_id": "w1"}

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    res = CliRunner().invoke(cli_mod.main, ["agent", "compact", "w1"])
    assert res.exit_code == 0, res.output
    assert "/compact" in res.output


def test_cli_compact_unsupported_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake(method, path, body=None, *, timeout=30.0):
        return _POST_DAEMON_ERROR, "HTTP 422: harness has no in-place compaction"

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    res = CliRunner().invoke(cli_mod.main, ["agent", "compact", "w1"])
    assert res.exit_code == 1
    assert "422" in res.output
