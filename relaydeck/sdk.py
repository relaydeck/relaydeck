"""Public SDK surface for relaydeck plugins and local tests.

`PluginHost` is the in-process daemon surface. `RemoteHost` is a
separate HTTP-backed client surface for external automation. They share
DTOs and naming, but not capabilities.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

from relaydeck.plugin import (
    Event,
    EventType,
    ModelEntry,
    PluginContext,
    PluginEventBus,
    ProviderPlugin,
    get_provider,
    list_providers,
)

if TYPE_CHECKING:
    from relaydeck.tasks import Task

API_VERSION = 1

__all__ = [
    "API_VERSION",
    "AgentInfo",
    "CapabilityNotDeclared",
    "Event",
    "EventType",
    "ModelEntry",
    "PluginContext",
    "PluginEventBus",
    "Plugin",
    "PluginHost",
    "ProviderPlugin",
    "RemoteHost",
    "SendResult",
    "WorkerHandle",
    "complete_with_model",
    "complete_with_model_ex",
    "embed_with_model",
    "get_provider",
    "list_providers",
    "resolve_model",
    "stream_with_model",
]


class CapabilityNotDeclared(PermissionError):  # noqa: N818
    """Raised when plugin code asks for a Host capability it did not declare."""


class Plugin:
    """Base class for new-style plugins."""

    def on_load(self, host: PluginHost) -> None:
        pass

    def on_unload(self) -> None:
        pass

    def extend_cli(self, root: Any) -> None:
        """Escape hatch: attach commands to *existing* top-level CLI
        groups that the SDK's declarative `host.cli.command(...)` API
        can't reach. The plugin receives the relaydeck root `click.Group`
        and may modify it freely.

        Default: no-op. Override sparingly — the declarative API is
        preferred for any new plugin. Used today by messaging to
        extend `relaydeck workspace message/inbox` and replace the
        `relaydeck agent send` stub.
        """
        del root


@dataclass(frozen=True)
class AgentInfo:
    id: str
    type: str = ""
    name: str = ""
    status: str = ""
    workspace: str | None = None
    purpose: str = ""
    tags: tuple[str, ...] = ()
    semantic_status: str | None = None  # "working" | "awaiting-input" | "complete-unread" | "idle"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentInfo:
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or ""),
            name=str(data.get("name") or data.get("id") or ""),
            status=str(data.get("status") or ""),
            workspace=data.get("workspace"),
            purpose=str(data.get("purpose") or ""),
            tags=tuple(str(v) for v in data.get("tags") or ()),
            semantic_status=(data.get("semantic_status") or None),
        )


@dataclass(frozen=True)
class SendResult:
    ids: tuple[str, ...]
    injected: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    recipients: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SendResult:
        return cls(
            ids=tuple(str(v) for v in data.get("ids") or ()),
            injected=tuple(str(v) for v in data.get("injected") or ()),
            pending=tuple(str(v) for v in data.get("pending") or ()),
            recipients=tuple(dict(v) for v in data.get("recipients") or ()),
        )


@dataclass(frozen=True)
class WorkerHandle:
    id: str
    name: str

    def stop(self) -> None:
        """Stop the underlying supervised worker. Idempotent.

        `host.workers.spawn()` hands back this lightweight handle, and the
        natural way to stop a worker is `handle.stop()` — without this method
        that call raised AttributeError, which plugins swallowed in a broad
        `except`, silently leaking the worker (it kept ticking forever). Look
        the worker up by id and stop it, the same as `host.workers.stop()`."""
        from relaydeck.workers import get_worker_registry
        worker = get_worker_registry().get(self.id)
        if worker is not None:
            worker.stop()


class _CapabilityGate:
    def __init__(self, declared: Iterable[str]) -> None:
        self._declared = set(declared)

    def require(self, capability: str) -> None:
        if capability not in self._declared:
            raise CapabilityNotDeclared(
                f"Capability {capability!r} was not declared in plugin.toml"
            )


class EventBusHost:
    def __init__(self, plugin: str, bus: Any, gate: _CapabilityGate) -> None:
        self._plugin = plugin
        self._bus = bus
        self._gate = gate
        self._handlers: list[Callable[[Event], None]] = []

    def subscribe(
        self,
        pattern: str,
        handler: Callable[[Event], None],
        *,
        durable: bool = False,
        key: str | None = None,
    ) -> Callable[[], None]:
        """Subscribe to events matching `pattern`.

        `durable=True` opts into at-least-once replay across daemon
        restarts: the bus persists every event before dispatch and
        tracks a per-subscriber cursor keyed by `key`, so a crash
        between a GitHub/CI event and the handler ack doesn't lose the
        work. Requires a stable `key` (the cursor id — reuse the same
        string across restarts) and a bus constructed with a db_path;
        an in-memory bus transparently degrades to a normal subscribe.

        Without `durable`, this is the historical fire-and-forget
        subscribe — fine for UI/refresh reactions, wrong for work that
        must survive a restart.
        """
        self._gate.require("events.subscribe")
        if durable:
            if not key:
                raise ValueError("durable=True requires a stable `key` for the cursor")
            subscribe_durable = getattr(self._bus, "subscribe_durable", None)
            if callable(subscribe_durable):
                subscribe_durable(pattern, handler, key=key)
            else:
                # Bus without durability support (a bare stub) — degrade
                # to in-memory rather than failing the subscribe.
                self._bus.subscribe(pattern, handler)
        else:
            self._bus.subscribe(pattern, handler)
        self._handlers.append(handler)

        def unsubscribe() -> None:
            self._bus.unsubscribe(handler)

        return unsubscribe

    def emit(self, event_type: str | Event, data: dict[str, Any] | None = None) -> None:
        self._gate.require("events.emit")
        # Accepts both shapes on purpose. The documented adapter signature
        # is (event_type: str, data: dict) — the host builds the Event and
        # stamps source_plugin. But a caller reaching for the raw-bus idiom
        # `emit(Event(...))` (used across the codebase) would otherwise get
        # the Event silently nested as the *type*, which corrupts bus_events
        # persistence and breaks subscriber matching with no error. Forward
        # a pre-built Event as-is instead of nesting it.
        if isinstance(event_type, Event):
            self._bus.emit(event_type)
            return
        self._bus.emit(Event(type=event_type, data=data or {}, source_plugin=self._plugin))

    def teardown(self) -> None:
        for handler in list(self._handlers):
            self._bus.unsubscribe(handler)
        self._handlers.clear()


class AgentRegistryHost:
    def __init__(self, orchestrator: Any, gate: _CapabilityGate, workspace: str | None) -> None:
        self._orchestrator = orchestrator
        self._gate = gate
        self._workspace = workspace

    def list(self) -> list[AgentInfo]:
        self._gate.require("agents.list")
        rows = self._orchestrator.list_agents()
        if self._workspace:
            rows = [r for r in rows if (r.get("workspace") or "") == self._workspace]
        return [AgentInfo.from_dict(r) for r in rows]

    def get(self, agent_id: str) -> AgentInfo | None:
        self._gate.require("agents.list")
        row = self._orchestrator.get_agent(agent_id)
        if not row:
            return None
        if self._workspace and (row.get("workspace") or "") != self._workspace:
            return None
        return AgentInfo.from_dict(row)

    def find(
        self,
        *,
        tag: str | None = None,
        purpose: str | None = None,
        workspace: str | None = None,
    ) -> list[AgentInfo]:
        self._gate.require("agents.list")
        target_workspace = workspace if workspace is not None else self._workspace
        rows = self._orchestrator.find_agents(
            tag=tag, purpose=purpose, workspace=target_workspace
        )
        return [AgentInfo.from_dict(r) for r in rows]

    def send_message(
        self,
        to: str,
        body: str,
        *,
        from_: str = "plugin",
        in_reply_to: str | None = None,
        wait: float | None = None,
    ) -> SendResult:
        del wait  # in-process send returns immediately; CLI wait stays in messaging plugin.
        self._gate.require("agents.send")
        msg_id, injected = self._orchestrator.send_message_to(
            to, body, from_id=from_, in_reply_to=in_reply_to
        )
        return SendResult(
            ids=(msg_id,),
            injected=(msg_id,) if injected else (),
            pending=() if injected else (msg_id,),
            recipients=({"agent_id": to, "msg_id": msg_id, "injected": injected},),
        )

    def stop(self, agent_id: str) -> bool:
        """Request a graceful stop of `agent_id`. Returns True if the
        agent existed and was running. Requires `agents.stop` in the
        plugin manifest's declared_capabilities — without it, the gate
        raises and the call is refused.

        Intentionally narrower than `send_message`: a plugin that
        wants to influence runtime lifecycle should declare it
        explicitly so the manifest captures the platform power it has.
        """
        self._gate.require("agents.stop")
        if self._workspace:
            row = self._orchestrator.get_agent(agent_id)
            if not row or (row.get("workspace") or "") != self._workspace:
                return False
        return bool(self._orchestrator.stop_agent(agent_id))

    def create(
        self,
        agent_id: str,
        agent_type: str,
        name: str,
        *,
        config: dict[str, Any] | None = None,
        auto_start: bool = False,
        purpose: str = "",
        tags: list[str] | None = None,
        system_prompt: str = "",
        inject_identity_preamble: bool = True,
        workspace: str | None = None,
    ) -> AgentInfo | None:
        """Create a new agent spec (YAML source of truth + DB mirror) and
        return its `AgentInfo`. Requires `agents.create`.

        Does NOT start the agent — call `start()` after (the spawn
        workflow is create-then-start). A workspace-scoped host pins the
        new agent to its own workspace so a scoped plugin can't create
        agents elsewhere; a daemon-wide host honors the `workspace` arg.
        """
        self._gate.require("agents.create")
        ws = self._workspace if self._workspace is not None else workspace
        self._orchestrator.create_agent(
            agent_id,
            agent_type,
            name,
            workspace=ws,
            config=config or {},
            auto_start=auto_start,
            purpose=purpose,
            tags=list(tags or []),
            system_prompt=system_prompt,
            inject_identity_preamble=inject_identity_preamble,
        )
        row = self._orchestrator.get_agent(agent_id)
        return AgentInfo.from_dict(row) if row else None

    def start(self, agent_id: str) -> AgentInfo | None:
        """Start a created/stopped agent. Returns its `AgentInfo` after
        the start request, or None if a workspace-scoped host can't see
        the agent. Requires `agents.start`.

        Symmetric with `stop()`: declaring this capability is how a
        plugin's manifest captures that it can spin up runtime processes.
        """
        self._gate.require("agents.start")
        if self._workspace:
            row = self._orchestrator.get_agent(agent_id)
            if not row or (row.get("workspace") or "") != self._workspace:
                return None
        self._orchestrator.start_agent(agent_id)
        row = self._orchestrator.get_agent(agent_id)
        return AgentInfo.from_dict(row) if row else None


class TaskStoreHost:
    """Plugin-facing task store (`host.tasks`).

    Plugin-owned work records (PR/issue/CI lifecycle). Every row is
    namespaced by the owning plugin, so a plugin only ever sees its own
    tasks. A workspace-scoped host scopes reads/creates to its workspace.
    Reads need `tasks.read`; writes need `tasks.write` (declare in
    plugin.toml). The data layer is `relaydeck/tasks.py`.
    """

    def __init__(
        self, plugin: str, gate: _CapabilityGate, workspace: str | None, db_path: str
    ) -> None:
        self._plugin = plugin
        self._gate = gate
        self._workspace = workspace
        self._db_path = db_path

    def create(
        self,
        *,
        kind: str = "",
        agent_id: str | None = None,
        external_ref: str | None = None,
        title: str = "",
        status: str = "open",
        metadata: dict[str, Any] | None = None,
        workspace: str | None = None,
    ) -> Task:
        self._gate.require("tasks.write")
        from relaydeck.tasks import create_task
        ws = self._workspace if self._workspace is not None else workspace
        return create_task(
            self._plugin, kind=kind, workspace=ws, agent_id=agent_id,
            external_ref=external_ref, title=title, status=status,
            metadata=metadata, db_path=self._db_path,
        )

    def get(self, task_id: str) -> Task | None:
        self._gate.require("tasks.read")
        from relaydeck.tasks import get_task
        task = get_task(task_id, db_path=self._db_path)
        # Owner scoping: never surface another plugin's task.
        if task is None or task.plugin != self._plugin:
            return None
        return task

    def find_by_ref(self, external_ref: str) -> Task | None:
        self._gate.require("tasks.read")
        from relaydeck.tasks import find_by_external_ref
        return find_by_external_ref(self._plugin, external_ref, db_path=self._db_path)

    def list(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        workspace: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        self._gate.require("tasks.read")
        from relaydeck.tasks import list_tasks
        ws = self._workspace if self._workspace is not None else workspace
        return list_tasks(
            plugin=self._plugin, workspace=ws, agent_id=agent_id,
            status=status, limit=limit, db_path=self._db_path,
        )

    def update(self, task_id: str, **fields: Any) -> Task | None:
        """Partial update. Accepts status / agent_id / workspace / title /
        kind / metadata (see relaydeck.tasks.update_task). Owner-scoped: a
        plugin can only update its own task."""
        self._gate.require("tasks.write")
        from relaydeck.tasks import get_task, update_task
        task = get_task(task_id, db_path=self._db_path)
        if task is None or task.plugin != self._plugin:
            return None
        fields.pop("plugin", None)
        fields.pop("db_path", None)
        return update_task(task_id, db_path=self._db_path, **fields)

    def upsert_by_ref(self, external_ref: str, **fields: Any) -> Task:
        """Create-or-update keyed on `external_ref` (idempotent for
        pollers re-seeing the same upstream PR/issue)."""
        self._gate.require("tasks.write")
        from relaydeck.tasks import upsert_by_external_ref
        fields.pop("plugin", None)
        fields.pop("db_path", None)
        if self._workspace is not None:
            fields["workspace"] = self._workspace
        return upsert_by_external_ref(
            self._plugin, external_ref, db_path=self._db_path, **fields
        )


class WorkspaceRegistryHost:
    """Plugin-facing workspace lifecycle (`host.workspaces`).

    Lets a plugin register task workspaces (e.g. a git worktree the
    GitHub plugin spun up for a PR) and adjust their plugin lists, going
    through the same registry writes as `relaydeck workspace add` and firing
    the same `workspace.added`/`.updated` events. `create` needs
    `workspaces.create`; `update` needs `workspaces.update`; `list` needs
    `workspaces.read`. There is no delete — tearing down a registered
    workspace is an operator decision (matches the CLI/API, which refuse
    to delete workspaces with live agents).
    """

    def __init__(self, gate: _CapabilityGate, config_home: Path, event_bus: Any) -> None:
        self._gate = gate
        self._config_home = config_home
        self._event_bus = event_bus

    def _fire(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.emit(Event(type=event_type, data=data, source_plugin="workspaces"))
        except Exception:
            pass

    def list(self) -> list[dict[str, Any]]:
        self._gate.require("workspaces.read")
        from relaydeck.config import load_workspace_registry
        return [
            {"name": w.name, "path": str(w.path), "plugins": list(w.plugins or [])}
            for w in load_workspace_registry(self._config_home)
        ]

    def create(
        self, name: str, path: str | Path, *, plugins: list[str] | None = None
    ) -> dict[str, Any]:
        self._gate.require("workspaces.create")
        from relaydeck.config import register_workspace
        info = register_workspace(
            self._config_home, name, Path(path), list(plugins or [])
        )
        self._fire("workspace.added", {"name": name, "path": str(info["path"])})
        return info

    def update(self, name: str, *, plugins: list[str]) -> dict[str, Any]:
        self._gate.require("workspaces.update")
        from relaydeck.config import set_workspace_plugins
        info = set_workspace_plugins(self._config_home, name, [str(p) for p in plugins])
        self._fire("workspace.updated", {"name": name, "plugins": info["plugins"]})
        return info


class MetricsRegistrarHost:
    """Plugin-facing metric registration.

    Plugins call `host.metrics.counter(...)` / `host.metrics.gauge(...)`
    to register or look up a series. Names are auto-prefixed with the
    plugin's manifest name (e.g. usage-limits's "threshold_total"
    becomes "usage_limits_threshold_total") so two plugins can't
    collide on a series name. The registration call is gated on the
    `metrics.register` capability — declare it in plugin.toml or the
    call raises.

    Returned objects expose `.inc(value=1, labels=None)` for counters
    and `.set(value, labels=None)` for gauges. We don't expose the
    full _Series surface so the SDK stays forward-compatible: future
    metric types can land without breaking declared callers.
    """

    def __init__(self, plugin_name: str, gate: _CapabilityGate) -> None:
        self._plugin = plugin_name
        self._gate = gate

    def _qualified_name(self, name: str) -> str:
        """Prefix the plugin name and normalize to Prometheus-valid
        characters. `usage-limits` → `usage_limits`, then
        `usage_limits_<name>`."""
        prefix = self._plugin.replace("-", "_").replace(".", "_")
        clean = name.replace("-", "_").replace(".", "_")
        if clean.startswith(prefix + "_"):
            return clean
        return f"{prefix}_{clean}"

    def counter(self, name: str, help_text: str = "") -> _PluginCounter:
        self._gate.require("metrics.register")
        from relaydeck.metrics import registry
        series = registry().counter(self._qualified_name(name), help_text)
        return _PluginCounter(series)

    def gauge(self, name: str, help_text: str = "") -> _PluginGauge:
        self._gate.require("metrics.register")
        from relaydeck.metrics import registry
        series = registry().gauge(self._qualified_name(name), help_text)
        return _PluginGauge(series)


class _PluginCounter:
    """Plugin-facing counter handle. Forward-compatible facade over
    the internal _Series so we can grow the contract (histograms,
    summaries) without breaking existing callers."""

    def __init__(self, series: Any) -> None:
        self._series = series

    def inc(self, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        if value < 0:
            raise ValueError("counter increments must be non-negative")
        self._series.add(float(value), labels)


class _PluginGauge:
    def __init__(self, series: Any) -> None:
        self._series = series

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        self._series.set(float(value), labels)

    def inc(self, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        self._series.add(float(value), labels)


class WorkerSpawnerHost:
    def __init__(self, plugin: str, gate: _CapabilityGate) -> None:
        self._plugin = plugin
        self._gate = gate
        self._workers: list[Any] = []

    def spawn(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        interval: float = 1.0,
        config: dict[str, Any] | None = None,
        description: str = "",
        restart_policy: str | None = None,
        crash_loop_threshold: int = 5,
        crash_loop_window_s: float = 60.0,
        restart_backoff_s: float = 1.0,
    ) -> WorkerHandle:
        """Register and start a supervised background worker.

        `restart_policy` (default `"stop"`) controls what happens when a
        tick raises: `"stop"` transitions the worker to ERRORED and the
        supervisor exits (manual restart needed); `"restart"` sleeps
        `restart_backoff_s` and re-runs the target, giving up with a
        CRASH_LOOP state after `crash_loop_threshold` restarts within
        `crash_loop_window_s`. A long-lived PR/CI lifecycle worker
        usually wants `"restart"`; a one-shot reconcile wants `"stop"`.
        """
        self._gate.require("workers.spawn")
        from relaydeck.workers import RestartPolicy, register_worker

        def _target(worker: Any) -> None:
            try:
                fn(worker)
            except TypeError:
                fn()

        worker = register_worker(
            name=f"{self._plugin}.{name}",
            plugin=self._plugin,
            target=_target,
            interval_s=interval,
            config=config or {},
            description=description,
            restart_policy=restart_policy or RestartPolicy.STOP,
            crash_loop_threshold=crash_loop_threshold,
            crash_loop_window_s=crash_loop_window_s,
            restart_backoff_s=restart_backoff_s,
        )
        self._workers.append(worker)
        return WorkerHandle(id=worker.id, name=worker.name)

    def stop(self, handle: WorkerHandle) -> None:
        from relaydeck.workers import get_worker_registry

        worker = get_worker_registry().get(handle.id)
        if worker:
            worker.stop()

    def teardown(self, timeout: float = 5.0) -> None:
        for worker in list(self._workers):
            worker.stop()
        for worker in list(self._workers):
            worker.join(timeout=timeout)
        self._workers.clear()


class KVStoreHost:
    def __init__(
        self,
        plugin: str,
        config_home: Path,
        gate: _CapabilityGate,
        workspace: str | None,
    ) -> None:
        self._plugin = plugin
        self._gate = gate
        self._path = config_home / "plugin-data" / plugin
        if workspace:
            self._path = self._path / "workspaces" / workspace
        self._file = self._path / "kv.json"

    def get(self, key: str, default: Any = None) -> Any:
        self._gate.require("kv.read")
        return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._gate.require("kv.write")
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> None:
        self._gate.require("kv.write")
        data = self._load()
        data.pop(key, None)
        self._save(data)

    def list(self, prefix: str = "") -> list[tuple[str, Any]]:
        self._gate.require("kv.read")
        return [(k, v) for k, v in sorted(self._load().items()) if k.startswith(prefix)]

    def _load(self) -> dict[str, Any]:
        if not self._file.exists():
            return {}
        try:
            data = json.loads(self._file.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(data, sort_keys=True, indent=2))


class SettingsViewHost:
    def __init__(
        self,
        plugin: str,
        defaults: dict[str, Any],
        workspace_path: Path | None = None,
    ) -> None:
        self._plugin = plugin
        self._defaults = defaults
        self._workspace_path = workspace_path
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []

    def get(self, key: str) -> Any:
        from relaydeck.plugin_settings import _env_key, get_setting

        env_val = os.environ.get(_env_key(self._plugin, key))
        if env_val not in (None, ""):
            return env_val
        workspace_values = self._workspace_values()
        if key in workspace_values:
            return workspace_values[key]
        return get_setting(self._plugin, key, self._defaults.get(key))

    def all(self) -> dict[str, Any]:
        from relaydeck.plugin_settings import get_all

        values = dict(self._defaults)
        values.update(get_all(self._plugin))
        values.update(self._workspace_values())
        from relaydeck.plugin_settings import _env_key

        for key in list(values):
            env_val = os.environ.get(_env_key(self._plugin, key))
            if env_val not in (None, ""):
                values[key] = env_val
        return values

    def on_change(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    def source(self, key: str) -> str:
        from relaydeck.plugin_settings import _env_key, value_source

        if os.environ.get(_env_key(self._plugin, key)) not in (None, ""):
            return "env"
        if key in self._workspace_values():
            return "workspace"
        return value_source(self._plugin, key)

    def _workspace_values(self) -> dict[str, Any]:
        if self._workspace_path is None:
            return {}
        path = self._workspace_path / "plugin-settings.toml"
        if not path.exists():
            return {}
        try:
            import tomllib

            data = tomllib.loads(path.read_text())
        except Exception:
            return {}
        values = data.get(self._plugin) or data.get("plugin", {}).get(self._plugin) or {}
        return dict(values) if isinstance(values, dict) else {}


CommandDecorator = Callable[[Callable[..., Any]], Callable[..., Any]]


class CLIRegistrarHost:
    def __init__(self, plugin: str, gate: _CapabilityGate, top_level: bool = False) -> None:
        self._plugin = plugin
        self._gate = gate
        self._top_level = top_level
        self.commands: list[tuple[str, Callable[..., Any], dict[str, Any]]] = []

    def command(self, name: str, **attrs: Any) -> CommandDecorator:
        self._gate.require("cli.register")
        if attrs.get("top_level") and not self._top_level:
            raise CapabilityNotDeclared("top-level CLI registration requires cli.top_level")

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.commands.append((name, fn, attrs))
            return fn

        return _decorator


class APIRegistrarHost:
    def __init__(self, gate: _CapabilityGate, top_level: bool = False) -> None:
        self._gate = gate
        self._top_level = top_level
        self.routes: list[dict[str, Any]] = []

    def route(
        self,
        path: str,
        methods: list[str] | tuple[str, ...] = ("GET",),
        *,
        top_level: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a FastAPI route.

        By default the path is mounted under `/api/plugins/<plugin>/...`.
        Pass `top_level=True` to mount at the raw `path` instead — needed
        for plugins that expose well-known external URLs (webhooks,
        OAuth callbacks). Requires `top_level_api = true` in plugin.toml;
        without it the registrar raises `CapabilityNotDeclared`.
        """
        self._gate.require("api.register")
        if top_level and not self._top_level:
            raise CapabilityNotDeclared(
                "top-level API registration requires top_level_api in plugin.toml"
            )

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.routes.append({
                "path": path,
                "methods": list(methods),
                "handler": fn,
                "top_level": bool(top_level),
            })
            return fn

        return _decorator


