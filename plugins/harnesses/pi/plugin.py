"""
Pi harness plugin — registers the PiAgent and CLI extensions.

`host.harnesses.register(type_name, cls)` binds the agent type to the
orchestrator and unbinds it on `on_unload`, so disabling this plugin makes
`pi` stop appearing in `known_agent_types()` immediately. Declared
capability: `harnesses.register`.
"""

from __future__ import annotations

from plugins.harnesses.pi.agent import PiAgent
from relaydeck.sdk import Plugin, PluginContext, PluginEventBus, PluginHost

PLUGIN_NAME = "pi-harness"


class PiHarnessPlugin(Plugin):
    """Registers `pi` + `pi-harness` as orchestrator agent types.

    The actual harness lives in `plugins/harnesses/pi/agent.py`
    (PiAgent) — this file is just the registration shim. Both
    type-name aliases point at the same class. `pi` is the canonical
    short name used in `relaydeck agent create --type pi`; `pi-harness`
    is the legacy form retained for back-compat with existing
    `agents/<id>.yaml` files.
    """

    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("pi", PiAgent)
        host.harnesses.register("pi-harness", PiAgent)


PLUGIN = PiHarnessPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim. The real loader path is the SDK adapter; this
    helper exists for the small number of tests that build a
    PluginContext by hand. The shim short-circuits to the
    orchestrator directly because the adapter's bind step doesn't
    run for legacy callers."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=["harnesses.register", "agents.list"],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
    # Mirror what HostPluginAdapter would do — register the types
    # directly with the orchestrator so the legacy-load path
    # actually makes `pi` spawnable.
    from relaydeck.orchestrator import register_agent_type
    for type_name, cls in host.harnesses.registrations:
        register_agent_type(type_name, cls)
