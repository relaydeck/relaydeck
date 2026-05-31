"""Cursor CLI harness plugin (cursor-agent)."""

from __future__ import annotations

from plugins.harnesses.cursor.agent import CursorAgent
from relaydeck.sdk import Plugin, PluginHost


class CursorHarnessPlugin(Plugin):
    """Registers `cursor`, `cursor-cli`, and `cursor-agent` as agent types."""

    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("cursor-cli", CursorAgent)
        host.harnesses.register("cursor", CursorAgent)
        host.harnesses.register("cursor-agent", CursorAgent)


PLUGIN = CursorHarnessPlugin()
