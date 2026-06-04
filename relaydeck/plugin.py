"""
Plugin system — the backbone of relaydeck.

Every capability in relaydeck (harness, tool, cognitive, metering, teleport, etc.)
is a plugin implementing `RelaydeckPlugin`. Plugins are discovered from:
  1. Built-in: `relaydeck/plugins/<category>/` (shipped with relaydeck)
  2. User-installed: `~/.relaydeck/plugins/<category>/`
  3. Workspace-scoped: `<workspace>/plugins/<category>/`

A plugin's `category` determines what it provides (harness, tool, etc.).
The registry loads all plugins at startup and routes lifecycle hooks.

## Event system

Plugins communicate through a typed event bus. Any plugin can:
  - Emit events via `ctx.event_bus.emit(event)`
  - Subscribe to events by overriding `on_event(event)` or `get_subscriptions()`

Event categories:
  - **agent.*** — agent lifecycle (start, stop, error, custom emits)
  - **workspace.*** — file changes, config changes, workspace lifecycle
  - **system.*** — daemon startup/shutdown, plugin load/unload, config changes
  - **gateway.*** — external triggers (webhooks, message queues, teleport)

Plugins declare interest either by overriding `on_event(event)` for all events,
or by returning `EventSubscription` objects from `get_subscriptions()` for
filtered delivery to specific handler methods.
"""

from __future__ import annotations

import enum
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
from fastapi import FastAPI

logger = logging.getLogger(__name__)


# ── Event types ──────────────────────────────────────────────────────


class EventCategory(str, enum.Enum):
    """Top-level event categories. Plugins subscribe to patterns within these."""
    AGENT = "agent"
    WORKSPACE = "workspace"
    SYSTEM = "system"
    GATEWAY = "gateway"


# Well-known event type constants for discoverability and type-safety.
# Plugins can also emit/listen for custom dotted names within any category.
class EventType:
    # ── Agent lifecycle ─────────────────────────────────────────
    AGENT_START = "agent.start"
    AGENT_STOP = "agent.stop"
    AGENT_ERROR = "agent.error"
    AGENT_EMIT = "agent.emit"
    AGENT_MESSAGE = "agent.message"

    # ── Workspace events ────────────────────────────────────────
    WORKSPACE_FILE_CHANGED = "workspace.file.changed"
    WORKSPACE_FILE_CREATED = "workspace.file.created"
    WORKSPACE_FILE_DELETED = "workspace.file.deleted"
    WORKSPACE_CONFIG_CHANGED = "workspace.config.changed"
    WORKSPACE_ADDED = "workspace.added"
    WORKSPACE_REMOVED = "workspace.removed"

    # ── System events ───────────────────────────────────────────
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_PLUGIN_LOADED = "system.plugin.loaded"
    SYSTEM_PLUGIN_UNLOADED = "system.plugin.unloaded"
    SYSTEM_CONFIG_CHANGED = "system.config.changed"

    # ── Gateway / external events ───────────────────────────────
    GATEWAY_WEBHOOK = "gateway.webhook"
    GATEWAY_MESSAGE = "gateway.message"
    GATEWAY_TELEPORT = "gateway.teleport"


# ── Event model ──────────────────────────────────────────────────────


