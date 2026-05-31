"""Claude Code harness plugin.

Wraps Anthropic's `claude` CLI via ClaudeCodeAgent. The harness is
minimal -- most of the work happens in the user's interactive
session -- but we pass `--append-system-prompt` so the spec-level
system_prompt + identity preamble reach the model.
"""

from __future__ import annotations

from plugins.harnesses.claude_code.agent import ClaudeCodeAgent
from relaydeck.sdk import Plugin, PluginHost


class ClaudeCodeHarnessPlugin(Plugin):
    """Registers `claude` + `claude-code` as orchestrator agent types."""

    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("claude", ClaudeCodeAgent)
        host.harnesses.register("claude-code", ClaudeCodeAgent)


PLUGIN = ClaudeCodeHarnessPlugin()
