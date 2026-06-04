"""
`relaydeck events emit` / `relaydeck broadcast` / `relaydeck events tail`
and their daemon backing (`Orchestrator.emit_event` + `POST
/api/events/emit`).

These cover the previously-missing write side of the event stream: there
was a read-only `/api/events` SSE feed and `agent events`, but no way to
*publish* an operator/announcement event onto the bus the dashboard and
`view` TUI watch. The emit path is the operator twin of `BaseAgent.emit`
— persist one `events` row + fan out to `_bus` (per-agent + `"*"`).
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.transports.cli as cli_mod
from relaydeck.transports.cli import _parse_data_pairs, _POST_OK


def _make_app(tmp_path: Path):
    """Build a real app via create_app — same pattern as the semantic
    status endpoint tests, so we exercise the actual handler + middleware."""
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


# ── Orchestrator.emit_event ────────────────────────────────────────


def test_emit_event_persists_and_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _app, orch = _make_app(tmp_path)

    # Subscribe to the broadcast ("*") stream BEFORE emitting.
    q = orch.subscribe_events("*")
    try:
        ev_id = orch.emit_event("operator", "deploy.started", {"service": "api"})
    finally:
        pass
    assert isinstance(ev_id, int) and ev_id > 0

    # Live fan-out reached the "*" subscriber.
    live = q.get_nowait()
    assert live["type"] == "deploy.started"
    assert live["agent_id"] == "operator"
    assert live["payload"] == {"service": "api"}
    assert live["id"] == ev_id
    orch.unsubscribe_events("*", q)

    # And it persisted to the events table, queryable as history.
    history = orch.get_events("operator")
    assert any(e["type"] == "deploy.started" for e in history)


def test_emit_event_reaches_per_agent_stream(tmp_path, monkeypatch):
    """An event emitted under a given agent_id reaches that agent's own
    stream too (so `agent events <id>` / its SSE see it), not only '*'."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _app, orch = _make_app(tmp_path)

    q = orch.subscribe_events("alice")
    orch.emit_event("alice", "note.left", {"text": "hi"})
    live = q.get_nowait()
    assert live["type"] == "note.left"
    orch.unsubscribe_events("alice", q)


# ── POST /api/events/emit ──────────────────────────────────────────


def test_emit_endpoint_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/events/emit",
            json={"type": "build.failed", "payload": {"errs": 3}, "agent_id": "ci"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["type"] == "build.failed"
    assert body["agent_id"] == "ci"
    assert isinstance(body["id"], int)
    # Persisted under the emitter label.
    assert any(e["type"] == "build.failed" for e in orch.get_events("ci"))


def test_emit_endpoint_defaults_agent_to_operator(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/events/emit", json={"type": "x.y"})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "operator"


def test_emit_endpoint_rejects_missing_type(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/events/emit", json={"payload": {"a": 1}})
    assert r.status_code == 400
    assert "type" in r.json()["detail"].lower()


def test_emit_endpoint_rejects_non_object_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/events/emit", json={"type": "x.y", "payload": [1, 2]})
    assert r.status_code == 400
    assert "object" in r.json()["detail"].lower()


# ── _parse_data_pairs (CLI --data coercion) ────────────────────────


def test_parse_data_pairs_coerces_json_else_string():
    out = _parse_data_pairs(("service=api", "n=3", "ok=true", 'tags=["a","b"]'))
    assert out == {"service": "api", "n": 3, "ok": True, "tags": ["a", "b"]}


def test_parse_data_pairs_keeps_first_equals_for_values_with_equals():
    out = _parse_data_pairs(("url=https://x/?a=b",))
    assert out == {"url": "https://x/?a=b"}


def test_parse_data_pairs_rejects_missing_equals():
    import click
    with pytest.raises(click.BadParameter):
        _parse_data_pairs(("noequals",))


def test_parse_data_pairs_rejects_empty_key():
    import click
    with pytest.raises(click.BadParameter):
        _parse_data_pairs(("=value",))


# ── CLI body assembly (events emit / broadcast) ────────────────────


def _capture_emit(monkeypatch):
    """Monkeypatch the daemon POST helper; capture the body the command
    would send and return a believable success response."""
    captured: dict = {}

    def fake(method, path, body=None, *, timeout=30.0):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return _POST_OK, {
            "ok": True, "id": 7,
            "type": (body or {}).get("type"),
            "agent_id": (body or {}).get("agent_id", "operator"),
        }

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    return captured


def test_events_emit_assembles_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = _capture_emit(monkeypatch)
    res = CliRunner().invoke(
        cli_mod.main,
        ["events", "emit", "deploy.started",
         "--data", "service=api", "--data", "attempt=2", "-m", "shipping"],
    )
    assert res.exit_code == 0, res.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/events/emit"
    assert captured["body"]["type"] == "deploy.started"
    # `attempt=2` JSON-coerces to an int; `service=api` stays a string.
    assert captured["body"]["payload"] == {
        "service": "api", "attempt": 2, "message": "shipping",
    }


def test_broadcast_uses_default_type_and_message(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = _capture_emit(monkeypatch)
    res = CliRunner().invoke(cli_mod.main, ["broadcast", "rolling out v2"])
    assert res.exit_code == 0, res.output
    assert captured["body"]["type"] == "operator.broadcast"
    assert captured["body"]["payload"]["message"] == "rolling out v2"


def test_emit_defaults_agent_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_AGENT_ID", "alice")
    captured = _capture_emit(monkeypatch)
    res = CliRunner().invoke(cli_mod.main, ["events", "emit", "note.left"])
    assert res.exit_code == 0, res.output
    assert captured["body"]["agent_id"] == "alice"


def test_emit_reports_daemon_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake(method, path, body=None, *, timeout=30.0):
        from relaydeck.transports.cli import _POST_TRANSPORT_FAILED
        return _POST_TRANSPORT_FAILED, "URLError: connection refused"

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    res = CliRunner().invoke(cli_mod.main, ["broadcast", "hello"])
    assert res.exit_code == 1
    assert "unreachable" in res.output.lower()


def test_events_tail_agent_history_decodes_db_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_get(path, *, timeout=5.0):
        assert path == "/api/agents/alice/events"
        return _POST_OK, [{
            "id": 12,
            "type": "harness.exit",
            "agent_id": "alice",
            "payload": '{"returncode": 7, "log_path": "/tmp/run.log"}',
        }]

    monkeypatch.setattr(cli_mod, "_get_from_daemon", fake_get)
    res = CliRunner().invoke(cli_mod.main, ["events", "tail", "--agent", "alice"])
    assert res.exit_code == 0, res.output
    assert '"returncode": 7' in res.output
    assert "/tmp/run.log" in res.output
    assert '\\"returncode\\"' not in res.output


def test_events_tail_follow_sends_sse_accept_header(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_DAEMON_URL", "http://daemon.test")
    seen: dict[str, str | None] = {}

    class _Resp:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    def fake_urlopen(req, *args, **kwargs):
        assert isinstance(req, urllib.request.Request)
        seen["url"] = req.full_url
        seen["accept"] = req.get_header("Accept")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    res = CliRunner().invoke(cli_mod.main, ["events", "tail", "-f"])
    assert res.exit_code == 0, res.output
    assert seen == {
        "url": "http://daemon.test/api/events",
        "accept": "text/event-stream",
    }
