"""
Tests for the idle-time DB maintenance helpers added in relaydeck/db.py:
prune_old_events, wal_checkpoint, db_status.
"""

from __future__ import annotations

import time

import pytest

from relaydeck.db import (
    DEFAULT_EVENT_RETENTION_DAYS,
    _close_all_pools,
    db_status,
    log_event,
    open_db,
    prune_old_events,
    vacuum_db,
    wal_checkpoint,
)


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "maint.db")
    # Touch the DB so session FK constraints don't fight log_event.
    conn = open_db(p)
    conn.execute(
        "INSERT INTO sessions (id, started_at) VALUES ('s', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()
    yield p
    _close_all_pools()


def _backdate_event(db_path: str, event_id: int, ts: float) -> None:
    conn = open_db(db_path)
    try:
        conn.execute("UPDATE events SET ts=? WHERE id=?", (ts, event_id))
        conn.commit()
    finally:
        conn.close()


# ── prune_old_events ────────────────────────────────────────────────


def test_prune_deletes_only_old_rows(db_path):
    # Three events: two old, one fresh.
    conn = open_db(db_path)
    try:
        old1 = log_event(conn, "s", "agent.start")
        old2 = log_event(conn, "s", "agent.stop")
        fresh = log_event(conn, "s", "agent.start")
    finally:
        conn.close()

    now = time.time()
    days = 7
    _backdate_event(db_path, old1, now - (days + 1) * 86400)
    _backdate_event(db_path, old2, now - (days + 2) * 86400)

    deleted = prune_old_events(db_path, retention_days=days)
    assert deleted == 2

    conn = open_db(db_path)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM events ORDER BY id")]
    finally:
        conn.close()
    assert ids == [fresh]


def test_prune_disabled_by_zero_or_negative(db_path):
    """retention_days <= 0 means 'keep forever' — explicit opt-out."""
    conn = open_db(db_path)
    try:
        eid = log_event(conn, "s", "agent.start")
    finally:
        conn.close()
    _backdate_event(db_path, eid, time.time() - 9999 * 86400)

    assert prune_old_events(db_path, retention_days=0) == 0
    assert prune_old_events(db_path, retention_days=-5) == 0

    conn = open_db(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    finally:
        conn.close()
    assert rows[0] == 1


def test_default_retention_is_30_days():
    """Sanity check: default constant matches the documented default."""
    assert DEFAULT_EVENT_RETENTION_DAYS == 30


# ── wal_checkpoint ──────────────────────────────────────────────────


def test_checkpoint_returns_busy_log_checkpointed(db_path):
    # Write a few rows so the WAL has frames to checkpoint.
    conn = open_db(db_path)
    try:
        for i in range(20):
            conn.execute(
                "INSERT INTO events (session_id, ts, type) "
                "VALUES ('s', ?, ?)",
                (time.time(), f"e{i}"),
            )
        conn.commit()
    finally:
        conn.close()

    stats = wal_checkpoint(db_path, mode="TRUNCATE")
    assert set(stats.keys()) == {"busy", "log", "checkpointed"}
    # On an uncontended DB the checkpoint should fully succeed.
    assert stats["busy"] == 0


def test_checkpoint_rejects_invalid_mode(db_path):
    with pytest.raises(ValueError, match="checkpoint mode"):
        wal_checkpoint(db_path, mode="NOT_A_MODE")


# ── db_status ───────────────────────────────────────────────────────


def test_status_reports_path_and_sizes(db_path):
    open_db(db_path).close()
    snap = db_status(db_path)
    assert snap["path"] == db_path
    assert snap["bytes"] > 0
    assert "wal_bytes" in snap


def test_status_includes_row_counts(db_path):
    """The status snapshot must expose the row counts the operator
    actually cares about (agents, events, messages, etc.)."""
    snap = db_status(db_path)
    rows = snap["rows"]
    assert "agents" in rows
    assert "events" in rows
    assert "agent_messages" in rows
    assert "usage_records" in rows


def test_status_includes_pool_stats(db_path):
    conn = open_db(db_path)
    conn.close()  # populate the pool
    snap = db_status(db_path)
    assert db_path in snap["pools"]
    assert snap["pools"][db_path]["max"] >= 1


def test_status_reports_free_bytes(db_path):
    """db_status exposes reclaimable (freelist) bytes — the number that
    explains why the file doesn't shrink after a prune."""
    snap = db_status(db_path)
    assert "free_bytes" in snap
    assert isinstance(snap["free_bytes"], int)
    assert snap["free_bytes"] >= 0


# ── vacuum_db ───────────────────────────────────────────────────────


def test_vacuum_reclaims_freed_space(db_path):
    """DELETE moves pages to the freelist but does NOT shrink the .db
    file; only VACUUM returns that space to the OS."""
    conn = open_db(db_path)
    try:
        for i in range(4000):
            conn.execute(
                "INSERT INTO events (session_id, ts, type, payload) "
                "VALUES ('s', ?, ?, ?)",
                (time.time(), f"e{i}", "x" * 300),
            )
        conn.commit()
    finally:
        conn.close()
    wal_checkpoint(db_path, mode="TRUNCATE")  # fold WAL into the main file
    big = db_status(db_path)["bytes"]

    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM events")
        conn.commit()
    finally:
        conn.close()
    wal_checkpoint(db_path, mode="TRUNCATE")

    after_delete = db_status(db_path)
    # File hasn't shrunk on DELETE alone; the pages are reclaimable.
    assert after_delete["bytes"] >= big * 0.9, "DELETE alone must not shrink the file"
    assert after_delete["free_bytes"] > 0, "freed pages should be reported reclaimable"

    stats = vacuum_db(db_path)
    assert stats["reclaimed_bytes"] > 0
    assert stats["after_bytes"] < stats["before_bytes"]
    assert db_status(db_path)["bytes"] < big, "VACUUM should shrink the file"
    assert db_status(db_path)["free_bytes"] == 0, "freelist emptied after VACUUM"


def test_maintenance_worker_skips_vacuum_on_small_db(tmp_path, monkeypatch):
    """VACUUM is gated on ≥64 MiB reclaimable AND once/day, so a healthy
    small DB must never trigger the whole-file rewrite — the common case
    stays free. The prune+checkpoint passes still run."""
    from relaydeck.transports.cli import _start_db_maintenance_worker
    from relaydeck.workers import get_worker_registry

    (tmp_path / "runtime").mkdir()
    dbp = str(tmp_path / "runtime" / "relaydeck.db")
    open_db(dbp).close()  # create the DB

    calls: list[str] = []
    # _start_db_maintenance_worker does `from relaydeck.db import vacuum_db`
    # at call time, so patching the module attribute first takes effect.
    monkeypatch.setattr(
        "relaydeck.db.vacuum_db",
        lambda path: calls.append(str(path)) or {
            "before_bytes": 0, "after_bytes": 0,
            "reclaimed_bytes": 0, "free_bytes_before": 0,
        },
    )

    _start_db_maintenance_worker(tmp_path)
    reg = get_worker_registry()
    w = [w for w in reg.by_plugin("relaydeck") if w.name == "db.maintenance"][-1]
    w.stop()
    w.join()
    w.target(w)  # one deterministic tick
    reg.unregister(w.id)
    _close_all_pools()

    assert calls == [], "a small DB must not be VACUUMed (gate must hold)"


def test_status_on_nonexistent_db(tmp_path):
    """A path that doesn't exist yet shouldn't blow up — operators
    might run status before the daemon ever booted."""
    missing = str(tmp_path / "nope.db")
    snap = db_status(missing)
    assert snap["bytes"] == 0
    assert snap["wal_bytes"] == 0
    # rows key may be absent (we skip the SELECTs when the file
    # doesn't exist) — that's the documented behavior.


# ── db.maintenance worker wiring ────────────────────────────────────


def test_maintenance_worker_prunes_invocations_and_runs(tmp_path):
    """The 5-minute db.maintenance worker must prune model_invocations
    and automation_runs — both grew unbounded before this wiring
    (prune_invocations was dead code; prune_runs was manual-CLI only)."""
    from relaydeck.transports.cli import _start_db_maintenance_worker
    from relaydeck.workers import get_worker_registry

    (tmp_path / "runtime").mkdir()
    dbp = str(tmp_path / "runtime" / "relaydeck.db")
    old = time.time() - 40 * 86400  # past the 30d retention
    fresh = time.time()

    conn = open_db(dbp)
    try:
        conn.execute(
            "INSERT INTO model_invocations (agent_id, ts) VALUES ('w', ?)", (old,))
        conn.execute(
            "INSERT INTO model_invocations (agent_id, ts) VALUES ('w', ?)", (fresh,))
        conn.execute(
            "INSERT INTO automation_runs (id, automation_id, status, "
            "started_at, finished_at) VALUES ('r1', 'a', 'succeeded', ?, ?)",
            (old, old))
        conn.execute(
            "INSERT INTO automation_runs (id, automation_id, status, "
            "started_at, finished_at) VALUES ('r2', 'a', 'succeeded', ?, ?)",
            (fresh, fresh))
        conn.commit()
    finally:
        conn.close()

    _start_db_maintenance_worker(tmp_path)
    reg = get_worker_registry()
    workers = [w for w in reg.by_plugin("relaydeck") if w.name == "db.maintenance"]
    assert workers, "db.maintenance worker not registered"
    w = workers[-1]
    # Halt the supervisor thread, then run one tick deterministically.
    w.stop()
    w.join()
    w.target(w)

    conn = open_db(dbp)
    try:
        inv = conn.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0]
    finally:
        conn.close()
    reg.unregister(w.id)
    _close_all_pools()

    assert inv == 1   # old invocation pruned, fresh kept
    assert runs == 1  # old run pruned, fresh kept