class ViewerRegistrarHost:
    """Plugin-facing terminal-viewer registration.

    A "viewer" is anything that can take a list of running agents
    and arrange one `relaydeck attach <id>` per agent on the operator's
    screen — tmux pane grids, Ghostty windows, kitty splits,
    iTerm2 tabs. Built-in viewers (tmux, ghostty, print) live in
    `relaydeck/transports/viewers/`. Third-party viewers register here
    from a plugin's `on_load`:

        class KittyViewerPlugin(Plugin):
            def on_load(self, host: PluginHost) -> None:
                host.viewers.register(KittyViewer())

    The viewer object must implement the `TerminalViewer` protocol
    (see `relaydeck.transports.viewers.TerminalViewer`). We don't enforce
    that at import time to avoid a circular dependency between the
    SDK and the transports package; the actual call into the
    viewer's `launch()` is what surfaces protocol mismatches.

    Registration is gated on the `viewers.register` capability.
    Plugins that don't declare it get CapabilityNotDeclared at the
    call site — symmetric to every other host.X.register surface.
    """

    def __init__(self, plugin_name: str, gate: _CapabilityGate) -> None:
        self._plugin = plugin_name
        self._gate = gate
        # Tracks what this plugin registered so on_unload can
        # tear it down cleanly. Insertion order; we de-register
        # in reverse so the last-registered wins (mirrors the
        # built-in registry's "last register wins" semantics).
        self.registrations: list[Any] = []

    def register(self, viewer: Any) -> None:
        """Register `viewer` for the duration of the plugin's
        lifetime. The SDK adapter wires this into the canonical
        viewer registry at load time and removes it on unload, so
        disabling a viewer plugin makes that viewer disappear from
        `relaydeck workspace view --list-viewers` immediately."""
        self._gate.require("viewers.register")
        if not getattr(viewer, "name", ""):
            raise ValueError("viewer must have a non-empty `name` attribute")
        self.registrations.append(viewer)


