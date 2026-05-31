"""
Daemon bind-host preference — persistence in state.yaml.

Operators who want the dashboard reachable from other devices on the
LAN set `relaydeck daemon start --host 0.0.0.0` once; that's persisted in
state.yaml so subsequent `relaydeck daemon start` (no flag) inherits the
choice. Pinning:

  - explicit --host writes to state.yaml
  - no --host reads from state.yaml
  - explicit --host 127.0.0.1 *resets* (clears any prior LAN binding)
  - `relaydeck serve` and `relaydeck daemon start` share the same preference
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck import state


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    monkeypatch.setattr("relaydeck.state.Path.home", lambda: tmp_path)
    return cfg


def test_get_returns_none_when_unset(home: Path):
    assert state.get_daemon_bind_host() is None


def test_set_then_get_roundtrip(home: Path):
    state.set_daemon_bind_host("0.0.0.0")
    assert state.get_daemon_bind_host() == "0.0.0.0"
    # And it lands on disk so a sibling process reads the same value.
    raw = (home / "state.yaml").read_text()
    assert "0.0.0.0" in raw


def test_set_strips_whitespace(home: Path):
    state.set_daemon_bind_host("  10.0.0.5  ")
    assert state.get_daemon_bind_host() == "10.0.0.5"


def test_set_none_clears(home: Path):
    state.set_daemon_bind_host("0.0.0.0")
    state.set_daemon_bind_host(None)
    assert state.get_daemon_bind_host() is None
    # Key is removed, not left as empty string.
    raw = (home / "state.yaml").read_text()
    assert "daemon_bind_host" not in raw


def test_set_empty_string_clears(home: Path):
    state.set_daemon_bind_host("0.0.0.0")
    state.set_daemon_bind_host("")
    assert state.get_daemon_bind_host() is None


def test_set_does_not_clobber_other_state(home: Path):
    """current_workspace and daemon_bind_host coexist in state.yaml."""
    state.set_current_workspace("demo")
    state.set_daemon_bind_host("0.0.0.0")
    assert state.get_current_workspace() == "demo"
    assert state.get_daemon_bind_host() == "0.0.0.0"
    state.set_daemon_bind_host(None)
    # Clearing the bind preference doesn't touch the workspace.
    assert state.get_current_workspace() == "demo"


def test_get_ignores_blank_string_on_disk(home: Path, monkeypatch):
    """A future hand-edit of state.yaml that puts `daemon_bind_host: ""`
    in shouldn't trip the read path into returning "" — return None as
    if the key wasn't set at all."""
    (home / "state.yaml").write_text('daemon_bind_host: ""\n')
    assert state.get_daemon_bind_host() is None
