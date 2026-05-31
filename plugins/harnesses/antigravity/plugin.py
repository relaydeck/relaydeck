"""Google Antigravity CLI harness plugin (`agy`)."""

from __future__ import annotations

from plugins.harnesses.antigravity.agent import AntigravityAgent
from relaydeck.sdk import Plugin, PluginHost


class AntigravityHarnessPlugin(Plugin):
    """Registers `antigravity` (and the `agy` alias) as agent types."""

    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("antigravity", AntigravityAgent)
        host.harnesses.register("agy", AntigravityAgent)


PLUGIN = AntigravityHarnessPlugin()
