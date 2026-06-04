"""
Shutdown joins agent threads concurrently against one shared deadline.

The old stop() joined each agent thread with a fixed per-agent timeout, so N
agents serialized to N×timeout and overran the daemon supervisor's SIGKILL
grace — clean stops got force-killed. Since every agent's stop_flag +
terminate() fire before the join loop, the reaps run in parallel; we now wait
against a single SHUTDOWN_JOIN_BUDGET_S deadline. This pins that total
wall-clock stays ~budget regardless of agent count.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import relaydeck.orchestrator as _orch_mod
from relaydeck.orchestrator import get_orchestrator


class _StubAgent:
    def __init__(self):
        self.stop_flag = threading.Event()
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_stop_joins_agents_against_shared_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)

    # Three agents whose threads outlive the budget (simulating slow reaps).
    agents = {aid: _StubAgent() for aid in ("a", "b", "c")}
    threads = {}
    for aid in agents:
        t = threading.Thread(target=lambda: time.sleep(5), daemon=True)
        t.start()
        threads[aid] = t
    orch._instances = dict(agents)
    orch._running = dict(threads)
    orch.SHUTDOWN_JOIN_BUDGET_S = 0.5  # instance override

    start = time.monotonic()
    orch.stop()
    elapsed = time.monotonic() - start

    # Shared deadline: ~budget, NOT 3× a per-agent timeout. Generous slack
    # for CI, but far below the old 3×10s=30s serial behaviour.
    assert elapsed < 2.0, f"stop() took {elapsed:.2f}s — not a shared deadline"
    # Every agent was signalled + terminated before the join loop.
    for a in agents.values():
        assert a.stop_flag.is_set()
        assert a.terminated
    assert orch._running == {}
    assert orch._instances == {}
