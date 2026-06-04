"""
One-tap human escalation (`agent escalate`).

The follow-up to an autopilot hold or a context-critical alert when you want a
person, not a policy: emits `hitl.escalation` (kind="manual") so every channel
plugin delivers it to a human, riding the hitl.* SSE bridge to the dashboard
too. Covers the orchestrator emit (plugin bus + SSE fallback), the endpoint
404/200, and the CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.orchestrator as _orch_mod
import relaydeck.transports.cli as cli_mod
from relaydeck.plugin import PluginEventBus
from relaydeck.orchestrator import get_orchestrator
from relaydeck.transports.cli import _POST_OK


def _make_app(tmp_path, monkeypatch):
    from relaydeck.transports.api import create_app
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch


def test_escalate_emits_on_plugin_bus(tmp_path, monkeypatch):
    _app, orch = _make_app(tmp_path, monkeypatch)
    orch.create_agent("worker", "claude-code", "Worker", workspace=None)
    bus = PluginEventBus()
    orch.set_event_bus(bus)
    seen = []
    bus.subscribe("hitl.escalation", lambda e: seen.append(e))

    assert orch.escalate_agent("worker", "please review") is True
    assert len(seen) == 1
    assert seen[0].data["agent_id"] == "worker"
    assert seen[0].data["kind"] == "manual"
    assert seen[0].data["message"] == "please review"


def test_escalate_falls_back_to_sse(tmp_path, monkeypatch):
    _app, orch = _make_app(tmp_path, monkeypatch)
    # No plugin bus wired → falls back to the SSE bus (still visible).
    q = orch.subscribe_events("*")
    assert orch.escalate_agent("ghost", "") is True
    got = []
    try:
        while True:
            got.append(q.get_nowait())
    except Exception:
        pass
    orch.unsubscribe_events("*", q)
    assert any(e["type"] == "hitl.escalation" for e in got)


def test_escalate_endpoint(tmp_path, monkeypatch):
    app, orch = _make_app(tmp_path, monkeypatch)
    orch.create_agent("worker", "claude-code", "Worker", workspace=None)
    with TestClient(app) as c:
        assert c.post("/api/agents/ghost/escalate").status_code == 404
        r = c.post("/api/agents/worker/escalate", json={"message": "hi"})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


def test_cli_escalate(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake(method, path, body=None, *, timeout=30.0):
        captured["path"] = path
        captured["body"] = body
        return _POST_OK, {"ok": True, "agent_id": "w1"}

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    res = CliRunner().invoke(cli_mod.main, ["agent", "escalate", "w1", "-m", "help"])
    assert res.exit_code == 0, res.output
    assert captured["path"] == "/api/agents/w1/escalate"
    assert captured["body"] == {"message": "help"}
    assert "escalated" in res.output
