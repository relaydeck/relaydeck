"""Testing helpers for third-party relaydeck plugins.

Two test surfaces live here:

  * `MockHost` — for new-style SDK plugins (`relaydeck.sdk.Plugin`,
    `relaydeck.sdk.PluginHost`). Wires an in-memory orchestrator, kv,
    settings, vault, models, and a `PluginEventBus`. Used by the
    `relaydeck plugin scaffold` template.
  * `MockContext` + `MockBus` + `make_plugin` — for legacy
    `relaydeck.plugin.RelaydeckPlugin` plugins. Lighter than MockHost: just
    enough to wire `on_load(ctx)` and observe emits.

Pick the surface that matches your plugin base class. Both keep
filesystem state under `tmp_path` and event state in memory, so
tests are hermetic without monkeypatching the daemon's globals.

Worked example for the legacy path:

```python
from relaydeck.plugin import EventSubscription, RelaydeckPlugin
from relaydeck.testing import MockBus, MockContext, make_plugin

class MyPlugin(RelaydeckPlugin):
    name = "my-plugin"
    def get_subscriptions(self):
        return [EventSubscription("agent.*", "_on_agent")]
    def _on_agent(self, event):
        self.last = event

def test_handler_receives_agent_events(tmp_path):
    bus = MockBus()
    p = make_plugin(MyPlugin, ctx=MockContext(tmp_path, bus=bus))
    bus.emit_event("agent.start", {"id": "demo"})
    assert p.last.data["id"] == "demo"
    assert bus.assert_emitted("agent.start") is True
```
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from relaydeck.plugin import (
    Event,
    PluginContext,
    PluginEventBus,
    RelaydeckPlugin,
)
from relaydeck.sdk import PluginHost


class _MemoryOrchestrator:
    def __init__(self) -> None:
        self._agents: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def add_agent(self, **agent: Any) -> None:
        self._agents.append(agent)

    def list_agents(self) -> list[dict[str, Any]]:
        return list(self._agents)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return next((a for a in self._agents if a.get("id") == agent_id), None)

    def find_agents(
        self,
        *,
        tag: str | None = None,
        purpose: str | None = None,
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self._agents)
        if workspace:
            rows = [r for r in rows if r.get("workspace") == workspace]
        if tag:
            rows = [r for r in rows if tag in (r.get("tags") or [])]
        if purpose:
            rows = [r for r in rows if purpose.lower() in (r.get("purpose") or "").lower()]
        return rows

    def send_message_to(self, to_id: str, body: str, **kwargs: Any) -> tuple[str, bool]:
        msg_id = f"mock_msg_{len(self.sent) + 1}"
        self.sent.append({"id": msg_id, "to": to_id, "body": body, **kwargs})
        return msg_id, True

    def create_agent(
        self,
        agent_id: str,
        agent_type: str,
        name: str,
        *,
        workspace: str | None = None,
        config: dict[str, Any] | None = None,
        auto_start: bool = False,
        purpose: str = "",
        tags: list[str] | None = None,
        system_prompt: str = "",
        inject_identity_preamble: bool = True,
    ) -> str:
        del system_prompt, inject_identity_preamble  # not surfaced in AgentInfo
        self._agents.append({
            "id": agent_id,
            "type": agent_type,
            "name": name,
            "workspace": workspace,
            "status": "stopped",
            "purpose": purpose,
            "tags": list(tags or []),
            "auto_start": auto_start,
            "config": config or {},
        })
        return agent_id

    def start_agent(self, agent_id: str) -> None:
        for a in self._agents:
            if a.get("id") == agent_id:
                a["status"] = "running"
        return None

    def delete_agent(self, agent_id: str) -> bool:
        before = len(self._agents)
        self._agents = [a for a in self._agents if a.get("id") != agent_id]
        return len(self._agents) < before


# The broad capability set a `MockHost()` grants by default so most plugin
# tests "just work". Pass `declared_capabilities=set()` to refuse everything
# (negative tests); pass an explicit subset to test partial grants.
_DEFAULT_MOCK_CAPABILITIES = frozenset({
    "events.subscribe",
    "events.emit",
    "agents.list",
    "agents.send",
    "agents.create",
    "agents.start",
    "agents.stop",
    "tasks.read",
    "tasks.write",
    "workspaces.read",
    "workspaces.create",
    "workspaces.update",
    "workers.spawn",
    "kv.read",
    "kv.write",
    "ui.register",
    "api.register",
    "cli.register",
    "harnesses.register",
    "fs.workspace",
    "vault.read",
    "vault.write",
    "vault.delete",
})


class MockHost(PluginHost):
    """In-memory PluginHost implementation for plugin unit tests.

    By default a `MockHost()` grants `_DEFAULT_MOCK_CAPABILITIES` so most
    plugin tests work without ceremony. The `declared_capabilities` argument
    uses a `None` sentinel — pass an explicit empty set to refuse every
    capability (the right shape for "assert my plugin is refused" tests),
    or a subset to test partial grants.
    """

    def __init__(
        self,
        *,
        name: str = "test-plugin",
        workspace: str | None = None,
        workspace_path: Path | None = None,
        declared_capabilities: Iterable[str] | None = None,
        vault_keys: Iterable[str] | None = None,
        config_home: Path | None = None,
    ) -> None:
        self.memory_orchestrator = _MemoryOrchestrator()
        super().__init__(
            name=name,
            config_home=config_home or Path(tempfile.mkdtemp(prefix="relaydeck-mockhost-")),
            workspace=workspace,
            workspace_path=workspace_path,
            # Sentinel default (G4): only fall back to the broad set when the
            # caller passed nothing. An explicit empty set is honored so the
            # "my plugin is refused with no capabilities" test actually refuses
            # instead of silently inheriting everything.
            declared_capabilities=_DEFAULT_MOCK_CAPABILITIES
            if declared_capabilities is None
            else declared_capabilities,
            event_bus=PluginEventBus(),
            orchestrator=self.memory_orchestrator,
            vault_keys=("*",) if vault_keys is None else vault_keys,
        )

    def add_agent(self, **agent: Any) -> None:
        self.memory_orchestrator.add_agent(**agent)


# ── Legacy-plugin helpers (relaydeck.plugin.RelaydeckPlugin) ──────────────────


class MockBus(PluginEventBus):
    """`PluginEventBus` plus test-friendly inspection helpers.

    Behaves identically to the production bus for emit/subscribe so
    plugins under test see the same semantics — the additions are
    `emit_event(type, data)` (one-liner for the common shape) and
    `assert_emitted(type, *, contains)` for the matching assertion.
    `events` is the in-order list of every emit (including events
    emitted in handlers), useful when an ordering bug needs pinning.
    """

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        super().__init__(db_path=db_path)
        self.events: list[Event] = []
        # Wire a catch-all subscriber so `events` mirrors emit order
        # (the bus's own `_history` is bounded; we keep an unbounded
        # mirror because tests inspect tail behavior).
        self.subscribe("*", self.events.append)

    def emit_event(
        self,
        type: str,
        data: dict[str, Any] | None = None,
        *,
        source_plugin: str = "test",
    ) -> None:
        """Convenience wrapper: build the Event and emit it.
        Equivalent to `bus.emit(Event(type=..., data=..., source_plugin=...))`."""
        self.emit(Event(type=type, data=dict(data or {}), source_plugin=source_plugin))

    def assert_emitted(
        self,
        type: str,
        *,
        contains: dict[str, Any] | None = None,
    ) -> bool:
        """Return True iff at least one emitted event matched `type`
        AND (if `contains` is given) every key in `contains` is present
        in the event's `data` with the same value. Subset match — extra
        keys in the event are fine."""
        for e in self.events:
            if e.type != type:
                continue
            if contains is None:
                return True
            if all(e.data.get(k) == v for k, v in contains.items()):
                return True
        return False


class MockContext(PluginContext):
    """`PluginContext` with sensible test defaults.

    Creates a `tmp_path/.relaydeck` config home (using the path
    passed in directly when it already looks like that), attaches a
    `MockBus` (or the one you provide), and leaves `orchestrator` as
    None — most plugins under test don't need it. The runtime
    directory is materialized eagerly so plugins that write under
    `runtime/<their-name>/` don't need to mkdir guards.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        bus: PluginEventBus | None = None,
        workspace_path: Path | None = None,
        orchestrator: Any = None,
    ) -> None:
        cfg_home = (
            tmp_path
            if tmp_path.name == ".relaydeck"
            else tmp_path / ".relaydeck"
        )
        (cfg_home / "runtime").mkdir(parents=True, exist_ok=True)
        super().__init__(
            config_home=cfg_home,
            workspace_path=workspace_path,
            orchestrator=orchestrator,
            db_path=str(cfg_home / "runtime" / "relaydeck.db"),
            event_bus=bus or MockBus(),
        )