class HarnessRegistrarHost:
    """Plugin-facing harness registration.

    A "harness" is a class that knows how to wrap one external CLI
    (pi, claude, codex, cursor, opencode) as a long-lived relaydeck
    agent. Plugins call `host.harnesses.register(type_name, cls)` in
    on_load. The SDK adapter binds those registrations to the
    orchestrator's `_TYPES` dict at load time and unbinds them at
    unload time, so disabling a harness plugin removes its agent
    type from `relaydeck agent create --type ...` autocomplete and from
    `known_agent_types()`.

    Registration is gated on the `harnesses.register` capability.
    Declared aliases are tracked so on_unload can release them all
    even if the plugin uses several names for the same class
    (`pi` + `pi-harness`, `codex-cli` + `codex`).
    """

    def __init__(self, plugin_name: str, gate: _CapabilityGate) -> None:
        self._plugin = plugin_name
        self._gate = gate
        # Records `(type_name, cls)` in declaration order so the
        # adapter can register on load and unregister on unload.
        self.registrations: list[tuple[str, type]] = []

    def register(self, type_name: str, cls: type) -> None:
        """Register `cls` as the agent type known by `type_name`.

        `cls` must subclass `BaseAgent`. We don't enforce that here
        to avoid a hard import of `relaydeck.agents_base` in the SDK
        module (which the harness plugins themselves import); the
        orchestrator handles bad types at spawn time. Pass the same
        class twice with different names to register aliases —
        common when shipping legacy + canonical type names.
        """
        self._gate.require("harnesses.register")
        if not isinstance(type_name, str) or not type_name.strip():
            raise ValueError("harness type_name must be a non-empty string")
        self.registrations.append((type_name, cls))