@dataclass
class Event:
    """A structured event on the plugin event bus.

    Events are immutable once created. The `type` field is a dotted
    string like "workspace.file.changed". Plugins subscribe with
    glob-style patterns: "workspace.file.*", "agent.*", "*", etc.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    source_plugin: str = ""  # plugin name that emitted this event


@dataclass
class EventSubscription:
    """A plugin's subscription to a pattern of event types.

    pattern: glob-style match against event type (e.g. "workspace.file.*")
    handler: method name on the plugin instance to call with the Event
    durable: if True, the bus persists every emit and replays unacked
        events to this handler on (re-)registration. Auto-advances the
        cursor only when the handler returns without raising. Default
        False keeps existing subscribers fire-and-forget (the previous
        behavior).
    key: stable identifier for the cursor row. Defaults to
        `<plugin>.<handler>` at wire time. Set explicitly when the same
        physical handler is registered under multiple names and you
        want each to keep its own progress.
    """

    pattern: str
    handler: str
    durable: bool = False
    key: str | None = None


# ── Event bus ────────────────────────────────────────────────────────


def _handler_name(handler: Callable[[Event], None]) -> str:
    """Best-effort human-readable identifier for an event handler.

    Bound methods produce 'ClassName.method', free functions produce
    'module.function'. Used in bus stats so operators can identify a
    slow subscriber without grepping for id() values."""
    try:
        if hasattr(handler, "__self__") and hasattr(handler, "__func__"):
            cls = type(handler.__self__).__name__
            fn = handler.__func__.__name__
            return f"{cls}.{fn}"
        mod = getattr(handler, "__module__", "?")
        fn = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "?")
        return f"{mod}.{fn}"
    except Exception:
        return repr(handler)


class PluginEventBus:
    """Central event bus for plugin-to-plugin communication.

    Plugins emit events and subscribe via patterns. The bus routes
    events to matching subscribers synchronously (in the emitting
    thread). For async use, plugins should queue or spawn threads.

    This is separate from the orchestrator's agent-focused EventBus —
    that one is for SSE streaming to the dashboard. This one is for
    plugin coordination.

    ## Backpressure

    Sync delivery means a slow subscriber stalls every other
    subscriber on the same event. We can't change that without
    breaking plugin semantics, but we CAN make the slow path
    visible: every handler call is timed; subscribers whose mean
    latency exceeds `SLOW_HANDLER_MS` get flagged. The bus emits
    `bus.slow_subscriber` events (rate-limited to once per minute
    per subscriber) so observability tooling surfaces the offender
    before operators have to guess.

    ## Durability

    When constructed with a `db_path`, every `emit()` persists the
    event to `bus_events` *before* dispatching to subscribers, so a
    crash between persist and dispatch is replayable on next startup.
    Subscribers opt into replay by calling `subscribe_durable(...)`
    with a stable key — the bus records the last-acked id per key in
    `bus_cursors` and replays unacked rows on (re-)registration.
    Without `db_path` the bus is in-memory-only (preserves the old
    fire-and-forget behavior for tests and the MockHost).
    """

    # Mean handler latency above this triggers a slow-subscriber
    # event. 100ms is generous enough that DB writes don't trigger
    # and tight enough that a stuck network call does.
    SLOW_HANDLER_MS: float = 100.0
    # Minimum samples before a subscriber is eligible for the slow
    # flag — avoids flagging the first-event JIT warm-up.
    SLOW_MIN_SAMPLES: int = 5
    # Rate-limit slow-subscriber emissions per handler.
    SLOW_EVENT_INTERVAL_S: float = 60.0
    # Page size for durable-subscriber replay. Keeps memory bounded
    # when a subscriber has been offline for a long time.
    REPLAY_PAGE_SIZE: int = 500

    def __init__(self, *, db_path: str | Path | None = None):
        self._subscriptions: list[tuple[str, Callable[[Event], None]]] = []
        self._history: list[Event] = []  # last 1000 events for replay
        self._max_history = 1000
        # Per-subscriber delivery stats keyed by id(handler) since
        # handlers can be bound methods that don't compare cleanly.
        # Held in a plain dict because all access happens during
        # emit, which is already single-threaded by Python's GIL for
        # the dict ops; only the slow-event emission needs the lock.
        self._handler_stats: dict[int, dict[str, Any]] = {}
        self._stats_lock = threading.Lock()
        # Durability: when db_path is set, emit() persists to bus_events
        # before dispatch and durable handlers carry a per-handler key
        # whose last-acked id lives in bus_cursors. _emit_lock serializes
        # emit + subscribe_durable so durable subscribers see events in
        # strict id order and cursors never regress.
        self._db_path: str | None = str(db_path) if db_path else None
        self._durable_meta: dict[int, dict[str, str]] = {}
        # ids of durable handlers that have failed a live matching event
        # this process. Their cursor is frozen so a later success can't
        # advance past the failed id (which would skip it on restart
        # replay — breaking at-least-once). Cleared naturally on restart
        # (a fresh bus has an empty set + re-replays from the cursor).
        self._durable_failed: set[int] = set()
        # RLock, not Lock: handlers commonly re-enter the bus (a
        # subscriber to harness.assistant_message re-emits emote.update;
        # set_semantic_status emits agent.status_changed from inside an
        # agent.message handler). A non-reentrant lock would deadlock
        # the daemon on that pattern.
        self._emit_lock = threading.RLock()

    def subscribe(self, pattern: str, handler: Callable[[Event], None]) -> None:
        """Subscribe to events matching a glob-style pattern.

        pattern examples: "*", "agent.*", "workspace.file.*", "agent.start"
        """
        self._subscriptions.append((pattern, handler))
        # Seed stats so the first call has a record to update.
        with self._stats_lock:
            self._handler_stats.setdefault(id(handler), {
                "pattern": pattern,
                "handler_name": _handler_name(handler),
                "count": 0,
                "total_s": 0.0,
                "slow_count": 0,
                "errors": 0,
                "last_slow_emit_ts": 0.0,
            })

    def unsubscribe(self, handler: Callable[[Event], None]) -> None:
        """Remove all subscriptions for a handler."""
        self._subscriptions = [
            (p, h) for p, h in self._subscriptions if h is not handler
        ]
        with self._stats_lock:
            self._handler_stats.pop(id(handler), None)
        self._durable_meta.pop(id(handler), None)
        self._durable_failed.discard(id(handler))

    def subscribe_durable(
        self,
        pattern: str,
        handler: Callable[[Event], None],
        *,
        key: str,
    ) -> None:
        """Subscribe with at-least-once replay across daemon restarts.

        On registration the bus reads `bus_cursors[key]` and replays
        every persisted event with `id > cursor` that matches the
        pattern to the handler. The cursor advances after each
        successful call; an exception halts the cursor at the
        last-acked id so the same event retries on the next start.

        Requires the bus to have been constructed with `db_path`. Used
        in-memory-only (`db_path=None`), this falls back to plain
        `subscribe()` so tests that don't care about durability stay
        light. Operators who want durability without realizing they
        need it would otherwise get silent data loss — flagging the
        downgrade in the log is the honest move.
        """
        if not self._db_path:
            logger.debug(
                "subscribe_durable(%s) downgraded to in-memory: no db_path",
                key,
            )
            self.subscribe(pattern, handler)
            return

        with self._emit_lock:
            self._subscriptions.append((pattern, handler))
            with self._stats_lock:
                self._handler_stats.setdefault(id(handler), {
                    "pattern": pattern,
                    "handler_name": _handler_name(handler),
                    "count": 0,
                    "total_s": 0.0,
                    "slow_count": 0,
                    "errors": 0,
                    "last_slow_emit_ts": 0.0,
                })
            self._durable_meta[id(handler)] = {"key": key, "pattern": pattern}
            self._replay_durable(handler, pattern, key)

    def _replay_durable(
        self,
        handler: Callable[[Event], None],
        pattern: str,
        key: str,
    ) -> None:
        """Replay unacked events to a freshly-registered durable
        subscriber. Pages through `bus_events` so a long-offline
        subscriber doesn't materialize the world into memory; halts
        the cursor at the first handler exception so the same event
        retries on the next start."""
        from relaydeck.db import (
            load_bus_cursor,
            open_db,
            read_bus_events_after,
            save_bus_cursor,
        )

        conn = open_db(self._db_path or "")
        try:
            cursor = load_bus_cursor(conn, key)
        finally:
            conn.close()

        while True:
            conn = open_db(self._db_path or "")
            try:
                page = read_bus_events_after(
                    conn, after_id=cursor, limit=self.REPLAY_PAGE_SIZE,
                )
            finally:
                conn.close()
            if not page:
                return
            for row in page:
                if not _match_pattern(pattern, row["type"]):
                    cursor = row["id"]
                    continue
                try:
                    data = json.loads(row["data"]) if row["data"] else {}
                except Exception:
                    data = {}
                ev = Event(
                    type=row["type"],
                    data=data,
                    ts=row["ts"],
                    source_plugin=row["source_plugin"],
                )
                try:
                    handler(ev)
                except Exception:
                    logger.exception(
                        "Durable replay halted for %s at id=%d",
                        key, row["id"],
                    )
                    # Persist progress up to the last successful event
                    # so we don't loop forever on a poisoned message
                    # — but only if we made progress this page.
                    if cursor > 0:
                        conn = open_db(self._db_path or "")
                        try:
                            save_bus_cursor(
                                conn, subscriber_key=key,
                                pattern=pattern, last_acked_id=cursor,
                            )
                        finally:
                            conn.close()
                    return
                cursor = row["id"]
            # Save progress at end of every page so a crash mid-replay
            # doesn't lose all the acknowledged work.
            conn = open_db(self._db_path or "")
            try:
                save_bus_cursor(
                    conn, subscriber_key=key,
                    pattern=pattern, last_acked_id=cursor,
                )
            finally:
                conn.close()
            if len(page) < self.REPLAY_PAGE_SIZE:
                return

    def emit(self, event: Event) -> None:
        """Emit an event to all matching subscribers.

        When the bus has a `db_path`, the event is persisted to
        `bus_events` *before* dispatch — a crash after persist but
        before dispatch is replayable on next startup via the durable
        subscribers' cursors. Without `db_path` this is the same
        in-memory fire-and-forget the bus has always been.
        """
        with self._emit_lock:
            # Persist first so durable subscribers can recover from a
            # crash between persist and dispatch.
            persisted_id = self._persist(event)

            # Store in history (in-memory ring buffer)
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            # Metrics: type-label increment per emit (covers all events,
            # including ones with zero subscribers, which is itself useful
            # for spotting "is anyone consuming X?").
            try:
                from relaydeck.metrics import record_bus_event
                record_bus_event(event.type)
            except Exception:
                pass

            # Deliver to matching subscribers, timing each handler so
            # slow subscribers surface in stats + the bus.slow_subscriber
            # event.
            for pattern, handler in self._subscriptions:
                if not _match_pattern(pattern, event.type):
                    continue
                t0 = time.perf_counter()
                errored = False
                try:
                    handler(event)
                except Exception:
                    errored = True
                    logger.exception(
                        "Error delivering event %s to handler", event.type
                    )
                    try:
                        from relaydeck.metrics import record_bus_error
                        record_bus_error(_handler_name(handler))
                    except Exception:
                        pass
                self._record_delivery(handler, time.perf_counter() - t0,
                                      errored=errored, source_event=event)
                if id(handler) in self._durable_meta:
                    if errored:
                        # Poison the cursor: once a matching live event
                        # fails for this durable subscriber, stop
                        # advancing. Advancing past the failed id on a
                        # *later* success would skip the failed event on
                        # restart-replay (replay reads `id > cursor`),
                        # breaking at-least-once. The cursor stays frozen
                        # so replay re-delivers from the failed id.
                        self._durable_failed.add(id(handler))
                    elif persisted_id and id(handler) not in self._durable_failed:
                        self._advance_cursor(handler, persisted_id)

    def _persist(self, event: Event) -> int:
        """Write the event to bus_events. Returns the inserted id or
        0 when persistence is disabled or the write failed. A failed
        write logs a warning and continues — losing durability beats
        dropping the in-memory dispatch."""
        if not self._db_path:
            return 0
        try:
            from relaydeck.db import append_bus_event, open_db
            data_json = json.dumps(event.data, default=str) if event.data else None
        except Exception:
            return 0
        try:
            conn = open_db(self._db_path)
            try:
                return append_bus_event(
                    conn,
                    ts=event.ts,
                    type=event.type,
                    data=data_json,
                    source_plugin=event.source_plugin,
                )
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "bus: persist failed for %s; dispatch will continue in-memory",
                event.type, exc_info=True,
            )
            return 0

    def _advance_cursor(
        self,
        handler: Callable[[Event], None],
        last_acked_id: int,
    ) -> None:
        """Update the durable-subscriber cursor for a handler that
        just successfully processed an event. No-op for non-durable
        handlers."""
        meta = self._durable_meta.get(id(handler))
        if not meta or not self._db_path:
            return
        # Belt-and-suspenders: never advance a poisoned subscriber's
        # cursor (the emit loop already gates on this).
        if id(handler) in self._durable_failed:
            return
        try:
            from relaydeck.db import open_db, save_bus_cursor
            conn = open_db(self._db_path)
            try:
                save_bus_cursor(
                    conn,
                    subscriber_key=meta["key"],
                    pattern=meta["pattern"],
                    last_acked_id=last_acked_id,
                )
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "bus: cursor save failed for %s",
                meta.get("key"), exc_info=True,
            )

    def _record_delivery(
        self,
        handler: Callable[[Event], None],
        dt_seconds: float,
        *,
        errored: bool,
        source_event: Event,
    ) -> None:
        """Update per-handler stats and emit a `bus.slow_subscriber`
        notification when a subscriber's running mean exceeds
        SLOW_HANDLER_MS (rate-limited per handler)."""
        hid = id(handler)
        with self._stats_lock:
            s = self._handler_stats.get(hid)
            if s is None:
                # Shouldn't happen — subscribe seeds — but be defensive.
                return
            s["count"] += 1
            s["total_s"] += dt_seconds
            if errored:
                s["errors"] += 1
            mean_ms = (s["total_s"] / s["count"]) * 1000.0
            is_slow = (
                s["count"] >= self.SLOW_MIN_SAMPLES
                and mean_ms >= self.SLOW_HANDLER_MS
            )
            if is_slow:
                s["slow_count"] += 1
                now = time.time()
                if now - s["last_slow_emit_ts"] >= self.SLOW_EVENT_INTERVAL_S:
                    s["last_slow_emit_ts"] = now
                    payload = {
                        "handler": s["handler_name"],
                        "pattern": s["pattern"],
                        "mean_ms": mean_ms,
                        "count": s["count"],
                        "errors": s["errors"],
                        "source_event_type": source_event.type,
                    }
                    # Avoid recursion: don't go back through emit()
                    # (which would re-time and re-trip). Log + walk
                    # subscribers manually for `bus.slow_subscriber`.
                    logger.warning(
                        "bus: slow subscriber %s (mean %.1f ms over %d events) on %s",
                        s["handler_name"], mean_ms, s["count"], s["pattern"],
                    )
                    self._emit_bypass("bus.slow_subscriber", payload)

    def _emit_bypass(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit without re-entering the timing path. Used for
        bus-internal events (slow_subscriber, dropped) that must not
        be themselves timed or they'd feedback-loop."""
        ev = Event(type=event_type, data=data, source_plugin="relaydeck.bus")
        self._history.append(ev)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        for pattern, handler in self._subscriptions:
            if _match_pattern(pattern, event_type):
                try:
                    handler(ev)
                except Exception:
                    logger.exception(
                        "Error delivering internal event %s", event_type
                    )

    def subscriber_stats(self) -> list[dict[str, Any]]:
        """Snapshot of every subscriber's delivery stats. Sorted by
        mean latency descending — slowest at the top, easiest for
        operators to scan."""
        with self._stats_lock:
            rows = []
            for s in self._handler_stats.values():
                count = s["count"]
                mean_ms = (s["total_s"] / count * 1000.0) if count else 0.0
                rows.append({
                    "handler": s["handler_name"],
                    "pattern": s["pattern"],
                    "count": count,
                    "errors": s["errors"],
                    "mean_ms": mean_ms,
                    "slow_count": s["slow_count"],
                })
        rows.sort(key=lambda r: r["mean_ms"], reverse=True)
        return rows

    def replay(self, pattern: str = "*") -> list[Event]:
        """Return historical events matching a pattern."""
        return [e for e in self._history if _match_pattern(pattern, e.type)]


