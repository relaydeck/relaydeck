"""
Tests for plugin event bus durability.

Pinning:
 - emit() persists to bus_events before dispatch
 - subscribe_durable() replays unacked events to the handler
 - cursor advances only on successful handler invocation
 - in-memory subscribers are unaffected by durability
 - prune respects the unacked floor
 - subscribe_durable on a bus without db_path degrades to subscribe()
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.db import (
    _close_all_pools,
    load_bus_cursor,
    open_db,
    prune_bus_events,
)
from relaydeck.plugin import Event, PluginEventBus


@pytest.fixture
def db_path(tmp_path):
    """Per-test isolated SQLite file. _close_all_pools between tests
    so the pooled connection from a prior test doesn't pin a deleted
    tmp file or leak schema state across the suite."""
    _close_all_pools()
    p = tmp_path / "relaydeck.db"
    # Touch via open_db so migrations run before the bus uses it.
    conn = open_db(str(p))
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    yield str(p)
    _close_all_pools()


def _row_count(db_path: str, table: str) -> int:
    conn = open_db(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class TestEmitPersistence:
    def test_emit_writes_row_to_bus_events(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        bus.emit(Event(type="agent.message", data={"body": "hi"},
                       source_plugin="messaging"))
        assert _row_count(db_path, "bus_events") == 1
        conn = open_db(db_path)
        try:
            row = conn.execute(
                "SELECT type, data, source_plugin FROM bus_events"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "agent.message"
        assert json.loads(row[1]) == {"body": "hi"}
        assert row[2] == "messaging"

    def test_in_memory_bus_does_not_persist(self, db_path):
        bus = PluginEventBus()  # no db_path
        bus.emit(Event(type="agent.message", data={"body": "hi"}))
        assert _row_count(db_path, "bus_events") == 0

    def test_in_memory_subscribers_still_fire_when_persistent(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        seen: list[Event] = []
        bus.subscribe("agent.*", lambda e: seen.append(e))
        bus.emit(Event(type="agent.message", data={"x": 1}))
        bus.emit(Event(type="other.type", data={}))
        assert len(seen) == 1
        assert seen[0].data == {"x": 1}


class TestDurableSubscribe:
    def test_replays_unacked_events_on_registration(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        bus.emit(Event(type="agent.message", data={"n": 1}))
        bus.emit(Event(type="agent.message", data={"n": 2}))
        bus.emit(Event(type="other", data={}))
        bus.emit(Event(type="agent.message", data={"n": 3}))

        seen: list[int] = []
        bus.subscribe_durable(
            "agent.message",
            lambda e: seen.append(e.data["n"]),
            key="test.sink",
        )
        assert seen == [1, 2, 3]

    def test_subsequent_emits_dispatch_live(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        seen: list[int] = []
        bus.subscribe_durable(
            "agent.message",
            lambda e: seen.append(e.data["n"]),
            key="test.sink",
        )
        bus.emit(Event(type="agent.message", data={"n": 1}))
        bus.emit(Event(type="agent.message", data={"n": 2}))
        assert seen == [1, 2]

    def test_cursor_advances_after_successful_dispatch(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        bus.subscribe_durable(
            "agent.message", lambda e: None, key="advances.sink",
        )
        bus.emit(Event(type="agent.message", data={}))
        bus.emit(Event(type="agent.message", data={}))
        conn = open_db(db_path)
        try:
            cursor = load_bus_cursor(conn, "advances.sink")
        finally:
            conn.close()
        # Two emits with auto-incremented ids 1 and 2 → cursor at 2.
        assert cursor == 2

    def test_cursor_halts_at_handler_exception_on_replay(self, db_path):
        """A poisoned event should not advance the cursor past it.
        On the next subscribe, replay re-attempts the same event."""
        bus = PluginEventBus(db_path=db_path)
        for n in (1, 2, 3, 4):
            bus.emit(Event(type="job", data={"n": n}))

        attempts: list[int] = []

        def handler(e: Event) -> None:
            attempts.append(e.data["n"])
            if e.data["n"] == 3:
                raise RuntimeError("poison")

        bus.subscribe_durable("job", handler, key="halt.sink")
        # Got 1, 2, 3 (raise). Cursor should sit at 2 (last successful).
        assert attempts == [1, 2, 3]
        conn = open_db(db_path)
        try:
            cursor = load_bus_cursor(conn, "halt.sink")
        finally:
            conn.close()
        assert cursor == 2

        # Re-register a non-failing handler; it should pick up at 3.
        retry: list[int] = []
        bus2 = PluginEventBus(db_path=db_path)
        bus2.subscribe_durable(
            "job", lambda e: retry.append(e.data["n"]), key="halt.sink",
        )
        assert retry == [3, 4]

    def test_live_failure_freezes_cursor_despite_later_success(self, db_path):
        """Regression: a durable LIVE delivery that fails must freeze the
        cursor — a *later* successful live event must not advance it past
        the failed id, or the failed event is silently skipped on restart
        replay (breaking at-least-once)."""
        bus = PluginEventBus(db_path=db_path)

        live: list[int] = []

        def handler(e: Event) -> None:
            live.append(e.data["n"])
            if e.data["n"] == 1:
                raise RuntimeError("transient failure on event 1")

        # Subscribe first (no backlog), then emit live.
        bus.subscribe_durable("job", handler, key="live.sink")
        bus.emit(Event(type="job", data={"n": 1}))  # fails
        bus.emit(Event(type="job", data={"n": 2}))  # succeeds
        assert live == [1, 2]

        # The cursor must NOT have advanced to 2 — it should sit below the
        # failed event so replay re-delivers from id 1.
        conn = open_db(db_path)
        try:
            cursor = load_bus_cursor(conn, "live.sink")
        finally:
            conn.close()
        assert cursor < 1, f"cursor advanced past the failed event (got {cursor})"

        # Restart: a fresh, non-failing handler must still receive event 1
        # (and 2 again — at-least-once permits the duplicate).
        retry: list[int] = []
        bus2 = PluginEventBus(db_path=db_path)
        bus2.subscribe_durable(
            "job", lambda e: retry.append(e.data["n"]), key="live.sink",
        )
        assert 1 in retry, "failed live event was not replayed after restart"

    def test_replay_after_restart(self, db_path):
        """Emit, then construct a fresh bus instance — durable sub
        sees every unacked event. Pins the crash-recovery scenario."""
        b1 = PluginEventBus(db_path=db_path)
        for n in range(5):
            b1.emit(Event(type="agent.message", data={"n": n}))
        # Simulate restart: drop b1, build b2 against the same db.
        del b1
        b2 = PluginEventBus(db_path=db_path)
        seen: list[int] = []
        b2.subscribe_durable(
            "agent.message",
            lambda e: seen.append(e.data["n"]),
            key="restart.sink",
        )
        assert seen == [0, 1, 2, 3, 4]

    def test_durable_without_db_path_falls_back_to_subscribe(self, db_path):
        """Belt-and-suspenders: durable on an in-memory bus shouldn't
        error — just degrades to plain subscribe."""
        bus = PluginEventBus()  # no db_path
        seen: list[int] = []
        bus.subscribe_durable(
            "agent.message",
            lambda e: seen.append(e.data["n"]),
            key="fallback.sink",
        )
        bus.emit(Event(type="agent.message", data={"n": 1}))
        assert seen == [1]


class TestPrune:
    def test_prune_skips_unacked_events(self, db_path):
        """An event older than the retention window but past every
        cursor's last-acked id must stay until acked. Otherwise the
        durability contract is hollow."""
        bus = PluginEventBus(db_path=db_path)
        old = time.time() - 30 * 86400
        # Emit two old events; backdate them by hand.
        bus.emit(Event(type="job", data={"n": 1}))
        bus.emit(Event(type="job", data={"n": 2}))
        conn = open_db(db_path)
        try:
            conn.execute("UPDATE bus_events SET ts = ?", (old,))
            conn.commit()
        finally:
            conn.close()

        # Register a durable sub with cursor=1 (only event 1 acked).
        conn = open_db(db_path)
        try:
            conn.execute(
                "INSERT INTO bus_cursors (subscriber_key, pattern, "
                "last_acked_id, updated_at) VALUES (?, ?, ?, ?)",
                ("test.sub", "job", 1, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        deleted = prune_bus_events(db_path, retention_days=7)
        assert deleted == 1  # only event 1 is past floor AND past cutoff
        assert _row_count(db_path, "bus_events") == 1

    def test_prune_age_only_when_no_durable_subs(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        bus.emit(Event(type="job", data={}))
        bus.emit(Event(type="job", data={}))
        conn = open_db(db_path)
        try:
            conn.execute(
                "UPDATE bus_events SET ts = ?", (time.time() - 30 * 86400,),
            )
            conn.commit()
        finally:
            conn.close()
        deleted = prune_bus_events(db_path, retention_days=7)
        assert deleted == 2
        assert _row_count(db_path, "bus_events") == 0

    def test_prune_disabled_when_retention_nonpositive(self, db_path):
        bus = PluginEventBus(db_path=db_path)
        bus.emit(Event(type="job", data={}))
        # Force old ts.
        conn = open_db(db_path)
        try:
            conn.execute(
                "UPDATE bus_events SET ts = ?", (time.time() - 100 * 86400,),
            )
            conn.commit()
        finally:
            conn.close()
        assert prune_bus_events(db_path, retention_days=0) == 0
        assert _row_count(db_path, "bus_events") == 1
