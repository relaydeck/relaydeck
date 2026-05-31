"""
Schema-version tracking for relaydeck/db.py:_migrate.

The migration mechanism stays additive + idempotent; `PRAGMA user_version`
is the anchor that (a) lets an already-current DB skip the idempotent DDL
on open and (b) gives future non-additive migrations a version to gate on.
The `ALTER TABLE ADD COLUMN` blocks now only swallow the benign
"duplicate column" error — a genuinely malformed DDL must surface.
"""

from __future__ import annotations

import sqlite3

from relaydeck.db import (
    SCHEMA,
    _SCHEMA_VERSION,
    _close_all_pools,
    _is_duplicate_column,
    _migrate,
    _user_version,
    open_db,
)


def test_fresh_db_stamps_schema_version(tmp_path):
    p = str(tmp_path / "v.db")
    conn = open_db(p)  # runs SCHEMA + _migrate on first open
    try:
        assert _user_version(conn) == _SCHEMA_VERSION
    finally:
        conn.close()
    _close_all_pools()


def test_migrate_is_idempotent_and_short_circuits(tmp_path):
    p = str(tmp_path / "v.db")
    conn = sqlite3.connect(p)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        assert _user_version(conn) == _SCHEMA_VERSION
        # Re-running is a no-op short-circuit (version already current).
        _migrate(conn)
        assert _user_version(conn) == _SCHEMA_VERSION
    finally:
        conn.close()


def test_legacy_db_at_version_zero_remigrates_without_data_loss(tmp_path):
    """An older relaydeck DB never stamped user_version (the run-every-boot
    code). On first open with the new code it sits at 0 < _SCHEMA_VERSION,
    so the idempotent blocks re-run (harmless), data survives, and the
    version is stamped."""
    p = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(p)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Pretend this DB predates version stamping.
        conn.execute("PRAGMA user_version = 0")
        conn.execute(
            "INSERT INTO agents (id, type, name, status, created_at) "
            "VALUES ('a', 'pi', 'A', 'stopped', 0)"
        )
        conn.commit()

        _migrate(conn)
        assert _user_version(conn) == _SCHEMA_VERSION
        # Additive columns exist and the pre-existing row is intact.
        row = conn.execute(
            "SELECT purpose, semantic_status FROM agents WHERE id='a'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_is_duplicate_column_only_matches_dup_error():
    dup = sqlite3.OperationalError("duplicate column name: purpose")
    other = sqlite3.OperationalError("no such table: agents")
    assert _is_duplicate_column(dup) is True
    assert _is_duplicate_column(other) is False