def _match_pattern(pattern: str, event_type: str) -> bool:
    r"""Match an event type against a glob-style pattern.

    Converts glob pattern to regex: * → .*, . → \.
    """
    if pattern == "*":
        return True
    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return bool(re.match(regex, event_type))


# ── Plugin base ──────────────────────────────────────────────────────


class RelaydeckPlugin:
    """Every relaydeck capability is a plugin.

    Subclass this for any pluggable component. The category determines
    which registry bucket it lands in and what hooks are available.

    ## Event handling

    Plugins receive events in two ways:
      1. Override `on_event(event: Event)` — called for every event
         matching any pattern returned by `get_subscriptions()`.
      2. Return `EventSubscription` objects from `get_subscriptions()`
         to route specific patterns to named handler methods.

    Example:
        def get_subscriptions(self) -> list[EventSubscription]:
            return [
                EventSubscription("workspace.file.*", "_on_file_change"),
                EventSubscription("agent.start", "_on_agent_start"),
            ]

        def _on_file_change(self, event: Event): ...
        def _on_agent_start(self, event: Event): ...
    """

    # Set by the subclass. Must be unique per category.
    name: str = ""
    version: str = "0.1.0"
    category: str = ""  # "harness", "tool", "cognitive", "infrastructure"

    # Optional description for introspection / help text.
    description: str = ""

    # Dependencies: other plugin names that must load first.
    requires: list[str] = []

    # When True, this plugin's name is a valid entry in a workspace's
    # `plugins = [...]` list — i.e. the plugin gates some of its
    # behavior on per-workspace opt-in (skill materialization, prompt
    # injection, harness arg, ...). The dashboard's Workspaces tab
    # uses this flag to filter which plugins get checkboxes; everything
    # else (providers, harnesses, system infrastructure) is always-on.
    workspace_scoped: bool = False

    # ── Lifecycle hooks ──────────────────────────────────────────

    def on_load(self, ctx: PluginContext) -> None:
        """Called when the plugin is loaded. ctx provides access to the
        orchestrator, config, and other services. Use for one-time setup."""
        pass

    def on_unload(self) -> None:
        """Called when the plugin is unloaded. Clean up resources."""
        pass

    def on_settings_changed(self, new_values: dict[str, Any]) -> None:
        """Called after the user updates this plugin's settings via the
        dashboard or `relaydeck plugin settings set`. Default no-op.

        Override to react synchronously — e.g., re-materialize a file
        derived from a setting, refresh a cached object. The settings
        store is already updated when this fires; `new_values` is the
        post-validation dict.

        Distinct from `on_load`: this can fire many times during a
        single process lifetime.
        """
        pass

    # ── Event handling ──────────────────────────────────────────

    def get_subscriptions(self) -> list[EventSubscription]:
        """Return event subscriptions for this plugin.

        Each subscription maps a glob pattern to a handler method name.
        The handler receives the Event object.

        Override this or `on_event()` — you don't need both.
        """
        return []

    def on_event(self, event: Event) -> None:
        """Called for every event matching a subscription pattern.

        Override this for catch-all handling. If you also override
        `get_subscriptions()`, named handlers take priority — this
        method only receives events that don't match a named handler.
        """
        pass

    # ── Extension points (override as needed) ────────────────────

    def register_cli(self, cli: click.Group) -> None:
        """Add CLI commands to the relaydeck CLI group.

        Example:
            @cli.group()
            def myplugin(): ...

            @myplugin.command()
            def status(): ...
        """
        pass

    def register_api_routes(self, app: FastAPI) -> None:
        """Add HTTP routes to the FastAPI application.

        Example:
            @app.get("/api/myplugin/status")
            async def status(): ...
        """
        pass

    def register_web_components(self) -> dict[str, str]:
        """Return a dict of {slot_name: html_fragment} for the dashboard.
        Slots: "sidebar", "header", "agent-card", "footer".
        Empty dict if no UI contribution.

        Prefer `register_ui()` for richer contributions (tabs, panels,
        header chips with JS modules) — this method is kept for tiny
        static HTML snippets only.
        """
        return {}

    def register_ui(self) -> dict[str, Any]:
        """Return a UI manifest contributing to the dashboard.

        Shape (all keys optional):
            {
              "tabs": [
                {"id": "fleet", "title": "Fleet", "icon": "⊕",
                 "module": "fleet.js",          # served from this plugin's static/
                 "order": 20},
                ...
              ],
              "header_chips": [
                {"id": "tokens", "module": "chip.js"},
                ...
              ],
            }

        If the plugin has a `static/` directory next to its `plugin.py`,
        it is auto-mounted at `/static/plugins/<plugin-name>/` and the
        `module` field is resolved against that mount point.

        Each module must default-export a class with `mount(container, api)`
        and `unmount()` lifecycle methods. `api` exposes:
          - `api.fetch(path, init?)` — same-origin fetch helper
          - `api.subscribe(eventPattern, cb)` — SSE event subscription
          - `api.workspace` — current workspace name
        """
        return {}

    def static_dir(self) -> Path | None:
        """Return a directory of static assets to expose under
        `/static/plugins/<plugin-name>/`. Default: a `static/` folder
        adjacent to the plugin's module file, if it exists."""
        import inspect as _inspect
        try:
            src = _inspect.getsourcefile(self.__class__)
            if not src:
                return None
            d = Path(src).resolve().parent / "static"
            return d if d.is_dir() else None
        except Exception:
            return None

    def register_agent_factory(self) -> dict[str, type]:
        """Return {type_name: AgentClass} for agents this plugin provides.
        Used by harness plugins to register their agent types."""
        return {}

    def get_settings_schema(self) -> list[dict[str, Any]]:
        """Declare configurable settings for this plugin.

        Return a list of field descriptors:
            {
              "key": "preset",
              "type": "preset_ref",   # text|number|bool|enum|preset_ref|secret_ref
              "label": "Sentiment preset",
              "description": "Optional helper text",
              "default": None,
              "options": [...],       # required when type=enum
            }

        Plugins read values with `relaydeck.plugin_settings.get_setting(name, key)`.
        Empty schema (default) = no configurable settings.
        """
        return []


# ── Provider catalog plugins ─────────────────────────────────────────
#
# A `ProviderPlugin` knows the model catalog for a single backend
# (openrouter, anthropic, openai, ollama, …). The catalog is what
# Models-tab health checks and pre-spawn validation query — so adding
# a new provider is just dropping a new plugin under
# `relaydeck_plugins/providers/<name>/`, no core changes.


