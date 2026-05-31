"""
Tests for worker supervision added in relaydeck/workers.py:

  - RestartPolicy.STOP (legacy default): one tick exception ends in
    ERRORED, supervisor exits.
  - RestartPolicy.RESTART: supervisor catches the exception, sleeps
    a short backoff, re-runs target.
  - Crash-loop detection: > N restarts in M seconds → CRASH_LOOP
    (terminal); supervisor stops trying.
  - retry_worker(): operator-initiated re-arm of CRASH_LOOP /
    ERRORED workers.

These tests deliberately run real threads — fast enough at sub-100ms
tick intervals that the suite stays snappy, real enough that race
conditions in the supervisor surface.
"""

from __future__ import annotations

import time

import pytest

from relaydeck.workers import (
    RestartPolicy,
    Worker,
    WorkerStatus,
    get_worker_registry,
    register_worker,
    retry_worker,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Each test starts from an empty registry so we don't see
    workers spawned by other tests (the daemon's db.maintenance
    worker, for instance)."""
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


# ── Default STOP policy (backward compat) ───────────────────────────


def test_stop_policy_errors_out_on_first_failure():
    """The legacy behavior: a tick exception transitions to ERRORED
    and the supervisor exits. No silent retries."""
    def boom(w):
        raise RuntimeError("kaboom")

    w = register_worker("test-stop", "test", boom, interval_s=0.01)
    # Give the supervisor time to hit the exception once.
    deadline = time.time() + 2.0
    while time.time() < deadline and w.status not in (
        WorkerStatus.ERRORED, WorkerStatus.STOPPED
    ):
        time.sleep(0.02)
    assert w.status == WorkerStatus.ERRORED
    assert "kaboom" in (w.last_error or "")
    assert w.restart_count == 0


# ── RESTART policy ──────────────────────────────────────────────────


def test_restart_policy_recovers_after_failure():
    """With restart_policy=RESTART, an erroring tick triggers a
    backoff + retry. Once the target stops erroring, the worker
    keeps ticking normally."""
    fails_remaining = [3]

    def flaky(w):
        if fails_remaining[0] > 0:
            fails_remaining[0] -= 1
            raise RuntimeError("transient")

    w = register_worker(
        "test-flaky", "test", flaky,
        interval_s=0.01,
        restart_policy=RestartPolicy.RESTART,
        restart_backoff_s=0.01,
        crash_loop_threshold=10,
    )
    # Wait until we've ticked successfully a few times AFTER the
    # transient failures.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if w.tick_count >= 3 and w.restart_count >= 3:
            break
        time.sleep(0.02)
    w.stop()
    w.join(timeout=2.0)
    assert w.restart_count >= 3
    assert w.tick_count >= 3  # supervisor kept invoking after recovery
    assert w.status in (WorkerStatus.STOPPED, WorkerStatus.RUNNING)


# ── Crash-loop detection ────────────────────────────────────────────


def test_crash_loop_threshold_transitions_to_crash_loop():
    """A worker that always raises must end up in CRASH_LOOP after
    crash_loop_threshold restarts. Supervisor stops; no infinite spin."""
    def always_fail(w):
        raise RuntimeError("permanent")

    w = register_worker(
        "test-crash", "test", always_fail,
        interval_s=0.01,
        restart_policy=RestartPolicy.RESTART,
        restart_backoff_s=0.005,
        crash_loop_threshold=3,
        crash_loop_window_s=10.0,
    )
    deadline = time.time() + 3.0
    while time.time() < deadline and w.status != WorkerStatus.CRASH_LOOP:
        time.sleep(0.02)
    assert w.status == WorkerStatus.CRASH_LOOP
    assert w.restart_count >= 3
    # Supervisor thread should have exited.
    w.join(timeout=2.0)
    assert not (w._thread and w._thread.is_alive())


def test_crash_loop_window_ages_out_old_restarts():
    """If failures are spread out beyond `crash_loop_window_s`, the
    worker should NOT trip crash-loop — the window slides forward and
    old timestamps fall out."""
    fail_for = [10]  # fail the first 10 times, then succeed

    def slow_fail(w):
        if fail_for[0] > 0:
            fail_for[0] -= 1
            raise RuntimeError("slow fail")

    w = register_worker(
        "test-window", "test", slow_fail,
        interval_s=0.01,
        restart_policy=RestartPolicy.RESTART,
        restart_backoff_s=0.005,
        crash_loop_threshold=3,
        # Window so short that consecutive restarts fall out fast.
        crash_loop_window_s=0.005,
    )
    # Wait until the target finally stops failing and we've ticked.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if w.tick_count >= 1 and fail_for[0] == 0:
            break
        time.sleep(0.02)
    w.stop()
    w.join(timeout=2.0)
    assert fail_for[0] == 0
    assert w.status != WorkerStatus.CRASH_LOOP
    # Many restarts, but never tripped the threshold-in-window.
    assert w.restart_count >= 5


# ── retry_worker ────────────────────────────────────────────────────


def test_retry_revives_crash_loop_worker():
    """After crash_loop, retry_worker() resets the counter and
    re-arms. If target now succeeds, the worker runs cleanly."""
    fail_until_retry = [True]

    def conditional(w):
        if fail_until_retry[0]:
            raise RuntimeError("until-retry")

    w = register_worker(
        "test-retry", "test", conditional,
        interval_s=0.01,
        restart_policy=RestartPolicy.RESTART,
        restart_backoff_s=0.005,
        crash_loop_threshold=2,
        crash_loop_window_s=5.0,
    )
    # Wait for crash_loop.
    deadline = time.time() + 2.0
    while time.time() < deadline and w.status != WorkerStatus.CRASH_LOOP:
        time.sleep(0.02)
    assert w.status == WorkerStatus.CRASH_LOOP

    # Operator "fixed" the issue and re-armed.
    fail_until_retry[0] = False
    assert retry_worker(w.id) is True

    deadline = time.time() + 2.0
    while time.time() < deadline and w.tick_count == 0:
        time.sleep(0.02)
    assert w.tick_count >= 1
    assert w.status in (WorkerStatus.RUNNING, WorkerStatus.STOPPED)
    w.stop()
    w.join(timeout=2.0)


def test_retry_refuses_running_worker():
    """A worker still running cleanly shouldn't be 'retryable' — the
    operator probably meant a different worker."""
    def ok(w):
        time.sleep(0.05)

    w = register_worker("test-running", "test", ok, interval_s=0.01)
    # Wait for it to be running.
    deadline = time.time() + 1.0
    while time.time() < deadline and w.status != WorkerStatus.RUNNING:
        time.sleep(0.02)
    assert retry_worker(w.id) is False
    w.stop()
    w.join(timeout=2.0)


def test_retry_unknown_worker():
    assert retry_worker("does-not-exist") is False


# ── Snapshot shape ──────────────────────────────────────────────────


def test_snapshot_includes_supervision_fields():
    """The API snapshot must surface restart_count + restart_policy
    so the dashboard / CLI can render them in the workers table."""
    def fast(w):
        pass

    w = register_worker(
        "test-snap", "test", fast,
        interval_s=0.01,
        restart_policy=RestartPolicy.RESTART,
    )
    snap = w.snapshot()
    assert "restart_count" in snap
    assert "restart_policy" in snap
    assert snap["restart_policy"] == RestartPolicy.RESTART
    w.stop()
    w.join(timeout=2.0)


def test_snapshot_includes_description():
    """The author-written description (what the worker does + what a
    tick means) must round-trip into the snapshot so the dashboard's
    system-worker detail can explain a quiet infra thread instead of
    showing a bare tick glyph."""
    def fast(w):
        pass

    desc = "Tails session JSONL and emits usage records. A tick is one sweep."
    w = register_worker(
        "test-desc", "test", fast,
        interval_s=0.01,
        description=desc,
    )
    snap = w.snapshot()
    assert snap["description"] == desc
    # Default stays empty when a registrant omits it (back-compat).
    w2 = register_worker("test-nodesc", "test", fast, interval_s=0.01)
    assert w2.snapshot()["description"] == ""
    for x in (w, w2):
        x.stop()
        x.join(timeout=2.0)
