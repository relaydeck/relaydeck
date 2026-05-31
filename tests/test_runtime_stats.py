"""/api/runtime-stats — daemon vitals for the dashboard status bar.

Covers the endpoint shape (keys + types) through a real FastAPI TestClient,
plus the collector's DB-size probe against a real file. No mocks at the
I/O boundary: a real config home + real `ps`/stat calls.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from relaydeck.transports.api import _collect_runtime_stats, create_app

EXPECTED_KEYS = {
    "pid", "boot_ts", "uptime_s",
    "db_size_bytes", "cpu_percent", "mem_rss_bytes",
    "load_avg", "proc_count",
}


def _client(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg))
    return TestClient(create_app(cfg))


def test_runtime_stats_returns_expected_keys(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/runtime-stats")
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= EXPECTED_KEYS
    assert isinstance(data["pid"], int)
    assert data["uptime_s"] >= 0
    # boot_ts is in the past (epoch seconds).
    assert 0 < data["boot_ts"] <= time.time() + 1


def test_runtime_stats_requires_auth(tmp_path, monkeypatch):
    """It lives under /api, so the bearer token is required (the test
    harness auto-injects one; strip it to confirm the guard)."""
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/runtime-stats", headers={"Authorization": ""})
    assert r.status_code == 401


def test_collect_runtime_stats_reads_db_size(tmp_path):
    db = tmp_path / "relaydeck.db"
    db.write_bytes(b"x" * 4096)
    stats = _collect_runtime_stats(str(db), time.time() - 60)
    assert stats["db_size_bytes"] == 4096
    assert stats["uptime_s"] >= 60
    # CPU/RSS come from `ps` on the test process — present on macOS + Linux.
    assert stats["mem_rss_bytes"] is None or stats["mem_rss_bytes"] > 0


def test_collect_runtime_stats_missing_db_is_null(tmp_path):
    """An unreadable/absent DB degrades to null, not an exception."""
    stats = _collect_runtime_stats(str(tmp_path / "nope.db"), time.time())
    assert stats["db_size_bytes"] is None