@dataclass
class ModelEntry:
    """A single model in a provider's catalog."""
    id: str                         # e.g. "anthropic/claude-3-5-haiku"
    display_name: str = ""
    context_length: int | None = None
    prompt_price: float | None = None      # USD per 1M prompt tokens
    completion_price: float | None = None  # USD per 1M completion tokens
    capabilities: list[str] = field(default_factory=list)


class ProviderPlugin(RelaydeckPlugin):
    """Base for provider catalog plugins.

    Subclasses set `provider_name` and override `fetch_catalog()`. The base
    handles on-disk caching at `~/.relaydeck/cache/providers/<name>.json`
    and exposes `list_models()` / `validate(model_id)`.

    Validation does an exact-id check first, then a fuzzy match (token
    overlap + substring) to produce a suggestion when the user mistyped
    or used a stale id like "claude-haiku-3.5" instead of "claude-3-5-haiku".
    """

    category = "provider"
    provider_name: str = ""
    cache_ttl_seconds: int = 24 * 3600
    # Provider config surface (Providers settings section). `key_env` is
    # the vault/env name for the API key; `default_base_url` is the
    # built-in endpoint an operator can override via provider_config.
    key_env: str = ""
    default_base_url: str | None = None

    def __init__(self) -> None:
        self._catalog: list[ModelEntry] = []
        self._last_refresh_ts: float = 0.0
        self._cache_loaded = False

    # ── Resolved config (vault key + base-url override) ──────────────

    def resolved_api_key(self) -> str | None:
        """API key from the vault first, then the env var. None if
        neither is set (providers then run anonymous / fail open)."""
        if not self.key_env:
            return None
        try:
            from relaydeck.vault import get_secret

            v = get_secret(self.key_env)
            if v:
                return v
        except Exception:
            pass
        import os
        return os.environ.get(self.key_env)

    def resolved_base_url(self, config_home: Path | None = None) -> str | None:
        """Operator base-URL override (provider_config) → built-in default.

        `config_home` is threaded from the API so reads hit the same
        daemon root the override was written to (alternate roots /
        RELAYDECK_CONFIG_HOME / tests); None falls back to the default home."""
        try:
            from relaydeck.provider_config import get_base_url
            override = get_base_url(self.provider_name or self.name, config_home)
            if override:
                return override
        except Exception:
            pass
        return self.default_base_url

    def has_api_key(self) -> bool:
        return bool(self.resolved_api_key())

    # ── Subclass override hooks ──────────────────────────────────

    def fetch_catalog(self) -> list[ModelEntry]:
        """Fetch from upstream. Override in subclasses. Default: empty."""
        return []

    # ── Public API ───────────────────────────────────────────────

    def list_models(self, refresh: bool = False) -> list[ModelEntry]:
        if refresh or not self._catalog:
            self._load_or_fetch(force_fetch=refresh)
        return list(self._catalog)

    def validate(self, model_id: str) -> tuple[bool, str | None]:
        """Returns (ok, suggestion). When `ok` is False and a close
        match is found, `suggestion` is the closest model id."""
        if not model_id:
            return False, None
        catalog = self.list_models()
        if not catalog:
            return True, None  # No catalog — fail open (don't block).
        ids = [m.id for m in catalog]
        if model_id in ids:
            return True, None
        # Try suffix match: user often writes the model name without
        # provider prefix ("claude-3-5-haiku" → "anthropic/claude-3-5-haiku").
        for mid in ids:
            if mid.endswith("/" + model_id) or mid == model_id:
                return True, None
        # Fuzzy suggestion.
        suggestion = self._best_match(model_id, ids)
        return False, suggestion

    def refresh(self) -> list[ModelEntry]:
        """Force re-fetch from upstream and re-cache."""
        self._load_or_fetch(force_fetch=True)
        return list(self._catalog)

    def last_refresh_ts(self) -> float:
        return self._last_refresh_ts

    # ── Internals ────────────────────────────────────────────────

    def _cache_path(self) -> Path:
        home = Path.home() / ".relaydeck"
        return home / "cache" / "providers" / f"{self.provider_name or self.name}.json"

    def _load_or_fetch(self, force_fetch: bool = False) -> None:
        import json as _json
        cache_path = self._cache_path()

        # Try disk cache first unless we're forcing a refresh.
        if not force_fetch and cache_path.exists():
            try:
                blob = _json.loads(cache_path.read_text())
                age = time.time() - blob.get("ts", 0)
                if age < self.cache_ttl_seconds:
                    self._catalog = [ModelEntry(**m) for m in blob.get("models", [])]
                    self._last_refresh_ts = blob.get("ts", 0)
                    self._cache_loaded = True
                    return
            except Exception:
                pass

        # Fetch from upstream.
        try:
            fetched = self.fetch_catalog()
        except Exception as exc:
            logger.warning("provider %s fetch failed: %s", self.provider_name, exc)
            # Fall back to stale cache if we have one.
            if cache_path.exists() and not self._cache_loaded:
                try:
                    blob = _json.loads(cache_path.read_text())
                    self._catalog = [ModelEntry(**m) for m in blob.get("models", [])]
                    self._last_refresh_ts = blob.get("ts", 0)
                except Exception:
                    pass
            return

        self._catalog = list(fetched)
        self._last_refresh_ts = time.time()
        # Persist to disk.
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(_json.dumps({
                "ts": self._last_refresh_ts,
                "provider": self.provider_name,
                "models": [m.__dict__ for m in self._catalog],
            }))
        except Exception:
            pass

    @staticmethod
    def _best_match(target: str, candidates: list[str]) -> str | None:
        """Score by shared substring length, prefer the longest match."""
        if not candidates:
            return None
        import difflib
        # difflib gives a good first pass; cap at one suggestion.
        matches = difflib.get_close_matches(target, candidates, n=1, cutoff=0.5)
        if matches:
            return matches[0]
        # Fallback: any candidate whose id contains a major token of target.
        toks = [t for t in target.replace("/", "-").split("-") if len(t) >= 3]
        for c in candidates:
            if all(t in c for t in toks):
                return c
        return None


def get_provider(name: str) -> ProviderPlugin | None:
    """Look up a provider by name. Discovered plugin providers win;
    otherwise fall back to the extra registry (known OpenAI-compatible
    backends + user-defined custom providers)."""
    reg = get_registry()
    for entry in reg.all():
        inst = entry.instance
        if isinstance(inst, ProviderPlugin) and (inst.provider_name == name or entry.name == name):
            return inst
    try:
        from relaydeck.providers_extra import get_extra
        return get_extra(name)
    except Exception:
        return None


def list_providers() -> list[ProviderPlugin]:
    reg = get_registry()
    out = [e.instance for e in reg.all() if isinstance(e.instance, ProviderPlugin)]
    names = {p.provider_name for p in out}
    try:
        from relaydeck.providers_extra import list_extra
        for p in list_extra():
            if p.provider_name not in names:
                out.append(p)
    except Exception:
        pass
    return out


# ── Plugin context ───────────────────────────────────────────────────


@dataclass
class PluginContext:
    """Context passed to plugins on load. Provides access to core services."""

    config_home: Path          # ~/.relaydeck/
    workspace_path: Path | None = None  # active workspace path, if any
    # Registered workspace NAME (registry key), which can differ from the
    # path basename — a workspace registered as `api` may live at a dir
    # named `relaydeck`. Workspace-scoped lifecycle (host.agents.create) must
    # scope by this, not by `workspace_path.name`. None for global loads.
    workspace_name: str | None = None
    orchestrator: Any = None   # Orchestrator instance (lazy, set after load)
    db_path: str = ""
    event_bus: Any = None      # PluginEventBus (lazy, set by registry)

    def resolve_path(self, relative: str) -> Path:
        """Resolve a path relative to the active workspace or config home."""
        if self.workspace_path:
            return self.workspace_path / relative
        return self.config_home / relative

    def emit(self, event_type: str, data: dict[str, Any] | None = None,
             source_plugin: str = "") -> None:
        """Convenience: emit an event on the plugin event bus."""
        if self.event_bus:
            self.event_bus.emit(Event(
                type=event_type, data=data or {},
                source_plugin=source_plugin,
            ))


# ── Registry ─────────────────────────────────────────────────────────


@dataclass
class PluginEntry:
    """A loaded plugin with its metadata and instance."""

    name: str
    category: str
    version: str
    instance: RelaydeckPlugin
    source: str  # "builtin", "user", "workspace"
    path: Path
    manifest: Any = None
    lock_approved: bool = False


