"""
LoopAgent — schedule- or event-driven action dispatcher.

A loop agent is a `BaseAgent` whose `run()` either:
  * ticks on a fixed interval (`schedule: interval:30s`), or
  * fires on a cron schedule (`schedule: cron:0 9 * * 1-5`) — requires
    the `croniter` soft dep (`pip install 'relaydeck[cron]'`), or
  * reacts to plugin-bus events matching a pattern
    (`schedule: on_event:agent.error`)

On each tick or event, every action in `config.actions` runs through
the shared core automation dispatcher (`relaydeck.automation`) — `bus.emit`,
`agent.message`, `script`, `gh`, `model`, `code`. Sharing the dispatcher is
intentional: one action schema across the codebase means an operator who
wrote a github rule already knows how to write a loop config.

## Lifecycle

  __init__ — validates `schedule` + `actions`; stores cadence.
  run()    — interval mode: sleep_unless_stopped → dispatch → repeat.
             event mode:    subscribe; the bus delivers from another
                            thread; run() just blocks on stop_flag.
  terminate() — flips stop_flag and (event mode) unsubscribes from
             the bus.

## Failure isolation

Action errors are logged + emitted as `loop.action_error` events
but never propagate to the run loop. A poison action shouldn't
silence the agent. The number of consecutive errors is tracked;
after `MAX_CONSECUTIVE_ERRORS` the agent enters the `errored`
status so workspace-health roll-up surfaces the problem.

## Config shape

```yaml
config:
  schedule: interval:30s              # or `cron:0 9 * * 1-5` or `on_event:<pattern>`
  workspace_path: /path/to/workspace  # optional; defaults to active workspace's path
  actions:
    - bus.emit:
        type: heartbeat.tick
        data: {message: alive}
    - agent.message:
        to: oncall
        body: "tick from {{ agent_id }}"
```
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from relaydeck.agents_base import BaseAgent
from relaydeck.automation import (
    ActionContext,
    ActionError,
    dispatch as dispatch_action,
    parse_schedule,
)

logger = logging.getLogger(__name__)


class LoopAgent(BaseAgent):
    """A loop agent ticks on a schedule and dispatches a list of
    actions. See module docstring for config shape.

    Tests can spawn a LoopAgent directly with a fake `stop_flag`,
    drive `_tick()` synchronously, and inspect the resulting `emit`
    history through the DB — `run()` exists mainly to glue the
    scheduler to the dispatcher.
    """

    # Stop after this many back-to-back action errors. Generous
    # enough to absorb a temporary network blip (3 tries × 10s
    # interval = 30s); tight enough that a misconfigured action
    # doesn't fail silently forever.
    MAX_CONSECUTIVE_ERRORS: int = 5
    # Used in tests to override the action dispatcher. Production
    # leaves this at the shared core automation dispatcher.
    _dispatch_action: Callable[[dict[str, Any], ActionContext], dict[str, Any]] = staticmethod(
        dispatch_action
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        schedule_raw = self.config.get("schedule", "")
        self._schedule_kind, self._schedule_value = parse_schedule(schedule_raw)
        actions = self.config.get("actions") or []
        if not isinstance(actions, list):
            raise ValueError("`actions` must be a list of action dicts")
        self._actions: list[dict[str, Any]] = actions
        self._workspace_path = self._resolve_workspace_path()
        self._consecutive_errors = 0
        # For on_event mode the bus handler runs from the bus
        # thread; protect _consecutive_errors with a lock so the
        # error counter doesn't race against an interval tick that
        # might run in parallel during a test.
        self._error_lock = threading.Lock()
        # Bound on registration in run(); kept on self so
        # terminate() can unsubscribe cleanly.
        self._bus_handler: Callable[[Any], None] | None = None

    def _config_home(self) -> Path:
        """Derive the daemon's config home from db_path. The orchestrator
        always builds `db_path` as `<config_home>/runtime/relaydeck.db`, so the
        config root is two levels up — this keeps a non-default config home
        (alternate daemon root / RELAYDECK_CONFIG_HOME / tests) working without
        threading a new constructor arg through every agent type."""
        return Path(self.db_path).resolve().parent.parent

    def _resolve_workspace_path(self) -> Path | None:
        """Honor an explicit `workspace_path` in config; otherwise
        look the workspace up via the workspace registry. None means
        scripts will use cwd as fallback."""
        explicit = self.config.get("workspace_path")
        if explicit:
            return Path(str(explicit))
        if not self.workspace:
            return None
        try:
            from relaydeck.config import load_workspace_registry
            for ws in load_workspace_registry(self._config_home()):
                if ws.name == self.workspace and ws.path:
                    return Path(ws.path)
            return None
        except Exception:
            return None

    # ── Run loop ─────────────────────────────────────────────────

    def run(self) -> None:
        """Either tick on schedule or wait on stop_flag while the bus
        delivers events asynchronously."""
        self.update_status("running")
        self.emit("loop.start", {
            "schedule_kind": self._schedule_kind,
            "schedule_value": self._schedule_value,
            "action_count": len(self._actions),
        })
        try:
            if self._schedule_kind == "interval":
                self._run_interval(float(self._schedule_value))
            elif self._schedule_kind == "on_event":
                self._run_on_event(str(self._schedule_value))
            elif self._schedule_kind == "cron":
                self._run_cron(str(self._schedule_value))
            # No other branches reachable — parse_schedule rejects others.
        finally:
            self._unsubscribe_bus()
            self.emit("loop.stop", {})
            self.update_status("stopped")

    def _run_interval(self, seconds: float) -> None:
        # First tick fires immediately so the operator sees activity
        # without waiting a full cycle for slow intervals.
        while not self.stop_flag.is_set():
            self._tick({"trigger": "interval", "interval_s": seconds})
            if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                self.update_status("errored",
                                   error=f"{self._consecutive_errors} consecutive action errors")
                return
            if self.sleep_unless_stopped(seconds):
                return

    def _run_cron(self, expr: str) -> None:
        """Fire on a cron schedule. Unlike interval mode (which ticks
        immediately), cron WAITS for the next scheduled time — that's the
        whole point of a calendar schedule. Recompute the next fire each
        iteration from `now` so a long-running tick or a daemon clock
        adjustment can't drift the schedule."""
        import time as _t

        from croniter import croniter
        while not self.stop_flag.is_set():
            now = _t.time()
            next_ts = croniter(expr, now).get_next(float)
            delay = max(0.0, float(next_ts) - now)
            if self.sleep_unless_stopped(delay):
                return
            self._tick({"trigger": "cron", "cron": expr})
            if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                self.update_status(
                    "errored",
                    error=f"{self._consecutive_errors} consecutive action errors",
                )
                return

    def _run_on_event(self, pattern: str) -> None:
        bus = self._resolve_bus()
        if bus is None:
            self.emit("loop.error", {
                "phase": "subscribe",
                "reason": "no plugin event bus available",
            })
            self.update_status("errored", error="no plugin event bus")
            return

        def _handler(event: Any) -> None:
            if self.stop_flag.is_set():
                return
            payload = {
                "trigger": "on_event",
                "event_type": getattr(event, "type", "?"),
                "event_data": getattr(event, "data", {}),
                "event_ts": getattr(event, "ts", None),
            }
            self._tick(payload)

        self._bus_handler = _handler
        # Durability is the right default for an automation trigger: with
        # subscribe_durable the bus replays events that arrived while this
        # worker was stopped/restarting (via bus_events + a per-agent
        # cursor), so on_event automations don't silently miss triggers.
        # Falls back to plain subscribe on a db-less bus (tests).
        subscribe_durable = getattr(bus, "subscribe_durable", None)
        if callable(subscribe_durable):
            subscribe_durable(pattern, _handler, key=f"loop:{self.agent_id}:{pattern}")
        else:
            bus.subscribe(pattern, _handler)
        # Block until terminate. The bus delivers events from another
        # thread; the sleep loop only watches for shutdown.
        while not self.stop_flag.is_set():
            if self.sleep_unless_stopped(1.0):
                return
            if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                self.update_status("errored",
                                   error=f"{self._consecutive_errors} consecutive action errors")
                return

    def _resolve_bus(self) -> Any:
        """Locate the plugin event bus. The daemon attaches it to the
        plugin registry at load time; if the test path bypasses the
        registry, the agent can also accept a bus via
        `config['_event_bus']` for direct injection."""
        injected = self.config.get("_event_bus")
        if injected is not None:
            return injected
        try:
            from relaydeck.plugin import get_registry
            reg = get_registry()
            return getattr(reg, "_event_bus", None)
        except Exception:
            return None

    def _unsubscribe_bus(self) -> None:
        if not self._bus_handler:
            return
        bus = self._resolve_bus()
        if bus is None:
            return
        try:
            bus.unsubscribe(self._bus_handler)
        except Exception:
            logger.debug("loop: bus unsubscribe failed for %s",
                         self.agent_id, exc_info=True)
        self._bus_handler = None

    # ── Action dispatch ──────────────────────────────────────────

    def _tick(self, trigger: dict[str, Any]) -> None:
        """Run one pass over the configured actions. Records the
        trigger payload + the per-action result so operators can
        inspect what happened from the events tab, and opens an
        `automation_runs` row so the pass survives daemon restart as
        durable history (not just scrolling events)."""
        self.emit("loop.tick", trigger)
        run = self._start_run(trigger)
        ctx = self._action_context(trigger)
        failed = 0
        for raw in self._actions:
            try:
                result = self._dispatch_action(raw, ctx)
                self.emit("loop.action", {"trigger": trigger, "result": result})
            except ActionError as exc:
                failed += 1
                self.emit("loop.action_error", {
                    "trigger": trigger,
                    "action": raw,
                    "error": str(exc),
                })
            except Exception as exc:
                failed += 1
                logger.exception(
                    "loop %s: unexpected action error", self.agent_id,
                )
                self.emit("loop.action_error", {
                    "trigger": trigger,
                    "action": raw,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        self._finish_run(run, total=len(self._actions), failed=failed)
        with self._error_lock:
            if failed:
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0

    # ── Run history ──────────────────────────────────────────────
    # Recording history must NEVER break the loop: every DB touch is
    # best-effort. A failure to write a run row downgrades to "no
    # history for this tick", it doesn't silence the automation.

    def _start_run(self, trigger: dict[str, Any]) -> Any:
        try:
            import json as _json

            from relaydeck import automation_runs as _runs
            ev = trigger.get("event_type")
            return _runs.start_run(
                self.agent_id,
                automation_type="loop",
                workspace=self.workspace,
                trigger_type=str(trigger.get("trigger") or ""),
                trigger_event_id=str(ev) if ev else None,
                detail=_json.dumps(trigger, default=str)[:2000],
                db_path=self.db_path,
            )
        except Exception:
            logger.debug("loop %s: start_run failed", self.agent_id, exc_info=True)
            return None

    def _finish_run(self, run: Any, *, total: int, failed: int) -> None:
        if run is None:
            return
        try:
            from relaydeck import automation_runs as _runs
            if failed == 0:
                status = _runs.STATUS_SUCCEEDED
            elif failed >= total:
                status = _runs.STATUS_FAILED
            else:
                status = _runs.STATUS_PARTIAL
            _runs.finish_run(
                run.id, status=status, action_count=total,
                error_count=failed, db_path=self.db_path,
            )
        except Exception:
            logger.debug("loop %s: finish_run failed", self.agent_id, exc_info=True)

    def _action_context(self, trigger: dict[str, Any]) -> ActionContext:
        """Build the per-tick ActionContext. send_message is wired
        through the orchestrator (so messages persist + drain into
        the peer's inbox); emit_event goes back through the plugin
        bus so other plugins can observe loop-emitted events."""
        return ActionContext(
            send_message=self._send_message,
            emit_event=self._emit_event,
            gh_binary=self.config.get("gh_binary", "gh"),
            workspace_path=self._workspace_path,
            event=trigger,
            model_complete=self._model_complete,
        )

    def _model_complete(self, prompt: str, *, model: str = "role:fast",
                        max_tokens: int = 256, **kwargs: Any) -> str:
        """Bridge the `model` action to the core model gateway, recording
        each call to `model_invocations` so the Workers lens has real LLM
        visibility (model, latency, tokens, ok/error). Core agents call
        the ungated helper directly — the capability gate is a
        plugin-boundary concern, and a loop agent is a first-class agent
        type, not a plugin."""
        from relaydeck.model_invocations import timed_complete
        return timed_complete(
            self.agent_id, prompt, model=model, max_tokens=max_tokens,
            source="loop", db_path=self.db_path,
            config_home=self._config_home(), **kwargs,
        )

    def _send_message(self, *, to: str, body: str, from_: str | None = None,
                      in_reply_to: str | None = None) -> Any:
        """Bridge to orchestrator.send_message_to. Sender defaults
        to our own agent_id so peer inboxes show the loop as the
        message origin, matching how peer agents see each other."""
        from relaydeck.orchestrator import get_orchestrator
        orch = get_orchestrator()
        msg_id, _ = orch.send_message_to(
            to,
            body,
            from_id=from_ or self.agent_id,
            in_reply_to=in_reply_to,
        )

        class _SendResult:
            ids = (msg_id,)

        return _SendResult()

    def _emit_event(self, type_: str, data: dict[str, Any]) -> None:
        bus = self._resolve_bus()
        if bus is None:
            return
        try:
            from relaydeck.plugin import Event
            bus.emit(Event(type=type_, data=data, source_plugin=f"loop:{self.agent_id}"))
        except Exception:
            logger.exception("loop %s: bus.emit failed", self.agent_id)

    def trigger_now(self) -> None:
        """Operator-initiated immediate tick ("run now"). Runs the exact
        same dispatch + run-recording path as a scheduled tick, tagged
        with `trigger: manual` so the run history shows it was hand-fired.
        Safe to call concurrently with the run loop: actions are
        independent, the error counter is lock-guarded, and each DB touch
        opens its own connection."""
        self._tick({"trigger": "manual"})

    def terminate(self) -> None:
        """Stop the loop and detach from the bus if subscribed."""
        self.stop_flag.set()
        self._unsubscribe_bus()
