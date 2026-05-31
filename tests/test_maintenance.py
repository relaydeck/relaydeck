"""
Danger-zone data wipe: core wipe module + the HTTP API.

Real SQLite + real FastAPI TestClient, isolated tmp config home.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relaydeck import maintenance
from relaydeck.db import open_db


def _seed(db_path: str) -> None:
    """Put a couple rows in each wipe-able table."""
    conn = open_db(db_path)
    try:
        conn.execute("INSERT INTO sessions (id, started_at) VALUES ('s', 0)")
        conn.execute("INSERT INTO agent_messages (id, from_id, to_id, body, ts) "
                     "VALUES ('m1','a','b','hi',1),('m2','b','a','yo',2)")
        conn.execute("INSERT INTO events (session_id, type, payload, agent_id, ts) "
                     "VALUES ('s','x','{}','a',1),('s','y','{}','a',2),('s','z','{}','a',3)")
        conn.execute("INSERT INTO usage_records (agent_id, session_id, model, provider, "
                     "prompt_tokens, completion_tokens, total_tokens, cost_usd, ts) "
                     "VALUES ('a','s','m','openrouter',1,2,3,0.0,1)")
        conn.commit()
    finally:
        conn.close()


def _db(tmp_path: Path) -> str:
    p = tmp_path / "runtime" / "relaydeck.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    open_db(str(p)).close()
    return str(p)


# ── core module ─────────────────────────────────────────────────────


def test_scopes_whitelist_and_labels():
    assert set(maintenance.SCOPES) == {"messages", "events", "usage", "invocations", "runs"}
    assert maintenance.SCOPES["messages"][0] == "agent_messages"
    assert "Peer" in maintenance.scope_labels()["messages"]


def test_history_stats_counts(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    counts = maintenance.history_stats(db)
    assert counts["messages"] == 2
    assert counts["events"] == 3
    assert counts["usage"] == 1
    assert counts["runs"] == 0


def test_wipe_only_selected_scope(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    deleted = maintenance.wipe(db, ["messages"])
    assert deleted["messages"] == 2
    after = maintenance.history_stats(db)
    assert after["messages"] == 0
    assert after["events"] == 3       # untouched


def test_wipe_ignores_unknown_scope(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    deleted = maintenance.wipe(db, ["events", "agents", "vault"])  # only events is valid
    assert deleted == {"events": 3}
    assert maintenance.history_stats(db)["messages"] == 2  # not touched


def test_resolve_scopes():
    assert maintenance.resolve_scopes(messages=True) == ["messages"]
    assert maintenance.resolve_scopes(history=True) == list(maintenance.HISTORY_SCOPES)
    both = maintenance.resolve_scopes(messages=True, history=True)
    assert both[0] == "messages" and "events" in both
    # dedupe + drop unknowns
    assert maintenance.resolve_scopes(messages=True, extra=["messages", "nope"]) == ["messages"]


# ── HTTP API ────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as om
    om._orchestrator = None
    from relaydeck.transports.api import create_app
    app = create_app(cfg)
    return TestClient(app), str(cfg / "runtime" / "relaydeck.db")


def test_api_history_and_wipe(client):
    c, db = client
    _seed(db)
    counts = c.get("/api/maintenance/history").json()
    assert counts["counts"]["messages"] == 2
    assert "labels" in counts
    r = c.post("/api/maintenance/wipe", json={"scopes": ["messages", "events"]})
    assert r.status_code == 200
    assert r.json()["deleted"] == {"messages": 2, "events": 3}
    # usage remains.
    assert c.get("/api/maintenance/history").json()["counts"]["usage"] == 1


def test_api_wipe_rejects_empty_or_invalid(client):
    c, _ = client
    assert c.post("/api/maintenance/wipe", json={"scopes": []}).status_code == 400
    assert c.post("/api/maintenance/wipe", json={"scopes": ["agents", "vault"]}).status_code == 400


def test_api_wipe_is_audited(client):
    c, db = client
    _seed(db)
    c.post("/api/maintenance/wipe", json={"scopes": ["messages"]})
    conn = open_db(db)
    try:
        row = conn.execute(
            "SELECT action FROM audit_events WHERE action='data.wipe'").fetchone()
    finally:
        conn.close()
    assert row is not None
