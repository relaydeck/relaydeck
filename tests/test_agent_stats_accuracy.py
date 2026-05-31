"""Per-agent stat-strip ACCURACY — end-to-end through `GET /api/agents/{id}/stats`.

The dashboard stat strip (Uptime · Tokens · Cost · Events · Last tick · Activity)
and the header model chip must show *real* numbers that match the database, never
fabricated ones. test_db.py unit-tests the `get_agent_stats` aggregation; this
file pins the contract the dashboard actually consumes — the HTTP endpoint — and
the cross-harness cases that bit us in practice:

  - the numbers the endpoint returns equal a raw-SQL recomputation (no fabrication)
  - the 24h token/cost window vs the all-time event count
  - a codex-style row that logs tokens but NO cost → honest $0.00 (not hidden)
  - a claude-style agent with no usage records → honest 0 tokens (a metering gap,
    not an inaccuracy)
  - uptime is derived from the real harness.spawn event; active model = the most
    recent usage record
  - process + semantic status reported accurately by /api/agents
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from relaydeck.db import (
    ensure_session,
    log_event,
    open_db,
    record_usage,
    upsert_agent,
)


def _app(tmp_path: Path):
    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.transports.api import create_app

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch


def _seed_agent(conn, aid, atype):
    upsert_agent(conn, aid, atype, aid)
    ensure_session(conn, f"agent:{aid}")


def _raw_truth(conn, aid, *, now):
    """Recompute the strip's headline numbers straight from the tables —
    the same SQL the dashboard would have no choice but to trust."""
    day_ago = now - 86400.0
    tok, cost = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(NULLIF(total_tokens,0), prompt_tokens+completion_tokens)),0), "
        "COALESCE(SUM(cost_usd),0) FROM usage_records WHERE agent_id=? AND ts>=?",
        (aid, day_ago),
    ).fetchone()
    ev = conn.execute("SELECT COUNT(*) FROM events WHERE agent_id=?", (aid,)).fetchone()[0]
    return int(tok or 0), float(cost or 0.0), int(ev or 0)


def test_stats_endpoint_matches_raw_db_per_harness(tmp_path, monkeypatch):
    """The endpoint's tokens/cost/events must equal a raw-SQL recomputation for
    every harness shape — pi (tokens+cost), codex (tokens, no cost), claude (no
    usage at all). This is the live accuracy cross-check, made hermetic."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _app(tmp_path)
    conn = open_db(orch.db_path)

    # pi: reports both tokens and cost.
    _seed_agent(conn, "pi-a", "pi")
    record_usage(conn, "pi-a", "s1", model="deepseek/x", provider="deepseek",
                 prompt_tokens=100, completion_tokens=40, total_tokens=140, cost_usd=0.0079)
    # codex: reports tokens, NO cost (cost_usd None) — the honest-$0 case.
    _seed_agent(conn, "codex-a", "codex-cli")
    record_usage(conn, "codex-a", "s1", model="gpt-5.5", provider="openai",
                 prompt_tokens=500, completion_tokens=20, total_tokens=520, cost_usd=None)
    # claude: NO usage records at all (metering gap → honest 0).
    _seed_agent(conn, "claude-a", "claude-code")
    log_event(conn, "agent:claude-a", "harness.spawn", {}, agent_id="claude-a")
    conn.commit()
    conn.close()

    now = time.time()
    with TestClient(app) as c:
        for aid in ("pi-a", "codex-a", "claude-a"):
            r = c.get(f"/api/agents/{aid}/stats")
            assert r.status_code == 200, r.text
            s = r.json()
            # Re-open to recompute raw truth from the same file.
            verify = open_db(orch.db_path)
            tok, cost, ev = _raw_truth(verify, aid, now=now)
            verify.close()
            assert s["tokens_24h"] == tok, f"{aid}: tokens drift {s['tokens_24h']} != {tok}"
            assert abs(s["cost_24h"] - cost) < 1e-9, f"{aid}: cost drift"
            assert s["events_total"] == ev, f"{aid}: events drift"

    # Honest per-harness values.
    with TestClient(app) as c:
        pi = c.get("/api/agents/pi-a/stats").json()
        cdx = c.get("/api/agents/codex-a/stats").json()
        cl = c.get("/api/agents/claude-a/stats").json()
    assert pi["tokens_24h"] == 140 and abs(pi["cost_24h"] - 0.0079) < 1e-9
    assert pi["tokens_in"] == 100 and pi["tokens_out"] == 40
    assert cdx["tokens_24h"] == 520 and cdx["cost_24h"] == 0.0, "codex: tokens kept, cost honestly 0"
    assert cl["tokens_24h"] == 0 and cl["cost_24h"] == 0.0, "claude: no usage → honest 0, not fabricated"


