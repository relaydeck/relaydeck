"""Per-agent workers (e.g. the codex / pi usage tailers) must not leak.

The bug: `register_worker` mints a fresh uuid per call and the supervisor
only marks a stopped worker STOPPED (never unregisters), so every agent
(re)start piled another `codex.usage-tailer` / `pi.usage-tailer` row into
the registry — the Workers lens filled with dead duplicates.

Fix: a worker carrying an `agent_id` is one-per-agent — register dedups it,
and a graceful stop unregisters it. ERRORED / CRASH_LOOP rows still stay
(they're surfaced for the operator).
"""

from __future__ import annotations

import time

import pytest

from relaydeck.workers import (
    WorkerStatus,
    get_worker_registry,
    register_worker,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    reg = get_worker_registry()
    for w in reg.all():
        w.stop()
        w.join(timeout=2.0)
    reg._workers.clear()
    yield
    for w in reg.all():
        w.stop()
        w.join(timeout=2.0)
    reg._workers.clear()


def _count(name, agent_id):
    return sum(1 for w in get_worker_registry().all()
               if w.name == name and w.agent_id == agent_id)


def _count_plugin(name, plugin):
    return sum(
        1 for w in get_worker_registry().all()
        if w.name == name and w.plugin == plugin and w.agent_id is None
    )


def _noop(w):
    pass


def test_restart_does_not_leak_agent_workers():
    reg = get_worker_registry()
    # Simulate an agent starting its tailer 4 times (restarts).
    workers = [register_worker("codex.usage-tailer", "codex", _noop,
                               interval_s=0.05, agent_id="rev") for _ in range(4)]
    # Exactly one tailer for this agent — not four.
    assert _count("codex.usage-tailer", "rev") == 1
    # The survivor is the most recent registration; earlier ones were stopped.
    assert reg.get(workers[-1].id) is not None
    for w in workers[:-1]:
        assert reg.get(w.id) is None


def test_two_different_agents_keep_separate_tailers():
    register_worker("pi.usage-tailer", "pi", _noop, interval_s=0.05, agent_id="a")
    register_worker("pi.usage-tailer", "pi", _noop, interval_s=0.05, agent_id="b")
    assert _count("pi.usage-tailer", "a") == 1
    assert _count("pi.usage-tailer", "b") == 1


def test_graceful_stop_unregisters_agent_worker():
    reg = get_worker_registry()
    w = register_worker("codex.usage-tailer", "codex", _noop,
                        interval_s=0.05, agent_id="rev")
    w.stop()
    w.join(timeout=2.0)
    deadline = time.time() + 2.0
    while time.time() < deadline and reg.get(w.id) is not None:
        time.sleep(0.02)
    assert reg.get(w.id) is None      # removed, no stale STOPPED row


def test_non_agent_worker_is_not_auto_unregistered():
    """A daemon-wide worker (no agent_id) keeps its row on stop so the
    Workers lens still shows it as STOPPED — unchanged behavior."""
    reg = get_worker_registry()
    w = register_worker("db.maintenance", "relaydeck", _noop, interval_s=0.05)
    w.stop()
    w.join(timeout=2.0)
    time.sleep(0.1)
    assert reg.get(w.id) is not None
    assert w.status == WorkerStatus.STOPPED


def test_errored_agent_worker_is_kept():
    """An agent worker that crashes stays in the registry (surfaced),
    even though it's agent-scoped."""
    reg = get_worker_registry()

    def boom(w):
        raise RuntimeError("kaboom")

    w = register_worker("codex.usage-tailer", "codex", boom,
                        interval_s=0.01, agent_id="rev")
    deadline = time.time() + 2.0
    while time.time() < deadline and w.status != WorkerStatus.ERRORED:
        time.sleep(0.02)
    assert w.status == WorkerStatus.ERRORED
    assert reg.get(w.id) is not None   # NOT auto-removed


def test_errored_row_survives_a_later_registration():
    """The dedup-on-register must NOT silently clear a past failure: when a
    fresh tailer registers for the same agent, the prior ERRORED row stays
    visible (reviewer's repro)."""
    reg = get_worker_registry()

    def boom(w):
        raise RuntimeError("kaboom")

    errored = register_worker("codex.usage-tailer", "codex", boom,
                              interval_s=0.01, agent_id="rev")
    deadline = time.time() + 2.0
    while time.time() < deadline and errored.status != WorkerStatus.ERRORED:
        time.sleep(0.02)
    assert errored.status == WorkerStatus.ERRORED

    # Agent restarts → a new tailer registers for the SAME agent.
    fresh = register_worker("codex.usage-tailer", "codex", _noop,
                            interval_s=0.05, agent_id="rev")
    assert reg.get(errored.id) is not None   # past failure still surfaced
    assert reg.get(fresh.id) is not None     # new tailer is live
    assert _count("codex.usage-tailer", "rev") == 2  # 1 errored + 1 running


def test_plugin_worker_dedup_on_reregister():
    """Daemon-wide workers (skills.scan, github pollers) must not pile up
    STOPPED rows when a plugin re-registers after disable/re-enable."""
    reg = get_worker_registry()
    workers = [
        register_worker("skills.scan", "skills", _noop, interval_s=0.05)
        for _ in range(3)
    ]
    assert sum(1 for w in reg.all() if w.name == "skills.scan") == 1
    assert reg.get(workers[-1].id) is not None
    for w in workers[:-1]:
        assert reg.get(w.id) is None


def test_errored_plugin_worker_survives_reregister():
    """Daemon-wide ERRORED rows must stay visible when a fresh worker
    registers — same contract as per-agent usage tailers."""
    reg = get_worker_registry()

    def boom(w):
        raise RuntimeError("kaboom")

    errored = register_worker("skills.scan", "skills", boom, interval_s=0.01)
    deadline = time.time() + 2.0
    while time.time() < deadline and errored.status != WorkerStatus.ERRORED:
        time.sleep(0.02)
    assert errored.status == WorkerStatus.ERRORED

    fresh = register_worker("skills.scan", "skills", _noop, interval_s=0.05)
    assert reg.get(errored.id) is not None
    assert reg.get(fresh.id) is not None
    assert _count_plugin("skills.scan", "skills") == 2
