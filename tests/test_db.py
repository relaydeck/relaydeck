"""
Tests for the database layer: schema creation, migrations, CRUD helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import (
    ensure_session,
    get_agent_stats,
    get_fleet_token_rollup,
    get_usage_summary,
    log_event,
    open_db,
    record_usage,
    update_agent_status,
    upsert_agent,
)


class TestDatabase:
    """Tests for SQLite database operations."""

    def test_open_db_creates_file(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        assert db_path.exists()
        conn.close()

    def test_open_db_creates_tables(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "agents" in table_names
        assert "events" in table_names
        assert "sessions" in table_names
        assert "usage_records" in table_names
        conn.close()

    def test_open_db_wal_mode(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()

    def test_upsert_agent(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        upsert_agent(conn, "agent-1", "pi", "Test Agent", workspace="ws-1")
        conn.close()

        # Verify
        conn2 = open_db(str(db_path))
        row = conn2.execute("SELECT * FROM agents WHERE id = ?", ("agent-1",)).fetchone()
        assert row["id"] == "agent-1"
        assert row["type"] == "pi"
        assert row["name"] == "Test Agent"
        assert row["status"] == "pending"
        assert row["workspace"] == "ws-1"
        conn2.close()

    def test_upsert_agent_idempotent(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        upsert_agent(conn, "agent-1", "pi", "First")
        upsert_agent(conn, "agent-1", "claude", "Second")  # update
        conn.close()

        conn2 = open_db(str(db_path))
        row = conn2.execute("SELECT * FROM agents WHERE id = ?", ("agent-1",)).fetchone()
        assert row["type"] == "claude"
        assert row["name"] == "Second"
        conn2.close()

    def test_update_agent_status(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        upsert_agent(conn, "agent-1", "pi", "Test")
        update_agent_status(conn, "agent-1", "running")
        conn.close()

        conn2 = open_db(str(db_path))
        row = conn2.execute("SELECT * FROM agents WHERE id = ?", ("agent-1",)).fetchone()
        assert row["status"] == "running"
        assert row["last_active_at"] is not None
        conn2.close()

    def test_update_agent_status_with_error(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        upsert_agent(conn, "agent-1", "pi", "Test")
        update_agent_status(conn, "agent-1", "errored", error="Something broke")
        conn.close()

        conn2 = open_db(str(db_path))
        row = conn2.execute("SELECT * FROM agents WHERE id = ?", ("agent-1",)).fetchone()
        assert row["status"] == "errored"
        assert row["last_error"] == "Something broke"
        conn2.close()


class TestEvents:
    """Tests for event logging."""

    def test_log_event(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        ensure_session(conn, "test-session", "test-label")
        ev_id = log_event(conn, "test-session", "agent.start",
                          {"command": "pi"}, agent_id="agent-1")
        conn.close()

        conn2 = open_db(str(db_path))
        row = conn2.execute(
            "SELECT * FROM events WHERE id = ?", (ev_id,)
        ).fetchone()
        assert row["type"] == "agent.start"
        assert row["agent_id"] == "agent-1"
        assert '"command"' in row["payload"]
        conn2.close()

    def test_log_event_no_payload(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        ensure_session(conn, "test-session")
        ev_id = log_event(conn, "test-session", "heartbeat")
        conn.close()

        conn2 = open_db(str(db_path))
        row = conn2.execute(
            "SELECT * FROM events WHERE id = ?", (ev_id,)
        ).fetchone()
        assert row["type"] == "heartbeat"
        assert row["payload"] is None
        conn2.close()


class TestUsage:
    """Tests for usage/metering records."""

    def test_record_usage(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        record_usage(
            conn, "agent-1", "sess-1",
            model="claude-sonnet-4",
            provider="openrouter",
            prompt_tokens=500,
            completion_tokens=300,
            total_tokens=800,
            cost_usd=0.0069,
        )
        conn.close()

        conn2 = open_db(str(db_path))
        row = conn2.execute(
            "SELECT * FROM usage_records WHERE agent_id = ?", ("agent-1",)
        ).fetchone()
        assert row["prompt_tokens"] == 500
        assert row["completion_tokens"] == 300
        assert row["total_tokens"] == 800
        assert abs(row["cost_usd"] - 0.0069) < 0.0001
        conn2.close()

    def test_get_usage_summary(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))

        # Record two usage events for different model+provider combos
        record_usage(conn, "agent-1", "sess-1",
                     model="claude-sonnet-4", provider="openrouter",
                     prompt_tokens=100, completion_tokens=50, total_tokens=150, cost_usd=0.001)
        record_usage(conn, "agent-1", "sess-2",
                     model="claude-sonnet-4", provider="openrouter",
                     prompt_tokens=200, completion_tokens=100, total_tokens=300, cost_usd=0.002)
        record_usage(conn, "agent-1", "sess-3",
                     model="gpt-4o", provider="openai",
                     prompt_tokens=50, completion_tokens=25, total_tokens=75, cost_usd=0.0005)
        conn.close()

        conn2 = open_db(str(db_path))
        summary = get_usage_summary(conn2, agent_id="agent-1")
        conn2.close()

        assert len(summary) == 2  # Two unique model+provider combos

        # Find claude-sonnet-4 + openrouter
        claude_row = next(r for r in summary if r["model"] == "claude-sonnet-4")
        assert claude_row["total_prompt"] == 300
        assert claude_row["total_completion"] == 150
        assert abs(claude_row["total_cost"] - 0.003) < 0.0001
        assert claude_row["requests"] == 2

        # Find gpt-4o + openai
        gpt_row = next(r for r in summary if r["model"] == "gpt-4o")
        assert gpt_row["total_tokens"] == 75
        assert gpt_row["requests"] == 1

    def test_get_usage_summary_scopes_to_workspace(self, tmp_path):
        """Usage scoped by workspace joins through the agents table so the
        dashboard doesn't leak fleet-wide totals into a fresh workspace."""
        conn = open_db(str(tmp_path / "test.db"))
        upsert_agent(conn, "a-alpha", "pi", "Alpha", workspace="alpha")
        upsert_agent(conn, "a-beta", "pi", "Beta", workspace="beta")
        record_usage(conn, "a-alpha", "s1", model="m", provider="p",
                     prompt_tokens=80, completion_tokens=20, total_tokens=100, cost_usd=0.01)
        record_usage(conn, "a-beta", "s2", model="m", provider="p",
                     prompt_tokens=400, completion_tokens=100, total_tokens=500, cost_usd=0.05)

        alpha = get_usage_summary(conn, workspace="alpha")
        assert sum(r["total_tokens"] for r in alpha) == 100

        beta = get_usage_summary(conn, workspace="beta")
        assert sum(r["total_tokens"] for r in beta) == 500

        # A workspace with no agents totals zero — not the fleet figure.
        assert get_usage_summary(conn, workspace="fresh-empty") == []

        # No filter still aggregates everything (All-workspaces view).
        assert sum(r["total_tokens"] for r in get_usage_summary(conn)) == 600
        conn.close()

    def test_get_usage_summary_falls_back_to_in_plus_out_when_total_zero(self, tmp_path):
        conn = open_db(str(tmp_path / "test.db"))
        record_usage(conn, "agent-1", "sess-1",
                     model="claude-sonnet-4", provider="openrouter",
                     prompt_tokens=100, completion_tokens=40,
                     total_tokens=140, cost_usd=0.001)
        record_usage(conn, "agent-1", "sess-2",
                     model="claude-sonnet-4", provider="openrouter",
                     prompt_tokens=10, completion_tokens=5,
                     total_tokens=0, cost_usd=0.001)

        summary = get_usage_summary(conn, agent_id="agent-1")

        row = next(r for r in summary if r["model"] == "claude-sonnet-4")
        assert row["total_tokens"] == 155
        conn.close()