def test_stats_24h_window_excludes_old_usage_but_events_all_time(tmp_path, monkeypatch):
    """Tokens/cost are a 24h window; events_total is all-time. An old usage row
    (>24h) must drop out of tokens but a matching activity decay is fine."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _app(tmp_path)
    conn = open_db(orch.db_path)
    _seed_agent(conn, "a", "pi")
    # Recent usage (counts).
    record_usage(conn, "a", "s1", model="m", provider="p",
                 prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.01)
    # Old usage (>24h) inserted directly with a stale ts — must NOT count.
    old_ts = time.time() - 90000.0  # ~25h
    conn.execute(
        "INSERT INTO usage_records (agent_id, session_id, ts, model, provider, "
        "prompt_tokens, completion_tokens, total_tokens, cost_usd, request_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("a", "s0", old_ts, "m", "p", 9999, 9999, 9999, 9.99, 1),
    )
    # Two events (all-time count).
    log_event(conn, "agent:a", "harness.spawn", {}, agent_id="a")
    log_event(conn, "agent:a", "usage.record", {}, agent_id="a")
    conn.commit()
    conn.close()

    with TestClient(app) as c:
        s = c.get("/api/agents/a/stats").json()
    assert s["tokens_24h"] == 15, "the >24h row must be excluded from the token window"
    assert abs(s["cost_24h"] - 0.01) < 1e-9
    assert s["events_total"] == 2, "events_total is all-time"


def test_stats_uptime_and_active_model(tmp_path, monkeypatch):
    """spawn_ts comes from the most recent harness.spawn (drives live uptime);
    active model = the most recent usage record's model (handles /model switches)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _app(tmp_path)
    conn = open_db(orch.db_path)
    _seed_agent(conn, "a", "pi")
    log_event(conn, "agent:a", "harness.spawn", {}, agent_id="a")
    record_usage(conn, "a", "s1", model="model-old", provider="p",
                 prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0)
    time.sleep(0.01)
    record_usage(conn, "a", "s1", model="model-new", provider="p",
                 prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0)
    conn.commit()
    conn.close()

    with TestClient(app) as c:
        s = c.get("/api/agents/a/stats").json()
    assert s["spawn_ts"] is not None, "a spawned agent must expose spawn_ts for the uptime ticker"
    assert s["spawn_ts"] <= time.time()
    assert s["model"] == "model-new", "active model must be the most recent usage record's model"
    assert len(s["activity"]) == 30, "30-bucket activity spark"


def test_stats_status_reported_accurately(tmp_path, monkeypatch):
    """Process status + semantic status come from the DB and are reported as-is —
    the strip/badges never invent a state."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch = _app(tmp_path)
    conn = open_db(orch.db_path)
    upsert_agent(conn, "a", "pi", "a")
    conn.commit()
    conn.close()
    # Set a semantic status the way the hook endpoint does.
    orch.set_semantic_status("a", "working", source="hook")

    with TestClient(app) as c:
        agents = c.get("/api/agents").json()
    rows = agents["agents"] if isinstance(agents, dict) else agents
    a = next(x for x in rows if x["id"] == "a")
    assert a["semantic_status"] == "working"
    # An agent we never started reads as a not-running process state (honest) —
    # never "running".
    assert a["status"] != "running"
    assert a["status"] in ("pending", "stopped", "idle", "unknown", "errored")
