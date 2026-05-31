"""
Worker primitive — every long-running background loop in the daemon is
a `Worker`. The registry gives the dashboard + CLI a single place to
look up "what's running, what's its last tick, what did it log."

Workers are NOT agents. Agents wrap user-facing harness CLIs; workers
are infrastructure (file pollers, log tailers, gateway listeners,
catalog refreshers). They run in daemon threads and are owned by the
plugin that registered them — when the plugin unloads, its workers stop.

Plugins use `register_worker(...)` and the runtime takes care of:
  - Tracking status (`idle | running | stopped | errored`)
  - Capturing the last exception so it surfaces in `/api/workers`
  - Recording each tick's timestamp so a stuck worker is easy to spot
  - A ring buffer of recent log lines (`worker.log("…")`) for triage

This is deliberately tiny; we resist re-inventing Celery. If a worker
needs queues, retries, or scheduling, that's a different abstraction.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Status enum (string-valued so it serializes cleanly) ─────────────


class WorkerStatus:
    IDLE = "idle"            # registered, hasn't started yet
    RUNNING = "running"      # the tick loop is executing
    STOPPED = "stopped"      # stop_event was set and thread exited
    ERRORED = "errored"      # the tick raised and the loop bailed
    CRASH_LOOP = "crash_loop"  # too many restart attempts; supervisor gave up


class RestartPolicy:
    """How the worker should behave on `target` raising.

    `STOP`   — original behavior: one tick fails, worker errors out
               and stays dead until something restarts it manually.
    `RESTART` — supervisor catches the exception, sleeps a short
                backoff, then re-runs `target`. Crash-loop detector
                eventually transitions the worker to CRASH_LOOP if
                restarts keep firing.
    """

    STOP = "stop"
    RESTART = "restart"


# ── Worker ───────────────────────────────────────────────────────────


@dataclass
class Worker:
    """A named background loop. The runtime drives ticks at `interval_s`.

    `target` is called repeatedly. It receives the Worker instance so it
    can `worker.log("…")`, read `worker.config`, or call `worker.stop()`
    early. Any exception is caught — the worker transitions to ERRORED,
    the exception message is preserved in `last_error`, and the thread
    exits. Use try/except inside `target` if you want a worker to keep
    going after a transient failure.
    """

    id: str
    name: str           # human-readable, e.g. "emote.tailer"
    plugin: str         # owning plugin name
    target: Callable[["Worker"], None]
    interval_s: float = 1.0
    config: dict[str, Any] = field(default_factory=dict)
    agent_id: str | None = None  # set when scoped to one agent (e.g. pi tailer)
    # One-line, author-written explanation of what this worker does and
    # what a single tick performs. Surfaced verbatim in the dashboard so
    # an operator can tell at a glance what an infra thread is for (and
    # why it's quiet — most workers only log when they have work to do).
    description: str = ""

    # ── Supervision ──────────────────────────────────────────────
    # `restart_policy=STOP` keeps the pre-supervision behavior:
    # any tick exception → ERRORED + thread exit. Plugins that opt
    # into RESTART get crash-loop detection: more than
    # `crash_loop_threshold` restarts within `crash_loop_window_s`
    # transitions the worker to CRASH_LOOP and stops the supervisor.
    restart_policy: str = "stop"  # workers.RestartPolicy.STOP
    crash_loop_threshold: int = 5
    crash_loop_window_s: float = 60.0
    restart_backoff_s: float = 1.0

    # ── Runtime state (set by the runtime, read by the API) ──────
    status: str = WorkerStatus.IDLE
    started_at: float = 0.0
    last_tick_at: float = 0.0
    tick_count: int = 0
    last_error: str | None = None
    restart_count: int = 0
    # Sliding window of recent restart timestamps. The supervisor
    # prunes timestamps older than `crash_loop_window_s` before
    # checking length. Bounded inherently since we prune.
    _restart_ts: list[float] = field(default_factory=list, repr=False)

    # ── Internals ────────────────────────────────────────────────
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _log_buffer: deque = field(default_factory=lambda: deque(maxlen=500), repr=False)
    _log_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── Public API used by plugins from inside `target` ──────────

    def log(self, line: str, level: str = "info") -> None:
        """Append a line to this worker's ring buffer. Visible in
        /api/workers/<id>/logs and `relaydeck workers logs <id>`."""
        ts = time.time()
        with self._log_lock:
            self._log_buffer.append({"ts": ts, "level": level, "msg": str(line)})

    def stop(self) -> None:
        """Ask the worker to exit. Idempotent. `target` should poll
        `worker.should_stop()` between iterations of any inner loop."""
        self._stop_event.set()

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    # ── State for the API ────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "plugin": self.plugin,
            "description": self.description,
            "agent_id": self.agent_id,
            "status": self.status,
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "tick_count": self.tick_count,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "restart_policy": self.restart_policy,
            "interval_s": self.interval_s,
            "config": dict(self.config),
            "alive": bool(self._thread and self._thread.is_alive()),
        }

    def recent_logs(self, tail: int = 200) -> list[dict[str, Any]]:
        with self._log_lock:
            buf = list(self._log_buffer)
        return buf[-tail:]


# ── Registry ─────────────────────────────────────────────────────────


class WorkerRegistry:
    """Singleton store of all live workers in the daemon."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._lock = threading.Lock()

    def all(self) -> list[Worker]:
        with self._lock:
            return list(self._workers.values())

    def by_plugin(self, plugin: str) -> list[Worker]:
        with self._lock:
            return [w for w in self._workers.values() if w.plugin == plugin]

    def get(self, worker_id: str) -> Worker | None:
        with self._lock:
            return self._workers.get(worker_id)

    def register(self, w: Worker) -> None:
        with self._lock:
            self._workers[w.id] = w

    def unregister(self, worker_id: str) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)

    def pop_agent_workers(self, name: str, agent_id: str) -> list[Worker]:
        """Remove + return the NON-terminal workers matching (name, agent_id)
        so register_worker can dedup a per-agent worker (e.g. a usage tailer)
        across restarts. Terminal rows (ERRORED / CRASH_LOOP) are LEFT IN
        PLACE so a past failure stays visible for the operator — registering
        a fresh tailer must never silently clear it."""
        terminal = (WorkerStatus.ERRORED, WorkerStatus.CRASH_LOOP)
        with self._lock:
            matches = [w for w in self._workers.values()
                       if w.name == name and w.agent_id == agent_id
                       and w.status not in terminal]
            for w in matches:
                self._workers.pop(w.id, None)
        return matches

    def pop_plugin_workers(self, name: str, plugin: str) -> list[Worker]:
        """Remove + return NON-terminal daemon-wide workers (no agent_id)
        matching (name, plugin). Plugin disable/re-enable and
        register_worker re-spawns must not pile STOPPED duplicates."""
        terminal = (WorkerStatus.ERRORED, WorkerStatus.CRASH_LOOP)
        with self._lock:
            matches = [
                w for w in self._workers.values()
                if w.name == name and w.plugin == plugin and w.agent_id is None
                and w.status not in terminal
            ]
            for w in matches:
                self._workers.pop(w.id, None)
        return matches