class UIRegistrarHost:
    def __init__(self, gate: _CapabilityGate) -> None:
        self._gate = gate
        self.tabs: list[dict[str, Any]] = []
        self.agent_tiles: list[dict[str, Any]] = []
        self.header_chips: list[dict[str, Any]] = []
        self.widgets: list[dict[str, Any]] = []

    def tab(self, ident: str, title: str, icon: str, module: str) -> None:
        self._gate.require("ui.register")
        self.tabs.append({"id": ident, "title": title, "icon": icon, "module": module})

    def widget(
        self,
        ident: str,
        title: str,
        module: str,
        *,
        def_w: int = 0,
        def_h: int = 0,
        min_w: int = 0,
        min_h: int = 0,
    ) -> None:
        """Register a home-dashboard widget programmatically — the parity
        counterpart to the manifest `[plugin.ui] widgets` declaration (G2).
        Size hints default to 0, which lets the dashboard pick a default.
        """
        self._gate.require("ui.register")
        self.widgets.append({
            "id": ident,
            "title": title,
            "module": module,
            "def_w": def_w,
            "def_h": def_h,
            "min_w": min_w,
            "min_h": min_h,
        })

    def agent_tile(
        self,
        ident: str,
        title: str,
        module: str,
        applies_to: list[str] | None = None,
    ) -> None:
        self._gate.require("ui.register")
        self.agent_tiles.append({
            "id": ident,
            "title": title,
            "module": module,
            "applies_to": applies_to or ["*"],
        })

    def header_chip(self, ident: str, module: str) -> None:
        self._gate.require("ui.register")
        self.header_chips.append({"id": ident, "module": module})

    def manifest(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "tabs": list(self.tabs),
            "agent_tiles": list(self.agent_tiles),
            "header_chips": list(self.header_chips),
            "widgets": list(self.widgets),
        }


