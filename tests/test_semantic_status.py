"""
Semantic agent status — the observable-state layer beyond
process-level `running`/`stopped`.

Covered here:

  - DB migration adds the new columns idempotently
  - `update_semantic_status` round-trips + rejects invalid values
  - `Orchestrator.set_semantic_status` returns True iff value changed
  - `agent.status_changed` plugin-bus event fires on transition only
  - `list_agents` exposes the new fields
  - `POST /api/agents/{id}/state` validates body, persists, emits

The hook installer and the mood-classifier fallback wiring are
tested separately — this file pins the
state-storage + event-dispatch primitives that both of those
build on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── DB helpers ─────────────────────────────────────────────────────


def test_semantic_status_columns_exist(tmp_path, monkeypatch):
    """Migration 8 adds three columns to `agents`: semantic_status,
    semantic_status_at, semantic_status_source. All nullable."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)
    from relaydeck.db import open_db

    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    conn = open_db(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    finally:
        conn.close()
    assert {"semantic_status", "semantic_status_at", "semantic_status_source"} <= cols


def test_update_semantic_status_rejects_invalid_value(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)
    from relaydeck.db import open_db, update_semantic_status, upsert_agent

    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    conn = open_db(db)
    upsert_agent(conn, "alice", "pi", "alice")
    with pytest.raises(ValueError):
        update_semantic_status(conn, "alice", "ruminating")  # nonsense
    conn.close()


def test_update_semantic_status_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)
    from relaydeck.db import open_db, update_semantic_status, upsert_agent

    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    conn = open_db(db)
    upsert_agent(conn, "alice", "pi", "alice")
    prior = update_semantic_status(conn, "alice", "working", source="hook")
    assert prior is None
    prior = update_semantic_status(conn, "alice", "idle", source="hook")
    assert prior == "working"
    # Read back via list_agents semantics.
    row = conn.execute(
        "SELECT semantic_status, semantic_status_source FROM agents WHERE id='alice'"
    ).fetchone()
    conn.close()
    assert row["semantic_status"] == "idle"
    assert row["semantic_status_source"] == "hook"


# ── Orchestrator.set_semantic_status + bus event ───────────────────


def test_orchestrator_set_semantic_status_emits_on_change(tmp_path, monkeypatch):
    """Bus event fires only when the new value differs from the
    prior. A chatty hook re-asserting the same state shouldn't
    spam subscribers."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)

    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.plugin import PluginEventBus
    import relaydeck.orchestrator as _orch_mod
    _orch_mod._orchestrator = None
    orch = get_orchestrator(cfg)

    bus = PluginEventBus()
    events: list = []
    bus.subscribe("agent.status_changed", lambda e: events.append(e))
    orch.set_event_bus(bus)

    # Seed an agent so the UPDATE has a row to hit.
    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    # First write: prior was None, new is "working" → change.
    assert orch.set_semantic_status("alice", "working") is True
    # Same value again: no change.
    assert orch.set_semantic_status("alice", "working") is False
    # Different value: change.
    assert orch.set_semantic_status("alice", "idle") is True

    # Two events total — the two transitions.
    assert len(events) == 2
    assert events[0].data["to"] == "working"
    assert events[1].data["from"] == "working"
    assert events[1].data["to"] == "idle"


# ── list_agents exposes the new fields ─────────────────────────────


def test_list_agents_exposes_semantic_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as _orch_mod
    _orch_mod._orchestrator = None

    from relaydeck.orchestrator import get_orchestrator
    orch = get_orchestrator(cfg)

    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    orch.set_semantic_status("alice", "working", source="hook")
    rows = orch.list_agents()
    alice = next(r for r in rows if r["id"] == "alice")
    assert alice["semantic_status"] == "working"
    assert alice["semantic_status_source"] == "hook"
    assert alice["semantic_status_at"] is not None


def test_stopped_agent_clears_stale_active_semantic_status(tmp_path, monkeypatch):
    """A stopped/errored process can't be 'working' or 'awaiting-input'.
    If the row carries a stale active state (set while live, never
    reconciled), the read layer must drop it so the dashboard doesn't
    paint a green 'working' dot on a dead agent. Quiescent states
    (complete-unread / idle) survive a stop."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as _orch_mod
    _orch_mod._orchestrator = None
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.db import open_db, update_agent_status, upsert_agent

    orch = get_orchestrator(cfg)
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    # Simulate: agent was working, then the process stopped (status
    # reconciled to stopped) but semantic_status wasn't cleared.
    orch.set_semantic_status("alice", "working", source="classifier")
    conn = open_db(orch.db_path)
    update_agent_status(conn, "alice", "stopped")
    conn.close()

    alice = next(r for r in orch.list_agents() if r["id"] == "alice")
    assert alice["status"] == "stopped"
    assert alice["semantic_status"] is None  # stale "working" dropped

    # Quiescent state survives a stop.
    orch.set_semantic_status("alice", "complete-unread", source="classifier")
    conn = open_db(orch.db_path)
    update_agent_status(conn, "alice", "stopped")
    conn.close()
    alice = next(r for r in orch.list_agents() if r["id"] == "alice")
    assert alice["semantic_status"] == "complete-unread"


# ── HTTP endpoint ──────────────────────────────────────────────────


def _make_app(tmp_path: Path):
    """Build a real FastAPI app via create_app so we exercise the
    actual auth middleware + endpoint handlers."""
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


def test_state_endpoint_rejects_invalid_status(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    with TestClient(app) as c:
        r = c.post("/api/agents/alice/state", json={"status": "ruminating"})
    assert r.status_code == 400
    assert "invalid status" in r.json()["detail"].lower()


def test_state_endpoint_404_for_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/no-such-agent/state", json={"status": "idle"})
    assert r.status_code == 404


def test_state_endpoint_persists_and_reports_change(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    with TestClient(app) as c:
        r = c.post(
            "/api/agents/alice/state",
            json={"status": "working", "source": "hook"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "working"
        assert body["changed"] is True

        # Same value again — server reports changed=False.
        r2 = c.post(
            "/api/agents/alice/state",
            json={"status": "working", "source": "hook"},
        )
        assert r2.status_code == 200
        assert r2.json()["changed"] is False

    # And the DB reflects the value.
    alice = next(r for r in orch.list_agents() if r["id"] == "alice")
    assert alice["semantic_status"] == "working"


def test_state_endpoint_accepts_null_to_clear(tmp_path, monkeypatch):
    """Posting `status: null` clears the field — useful for an
    operator overriding a stale hook signal."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _make_app(tmp_path)
    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "alice", "pi", "alice")
    conn.close()

    orch.set_semantic_status("alice", "working")
    with TestClient(app) as c:
        r = c.post("/api/agents/alice/state", json={"status": None})
    assert r.status_code == 200
    alice = next(r for r in orch.list_agents() if r["id"] == "alice")
    assert alice["semantic_status"] is None
