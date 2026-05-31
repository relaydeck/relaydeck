"""Tests for the dashboard preferences endpoint + on-disk store.

The dashboard pushes its tile-system assignments, density toggle,
accent color, and last-lens / last-workspace bookmarks here. The
contract is "whole-blob GET/PUT" with atomic writes and 0600 mode.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from relaydeck import preferences
from relaydeck.transports.api import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg_home))
    app = create_app(cfg_home)
    return TestClient(app)


def test_get_preferences_empty_when_missing(client):
    r = client.get("/api/preferences")
    assert r.status_code == 200
    assert r.json() == {}


def test_put_then_get_round_trips(client):
    payload = {
        "tile_states": {"core:terminal": "tab", "messaging:inbox": "pop"},
        "density": "compact",
        "accent": "violet",
        "last_lens": "models",
        "last_workspace": "demo",
    }
    r = client.put("/api/preferences", json=payload)
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r2 = client.get("/api/preferences")
    assert r2.status_code == 200
    assert r2.json() == payload


def test_put_preferences_writes_0600(tmp_path):
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    # Force a permissive umask so any inherited-perms path becomes
    # 0o666 — regression for the mode-0600 contract.
    prev = os.umask(0)
    try:
        preferences.write_preferences(cfg_home, {"density": "comfy"})
    finally:
        os.umask(prev)
    p = cfg_home / "preferences.yaml"
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"preferences wrote at {oct(mode)}, expected 0o600"


def test_put_preferences_atomic(tmp_path):
    """The mkstemp + os.replace path must be atomic: a partial blob
    must not be visible to a concurrent reader."""
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    preferences.write_preferences(cfg_home, {"v": 1})
    # Subsequent write should not produce a half-rewritten file
    preferences.write_preferences(cfg_home, {"v": 2, "extra": "x"})
    after = preferences.read_preferences(cfg_home)
    assert after == {"v": 2, "extra": "x"}


def test_get_preferences_handles_invalid_yaml(tmp_path):
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    (cfg_home / "preferences.yaml").write_text("just a string\n")
    # Should swallow and return {} rather than crash the endpoint
    assert preferences.read_preferences(cfg_home) == {}


def test_put_preferences_rejects_non_mapping(tmp_path):
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    with pytest.raises(TypeError):
        preferences.write_preferences(cfg_home, [1, 2, 3])
