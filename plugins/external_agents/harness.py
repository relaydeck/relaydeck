"""
Harnesses that run a linked external runtime's interactive chat TUI as a
relaydeck agent.

The `external` plugin observes Hermes/OpenClaw read-only — but once a
runtime is linked, the most useful next step is to actually *use* it: drop
an instance of its chat TUI into any workspace, live in the dashboard
terminal tile alongside your pi/claude agents (attach, watch, message it).

relaydeck already wraps CLIs (pi, claude-code, codex) as PTY-backed
`HarnessAgent`s, so the external runtime's CLI is just another harness. We
spawn `<cli> <subcommand>` in the *workspace's* directory (so the runtime
operates on that workspace) using the runtime's OWN config/auth home
(`~/.hermes`, `~/.openclaw`). We never copy secrets or mutate the external
config — relaydeck only runs the binary.

Unlike the native harnesses this deliberately does NOT inject relaydeck's
model preset or `--append-system-prompt`: Hermes/OpenClaw own their model
selection + prompting, and those flags differ per runtime. The command is
just `<CLI> <DEFAULT_ARGS>` plus optional per-agent `config.command` (full
override) / `config.args` (additive) — so an operator can tune it
(`-m`, `--continue`, `--worktree`, …) without code changes.
"""

from __future__ import annotations

import shlex

from relaydeck.harness import HarnessAgent


class ExternalChatAgent(HarnessAgent):
    """Base harness for an external runtime's interactive chat CLI."""

    CLI = ""  # set by subclass
    DEFAULT_ARGS: list[str] = []

    def _build_command(self) -> list[str]:
        cmd = [self.CLI] + list(self.DEFAULT_ARGS)
        user_cmd = self.config.get("command")
        if isinstance(user_cmd, list):
            cmd = [str(a) for a in user_cmd]
        elif isinstance(user_cmd, str) and user_cmd.strip():
            cmd = shlex.split(user_cmd)
        extra = self.config.get("args")
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
        elif isinstance(extra, str) and extra.strip():
            cmd.extend(shlex.split(extra))
        return cmd


class HermesChatAgent(ExternalChatAgent):
    """Hermes Agent — `hermes chat` (verified: "Interactive chat with the agent")."""

    CLI = "hermes"
    DEFAULT_ARGS = ["chat"]


class OpenClawChatAgent(ExternalChatAgent):
    """OpenClaw — bare `openclaw` launches its TUI.

    The exact chat subcommand is best-effort (OpenClaw needs Node >= 22.19;
    if the local node is older the CLI exits with an upgrade hint, which the
    operator will see in the terminal tile). Override per-agent with
    `config.args` / `config.command` if the invocation differs.
    """

    CLI = "openclaw"
    DEFAULT_ARGS: list[str] = []


# External-agent kind -> registered relaydeck harness type name.
KIND_TO_TYPE: dict[str, str] = {
    "hermes": "hermes",
    "openclaw": "openclaw",
}

# Harness type name -> class, registered by the plugin's on_load.
HARNESS_TYPES: dict[str, type[ExternalChatAgent]] = {
    "hermes": HermesChatAgent,
    "openclaw": OpenClawChatAgent,
}