def effective_trust_level(entry: PluginEntry) -> str:
    """Trust level for an entry, resolved against install source.

    The manifest's explicit `trust_level` wins (plugin author opted
    in to a specific level). Otherwise:
      - source == "builtin"  -> "bundled" (shipped with the package)
      - source == "user"     -> "local"   (operator installed via
                                           `relaydeck plugin install <path>`)
      - source startswith
        "workspace:"         -> "local"
      - source == "installed", approved, AND named in the curated registry
        -> "curated" (relaydeck-recommended, pinned in plugins.lock)
      - source == "installed" with a matching package lock entry -> "local"
      - source == "installed" otherwise -> "untrusted" (third-party package
        discovered via entry point)

    Ladder (high → low): bundled > curated > local > untrusted.
    """
    if entry.manifest and getattr(entry.manifest, "trust_level", ""):
        return entry.manifest.trust_level
    if entry.source == "installed":
        if entry.lock_approved:
            try:
                from relaydeck.plugin_registry import is_curated
                if is_curated(entry.name):
                    return "curated"
            except Exception:
                pass
            return "local"
        return "untrusted"
    return "bundled" if entry.source == "builtin" else "local"


def _allow_untrusted() -> bool:
    return os.environ.get("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", "").lower() in (
        "1", "true", "yes",
    )