def make_plugin(
    plugin_cls: type[RelaydeckPlugin],
    *,
    ctx: PluginContext | None = None,
    tmp_path: Path | None = None,
    wire_subscriptions: bool = True,
) -> RelaydeckPlugin:
    """Instantiate a plugin, call its `on_load`, and wire its
    `get_subscriptions()` declarations to the context's bus.

    Either `ctx` OR `tmp_path` must be supplied — if only `tmp_path`,
    we build a `MockContext` for it. Returns the live plugin instance
    so the test can poke at its state.

    `wire_subscriptions=False` skips the subscription wiring for tests
    that drive handlers directly and don't want the bus echo. The
    behavior otherwise matches `PluginRegistry._wire_subscriptions`:
    each `EventSubscription.handler` name is looked up on the
    instance; missing handlers are skipped silently.
    """
    if ctx is None:
        if tmp_path is None:
            tmp_path = Path(tempfile.mkdtemp(prefix="relaydeck-makeplugin-"))
        ctx = MockContext(tmp_path)
    plugin = plugin_cls()
    plugin.on_load(ctx)

    if wire_subscriptions and ctx.event_bus is not None:
        for sub in plugin.get_subscriptions():
            handler = getattr(plugin, sub.handler, None)
            if not handler:
                continue
            if sub.durable and getattr(ctx.event_bus, "_db_path", None):
                ctx.event_bus.subscribe_durable(
                    sub.pattern, handler,
                    key=sub.key or f"{plugin.name}.{sub.handler}",
                )
            else:
                ctx.event_bus.subscribe(sub.pattern, handler)

    return plugin


