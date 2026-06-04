"""
Durable structured results — the reliable "collect results" path (R1).

Before this, an orchestrator collected results from peer inbox messages or PTY
scrollback; a crashed agent's output was simply gone. `agent_results` is a
durable, latest-wins-per-(agent,key) hand-back, announced as an `agent.result`
event. Covers the DB helpers, the orchestrator put/get + event, the HTTP
endpoints, and the CLI body resolution.
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relaydeck.transports.cli as cli_mod
from relaydeck.transports.cli import _POST_OK, _read_body_arg


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


# ── DB helpers ─────────────────────────────────────────────────────


def test_put_get_result_latest_wins(tmp_path):
    from relaydeck.db import get_results, open_db, put_result

    db = str(tmp_path / "r.db")
    conn = open_db(db)
    put_result(conn, "alice", "first", key="build", summary="v1")
    put_result(conn, "alice", "second", key="build", summary="v2")
    put_result(conn, "alice", "other", key="review")

    latest = get_results(conn, "alice", key="build", latest=True)
    assert len(latest) == 1
    assert latest[0]["body"] == "second"
    assert latest[0]["summary"] == "v2"

    # Newest across all keys.
    newest = get_results(conn, "alice", latest=True)
    assert newest[0]["body"] == "other"

    # History, newest-first.
    history = get_results(conn, "alice", key="build", latest=False)
    assert [h["body"] for h in history] == ["second", "first"]


# ── Orchestrator put emits agent.result ────────────────────────────


def test_orchestrator_put_result_emits_event(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _app, orch = _make_app(tmp_path)

    q = orch.subscribe_events("*")
    rid = orch.put_result("alice", "the answer", key="task1",
                          status="ok", summary="done")
    assert isinstance(rid, int) and rid > 0

    seen = []
    try:
        while True:
            seen.append(q.get_nowait())
    except Exception:
        pass
    orch.unsubscribe_events("*", q)

    results = [e for e in seen if e["type"] == "agent.result"]
    assert results, f"no agent.result event; saw {[e['type'] for e in seen]}"
    assert results[0]["payload"]["result_id"] == rid
    assert results[0]["payload"]["summary"] == "done"
    # Readable back.
    got = orch.get_results("alice", key="task1")
    assert got[0]["body"] == "the answer"


# ── HTTP endpoints ─────────────────────────────────────────────────


def test_result_endpoints_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _orch = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/bob/result",
                   json={"body": "report text", "summary": "scan done", "key": "scan"})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        g = c.get("/api/agents/bob/result?key=scan")
        assert g.status_code == 200, g.text
        results = g.json()["results"]
        assert len(results) == 1
        assert results[0]["body"] == "report text"
        assert results[0]["summary"] == "scan done"


def test_result_endpoint_requires_body(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _orch = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/bob/result", json={"summary": "no body"})
    assert r.status_code == 400


# ── CLI ────────────────────────────────────────────────────────────


def test_read_body_arg_file_and_literal(tmp_path):
    f = tmp_path / "out.md"
    f.write_text("from a file")
    assert _read_body_arg(f"@{f}") == "from a file"
    assert _read_body_arg("literal text") == "literal text"
    assert _read_body_arg(None) == ""


def test_cli_result_put_posts_body(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured: dict = {}

    def fake(method, path, body=None, *, timeout=30.0):
        captured["path"] = path
        captured["body"] = body
        return _POST_OK, {"ok": True, "id": 3}

    monkeypatch.setattr(cli_mod, "_json_to_daemon", fake)
    res = CliRunner().invoke(
        cli_mod.main,
        ["agent", "result", "put", "worker1",
         "--body", "the result", "--key", "t1", "--summary", "did it"],
    )
    assert res.exit_code == 0, res.output
    assert captured["path"] == "/api/agents/worker1/result"
    assert captured["body"] == {
        "body": "the result", "key": "t1", "status": "ok", "summary": "did it",
    }


def test_cli_result_get_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_get(path, *, timeout=5.0):
        assert "/api/agents/worker1/result" in path
        return _POST_OK, {"results": [
            {"id": 5, "status": "ok", "key": "t1",
             "summary": "did it", "body": "BODY-CONTENT"},
        ]}

    monkeypatch.setattr(cli_mod, "_get_from_daemon", fake_get)
    res = CliRunner().invoke(cli_mod.main, ["agent", "result", "get", "worker1"])
    assert res.exit_code == 0, res.output
    assert "did it" in res.output
    assert "BODY-CONTENT" in res.output