class PluginRegistry:
    """Discovers, loads, and manages all plugins.

    Singleton per daemon instance. Plugins are organized by category
    for fast lookup by the orchestrator and transports.
    """

    def __init__(self, config_home: Path | None = None):
        self.config_home = config_home or Path.home() / ".relaydeck"
        self._entries: dict[str, PluginEntry] = {}  # name → entry (currently loaded)
        self._by_category: dict[str, list[PluginEntry]] = {}
        # All entries the discoverer ever produced this process — even
        # ones we skipped because they're disabled. Lets the UI list +
        # re-enable them without a daemon restart.
        self._discovered: dict[str, PluginEntry] = {}
        # Last PluginContext we loaded with — needed to re-load on enable.
        self._last_ctx: PluginContext | None = None
        self._workspace_entries: dict[tuple[str, str], PluginEntry] = {}
        self._workspace_locks: dict[tuple[str, str], threading.Lock] = {}

    # ── Discovery ────────────────────────────────────────────────

    def discover(self) -> list[PluginEntry]:
        """Scan all plugin directories and return unloaded entries."""
        discovered: list[PluginEntry] = []

        # Official plugins — every relaydeck-managed plugin lives in the root
        # `plugins` package (vault, github, loop, external_agents, harnesses,
        # messaging, telegram, skills, theme, metering, gateway, file_watcher,
        # usage_limits, hitl, dashboard, providers). One discovery path: real
        # package import so each plugin's intra-package relative imports
        # resolve. Core never imports this package statically — the boundary is
        # one-directional (plugins → core facades only).
        discovered.extend(self._scan_package("plugins", "builtin"))

        # Third-party package plugins. Packages expose a `relaydeck.plugins`
        # entry point whose value is the plugin module (usually
        # `pkg.plugin:PLUGIN`). The manifest still lives next to that module
        # and is parsed before the module is imported.
        discovered.extend(self._scan_entry_points("relaydeck.plugins", "installed"))

        # User-installed plugins
        user_root = self.config_home / "plugins"
        if user_root.exists():
            discovered.extend(self._scan_directory(user_root, "user"))

        discovered = self._dedupe_discovered(discovered)
        for entry in discovered:
            self._discovered[entry.name] = entry
        return discovered

    def _dedupe_discovered(self, entries: list[PluginEntry]) -> list[PluginEntry]:
        """Drop exact duplicate discoveries of the same plugin package.

        If a future split advertises official plugins through entry points for
        package metadata parity while core still scans the `plugins` package
        directly, both paths could produce the same plugin from the same
        directory. Keep the first discovery result.
        """
        out: list[PluginEntry] = []
        seen: set[tuple[str, Path]] = set()
        for entry in entries:
            try:
                path = entry.path.resolve()
            except OSError:
                path = entry.path
            key = (entry.name, path)
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
        return out

    def _scan_directory(self, root: Path, source: str) -> list[PluginEntry]:
        """Scan a directory tree for plugin packages, loading by file path.

        A plugin is a Python package with a `plugin.py` module containing
        a `PLUGIN` attribute that is a RelaydeckPlugin instance. Used for
        user-installed (`~/.relaydeck/plugins/`) and workspace plugin dirs.
        Official plugins ship in the `plugins` package and load via
        `_scan_package`, which uses real import so intra-package relative
        imports resolve.
        """
        entries: list[PluginEntry] = []
        if not root.exists():
            return entries

        from relaydeck.plugin_manifest import ManifestError, find_manifest

        # Walk subdirectories looking for plugin.py
        for pkg_dir in sorted(root.rglob("plugin.py")):
            rel_dir = pkg_dir.parent.relative_to(root)
            # Skip __pycache__ and hidden dirs
            if any(part.startswith("_") or part.startswith(".") for part in rel_dir.parts):
                continue

            category = str(rel_dir).replace("/", ".")
            try:
                try:
                    manifest = find_manifest(pkg_dir.parent)
                except ManifestError as exc:
                    logger.warning("Invalid plugin manifest at %s: %s", pkg_dir.parent, exc)
                    continue

                # Import the plugin module by file path.
                spec = importlib.util.spec_from_file_location(
                    f"relaydeck.plugins.{category}.plugin",
                    str(pkg_dir),
                )
                if not (spec and spec.loader):
                    continue
                # If a user/workspace plugin module is already imported from the
                # same file, reuse it rather than loading a *second* module
                # object under the same sys.modules key (which would split any
                # module-level singletons it owns between the two copies).
                existing = sys.modules.get(spec.name)
                existing_file = getattr(existing, "__file__", None)
                if existing_file and Path(existing_file).resolve() == pkg_dir.resolve():
                    module = existing
                else:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)

                entry = self._build_entry(module, manifest, category, source, pkg_dir.parent)
                if entry is not None:
                    entries.append(entry)
            except Exception as exc:
                logger.warning("Failed to load plugin at %s: %s", pkg_dir, exc)

        return entries

    def _scan_entry_points(self, group: str, source: str) -> list[PluginEntry]:
        """Discover plugins exposed by installed Python distributions.

        Entry-point values point at the plugin module, e.g.
        `demo = relaydeck_demo.plugin:PLUGIN`. We resolve the module spec
        first, load `plugin.toml` from the module directory, then import the
        module. That preserves the manifest-before-plugin-code contract for
        plugin packages that follow the SDK layout.
        """
        entries: list[PluginEntry] = []
        try:
            eps = importlib.metadata.entry_points()
            selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, ())
        except Exception:
            return entries

        from relaydeck.plugin_manifest import ManifestError, find_manifest

        for ep in selected:
            value = getattr(ep, "value", "") or ""
            module_name = value.split(":", 1)[0].strip()
            if not module_name:
                continue
            if module_name == "plugins" or module_name.startswith("plugins."):
                # Official plugins live in the `plugins` package and load via
                # _scan_package (bundled trust defaults + relative imports). If
                # a future split publishes entry points for them too, skip the
                # duplicate here so they aren't discovered twice.
                continue
            try:
                spec = importlib.util.find_spec(module_name)
            except (ImportError, AttributeError, ValueError) as exc:
                logger.warning("Failed to resolve plugin entry point %s: %s", ep, exc)
                continue
            if spec is None or spec.origin is None:
                continue
            plugin_path = Path(spec.origin).parent
            try:
                manifest = find_manifest(plugin_path)
                if manifest is None:
                    logger.warning(
                        "Plugin entry point %s has no plugin.toml next to %s",
                        ep, spec.origin,
                    )
                    continue
            except ManifestError as exc:
                logger.warning("Invalid plugin manifest for entry point %s: %s", ep, exc)
                continue
            approved_package = False
            if source == "installed":
                approved_package = self._approved_installed_package(manifest)
            if (
                source == "installed"
                and manifest.trust_level in ("", "untrusted")
                and not approved_package
                and not _allow_untrusted()
            ):
                logger.warning(
                    "Skipped plugin entry point %s before import "
                    "(untrusted, set RELAYDECK_ALLOW_UNTRUSTED_PLUGINS=1 to load)",
                    ep,
                )
                continue
            try:
                module = importlib.import_module(module_name)
                entry = self._build_entry(module, manifest, manifest.category, source, plugin_path)
                if entry is not None:
                    entry.lock_approved = approved_package
                    entries.append(entry)
            except Exception as exc:
                logger.warning("Failed to load plugin entry point %s: %s", ep, exc)

        return entries

    def _approved_installed_package(self, manifest) -> bool:
        try:
            from relaydeck.plugin_lock import HOST_API_VERSION, load_lock
            entry = load_lock(self.config_home).get(manifest.name)
        except Exception:
            return False
        if entry is None:
            return False
        if entry.installed_via == "editable-package":
            return (
                entry.state == "enabled"
                and manifest.host_api_version == HOST_API_VERSION
            )
        return (
            entry.installed_via == "package"
            and entry.state == "enabled"
            and entry.manifest_hash == manifest.manifest_hash
            and manifest.host_api_version == HOST_API_VERSION
        )

    def _scan_package(self, import_name: str, source: str) -> list[PluginEntry]:
        """Discover bundled plugins shipped as a separate installed package.

        Unlike `_scan_directory` (file-path loading), entries here load
        through real package import (`importlib.import_module`) so each
        plugin's intra-package relative imports (`from . import worker`)
        resolve against its true package name. Absent (`plugins` package not
        importable) → no entries; the daemon still boots (no official plugins).
        """
        entries: list[PluginEntry] = []
        try:
            spec = importlib.util.find_spec(import_name)
        except (ImportError, ValueError):
            return entries
        if spec is None or not spec.submodule_search_locations:
            return entries

        from relaydeck.plugin_manifest import ManifestError, find_manifest

        seen: set[str] = set()
        for root in (Path(p) for p in spec.submodule_search_locations):
            for pkg_dir in sorted(root.rglob("plugin.py")):
                rel_dir = pkg_dir.parent.relative_to(root)
                if any(part.startswith("_") or part.startswith(".") for part in rel_dir.parts):
                    continue
                category = ".".join(rel_dir.parts)
                modname = f"{import_name}.{category}.plugin"
                if modname in seen:
                    continue
                seen.add(modname)
                try:
                    try:
                        manifest = find_manifest(pkg_dir.parent)
                    except ManifestError as exc:
                        logger.warning("Invalid plugin manifest at %s: %s", pkg_dir.parent, exc)
                        continue
                    module = importlib.import_module(modname)
                    entry = self._build_entry(module, manifest, category, source, pkg_dir.parent)
                    if entry is not None:
                        entries.append(entry)
                except Exception as exc:
                    logger.warning("Failed to load plugin %s: %s", modname, exc)

        return entries

    def _build_entry(self, module, manifest, category: str, source: str, path: Path):
        """Build a PluginEntry from an already-imported plugin module.

        Handles both classic `RelaydeckPlugin` instances and new-style SDK
        `Plugin` instances (wrapped in a `HostPluginAdapter`). Shared by
        `_scan_directory` and `_scan_package`. Returns None when the module
        has no usable `PLUGIN`.
        """
        if not hasattr(module, "PLUGIN"):
            return None
        plugin = module.PLUGIN
        if isinstance(plugin, RelaydeckPlugin):
            plugin.name = manifest.name if manifest else (plugin.name or category)
            plugin.category = manifest.category if manifest else (
                plugin.category or category.split(".")[0]
            )
            plugin.version = manifest.version if manifest else plugin.version
            if manifest:
                plugin.description = manifest.description or plugin.description
                plugin.requires = list(manifest.dependencies)
                plugin.workspace_scoped = manifest.workspace_scoped
            return PluginEntry(
                name=plugin.name,
                category=plugin.category,
                version=plugin.version,
                instance=plugin,
                source=source,
                path=path,
                manifest=manifest,
            )

        try:
            from relaydeck.sdk import Plugin as SDKPlugin
        except Exception:
            SDKPlugin = None  # type: ignore[assignment]
        if SDKPlugin is not None and isinstance(plugin, SDKPlugin):
            if manifest is None:
                logger.warning("New-style plugin at %s has no plugin.toml; skipping", path)
                return None
            adapter = HostPluginAdapter(plugin, manifest, path)
            return PluginEntry(
                name=adapter.name,
                category=adapter.category,
                version=adapter.version,
                instance=adapter,
                source=source,
                path=path,
                manifest=manifest,
            )
        return None

    def discover_workspace(self, workspace: str, workspace_path: Path) -> list[PluginEntry]:
        """Discover plugins stored under a workspace's plugins/ directory."""
        entries = self._scan_directory(workspace_path / "plugins", f"workspace:{workspace}")
        for entry in entries:
            self._discovered[f"{entry.name}@{workspace}"] = entry
        return entries

    def load_workspace_plugins(
        self,
        workspace: str,
        workspace_path: Path,
        ctx: PluginContext | None = None,
    ) -> list[PluginEntry]:
        """Load one plugin instance per workspace.

        This is the implementation hook for the Phase 1a lifecycle
        contract. Callers decide when workspace.added / first agent.start
        should trigger it; the registry serializes concurrent loads for
        the same (plugin, workspace).
        """
        base_ctx = ctx or self._last_ctx or PluginContext(config_home=self.config_home)
        loaded: list[PluginEntry] = []
        for entry in self.discover_workspace(workspace, workspace_path):
            key = (workspace, entry.name)
            lock = self._workspace_locks.setdefault(key, threading.Lock())
            with lock:
                if key in self._workspace_entries:
                    loaded.append(self._workspace_entries[key])
                    continue
                ws_ctx = PluginContext(
                    config_home=base_ctx.config_home,
                    workspace_path=workspace_path,
                    # Thread the REGISTERED name through so a scoped host
                    # scopes by it, not the path basename.
                    workspace_name=workspace,
                    orchestrator=base_ctx.orchestrator,
                    db_path=base_ctx.db_path,
                    event_bus=base_ctx.event_bus or getattr(self, "_event_bus", None),
                )
                try:
                    entry.instance.on_load(ws_ctx)
                except Exception:
                    self._rollback_failed_load(entry)
                    raise
                self._workspace_entries[key] = entry
                self._entries[f"{entry.name}@{workspace}"] = entry
                self._by_category.setdefault(entry.category, []).append(entry)
                loaded.append(entry)
        return loaded

    def unload_workspace_plugins(self, workspace: str) -> list[str]:
        """Unload every workspace-scoped plugin instance for a workspace."""
        unloaded: list[str] = []
        for key, entry in list(self._workspace_entries.items()):
            ws, name = key
            if ws != workspace:
                continue
            try:
                entry.instance.on_unload()
            except Exception as exc:
                logger.warning("Error unloading workspace plugin %s@%s: %s", name, ws, exc)
            self._workspace_entries.pop(key, None)
            self._entries.pop(f"{name}@{ws}", None)
            cat_list = self._by_category.get(entry.category)
            if cat_list:
                self._by_category[entry.category] = [
                    e for e in cat_list
                    if not (e.name == name and e.source == f"workspace:{ws}")
                ]
            unloaded.append(name)
        return unloaded

    # ── Loading ──────────────────────────────────────────────────

    def load_all(self, ctx: PluginContext) -> None:
        """Discover and load all plugins, respecting dependency order.

        Creates the plugin event bus, loads plugins, wires event
        subscriptions, and emits system lifecycle events.
        Plugins listed in `~/.relaydeck/plugins-disabled.yaml` are
        discovered (so they show up in /api/plugins for re-enable) but
        not loaded.
        """
        from relaydeck.plugin_disabled import disabled_set
        disabled = disabled_set()
        discovered = self.discover()
        for entry in discovered:
            self._discovered[entry.name] = entry

        try:
            from relaydeck.plugin_lock import verify_lock

            lock_entries = verify_lock(
                ctx.config_home,
                [entry.manifest for entry in discovered if entry.manifest is not None],
            )
            blocked = {
                name for name, entry in lock_entries.items()
                if entry.state == "blocked"
            }
        except Exception:
            blocked = set()

        self._last_ctx = ctx
        if ctx.orchestrator is None:
            try:
                from relaydeck.orchestrator import get_orchestrator
                ctx.orchestrator = get_orchestrator(ctx.config_home)
            except Exception:
                ctx.orchestrator = None

        # Create event bus and attach to context. When ctx.db_path
        # is unset, derive the conventional location so durability
        # works out of the box for any caller that constructs a
        # PluginContext with only `config_home`.
        bus_db_path = ctx.db_path or str(
            ctx.config_home / "runtime" / "relaydeck.db"
        )
        bus = PluginEventBus(db_path=bus_db_path)
        ctx.event_bus = bus
        ctx.db_path = bus_db_path
        self._event_bus = bus

        # Emit system startup event
        bus.emit(Event(type=EventType.SYSTEM_STARTUP, data={}))

        # Trust gate: refuse to load `untrusted` plugins unless the
        # operator opts in via RELAYDECK_ALLOW_UNTRUSTED_PLUGINS=1. Built-in
        # plugins default to `bundled` and pass through; user-installed
        # default to `local` and pass through. A plugin only lands in
        # `untrusted` if its manifest declared so or a future install
        # path tags it that way.
        if _allow_untrusted():
            untrusted: set[str] = set()
        else:
            untrusted = {
                e.name for e in discovered
                if effective_trust_level(e) == "untrusted"
            }

        # Topological sort by requires — skip disabled plugins entirely.
        loaded: set[str] = set()
        remaining = {
            e.name: e for e in discovered
            if (e.name not in disabled and e.name not in blocked
                and e.name not in untrusted)
        }
        for name in (disabled & {e.name for e in discovered}):
            logger.info("Skipped plugin (disabled): %s", name)
        for name in (blocked & {e.name for e in discovered}):
            logger.info("Skipped plugin (blocked): %s", name)
        for name in untrusted:
            logger.warning(
                "Skipped plugin (untrusted, set RELAYDECK_ALLOW_UNTRUSTED_PLUGINS=1 "
                "to load anyway): %s", name,
            )

        while remaining:
            progress = False
            for name, entry in list(remaining.items()):
                if all(dep in loaded for dep in entry.instance.requires):
                    try:
                        entry.instance.on_load(ctx)
                        self._entries[name] = entry
                        self._by_category.setdefault(entry.category, []).append(entry)
                        loaded.add(name)
                        del remaining[name]
                        progress = True
                        logger.info(
                            "Loaded plugin: %s (%s, v%s)",
                            name,
                            entry.category,
                            entry.version,
                        )

                        # Wire event subscriptions
                        self._wire_subscriptions(entry, bus)

                        # Emit plugin loaded event
                        bus.emit(Event(
                            type=EventType.SYSTEM_PLUGIN_LOADED,
                            data={"plugin": name, "category": entry.category},
                        ))
                    except Exception as exc:
                        self._rollback_failed_load(entry)
                        logger.error("Failed to load plugin %s: %s", name, exc)
                        del remaining[name]

            if not progress:
                stuck = ", ".join(remaining.keys())
                logger.error("Cannot load plugins (circular/missing deps): %s", stuck)
                break


    def _wire_subscriptions(self, entry: PluginEntry, bus: PluginEventBus) -> None:
        """Wire a plugin's event subscriptions to the event bus.

        Durable subscriptions are registered via `subscribe_durable`
        with a stable key (`<plugin>.<handler>` unless the
        subscription overrides). Replay-on-registration happens
        synchronously inside that call.
        """
        plugin = entry.instance
        subs = plugin.get_subscriptions()

        for sub in subs:
            handler = getattr(plugin, sub.handler, None)
            if not handler:
                continue
            if sub.durable:
                key = sub.key or f"{plugin.name}.{sub.handler}"
                bus.subscribe_durable(sub.pattern, handler, key=key)
            else:
                bus.subscribe(sub.pattern, handler)

        # Also check if plugin overrides on_event (catch-all handler)
        if type(plugin).on_event is not RelaydeckPlugin.on_event:
            # Plugin has its own on_event — subscribe to all patterns from get_subscriptions
            if subs:
                for sub in subs:
                    bus.subscribe(sub.pattern, plugin.on_event)
            else:
                # No specific subscriptions, but on_event is overridden — subscribe to *
                bus.subscribe("*", plugin.on_event)

    def unload_all(self) -> None:
        """Unload all plugins in reverse dependency order."""
        bus = getattr(self, "_event_bus", None)
        for entry in reversed(list(self._entries.values())):
            try:
                entry.instance.on_unload()
            except Exception as exc:
                logger.warning("Error unloading plugin %s: %s", entry.name, exc)
            # Emit unloaded event
            if bus:
                bus.emit(Event(
                    type=EventType.SYSTEM_PLUGIN_UNLOADED,
                    data={"plugin": entry.name},
                ))
        self._entries.clear()
        self._by_category.clear()
        # Emit system shutdown
        if bus:
            bus.emit(Event(type=EventType.SYSTEM_SHUTDOWN, data={}))

    def _rollback_failed_load(self, entry: PluginEntry) -> None:
        """Best-effort cleanup for plugins whose on_load raised.

        New-style plugins can register Host side effects before their
        on_load finishes. The adapter tears Host handles down on the
        original exception path; this registry-level rollback gives the
        plugin a matching on_unload hook and also covers legacy plugins
        that partially initialized before raising.
        """
        try:
            entry.instance.on_unload()
        except Exception as exc:
            logger.warning("Error rolling back failed plugin %s: %s", entry.name, exc)

    @property
    def event_bus(self) -> PluginEventBus | None:
        return getattr(self, "_event_bus", None)

    # ── Query ────────────────────────────────────────────────────

    def get(self, name: str) -> PluginEntry | None:
        return self._entries.get(name)

    def by_category(self, category: str) -> list[PluginEntry]:
        return self._by_category.get(category, [])

    def all(self) -> list[PluginEntry]:
        return list(self._entries.values())

    def discovered_all(self) -> list[PluginEntry]:
        """Every plugin the discoverer found this process — loaded or
        not. Used by the dashboard's Plugins panel so a disabled plugin
        still shows up (and can be re-enabled) without a daemon restart.
        Falls back to `all()` if nothing was discovered yet."""
        if self._discovered:
            return list(self._discovered.values())
        return self.all()

    # ── Runtime enable / disable ─────────────────────────────────

    def disable(self, name: str) -> tuple[bool, str]:
        """Persist the disabled flag and best-effort unload the running
        plugin: call on_unload(), unsubscribe its event-bus handlers,
        and stop its workers. Returns (live_unloaded, message)."""
        from relaydeck.plugin_disabled import set_disabled

        set_disabled(name, True)

        entry = self._entries.get(name)
        if entry is None:
            return False, f"Plugin {name} is not currently loaded; flag saved for next start."

        # 1. Stop + drop any workers this plugin registered so disable
        # doesn't leave STOPPED rows that re-enable would duplicate.
        # Workers are stopped serially (join per worker). Fine for the
        # current plugin set (≤1 worker each); revisit if a plugin
        # registers many long-lived workers.
        worker_count = 0
        try:
            from relaydeck.workers import get_worker_registry
            reg = get_worker_registry()
            for w in reg.by_plugin(name):
                try:
                    w.stop()
                    w.join(timeout=2.0)
                    if w._thread and w._thread.is_alive():
                        logger.warning(
                            "Worker %s (%s) did not exit within 2s after stop; "
                            "unregistering from registry but thread may still run",
                            w.name, w.id,
                        )
                    reg.unregister(w.id)
                    worker_count += 1
                except Exception as exc:
                    logger.warning("Failed to stop worker %s: %s", w.name, exc)
        except Exception:
            pass

        # 2. Unsubscribe its handlers from the plugin event bus.
        bus = getattr(self, "_event_bus", None)
        if bus is not None:
            for sub in entry.instance.get_subscriptions():
                handler = getattr(entry.instance, sub.handler, None)
                if handler is not None:
                    bus.unsubscribe(handler)
            # Catch-all on_event handler too.
            if type(entry.instance).on_event is not RelaydeckPlugin.on_event:
                bus.unsubscribe(entry.instance.on_event)

        # 3. Run the plugin's own cleanup.
        try:
            entry.instance.on_unload()
        except Exception as exc:
            logger.warning("Error in %s.on_unload: %s", name, exc)

        # 4. Drop from the live registry. Keep in _discovered so we can
        # re-enable later.
        self._entries.pop(name, None)
        cat_list = self._by_category.get(entry.category)
        if cat_list:
            self._by_category[entry.category] = [e for e in cat_list if e.name != name]

        # 5. Emit lifecycle event so the UI / SSE pipeline sees it.
        if bus is not None:
            bus.emit(Event(
                type=EventType.SYSTEM_PLUGIN_UNLOADED,
                data={"plugin": name, "reason": "disabled"},
            ))

        return True, (
            f"Stopped {worker_count} worker(s) and unsubscribed handlers. "
            "CLI commands or API routes registered at startup will linger until daemon restart."
        )

    def enable(self, name: str) -> tuple[bool, str]:
        """Persist the enable flag and re-load if we still have the
        discovered entry + the original load context. CLI/API routes
        registered at startup can't be reinstalled at runtime — if the
        plugin contributes those, daemon restart is required for a
        full revival. Returns (live_reloaded, message)."""
        from relaydeck.plugin_disabled import set_disabled

        set_disabled(name, False)

        if name in self._entries:
            return False, f"Plugin {name} is already loaded."

        entry = self._discovered.get(name)
        ctx = self._last_ctx
        if entry is None or ctx is None:
            return False, f"{name} not discovered this process; restart daemon to load."

        try:
            entry.instance.on_load(ctx)
        except Exception as exc:
            self._rollback_failed_load(entry)
            return False, f"on_load failed: {exc}"
        self._entries[name] = entry
        self._by_category.setdefault(entry.category, []).append(entry)
        bus = getattr(self, "_event_bus", None)
        if bus is not None:
            self._wire_subscriptions(entry, bus)
            bus.emit(Event(
                type=EventType.SYSTEM_PLUGIN_LOADED,
                data={"plugin": name, "category": entry.category, "reason": "enabled"},
            ))
        return True, f"Re-loaded {name}."

    def categories(self) -> list[str]:
        return sorted(self._by_category.keys())

    @property
    def harnesses(self) -> list[PluginEntry]:
        return self.by_category("harness")

    @property
    def tools(self) -> list[PluginEntry]:
        return self.by_category("tool")