def harness_delivery_blob(agent) -> str:
    """Resolve the full model-visible injection blob a harness delivers at spawn.

    Walks each harness's actual delivery shape (``--append-system-prompt``,
    ``--skill`` dirs, ``--plugin-dir`` skills trees, codex
    ``developer_instructions``, opencode ``OPENCODE_CONFIG`` instruction paths)
    so unit + e2e tests can grep for skill names and preamble markers in one
    place.
    """
    env: dict = {}
    if hasattr(agent, "_build_env"):
        try:
            built = agent._build_env()
            if isinstance(built, dict):
                env = built
        except Exception:
            pass

    opencfg = env.get("OPENCODE_CONFIG")
    if opencfg:
        cfg = json.loads(Path(opencfg).read_text())
        parts: list[str] = []
        for p in cfg.get("instructions") or []:
            parts.append(p)
            if Path(p).exists():
                parts.append(Path(p).read_text())
        return "\n".join(parts)

    cmd = agent._build_command()
    blob = " ".join(cmd)
    for i, part in enumerate(cmd):
        if part == "--config" and i + 1 < len(cmd):
            cfg_part = cmd[i + 1]
            if cfg_part.startswith("developer_instructions="):
                blob += "\n" + cfg_part.split("=", 1)[1]
        if part == "--skill" and i + 1 < len(cmd):
            sm = Path(cmd[i + 1]) / "SKILL.md"
            if sm.exists():
                blob += "\n" + sm.read_text()
        if part == "--plugin-dir" and i + 1 < len(cmd):
            sk = Path(cmd[i + 1]) / "skills"
            if sk.exists():
                for d in sorted(sk.iterdir()):
                    blob += "\n" + d.name
                    sm = d / "SKILL.md"
                    if sm.exists():
                        blob += "\n" + sm.read_text()
        if part == "--append-system-prompt" and i + 1 < len(cmd):
            blob += "\n" + cmd[i + 1]
    return blob


__all__ = [
    "MockBus",
    "MockContext",
    "MockHost",
    "harness_delivery_blob",
    "make_plugin",
]