class ScopedFSHost:
    def __init__(self, root: Path, gate: _CapabilityGate) -> None:
        self.root = root.resolve()
        self._gate = gate

    def read(self, rel_path: str) -> bytes:
        self._gate.require("fs.workspace")
        return self._resolve(rel_path).read_bytes()

    def write(self, rel_path: str, data: bytes) -> None:
        self._gate.require("fs.workspace")
        path = self._resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def list(self, rel_path: str = ".") -> list[Path]:
        self._gate.require("fs.workspace")
        return sorted(self._resolve(rel_path).iterdir())

    def _resolve(self, rel_path: str) -> Path:
        path = (self.root / rel_path).resolve()
        if path != self.root and self.root not in path.parents:
            raise PermissionError(f"path escapes plugin scope: {rel_path}")
        return path


# ── Model resolution + completion (module-level) ─────────────────────
# These are the reusable core of the model gateway. `ModelGatewayHost`
# wraps them with a capability gate for *plugins*; core agents (the loop
# agent's `model` action) call them directly — same pattern as the loop
# agent calling the orchestrator directly rather than through a gated
# host. Keeping the resolution logic in one place means presets, the
# provider/model split, and `role:` indirection behave identically
# everywhere a model is named. No assumed aliases — the package ships none.


def resolve_model(
    model: str, config_home: Path | None = None, *, _depth: int = 0
) -> tuple[str, str]:
    """Resolve a model name into `(provider, model_id)`.

    Order: a `role:<name>` reference resolves to the operator's default
    (or the role's built-in fallback) and re-resolves that; then a named
    preset wins; then an explicit `provider/model_id` split; then the
    built-in aliases; otherwise treat the string as an openrouter model
    id. `config_home` is threaded so preset/role lookup hits the same
    daemon root they're written to (alternate roots / RELAYDECK_CONFIG_HOME /
    tests); None falls back to the default home."""
    if model.startswith("role:"):
        # Roles indirect to a concrete spec, then re-resolve it. The
        # depth guard stops a role→role→role cycle (a hand-edited
        # model-defaults.yaml could point a role at itself).
        from relaydeck.model_roles import resolve_role

        spec = resolve_role(model[5:], config_home, require=True)
        if _depth >= 5:
            raise RuntimeError(f"model role resolution too deep at {model!r}")
        return resolve_model(spec, config_home, _depth=_depth + 1)
    try:
        from relaydeck.config import load_model_presets

        for preset in load_model_presets(config_home):
            if preset.name == model:
                return preset.provider, preset.model
    except Exception:
        pass
    if "/" in model:
        provider, model_id = model.split("/", 1)
        return provider, model_id
    # No assumed local/frontier aliases — the package ships zero model picks.
    # A bare id is interpreted as an openrouter model (the resolve endpoint
    # surfaces this as a soft warning), nothing else is assumed.
    return "openrouter", model


def complete_with_model(
    prompt: str,
    *,
    model: str = "role:fast",
    max_tokens: int = 256,
    config_home: Path | None = None,
    **kwargs: Any,
) -> str:
    """Resolve `model`, find its loaded provider, and run a completion.
    Ungated — the caller owns access control. Raises RuntimeError if the
    provider isn't loaded or can't complete. `config_home` is threaded to
    `resolve_model` so presets + role defaults hit the same daemon root
    they were written to (a `role:` model otherwise resolves against the
    process default home, ignoring operator config)."""
    provider_name, model_id = resolve_model(model, config_home)
    from relaydeck.plugin import get_provider

    provider = get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f"provider {provider_name!r} is not loaded")
    complete = getattr(provider, "complete", None)
    if complete is None:
        raise RuntimeError(f"provider {provider_name!r} does not implement completion")
    return str(complete(prompt, model=model_id, max_tokens=max_tokens, **kwargs))