class TestAgentStats:
    """Per-agent stat-strip + fleet rollup helpers (dashboard)."""

    def test_agent_stats_aggregates_tokens_and_cost(self, tmp_path):
        conn = open_db(str(tmp_path / "t.db"))
        record_usage(conn, "a", "s1", model="m", provider="p",
                     prompt_tokens=100, completion_tokens=40,
                     total_tokens=140, cost_usd=0.01)
        record_usage(conn, "a", "s2", model="m2", provider="p",
                     prompt_tokens=60, completion_tokens=20,
                     total_tokens=80, cost_usd=0.02)
        s = get_agent_stats(conn, "a")
        assert s["tokens_24h"] == 220
        assert s["tokens_in"] == 160
        assert s["tokens_out"] == 60
        assert abs(s["cost_24h"] - 0.03) < 1e-6
        assert s["model_count"] == 2
        # Active model = most recent usage record's model (the m2 insert).
        assert s["model"] == "m2"
        conn.close()

    def test_agent_stats_falls_back_to_in_plus_out_when_total_zero(self, tmp_path):
        """Harnesses often log prompt/completion but leave total_tokens
        at 0 — the strip must not show 0 when there was real usage."""
        conn = open_db(str(tmp_path / "t.db"))
        record_usage(conn, "a", "s1", model="m", provider="p",
                     prompt_tokens=100, completion_tokens=40,
                     total_tokens=0, cost_usd=0.0)
        s = get_agent_stats(conn, "a")
        assert s["tokens_24h"] == 140
        conn.close()

    def test_agent_stats_mixed_total_rows_counted_per_row(self, tmp_path):
        """Regression: with one row carrying a real total and another
        leaving total_tokens=0, the per-row fallback must count both —
        the old aggregate `SUM(total) or (in+out)` dropped the split row."""
        conn = open_db(str(tmp_path / "t.db"))
        # Row 1: total populated.
        record_usage(conn, "a", "s1", model="m", provider="p",
                     prompt_tokens=100, completion_tokens=40,
                     total_tokens=140, cost_usd=0.0)
        # Row 2: total left at 0, only the split logged.
        record_usage(conn, "a", "s2", model="m", provider="p",
                     prompt_tokens=10, completion_tokens=5,
                     total_tokens=0, cost_usd=0.0)
        s = get_agent_stats(conn, "a")
        assert s["tokens_24h"] == 140 + 15, "split-only row must not be dropped"
        conn.close()

    def test_agent_stats_events_and_last_tick(self, tmp_path):
        conn = open_db(str(tmp_path / "t.db"))
        ensure_session(conn, "agent:a")
        log_event(conn, "agent:a", "harness.spawn", {}, agent_id="a")
        log_event(conn, "agent:a", "usage.record", {}, agent_id="a")
        s = get_agent_stats(conn, "a")
        assert s["events_total"] == 2
        assert s["last_event_type"] == "usage.record"
        assert s["spawn_ts"] is not None
        assert len(s["activity"]) == 30
        conn.close()

    def test_agent_stats_excludes_old_usage(self, tmp_path):
        conn = open_db(str(tmp_path / "t.db"))
        record_usage(conn, "a", "s1", model="m", provider="p",
                     prompt_tokens=100, completion_tokens=40,
                     total_tokens=140, cost_usd=0.01)
        # Backdate it beyond 24h.
        conn.execute("UPDATE usage_records SET ts = ts - 90000")
        conn.commit()
        s = get_agent_stats(conn, "a")
        assert s["tokens_24h"] == 0
        conn.close()

    def test_fleet_rollup_groups_by_agent(self, tmp_path):
        conn = open_db(str(tmp_path / "t.db"))
        record_usage(conn, "a", "s1", model="m", provider="p",
                     prompt_tokens=100, completion_tokens=40, total_tokens=140)
        record_usage(conn, "b", "s2", model="m", provider="p",
                     prompt_tokens=10, completion_tokens=5, total_tokens=15)
        roll = get_fleet_token_rollup(conn)
        assert roll["a"]["tokens"] == 140
        assert roll["b"]["tokens"] == 15
        assert len(roll["a"]["spark"]) == 24
        assert "c" not in roll  # no usage → absent
        conn.close()

    def test_get_usage_summary_empty(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        summary = get_usage_summary(conn)
        assert summary == []
        conn.close()

    def test_migration_adds_indexes(self, tmp_path):
        """Ensure _migrate creates the usage indexes on pre-existing DBs."""
        db_path = tmp_path / "test.db"
        conn = open_db(str(db_path))
        # Check indexes exist
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'usage_%'"
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "usage_agent_idx" in index_names
        assert "usage_model_idx" in index_names
        conn.close()


def test_db_status_counts_tasks(tmp_path):
    """db_status's bounded row counts must include the tasks table
    (migration 10) so operators see it in `relaydeck db status`."""
    from relaydeck.db import db_status
    from relaydeck.tasks import create_task

    db = str(tmp_path / "t.db")
    open_db(db).close()
    create_task("gh", title="t", db_path=db)
    status = db_status(db)
    assert status["rows"].get("tasks") == 1


class TestModelsUsageMaps:
    """Per-model / per-provider usage rollups backing the Models lens."""

    def _seed(self, conn):
        record_usage(conn, "a1", "s", "gpt-5.5", "openai",
                     prompt_tokens=100, completion_tokens=50, total_tokens=150,
                     cost_usd=0.01, request_count=2)
        record_usage(conn, "a2", "s", "GPT-5.5", "OpenAI",
                     prompt_tokens=10, completion_tokens=5, total_tokens=15,
                     cost_usd=0.002, request_count=1)
        record_usage(conn, "a1", "s", "gemma:4b", "ollama",
                     prompt_tokens=20, completion_tokens=20, total_tokens=40,
                     request_count=1)

    def test_preset_usage_map_groups_by_lower_model(self, tmp_path):
        from relaydeck.db import get_preset_usage_map
        conn = open_db(str(tmp_path / "t.db"))
        self._seed(conn)
        m = get_preset_usage_map(conn)
        assert m["gpt-5.5"]["tokens_24h"] == 165
        assert m["gpt-5.5"]["requests_24h"] == 3
        assert round(m["gpt-5.5"]["cost_24h"], 4) == 0.012
        assert len(m["gpt-5.5"]["spark"]) == 24
        assert sum(m["gpt-5.5"]["spark"]) == 165
        assert "gemma:4b" in m
        conn.close()

    def test_provider_usage_map(self, tmp_path):
        from relaydeck.db import get_provider_usage_map
        conn = open_db(str(tmp_path / "t.db"))
        self._seed(conn)
        m = get_provider_usage_map(conn)
        assert m["openai"]["tokens_24h"] == 165
        assert m["openai"]["requests_24h"] == 3
        assert m["ollama"]["tokens_24h"] == 40
        conn.close()

    def test_model_stats_series_and_used_by(self, tmp_path):
        from relaydeck.db import get_model_stats
        conn = open_db(str(tmp_path / "t.db"))
        self._seed(conn)
        s = get_model_stats(conn, "gpt-5.5", provider="openai")
        assert len(s["requests_series"]) == 60
        assert len(s["tokens_series"]) == 60
        assert s["requests_hour"] == 3
        assert s["tokens_hour"] == 165
        assert s["tokens_24h"] == 165
        ids = {u["agent_id"] for u in s["used_by"]}
        assert ids == {"a1", "a2"}
        a1 = next(u for u in s["used_by"] if u["agent_id"] == "a1")
        assert a1["requests"] == 2 and a1["tokens"] == 150
        conn.close()

    def test_model_stats_empty_is_zero_not_fabricated(self, tmp_path):
        from relaydeck.db import get_model_stats
        conn = open_db(str(tmp_path / "t.db"))
        s = get_model_stats(conn, "nonexistent")
        assert s["requests_24h"] == 0
        assert s["used_by"] == []
        assert sum(s["tokens_series"]) == 0
        conn.close()
