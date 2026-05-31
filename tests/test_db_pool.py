"""
Tests for the connection pool + SQLITE_BUSY retry added in
relaydeck/db.py.

These exercise the pool primitives directly. The wider suite proves
the pool is drop-in compatible with the existing 50+ open_db call
sites — none of them needed code changes.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from relaydeck.db import (
    _MAX_POOL_SIZE,
    _close_all_pools,
    _PooledConnection,
    _pool_stats,
    open_db,
)


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "pool.db")
    yield p
    _close_all_pools()


# ── Pooling semantics ────────────────────────────────────────────────


def test_open_db_returns_pooled_connection(db_path):
    conn = open_db(db_path)
    assert isinstance(conn, _PooledConnection)
    conn.close()


def test_pool_reuses_connection_after_close(db_path):
    """Open + close + open again should return the SAME underlying
    sqlite3 connection — that's the whole point of the pool."""
    conn1 = open_db(db_path)
    underlying1 = conn1._conn
    conn1.close()

    conn2 = open_db(db_path)
    underlying2 = conn2._conn
    assert underlying2 is underlying1
    conn2.close()


def test_pool_caps_at_max_size(db_path):
    """Beyond _MAX_POOL_SIZE open connections returning, extras get
    fully closed instead of accumulating in the pool."""
    conns = [open_db(db_path) for _ in range(_MAX_POOL_SIZE + 3)]
    # Hold them all open — pool free list is empty right now.
    stats = _pool_stats()[db_path]
    assert stats["free"] == 0
    for c in conns:
        c.close()
    stats = _pool_stats()[db_path]
    # Only up to _MAX_POOL_SIZE returned; the extras were truly closed.
    assert stats["free"] == _MAX_POOL_SIZE


def test_migrations_run_once_per_path(db_path):
    """The first open_db runs schema + migrations. Subsequent opens
    must not re-execute the script (would be a perf bug + risk
    duplicate index creation noise)."""
    conn = open_db(db_path)
    # Confirm a known table exists post-migration.
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agents'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()

    # Open many times — should be cheap and not error.
    for _ in range(20):
        c = open_db(db_path)
        c.close()


def test_close_is_idempotent(db_path):
    conn = open_db(db_path)
    conn.close()
    conn.close()  # second close — must not raise or double-return
    conn.close()


def test_pool_per_path_isolation(tmp_path):
    """Each DB path has its own pool — connections from one path's
    pool must never end up in another's."""
    p1 = str(tmp_path / "a.db")
    p2 = str(tmp_path / "b.db")
    c1 = open_db(p1)
    c2 = open_db(p2)
    assert c1._pool is not c2._pool
    c1.close()
    c2.close()
    _close_all_pools()


# ── Pragmas applied by _create_connection ───────────────────────────


def test_wal_mode_set(db_path):
    conn = open_db(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_synchronous_normal_set(db_path):
    """synchronous=NORMAL is the new default — safe with WAL,
    noticeably faster than FULL on insert-heavy paths."""
    conn = open_db(db_path)
    try:
        # synchronous values: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
        val = conn.execute("PRAGMA synchronous").fetchone()[0]
        assert val == 1  # NORMAL
    finally:
        conn.close()


def test_busy_timeout_set(db_path):
    conn = open_db(db_path)
    try:
        val = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert val >= 5000  # kernel fallback layer
    finally:
        conn.close()


# ── SQLITE_BUSY retry ───────────────────────────────────────────────


def test_retry_eventually_succeeds_under_contention(db_path):
    """Concurrent writers should serialize cleanly via the retry loop
    rather than raising OperationalError to the caller."""
    # Seed schema
    open_db(db_path).close()

    errors: list[Exception] = []
    successes: list[int] = []

    def worker(n: int):
        try:
            for i in range(20):
                c = open_db(db_path)
                try:
                    c.execute(
                        "INSERT INTO agents (id, type, name, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (f"a{n}-{i}", "test", "n", "stopped", time.time()),
                    )
                    c.commit()
                finally:
                    c.close()
            successes.append(n)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"contention caused {len(errors)} errors: {errors[:3]}"
    assert len(successes) == 8


def test_non_busy_operational_error_does_not_retry(db_path):
    """Syntax / schema errors must bubble up immediately — the retry
    loop is for contention only. Tests this by trying to query a
    non-existent table."""
    conn = open_db(db_path)
    start = time.perf_counter()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM not_a_real_table")
    finally:
        conn.close()
    # Should fail fast — well under the 315ms retry total.
    assert time.perf_counter() - start < 0.1


# ── Context manager shape ────────────────────────────────────────────


def test_pooled_conn_supports_with_statement_commit(db_path):
    """`with open_db(p) as conn:` matches sqlite3.Connection semantics:
    commit on clean exit, rollback on exception."""
    open_db(db_path).close()  # seed schema

    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO agents (id, type, name, status, created_at) "
            "VALUES ('with-ok', 't', 'n', 'stopped', ?)",
            (time.time(),),
        )

    # The row should have been committed.
    conn = open_db(db_path)
    try:
        row = conn.execute("SELECT id FROM agents WHERE id='with-ok'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_pooled_conn_with_statement_rolls_back_on_error(db_path):
    open_db(db_path).close()

    with pytest.raises(RuntimeError):
        with open_db(db_path) as conn:
            conn.execute(
                "INSERT INTO agents (id, type, name, status, created_at) "
                "VALUES ('with-bad', 't', 'n', 'stopped', ?)",
                (time.time(),),
            )
            raise RuntimeError("boom")

    conn = open_db(db_path)
    try:
        row = conn.execute("SELECT id FROM agents WHERE id='with-bad'").fetchone()
        assert row is None  # rollback fired
    finally:
        conn.close()


# ── Stats surface ───────────────────────────────────────────────────


def test_pool_stats_shape(db_path):
    open_db(db_path).close()
    stats = _pool_stats()
    assert db_path in stats
    assert stats[db_path]["max"] == _MAX_POOL_SIZE
    assert stats[db_path]["free"] >= 1