_registry = WorkerRegistry()


def get_worker_registry() -> WorkerRegistry:
    return _registry


def _stop_replaced_workers(workers: list[Worker]) -> None:
    """Stop superseded workers and wait for supervisor threads to exit
    before starting a replacement — avoids brief overlap (e.g. two
    concurrent skills.scan ticks writing SQLite)."""
    for old in workers:
        old.stop()
        old.join(timeout=2.0)


# ── Public helper used by plugins ────────────────────────────────────


def register_worker(
    name: str,
    plugin: str,
    target: Callable[[Worker], None],
    *,
    interval_s: float = 1.0,
    config: dict[str, Any] | None = None,
    agent_id: str | None = None,
    description: str = "",
    restart_policy: str = RestartPolicy.STOP,
    crash_loop_threshold: int = 5,
    crash_loop_window_s: float = 60.0,
    restart_backoff_s: float = 1.0,
) -> Worker:
    """Create, register, and start a Worker in one call.

    `target(worker)` is invoked in a loop with `interval_s` between
    iterations. Set `interval_s=0` for a target that drives its own
    cadence (it'll be called in a tight loop — usually you want a
    `while not worker.should_stop():` inside).

    ## Supervision

    `restart_policy=RestartPolicy.STOP` keeps the original behavior:
    a tick exception transitions the worker to ERRORED and the
    supervisor exits. Manual intervention required to restart.

    `restart_policy=RestartPolicy.RESTART` makes the supervisor catch
    the exception, sleep `restart_backoff_s`, and re-invoke `target`.
    If the worker restarts more than `crash_loop_threshold` times in
    `crash_loop_window_s` seconds, it transitions to CRASH_LOOP and
    the supervisor gives up — surfaces in `relaydeck workers list` so
    operators can investigate root cause instead of an infinite
    restart spiral.
    """
    w = Worker(
        id=str(uuid.uuid4()),
        name=name,
        plugin=plugin,
        target=target,
        interval_s=interval_s,
        config=config or {},
        agent_id=agent_id,
        description=description,
        restart_policy=restart_policy,
        crash_loop_threshold=crash_loop_threshold,
        crash_loop_window_s=crash_loop_window_s,
        restart_backoff_s=restart_backoff_s,
    )
    # Per-agent workers (usage tailers) are one-per-agent: a restart
    # must not leak the prior one. Stop + drop any existing NON-terminal
    # match (ERRORED/CRASH_LOOP rows stay surfaced for the operator).
    if agent_id:
        replaced = _registry.pop_agent_workers(name, agent_id)
    else:
        replaced = _registry.pop_plugin_workers(name, plugin)
    _stop_replaced_workers(replaced)
    _registry.register(w)
    _start_worker(w)
    return w


