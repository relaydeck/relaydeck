"""
Process-tree reaping — no leaked workers, bounded teardown.

Regression for the `pi` leak: a harness child that forks a `setsid()`'d
worker detaches that worker into its own session/process group, so the old
`killpg(getpgid(child))` reap missed it and it leaked (reparented to init,
100+ stray procs observed). HarnessAgent now walks the descendant tree and
signals every member. These tests pin that the setsid'd worker is actually
in its own group (i.e. the bug scenario is reproduced) AND that it dies
when the agent stops.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from relaydeck.db import open_db
from relaydeck.harness import HarnessAgent
from relaydeck.harness.base import (
    _descendant_pids,
    _pid_alive,
    _wait_pids_gone,
)


class _ForkingHarness(HarnessAgent):
    """A 'CLI' that forks a setsid'd worker (own session/group), then waits.

    The python worker calls os.setsid() so it leaves the shell's process
    group — exactly the case a single killpg can't reach.
    """

    CLI = "sh"
    DEFAULT_ARGS = [
        "-c",
        "python3 -c 'import os,time; os.setsid(); time.sleep(30)' & sleep 30",
    ]


def _mk(tmp_path: Path) -> _ForkingHarness:
    db = str(tmp_path / "d.db")
    open_db(db).close()
    return _ForkingHarness(
        agent_id="a", name="a", config={}, workspace=None,
        db_path=db, stop_flag=threading.Event(),
    )


def _run_until_pty(agent) -> threading.Thread:
    t = threading.Thread(target=agent.run, daemon=True)
    t.start()
    for _ in range(60):                      # ~3s for the fork to land
        if agent._master_fd is not None:
            break
        time.sleep(0.05)
    return t


def _setsid_worker_pid(child_pid: int) -> int | None:
    """Find a descendant of `child_pid` that's in its OWN process group
    (i.e. it called setsid and would be missed by killpg(child))."""
    child_pgid = os.getpgid(child_pid)
    for _ in range(40):                      # give the worker time to fork
        for pid in _descendant_pids(child_pid):
            try:
                if os.getpgid(pid) != child_pgid:
                    return pid
            except ProcessLookupError:
                continue
        time.sleep(0.05)
    return None


def test_descendant_pids_finds_grandchildren(tmp_path):
    a = _mk(tmp_path)
    t = _run_until_pty(a)
    try:
        assert a._proc is not None, "child never forked"
        desc = _descendant_pids(a._proc.pid)
        assert desc, "expected the sh child to have descendants"
    finally:
        a.stop_flag.set(); a.terminate(); t.join(timeout=8)


def test_setsid_worker_is_reaped(tmp_path):
    """The core regression: a worker in its own process group must NOT
    survive the agent stopping. killpg alone would leak it."""
    a = _mk(tmp_path)
    t = _run_until_pty(a)
    assert a._proc is not None, "child never forked"
    worker = _setsid_worker_pid(a._proc.pid)
    assert worker is not None, "test setup: no setsid'd worker observed"
    assert _pid_alive(worker)

    # Stop the way the orchestrator does: flag + terminate, then the run
    # loop's finally runs _reap. terminate() snapshots the tree while the
    # child is alive so the detached worker stays reachable.
    a.terminate()
    a.stop_flag.set()
    t.join(timeout=8)

    # Worker (own group, would-be leak) must be gone.
    assert not _wait_pids_gone({worker}, time.time() + 3.0), \
        f"setsid'd worker {worker} leaked after stop"
    assert a._proc is not None and a._proc.poll() is not None, \
        "direct child not reaped"


def test_reap_is_bounded(tmp_path):
    """Teardown must finish well inside the orchestrator's 10s join."""
    a = _mk(tmp_path)
    t = _run_until_pty(a)
    assert a._proc is not None, "child never forked"
    _setsid_worker_pid(a._proc.pid)          # ensure the tree exists
    start = time.time()
    a.terminate()
    a.stop_flag.set()
    t.join(timeout=10)
    assert not t.is_alive(), "run thread did not stop within join window"
    assert time.time() - start < 9.0, "reap exceeded the bounded window"