def complete_with_model_ex(
    prompt: str,
    *,
    model: str = "role:fast",
    max_tokens: int = 256,
    config_home: Path | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Like `complete_with_model` but returns `(text, usage)`.

    `usage` carries `provider`, `model` (resolved), and token counts.
    Token counts are REAL when the provider implements `complete_ex`
    (e.g. ollama reports them); when it only implements `complete`, the
    tokens are 0 and `tokens_known` is False — we never fabricate counts.
    Used for per-call invocation metering (the `model` action).
    `config_home` threads to `resolve_model` (see `complete_with_model`)."""
    provider_name, model_id = resolve_model(model, config_home)
    from relaydeck.plugin import get_provider

    provider = get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f"provider {provider_name!r} is not loaded")
    ex = getattr(provider, "complete_ex", None)
    if ex is not None:
        r = ex(prompt, model=model_id, max_tokens=max_tokens, **kwargs)
        text = str(r.get("text") or "")
        reasoning = str(r.get("reasoning") or "")
        # Reasoning-only models may report tokens but leave `text` empty
        # while surfacing the answer in `reasoning` — use it as fallback
        # so worker `model` actions and agent messaging don't go blank.
        if not text.strip() and reasoning.strip():
            text = reasoning
        return text, {
            "provider": provider_name,
            "model": model_id,
            "prompt_tokens": int(r.get("prompt_tokens") or 0),
            "completion_tokens": int(r.get("completion_tokens") or 0),
            "total_tokens": int(r.get("total_tokens") or 0),
            # Honor the provider's own signal (default True for providers
            # that always report counts, e.g. ollama); openrouter sets False
            # when no usage block arrived so we never fake a confident 0.
            "tokens_known": bool(r.get("tokens_known", True)),
            "reasoning": reasoning,
        }
    complete = getattr(provider, "complete", None)
    if complete is None:
        raise RuntimeError(f"provider {provider_name!r} does not implement completion")
    text = str(complete(prompt, model=model_id, max_tokens=max_tokens, **kwargs))
    return text, {
        "provider": provider_name, "model": model_id,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "tokens_known": False,
    }


def stream_with_model(
    prompt: str,
    *,
    model: str = "role:fast",
    max_tokens: int = 256,
    on_delta=None,
    config_home: Path | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Streaming counterpart to complete_with_model_ex. When the provider
    implements `complete_stream`, `on_delta(kind, text)` fires per chunk
    ('reasoning' | 'content') and we return (text, usage). Providers without
    streaming degrade to a single non-streamed call (on_delta never fires)."""
    provider_name, model_id = resolve_model(model, config_home)
    from relaydeck.plugin import get_provider

    provider = get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f"provider {provider_name!r} is not loaded")
    cs = getattr(provider, "complete_stream", None)
    if cs is not None:
        r = cs(prompt, model=model_id, max_tokens=max_tokens, on_delta=on_delta, **kwargs)
        text = str(r.get("text") or "")
        reasoning = str(r.get("reasoning") or "")
        if not text.strip() and reasoning.strip():
            text = reasoning
        return text, {
            "provider": provider_name, "model": model_id,
            "prompt_tokens": int(r.get("prompt_tokens") or 0),
            "completion_tokens": int(r.get("completion_tokens") or 0),
            "total_tokens": int(r.get("total_tokens") or 0),
            "tokens_known": bool(r.get("tokens_known", True)),
            "reasoning": reasoning,
        }
    return complete_with_model_ex(
        prompt, model=model, max_tokens=max_tokens,
        config_home=config_home, **kwargs)


def embed_with_model(
    text: str, *, model: str = "role:embedding",
    config_home: Path | None = None,
) -> list[float]:
    """Ungated embedding counterpart to `complete_with_model`."""
    provider_name, model_id = resolve_model(model, config_home)
    from relaydeck.plugin import get_provider

    provider = get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f"provider {provider_name!r} is not loaded")
    embed = getattr(provider, "embed", None)
    if embed is None:
        raise RuntimeError(f"provider {provider_name!r} does not implement embeddings")
    return [float(value) for value in embed(text, model=model_id)]


class ModelGatewayHost:
    def __init__(self, gate: _CapabilityGate, config_home: Path) -> None:
        self._gate = gate
        self._config_home = config_home

    def complete(
        self,
        prompt: str,
        *,
        model: str = "role:fast",
        max_tokens: int = 256,
        **kwargs: Any,
    ) -> str:
        self._gate.require("models.complete")
        return complete_with_model(
            prompt, model=model, max_tokens=max_tokens,
            config_home=self._config_home, **kwargs)

    def embed(self, text: str, *, model: str = "role:embedding") -> list[float]:
        self._gate.require("models.embed")
        return embed_with_model(text, model=model, config_home=self._config_home)

    def for_role(self, role: str) -> str:
        """The effective model spec for a semantic role (e.g. "classifier",
        "voice"). Raises if the role is unknown or unconfigured with no
        fallback — declare it in `required_model_roles` so onboarding
        surfaces the gap. Pass the result to `complete`, or just call
        `complete(prompt, model="role:<name>")` directly."""
        self._gate.require("models.complete")
        from relaydeck.model_roles import resolve_role

        return resolve_role(role, self._config_home, require=True)

    def _resolve_model(self, model: str) -> tuple[str, str]:
        return resolve_model(model, self._config_home)


class VaultViewHost:
    def __init__(self, gate: _CapabilityGate, allowed_keys: Iterable[str] = ()) -> None:
        self._gate = gate
        self._allowed_keys = set(allowed_keys)

    def get(self, key: str) -> str:
        self._gate.require("vault.read")
        self._require_key(key)
        from relaydeck.vault import get_secret

        value = get_secret(key)
        if value is None:
            raise KeyError(key)
        return value

    def has(self, key: str) -> bool:
        self._gate.require("vault.read")
        self._require_key(key)
        from relaydeck.vault import get_secret

        return get_secret(key) is not None

    def set(self, key: str, value: str) -> None:
        self._gate.require("vault.write")
        self._require_key(key)
        from relaydeck.vault import set_secret

        set_secret(key, value)

    def delete(self, key: str) -> bool:
        self._gate.require("vault.delete")
        self._require_key(key)
        from relaydeck.vault import delete_secret

        return delete_secret(key)

    def _require_key(self, key: str) -> None:
        if "*" in self._allowed_keys:
            return
        if not any(fnmatchcase(key, pattern) for pattern in self._allowed_keys):
            raise CapabilityNotDeclared(
                f"Vault key {key!r} was not declared in plugin.toml needs_vault"
            )


