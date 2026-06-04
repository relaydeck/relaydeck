"""
RELAYDECK_ORCHESTRATION_DEPTH spawn marker (relaydeck/harness/base.py
`_build_env`).

RELAYDECK_AGENT_ID already marks "you are a relaydeck-managed agent"; the
depth marker adds "how deep". The relaydeck skill reads it to
refuse bootstrapping a runaway nested fleet when it finds itself already
inside one (a relaydeck daemon spawned inside a managed agent → its agents
are depth 2+).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.config import register_workspace
from relaydeck.harness.base import HarnessAgent


class _Bare(HarnessAgent):
    CLI = "true"


def _agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_CONFIG_HOME", raising=False)
    home = tmp_path / ".relaydeck"
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "proj"
    ws.mkdir(exist_ok=True)
    register_workspace(home, "proj", ws, [])
    return _Bare(
        agent_id="probe", name="probe", config={}, workspace="proj",
        db_path=str(home / "runtime" / "relaydeck.db"), stop_flag=threading.Event(),
    )


def test_depth_marker_defaults_to_1(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAYDECK_ORCHESTRATION_DEPTH", raising=False)
    env = _agent(tmp_path, monkeypatch)._build_env()
    assert env["RELAYDECK_ORCHESTRATION_DEPTH"] == "1"
    # The definitive "I am managed" signal is still set alongside it.
    assert env["RELAYDECK_AGENT_ID"] == "probe"


def test_depth_marker_increments_from_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_ORCHESTRATION_DEPTH", "2")
    env = _agent(tmp_path, monkeypatch)._build_env()
    assert env["RELAYDECK_ORCHESTRATION_DEPTH"] == "3"


def test_depth_marker_tolerates_garbage_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_ORCHESTRATION_DEPTH", "not-a-number")
    env = _agent(tmp_path, monkeypatch)._build_env()
    assert env["RELAYDECK_ORCHESTRATION_DEPTH"] == "1"
