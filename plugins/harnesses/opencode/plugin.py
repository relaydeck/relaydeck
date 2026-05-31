"""OpenCode harness plugin."""

from __future__ import annotations

from plugins.harnesses.opencode.agent import OpenCodeAgent
from relaydeck.sdk import Plugin, PluginHost

PLUGIN_NAME = "opencode-harness"


class OpenCodeHarnessPlugin(Plugin):
    """Registers `opencode` + `opencode-cli` as orchestrator agent types."""

    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("opencode", OpenCodeAgent)
        host.harnesses.register("opencode-cli", OpenCodeAgent)


PLUGIN = OpenCodeHarnessPlugin()