class ChannelRegistrarHost:
    """Plugin-facing messaging-channel registration (`host.channels`).

    A "channel provider" knows how to deliver outbound messages — and,
    crucially, *interactive prompts* (button keyboards) — to one external
    messaging platform: Telegram, Discord, Slack, WhatsApp, SMS, or the
    built-in `web` dashboard. Plugins call
    `host.channels.register(provider)` in on_load; the SDK adapter binds
    those registrations into the process-wide registry in
    `relaydeck.channels` at load time and unbinds them at unload, so
    disabling a channel plugin removes it from prompt fan-out and
    `relaydeck prompt ask --to` resolution immediately.

    The provider object must satisfy the `relaydeck.channels.MessagingProvider`
    protocol (a `channel` attribute + `capabilities()` + delivery methods).
    We don't import/enforce it here to keep the SDK free of a hard
    dependency on the channels module; the registry validates `channel`
    at bind time. Registration is gated on the `channels.register`
    capability — declare it in plugin.toml or the call raises.
    """

    def __init__(self, plugin_name: str, gate: _CapabilityGate) -> None:
        self._plugin = plugin_name
        self._gate = gate
        # Declaration-order list so the adapter can register on load and
        # unregister on unload (mirrors harnesses/viewers).
        self.registrations: list[Any] = []

    def register(self, provider: Any) -> None:
        self._gate.require("channels.register")
        if not getattr(provider, "channel", ""):
            raise ValueError("channel provider must set a non-empty `channel`")
        self.registrations.append(provider)