class HostPluginAdapter(RelaydeckPlugin):
    """Compatibility wrapper for new `relaydeck.sdk.Plugin` instances.

    The existing registry and transports still know how to call
    `RelaydeckPlugin` hooks. This adapter lets a plugin implement the new
    `PluginHost` contract while the old hooks continue to drive CLI,
    API, UI, settings, and event teardown.
    """

    def __init__(self, plugin: Any, manifest: Any, path: Path) -> None:
        self.plugin = plugin
        self.manifest = manifest
        self.path = path
        self.name = manifest.name
        self.version = manifest.version
        self.category = manifest.category
        self.description = manifest.description
        self.requires = list(manifest.dependencies)
        self.workspace_scoped = manifest.workspace_scoped
        self._host: Any = None

    def on_load(self, ctx: PluginContext) -> None:
        from relaydeck.sdk import PluginHost

        defaults = {field.key: field.default for field in self.manifest.settings}
        # Prefer the registered workspace name; fall back to the path
        # basename only when the context didn't carry a name (e.g. a
        # global load). Using the basename for a workspace-scoped host
        # would mis-scope host.agents.create/start when a workspace's
        # registry name differs from its directory name.
        workspace_name = ctx.workspace_name or (
            ctx.workspace_path.name if ctx.workspace_path is not None else None
        )
        host = PluginHost(
            name=self.name,
            config_home=ctx.config_home,
            declared_capabilities=self.manifest.declared_capabilities,
            event_bus=ctx.event_bus,
            orchestrator=ctx.orchestrator,
            workspace=workspace_name,
            workspace_path=ctx.workspace_path,
            settings_defaults=defaults,
            top_level_cli=self.manifest.top_level_cli,
            top_level_api=self.manifest.top_level_api,
            vault_keys=self.manifest.needs_vault,
        )
        self._host = host
        try:
            self.plugin.on_load(host)
        except Exception:
            host.teardown()
            self._host = None
            raise
        # After on_load: bind any harness types the plugin registered
        # into the orchestrator's _TYPES dict. We do this here (not
        # inside HarnessRegistrarHost.register) so the plugin's
        # registration is purely declarative — the side effect on
        # the orchestrator happens exactly once, at the adapter
        # boundary, and the on_unload path can mirror it cleanly.
        try:
            from relaydeck.orchestrator import register_agent_type
            for type_name, cls in host.harnesses.registrations:
                register_agent_type(type_name, cls)
        except Exception:
            pass
        # Bind plugin-contributed viewers into the canonical
        # registry. Same shape as harnesses — declarative on the
        # plugin side, side effect at the adapter boundary so
        # on_unload can mirror cleanly.
        try:
            from relaydeck.transports import viewers as _viewers
            for viewer in host.viewers.registrations:
                _viewers.register(viewer)
        except Exception:
            pass
        # Bind plugin-contributed messaging-channel providers into the
        # process-wide channel registry. Same declarative shape as
        # harnesses/viewers: the plugin only records the provider; the
        # side effect (and its on_unload mirror) lives at this boundary.
        try:
            from relaydeck import channels as _channels
            for provider in host.channels.registrations:
                _channels.register_channel(provider)
        except Exception:
            logger.debug("channel provider bind failed for %s", self.name, exc_info=True)

    def on_unload(self) -> None:
        try:
            self.plugin.on_unload()
        finally:
            # Mirror the harness bind from on_load. Idempotent — names
            # the plugin didn't register are silently skipped.
            try:
                from relaydeck.orchestrator import unregister_agent_type
                if self._host is not None:
                    for type_name, _ in self._host.harnesses.registrations:
                        unregister_agent_type(type_name)
            except Exception:
                pass
            # Mirror the viewer bind. We can't "remove" from the
            # registry by name because a built-in might have been
            # overridden by this plugin and we'd lose the override;
            # instead delete the slot only if it still points to
            # the plugin's instance.
            try:
                from relaydeck.transports import viewers as _viewers
                if self._host is not None:
                    for viewer in self._host.viewers.registrations:
                        current = _viewers.get(viewer.name)
                        if current is viewer:
                            _viewers._registry.pop(viewer.name, None)
            except Exception:
                pass
            # Mirror the channel-provider bind — drop only if this plugin
            # still owns the slot (same shape as viewers).
            try:
                from relaydeck import channels as _channels
                if self._host is not None:
                    for provider in self._host.channels.registrations:
                        _channels.unregister_channel(
                            getattr(provider, "channel", ""), provider
                        )
            except Exception:
                pass
            if self._host is not None:
                self._host.teardown()

    def register_cli(self, cli: click.Group) -> None:
        if self._host is None:
            return
        if self._host.cli.commands:
            group = click.Group(name=self.name)
            for command_name, fn, attrs in self._host.cli.commands:
                command = click.command(name=command_name, **_click_attrs(attrs))(fn)
                if attrs.get("top_level"):
                    if command_name not in cli.commands:
                        cli.add_command(command)
                    continue
                if command_name not in group.commands:
                    group.add_command(command)
            if group.commands and self.name not in cli.commands:
                cli.add_command(group)
        # Escape hatch for plugins that attach to existing top-level
        # groups (e.g. messaging extending `workspace`). The default
        # Plugin.extend_cli is a no-op.
        try:
            self.plugin.extend_cli(cli)
        except AttributeError:
            pass

    def register_api_routes(self, app: FastAPI) -> None:
        if self._host is None:
            return
        for route in self._host.api.routes:
            raw_path = "/" + str(route["path"]).strip("/")
            if route.get("top_level"):
                path = raw_path
            else:
                path = f"/api/plugins/{self.name}{raw_path}"
            methods = {str(m).upper() for m in route["methods"]}
            if _api_route_exists(app, path, methods):
                continue
            app.add_api_route(path, route["handler"], methods=route["methods"])

    def register_ui(self) -> dict[str, Any]:
        base = _prefix_ui_ids(self.name, self.manifest.ui_manifest())
        if self._host is None:
            return base
        live = _prefix_ui_ids(self.name, self._host.ui.manifest())
        return {
            "tabs": base.get("tabs", []) + live.get("tabs", []),
            "header_chips": base.get("header_chips", []) + live.get("header_chips", []),
            "agent_tiles": base.get("agent_tiles", []) + live.get("agent_tiles", []),
            "widgets": base.get("widgets", []) + live.get("widgets", []),
            "tui": base.get("tui", []) + live.get("tui", []),
        }

    def get_settings_schema(self) -> list[dict[str, Any]]:
        return self.manifest.settings_schema()

    def static_dir(self) -> Path | None:
        static = self.path / "static"
        return static if static.is_dir() else None

    # ── skill provider hooks (duck-typed; consumed by the skills manager) ──
    # The skills manager sees this adapter as the plugin `instance`, so it
    # must forward the optional `[plugin.skills]` provider hooks to the
    # wrapped SDK plugin. Without this, a DAEMON-WIDE skill-contributing
    # plugin (theme; telegram with routes) resolves to NO target workspaces
    # — `_resolve_targets` only falls back to opted-in workspaces for
    # workspace-scoped plugins. Returning None when the wrapped plugin
    # doesn't define the hook lets the manager apply its own default.
    def skill_target_workspaces(self, all_workspaces: list[str]) -> list[str] | None:
        hook = getattr(self.plugin, "skill_target_workspaces", None)
        return hook(all_workspaces) if callable(hook) else None

    def skill_content(self, skill_name: str, source_text: str) -> str:
        hook = getattr(self.plugin, "skill_content", None)
        return hook(skill_name, source_text) if callable(hook) else source_text


def _click_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    allowed = {"help", "short_help", "epilog", "hidden", "deprecated"}
    return {k: v for k, v in attrs.items() if k in allowed}


def _api_route_exists(app: FastAPI, path: str, methods: set[str]) -> bool:
    for route in app.routes:
        if getattr(route, "path", None) != path:
            continue
        existing = {str(m).upper() for m in getattr(route, "methods", set())}
        if existing & methods:
            return True
    return False


def _prefix_ui_ids(plugin: str, manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for key in ("tabs", "header_chips", "agent_tiles", "widgets", "tui"):
        out[key] = []
        for item in manifest.get(key, []) or []:
            copied = dict(item)
            ident = str(copied.get("id") or "")
            if ident and not ident.startswith(f"{plugin}:"):
                copied["id"] = f"{plugin}:{ident}"
            out[key].append(copied)
    return out


# Module-level singleton (lazy)
_registry: PluginRegistry | None = None


def get_registry(config_home: Path | None = None) -> PluginRegistry:
    global _registry
    if _registry is None:
        _registry = PluginRegistry(config_home or Path.home() / ".relaydeck")
    return _registry
