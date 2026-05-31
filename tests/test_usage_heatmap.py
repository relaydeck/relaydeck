"""Token-usage heatmap — `get_agent_usage_heatmap` + the Context-tab
endpoint. Pins that cells are real hourly token sums bucketed in local
time, with honest 0s and a correct max/total/busiest.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient

from relaydeck.db import (
    ensure_session,
    get_agent_session_contexts,
    get_agent_usage_heatmap,
    open_db,
    upsert_agent,
)


def _usage(conn, agent_id, ts, total, *, session="s", prompt=0, model="m"):
    conn.execute(
        "INSERT INTO usage_records (agent_id, session_id, ts, model, provider, "
        "prompt_tokens, completion_tokens, total_tokens, cost_usd, request_count) "
        "VALUES (?, ?, ?, ?, 'p', ?, 0, ?, 0, 1)",
        (agent_id, session, ts, model, prompt, total),
    )
    conn.commit()


def _make_app(tmp_path: Path):
    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.transports.api import create_app

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch, home


def test_heatmap_buckets_real_tokens(tmp_path):
    conn = open_db(str(tmp_path / "r.db"))
    upsert_agent(conn, "a", "pi", "a", workspace="w")
    # two usage records today at a known hour
    now = dt.datetime.now()
    at = dt.datetime.combine(now.date(), dt.time(hour=9, minute=5)).timestamp()
    _usage(conn, "a", at, 120)
    _usage(conn, "a", at + 60, 30)

    hm = get_agent_usage_heatmap(conn, "a", days=7)
    assert hm["days"] == 7
    assert len(hm["rows"]) == 7
    assert all(len(r["cells"]) == 24 for r in hm["rows"])
    today = now.date().isoformat()
    row = next(r for r in hm["rows"] if r["date"] == today)
    assert row["cells"][9] == 150          # 120 + 30 in hour 9
    assert hm["total"] == 150
    assert hm["max"] == 150
    assert hm["busiest"] == {"date": today, "hour": 9, "tokens": 150}


def test_heatmap_empty_is_honest_zeros(tmp_path):
    conn = open_db(str(tmp_path / "r.db"))
    upsert_agent(conn, "a", "pi", "a", workspace="w")
    hm = get_agent_usage_heatmap(conn, "a", days=3)
    assert hm["total"] == 0
    assert hm["max"] == 0
    assert all(sum(r["cells"]) == 0 for r in hm["rows"])
    assert hm["busiest"]["tokens"] == 0


def test_session_contexts_current_is_latest_prompt(tmp_path):
    conn = open_db(str(tmp_path / "r.db"))
    upsert_agent(conn, "a", "pi", "a", workspace="w")
    ensure_session(conn, "thread-1", "first thread")
    # context grows turn by turn; latest prompt_tokens = current fill
    _usage(conn, "a", 1000, 50, session="thread-1", prompt=2000, model="deepseek/x")
    _usage(conn, "a", 1100, 60, session="thread-1", prompt=5200, model="deepseek/x")
    _usage(conn, "a", 1050, 40, session="thread-2", prompt=800, model="deepseek/x")

    rows = get_agent_session_contexts(conn, "a")
    by = {r["session_id"]: r for r in rows}
    t1 = by["thread-1"]
    assert t1["label"] == "first thread"
    assert t1["current_context"] == 5200    # latest turn's prompt
    assert t1["peak_context"] == 5200
    assert t1["turns"] == 2
    assert t1["total_tokens"] == 110
    assert t1["model"] == "deepseek/x"
    # ordered newest-active first → thread-1 (ts 1100) before thread-2 (1050)
    assert rows[0]["session_id"] == "thread-1"


def test_session_contexts_empty(tmp_path):
    conn = open_db(str(tmp_path / "r.db"))
    upsert_agent(conn, "a", "pi", "a", workspace="w")
    assert get_agent_session_contexts(conn, "a") == []


def test_heatmap_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    conn = open_db(orch.db_path)
    upsert_agent(conn, "rev", "pi", "rev", workspace="w")
    conn.close()
    with TestClient(app) as c:
        r = c.get("/api/agents/rev/usage-heatmap?days=7")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["days"] == 7 and len(body["rows"]) == 7
        rs = c.get("/api/agents/rev/sessions")
        assert rs.status_code == 200
        assert "sessions" in rs.json()
        r2 = c.get("/api/agents/nope/usage-heatmap")
        assert r2.status_code == 404
