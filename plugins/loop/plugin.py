"""
Loop agent plugin — registers the `loop` agent type.

Mirrors the harness pattern (pi, codex, claude-code) on purpose:
one `Plugin.on_load` that hands the agent class to the host's
harness registrar. Disable-aware: unloading the plugin removes
`loop` from `known_agent_types()` immediately.

The loop agent itself doesn't wrap an external CLI like the
harnesses do — its `run()` is a scheduled action dispatcher. But
from the orchestrator's point of view it is still an agent type
that gets spawned, lives in a thread, and reports events. So
registering it through the harness registrar is the right shape;
the "harness" naming is wider than its origin.
"""

from __future__ import annotations

from relaydeck.plugin import PluginContext, PluginEventBus
from plugins.loop.agent import LoopAgent
from relaydeck.sdk import Plugin, PluginHost

PLUGIN_NAME = "loop"


class LoopPlugin(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("loop", LoopAgent)


PLUGIN = LoopPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim mirroring the pi-harness pattern. Tests that
    build a PluginContext by hand can use this to wire the agent
    type without going through the full SDK adapter."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "harnesses.register", "events.subscribe",
            "events.emit", "agents.send",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
    from relaydeck.orchestrator import register_agent_type
    for type_name, cls in host.harnesses.registrations:
        register_agent_type(type_name, cls)