class PromptServiceHost:
    """Plugin-facing interactive-prompt service (`host.prompts`).

    The provider-agnostic approval primitive. A plugin (or an agent via
    the bundled `prompts` plugin's CLI) calls `host.prompts.ask(...)` to
    raise a question with tap-able choices, fanned out across one or more
    messaging channels. A channel provider calls
    `host.prompts.respond(...)` when a human taps a button (or
    `respond_text` for a typed reply on a text-only channel); the first
    valid response wins the race and the decision is delivered back to the
    asking agent.

    Reads (`get`/`list`) need `prompts.read`; writes (`ask`/`respond`/
    `cancel`) need `prompts.write`. The underlying `PromptService`
    singleton is wired here with the orchestrator's `send_message_to`
    (the agent-resume primitive) and the plugin event bus (lifecycle
    events for the dashboard), so neither the service nor this host
    imports a concrete channel.
    """

    def __init__(
        self,
        gate: _CapabilityGate,
        config_home: Path,
        orchestrator: Any,
        event_bus: Any,
        workspace: str | None,
    ) -> None:
        self._gate = gate
        self._config_home = config_home
        self._orchestrator = orchestrator
        self._event_bus = event_bus
        self._workspace = workspace

    def _make_emit(self) -> Callable[[str, dict[str, Any]], None] | None:
        bus = self._event_bus
        if bus is None:
            return None

        def emit(event_type: str, data: dict[str, Any]) -> None:
            bus.emit(Event(type=event_type, data=data, source_plugin="prompts"))

        return emit

    def _service(self) -> Any:
        from relaydeck.prompts import get_service

        resume_fn = (
            getattr(self._orchestrator, "send_message_to", None)
            if self._orchestrator
            else None
        )
        return get_service(
            db_path=str(self._config_home / "runtime" / "relaydeck.db"),
            resume_fn=resume_fn,
            event_emit=self._make_emit(),
        )

    def ask(
        self,
        body: str,
        choices: list[Any],
        *,
        addresses: list[str] | None = None,
        agent_id: str = "",
        workspace: str | None = None,
        expires_in: float | None = None,
        multi: bool = False,
        notify_agent: bool = True,
    ) -> Any:
        """Raise a prompt. `choices` are `relaydeck.prompts.Choice` objects
        (or use `Choice.parse("approve:Approve:primary")`). A
        workspace-scoped host stamps its own workspace."""
        self._gate.require("prompts.write")
        ws = self._workspace if self._workspace is not None else workspace
        return self._service().ask(
            body, choices, addresses=addresses, agent_id=agent_id,
            workspace=ws, expires_in=expires_in, multi=multi,
            notify_agent=notify_agent,
        )

    def respond(
        self,
        prompt_id: str,
        choice_id: str,
        *,
        answered_by: str,
        value: str | None = None,
    ) -> Any:
        """Resolve a prompt to a choice (e.g. a Telegram callback). Returns
        the resolved prompt if this call won the race, else None."""
        self._gate.require("prompts.write")
        return self._service().respond(
            prompt_id, choice_id, answered_by=answered_by, value=value
        )

    def respond_text(self, prompt_id: str, reply: str, *, answered_by: str) -> Any:
        """Resolve via a typed reply on a text-only channel (matches the
        reply to a choice). Returns None if nothing matched."""
        self._gate.require("prompts.write")
        return self._service().respond_text(
            prompt_id, reply, answered_by=answered_by
        )

    def cancel(self, prompt_id: str, *, reason: str = "") -> Any:
        self._gate.require("prompts.write")
        return self._service().cancel(prompt_id, reason=reason)

    def sweep(self) -> list[Any]:
        """Expire overdue prompts and resume their askers with a timeout
        signal. Idempotent; meant to be driven on a timer (the prompts
        plugin spawns a worker for it). Returns the prompts it expired."""
        self._gate.require("prompts.write")
        return self._service().sweep_expired()

    def get(self, prompt_id: str) -> Any:
        self._gate.require("prompts.read")
        from relaydeck.prompts import get_prompt

        return get_prompt(
            prompt_id, db_path=str(self._config_home / "runtime" / "relaydeck.db")
        )

    def list(
        self,
        *,
        state: str | None = None,
        agent_id: str | None = None,
        workspace: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        self._gate.require("prompts.read")
        from relaydeck.prompts import list_prompts

        ws = self._workspace if self._workspace is not None else workspace
        return list_prompts(
            state=state, agent_id=agent_id, workspace=ws, limit=limit,
            db_path=str(self._config_home / "runtime" / "relaydeck.db"),
        )


class PluginHost:
    api_version = API_VERSION

    def __init__(
        self,
        *,
        name: str,
        config_home: Path,
        declared_capabilities: Iterable[str],
        event_bus: Any,
        orchestrator: Any = None,
        workspace: str | None = None,
        workspace_path: Path | None = None,
        settings_defaults: dict[str, Any] | None = None,
        top_level_cli: bool = False,
        top_level_api: bool = False,
        vault_keys: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.workspace = workspace
        self.config_home = config_home
        # Keep the orchestrator reachable from inside plugins that need
        # to read spec config (per-agent overrides, etc.). Plugins
        # SHOULD prefer the public `host.agents.*` surface; this
        # attribute exists for the long-tail cases the agents-host
        # methods don't cover yet. Marked private-ish to discourage
        # casual use.
        self._orchestrator = orchestrator
        self._gate = _CapabilityGate(declared_capabilities)
        self.events = EventBusHost(name, event_bus, self._gate)
        self.agents = (
            AgentRegistryHost(orchestrator, self._gate, workspace)
            if orchestrator
            else None
        )
        self.workers = WorkerSpawnerHost(name, self._gate)
        self.tasks = TaskStoreHost(
            name, self._gate, workspace, str(config_home / "runtime" / "relaydeck.db")
        )
        self.workspaces = WorkspaceRegistryHost(self._gate, config_home, event_bus)
        self.metrics = MetricsRegistrarHost(name, self._gate)
        self.models = ModelGatewayHost(self._gate, config_home)
        self.kv = KVStoreHost(name, config_home, self._gate, workspace)
        self.settings = SettingsViewHost(name, settings_defaults or {}, workspace_path)
        self.cli = CLIRegistrarHost(name, self._gate, top_level=top_level_cli)
        self.api = APIRegistrarHost(self._gate, top_level=top_level_api)
        self.harnesses = HarnessRegistrarHost(name, self._gate)
        self.viewers = ViewerRegistrarHost(name, self._gate)
        self.channels = ChannelRegistrarHost(name, self._gate)
        self.prompts = PromptServiceHost(
            self._gate, config_home, orchestrator, event_bus, workspace
        )
        self.ui = UIRegistrarHost(self._gate)
        fs_root = workspace_path or config_home / "plugin-data" / name
        self.fs = ScopedFSHost(fs_root, self._gate)
        self.log = logging.getLogger(f"relaydeck.plugin.{name}")
        self.vault = VaultViewHost(self._gate, vault_keys)

    def teardown(self) -> None:
        self.events.teardown()
        self.workers.teardown()


class RemoteHost:
    api_version = API_VERSION

    def __init__(
        self,
        daemon_url: str,
        token: str | None = None,
        *,
        verify: bool | str = True,
    ) -> None:
        """Connect to a remote daemon.

        `verify` controls TLS verification for `https://` URLs:
          - `True`  → use the system trust store (production certs).
          - `False` → disable verification entirely (NOT recommended;
                      use the explicit cert path for self-signed dev).
          - `str`   → path to a CA cert file to verify against.
                      `RemoteHost.from_local()` picks this up from
                      `state.yaml.daemon_ca` so the dev self-signed
                      path verifies pinned-by-default.
        """
        self.daemon_url = daemon_url.rstrip("/")
        self.token = token
        self.verify = verify
        self.agents = _RemoteAgents(self)
        self.models = _RemoteModels(self)
        self.plugins = _RemotePlugins(self)

    @classmethod
    def connect(
        cls,
        daemon_url: str,
        token: str | None = None,
        *,
        verify: bool | str = True,
    ) -> RemoteHost:
        return cls(daemon_url, token=token, verify=verify)

    @classmethod
    def from_local(cls) -> RemoteHost:
        """Connect to the local daemon using ambient state.yaml + auth-token.

        Mirrors the CLI's resolution: `RELAYDECK_DAEMON_URL` → state.yaml →
        default localhost; `RELAYDECK_AUTH_TOKEN` → auth-token file;
        `RELAYDECK_DAEMON_CA` or `state.yaml.daemon_ca` → cert pin for
        the dev self-signed path. Raises `RuntimeError` if no token
        is configured (daemon never booted on this machine).
        """
        from relaydeck.auth import read_token
        from relaydeck.state import get_daemon_ca, get_daemon_url

        tok = read_token()
        if not tok:
            raise RuntimeError(
                "no daemon token on disk — run `relaydeck serve` first or set RELAYDECK_AUTH_TOKEN"
            )
        ca = get_daemon_ca()
        verify: bool | str = ca if ca else True
        return cls(get_daemon_url(), token=tok, verify=verify)

    def _ssl_context(self) -> Any:
        """Build the ssl context for urllib. None means urllib uses its
        default behavior (system trust). For `verify=False` we return
        an unverified context; for a CA path we load it explicitly."""
        if not self.daemon_url.startswith("https://"):
            return None
        import ssl
        if self.verify is True:
            return ssl.create_default_context()
        if self.verify is False:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context(cafile=str(self.verify))

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.daemon_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(
                req, timeout=15, context=self._ssl_context(),
            ) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {text}") from exc


class _RemoteAgents:
    def __init__(self, host: RemoteHost) -> None:
        self._host = host

    def list(self) -> list[AgentInfo]:
        return [AgentInfo.from_dict(row) for row in self._host._request("/api/agents")]

    def get(self, agent_id: str) -> AgentInfo | None:
        try:
            return AgentInfo.from_dict(self._host._request(f"/api/agents/{agent_id}"))
        except RuntimeError:
            return None

    def send_message(
        self,
        to: str,
        body: str,
        *,
        workspace: str,
        from_: str = "user",
    ) -> SendResult:
        payload = self._host._request(
            f"/api/workspaces/{workspace}/messages",
            method="POST",
            body={"agent": to, "body": body, "from": from_},
        )
        return SendResult.from_dict(payload)


class _RemoteModels:
    def __init__(self, host: RemoteHost) -> None:
        self._host = host

    def providers(self) -> list[dict[str, Any]]:
        return list(self._host._request("/api/providers"))

    def list(self, provider: str) -> list[dict[str, Any]]:
        return list(self._host._request(f"/api/providers/{provider}/models"))

    def refresh(self, provider: str) -> dict[str, Any]:
        return dict(self._host._request(f"/api/providers/{provider}/refresh", method="POST"))


class _RemotePlugins:
    def __init__(self, host: RemoteHost) -> None:
        self._host = host

    def list(self) -> list[dict[str, Any]]:
        return list(self._host._request("/api/plugins"))

    def settings(self, name: str) -> dict[str, Any]:
        return dict(self._host._request(f"/api/plugins/{name}/settings"))

    def set_settings(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self._host._request(
                f"/api/plugins/{name}/settings",
                method="POST",
                body=values,
            )
        )

    def enable(self, name: str) -> dict[str, Any]:
        return dict(self._host._request(f"/api/plugins/{name}/enable", method="POST"))

    def disable(self, name: str) -> dict[str, Any]:
        return dict(self._host._request(f"/api/plugins/{name}/disable", method="POST"))

    def install(self, source: str, *, editable: bool = False) -> dict[str, Any]:
        """Install a plugin through the daemon's package environment.

        Mirrors Settings -> Plugins and `relaydeck plugin install`. Package,
        wheel, git, and local paths are resolved by the daemon process, so
        local paths must be visible to the daemon host.
        """
        return dict(
            self._host._request(
                "/api/plugins/install",
                method="POST",
                body={"source": source, "editable": editable},
            )
        )

    def uninstall(self, name: str) -> dict[str, Any]:
        """Uninstall a user-installed plugin by name."""
        return dict(self._host._request(f"/api/plugins/{name}", method="DELETE"))


type Host = PluginHost | RemoteHost


def open_host(daemon_url: str, token: str | None = None) -> RemoteHost:
    return RemoteHost.connect(daemon_url, token=token)
