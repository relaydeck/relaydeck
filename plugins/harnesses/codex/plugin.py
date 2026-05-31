"""Codex CLI harness plugin (OpenAI Codex)."""

from __future__ import annotations

from plugins.harnesses.codex.agent import CodexAgent
from relaydeck.sdk import Plugin, PluginHost


class CodexHarnessPlugin(Plugin):
    """Registers `codex` + `codex-cli` as orchestrator agent types."""

    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("codex", CodexAgent)
        host.harnesses.register("codex-cli", CodexAgent)


PLUGIN = CodexHarnessPlugin()