def retry_worker(worker_id: str) -> bool:
    """Re-arm a worker in CRASH_LOOP / ERRORED state.

    The supervisor refuses to keep restarting a worker that hit the
    crash-loop threshold — operators have to manually re-enable it
    once they've fixed root cause (paged a downstream, restarted a
    dep, restored a missing file, whatever). Resets the restart
    counter window and re-starts the runner thread.

    Returns True if the worker existed and was re-armed; False
    otherwise (unknown id, or a worker that's still actively running).
    """
    w = _registry.get(worker_id)
    if w is None:
        return False
    if w.status in (WorkerStatus.RUNNING, WorkerStatus.IDLE):
        return False
    w.status = WorkerStatus.IDLE
    w.last_error = None
    w._restart_ts.clear()
    w.restart_count = 0
    w._stop_event.clear()
    _start_worker(w)
    return True


def _emit_crash_loop_event(w: Worker) -> None:
    """Best-effort notification to the plugin bus when a worker hits
    the crash-loop threshold. Useful for observability + alerting
    plugins to react without polling the workers API."""
    try:
        from relaydeck.plugin import Event, get_registry
        reg = get_registry()
        bus = getattr(reg, "event_bus", None)
        if bus is None:
            return
        bus.emit(Event(
            type="worker.crash_loop",
            data={
                "worker_id": w.id,
                "name": w.name,
                "plugin": w.plugin,
                "restart_count": w.restart_count,
                "last_error": w.last_error,
            },
            source_plugin="relaydeck.workers",
        ))
    except Exception:
        # Best-effort — the supervisor's job is to STOP the worker,
        # not to guarantee a bus emission.
        pass


def _start_worker(w: Worker) -> None:
    """Start (or re-start) the supervisor thread for `w`.

    The supervisor:
      1. Calls target() in a loop.
      2. On exception, records the error.
      3. If restart_policy=STOP, transitions to ERRORED and exits.
      4. If restart_policy=RESTART:
         a. Logs the failure.
         b. Sleeps restart_backoff_s.
         c. Prunes the sliding window of restart timestamps.
         d. If window length >= crash_loop_threshold → CRASH_LOOP,
            emit worker.crash_loop, exit.
         e. Otherwise increments restart_count, appends a fresh
            timestamp, and continues.
    """
    def _runner() -> None:
        w.status = WorkerStatus.RUNNING
        w.started_at = time.time()
        try:
            while not w.should_stop():
                try:
                    w.target(w)
                    w.last_tick_at = time.time()
                    w.tick_count += 1
                except Exception as exc:
                    w.last_error = f"{type(exc).__name__}: {exc}"
                    w.log(f"tick failed: {w.last_error}", level="error")
                    logger.exception("worker %s tick failed", w.name)
                    if w.restart_policy != RestartPolicy.RESTART:
                        w.status = WorkerStatus.ERRORED
                        return
                    # Crash-loop detection: prune old restart timestamps,
                    # then check the window count.
                    now = time.time()
                    w._restart_ts.append(now)
                    cutoff = now - w.crash_loop_window_s
                    w._restart_ts[:] = [t for t in w._restart_ts if t >= cutoff]
                    w.restart_count += 1
                    if len(w._restart_ts) >= w.crash_loop_threshold:
                        w.status = WorkerStatus.CRASH_LOOP
                        w.log(
                            f"crash loop: {len(w._restart_ts)} restarts in "
                            f"{w.crash_loop_window_s:.0f}s — supervisor giving up",
                            level="error",
                        )
                        logger.error(
                            "worker %s in crash loop after %d restarts",
                            w.name, len(w._restart_ts),
                        )
                        _emit_crash_loop_event(w)
                        try:
                            from relaydeck.metrics import record_worker_crash_loop
                            record_worker_crash_loop(w.plugin)
                        except Exception:
                            pass
                        return
                    try:
                        from relaydeck.metrics import record_worker_restart
                        record_worker_restart(w.plugin)
                    except Exception:
                        pass
                    w.log(
                        f"restarting after {w.restart_backoff_s:.1f}s "
                        f"({len(w._restart_ts)}/{w.crash_loop_threshold} in window)",
                        level="warn",
                    )
                    # Sleep through the backoff but bail early on stop.
                    if w._stop_event.wait(w.restart_backoff_s):
                        break
                    continue
                if w.interval_s > 0:
                    if w._stop_event.wait(w.interval_s):
                        break
        finally:
            if w.status not in (WorkerStatus.ERRORED, WorkerStatus.CRASH_LOOP):
                w.status = WorkerStatus.STOPPED
                # A per-agent worker is recreated on the next agent start;
                # don't leave a stale STOPPED row piling up in the registry
                # (ERRORED/CRASH_LOOP stay — they're surfaced for the operator).
                if w.agent_id:
                    _registry.unregister(w.id)

    t = threading.Thread(target=_runner, name=f"relaydeck-worker-{w.name}", daemon=True)
    w._thread = t
    t.start()
