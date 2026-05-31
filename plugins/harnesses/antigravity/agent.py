"""
Google Antigravity harness — wraps the `agy` CLI (Antigravity 2.0's terminal
agent, Google's successor to Gemini CLI).

Closest sibling is the **cursor** harness: `agy` authenticates via the
operator's own Google account (browser OAuth, cached in the system keyring)
rather than relaydeck-injected provider keys, runs a full-screen Go TUI, and
exposes no tail-able per-call token record — so this adapter mirrors cursor's
shape:

  - identity preamble + plugin context + skills delivered as the initial turn
    via `--prompt-interactive` (newlines preserved), which keeps the session
    interactive for relaydeck's follow-up PTY messages
  - autonomy posture mapped onto `agy`'s permission flags
  - account-default model (no model flag — see below)
  - no per-call token metering (honest 0 / $0.00) and, for the same
    "no structured assistant turn" reason, no `harness.assistant_message`

Flag surface VERIFIED against agy 1.0.3 (`agy --help`):
  - `--prompt-interactive` / `-i`  initial prompt + continue the session (the
    PTY-harness injection point). `-p` / `--print` is one-shot — it prints and
    EXITS, which would kill a long-lived agent, so we never use it for spawn.
  - `--dangerously-skip-permissions`  auto-approve all tool requests.
  - `--sandbox`  terminal-restricted sandbox.
  - `--continue` / `-c`, `--conversation <id>`  resume a prior conversation.
  - NO `--model` flag: the model is the Google-account default, switched
    in-session via the `/model` slash-command. relaydeck offers no model picker
    for agy (it would mislead — there's no flag to pass it to).
  - NO API-key env: auth is the operator's `agy` login (Google OAuth/keyring),
    same trust model as cursor's `~/.cursor`.

Unattended autonomy note: `agy` has no safe-vs-dangerous classifier and no
per-command allowlist (unlike claude/cursor). A relaydeck agent has no human at
the TUI to answer an approval prompt, so `auto` and `bypass` both pass
`--dangerously-skip-permissions` (the only way to keep the run from blocking
forever) — which also keeps the `relaydeck` CLI runnable for peer messaging.
`locked` injects nothing extra (no allowlist to lock to); the operator accepts
it may block on a prompt. `manual` injects nothing. Pinned in
`tests/test_antigravity_harness.py`.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from relaydeck.harness import HarnessAgent

logger = logging.getLogger(__name__)

# agy's documented "auto-approve every tool" flag. With it set, all tools
# (including the `relaydeck` CLI for peer messaging) run without prompting —
# the only unattended-safe posture, since agy has no per-command allowlist.
_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"


class AntigravityAgent(HarnessAgent):
    """Wraps the Antigravity CLI (`agy`)."""

    CLI = "agy"
    HARNESS_TYPE = "antigravity"
    DEFAULT_ARGS: list[str] = []
    # Antigravity prompts for workspace trust + tool approval.
    AWAITING_INPUT_PATTERNS = HarnessAgent.AWAITING_INPUT_PATTERNS + (
        r"\btrust (?:this )?(?:folder|workspace)\b.*\?",
        r"\ballow\b.*\?\s*$",
    )

    # agy runs a full-screen Go TUI; submit the body and Enter as two separate
    # writes so a paste-debouncing input widget can't swallow the trailing CR
    # (same failure class as Claude Code's Ink / cursor). Override per-agent
    # via config submit_split / submit_delay_s.
    SUBMIT_SPLIT = True
    SUBMIT_DELAY_S = 0.08

    _PROMPT_INJECTORS: dict[str, str] = {
        "fleet-context": "_inject_fleet_context",
        "skills": "_inject_skills",
        "forbidden-tools": "_inject_forbidden_tools",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config_home = Path.home() / ".relaydeck"

    # -- Workspace-trust pre-seed ------------------------------------------
    #
    # agy prompts "Do you trust the contents of this project?" on first run in
    # a directory and BLOCKS until a human answers — fatal for an unattended
    # agent (no one to press "Yes"). `--dangerously-skip-permissions` does NOT
    # suppress it. agy records trusted dirs in a global settings file as
    # `trustedWorkspaces: [<abs path>, ...]`; pre-seeding the agent's cwd there
    # makes it boot straight into work. Same shape as the cursor harness's
    # workspace-trust pre-seed. Skipped for `manual` autonomy (operator drives).

    def _agy_settings_path(self) -> Path:
        # agy stores CLI state under ~/.gemini/antigravity-cli (verified on
        # agy 1.0.3); allow an env override if a future build documents one.
        base = os.environ.get("ANTIGRAVITY_CLI_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".gemini" / "antigravity-cli"
        return root / "settings.json"

    def _ensure_workspace_trusted(self) -> None:
        if self._autonomy_mode() == "manual":
            return
        cwd = self._get_cwd()
        if not cwd:
            return
        # agy keys trust by the *resolved* absolute path (it prints /private/tmp
        # on macOS); seed both raw + resolved so either match hits.
        wants = {str(Path(cwd)), str(Path(cwd).resolve())}
        path = self._agy_settings_path()
        try:
            data: dict[str, Any] = {}
            if path.exists():
                with suppress(json.JSONDecodeError, OSError):
                    data = json.loads(path.read_text()) or {}
            trusted = data.get("trustedWorkspaces")
            trusted = list(trusted) if isinstance(trusted, list) else []
            if wants.issubset(set(trusted)):
                return
            for w in wants:
                if w not in trusted:
                    trusted.append(w)
            data["trustedWorkspaces"] = trusted
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        except OSError as exc:  # never block a spawn on trust pre-seed
            logger.debug("agy trust pre-seed skipped for %s: %s", self.agent_id, exc)

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        self._ensure_workspace_trusted()
        return env

    # -- Command -------------------------------------------------------------

    def _build_command(self) -> list[str]:
        cmd = [self.CLI]

        # agy has NO `--model` flag (verified against agy 1.0.3 `--help`): the
        # model is the operator's Google-account default, switchable in-session
        # via the `/model` slash-command. So relaydeck passes no model flag.

        # Autonomy → permission flags. See the module docstring: auto/bypass
        # must skip permission prompts (no human to answer them); locked/manual
        # add nothing. An operator who passes their own flag via config.args is
        # respected — we don't double-add.
        mode = self._autonomy_mode()
        op_args = self._raw_args_tokens()
        already_skips = any(
            tok == _SKIP_PERMISSIONS_FLAG for tok in op_args
        )
        if mode in ("auto", "bypass") and not already_skips:
            cmd.append(_SKIP_PERMISSIONS_FLAG)

        cmd.extend(op_args)

        # Initial prompt → `--prompt-interactive`: agy runs it as the first turn
        # AND keeps the session interactive, so relaydeck sends follow-up
        # messages over the PTY. agy has NO positional prompt arg; `-p`/`--print`
        # is one-shot (prints + exits) and would kill a long-lived agent.
        prompt = self._initial_prompt()
        if prompt:
            cmd.extend(["--prompt-interactive", prompt])

        # Whole-command override (escape hatch) wins over everything.
        if "command" in self.config:
            user_cmd = self.config["command"]
            if isinstance(user_cmd, list):
                cmd = [str(part) for part in user_cmd]
            elif isinstance(user_cmd, str):
                cmd = user_cmd.split()

        return cmd

    def _raw_args_tokens(self) -> list[str]:
        """`config.args` shell-split into tokens (list or string form)."""
        extra = self.config.get("args")
        if isinstance(extra, list):
            return [str(a) for a in extra]
        if isinstance(extra, str) and extra.strip():
            import shlex
            return shlex.split(extra)
        return []

    # -- Positional-prompt context injection (mirrors the cursor adapter) ----

    def _initial_prompt(self) -> str | None:
        """Compose the model-visible context (identity preamble + plugin
        contributions + skills) and the operator's optional first prompt into a
        single positional argument. Newlines survive — this is one argv element,
        not PTY-submitted text."""
        context = self._composed_context()
        user = self.config.get("initial_prompt")
        user = str(user).strip() if user else ""
        if context and user:
            return f"{context}\n\n---\n\n{user}"
        return context or user or None

    def _composed_context(self) -> str:
        parts: list[str] = []
        composed = self._compose_system_prompt()
        if composed:
            parts.append(composed)

        enabled = set(self._workspace_plugins())
        for plugin_name, method_name in self._PROMPT_INJECTORS.items():
            if plugin_name not in enabled:
                continue
            method = getattr(self, method_name, None)
            if not method:
                continue
            text = method()
            if text:
                parts.append(str(text).strip())

        plugin_skills = self._inject_plugin_skills()
        if plugin_skills:
            parts.append(plugin_skills.strip())

        return "\n\n".join(part for part in parts if part)

    def _workspace_plugins(self) -> list[str]:
        if not self.workspace:
            return []
        try:
            toml_path = self._config_home / "workspaces" / self.workspace / "agent.toml"
            if toml_path.exists():
                from relaydeck.config import _load_yaml_or_toml
                data = _load_yaml_or_toml(toml_path)
                return data.get("workspace", {}).get("plugins", [])
        except Exception:
            pass
        return []

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        return [value]

    def _inject_fleet_context(self) -> str | None:
        fleet_path = self._get_fleet_context_path()
        if not fleet_path or not fleet_path.exists():
            return None
        content = fleet_path.read_text().strip()
        return content or None

    def _inject_skills(self) -> str | None:
        """User-authored skills (plugin: skills) — list SKILL.md paths rather
        than inlining bodies. Discovery/validation shared with the Skills lens
        via `relaydeck.skills` (invalid skills skipped)."""
        if not self.workspace:
            return None
        from relaydeck import skills as _skills
        user, _runtime = _skills.injection_dirs(self._config_home, self.workspace)
        if not user:
            return None
        skill_paths = [str(d / "SKILL.md") for d in user]
        return "Workspace skills are available in these SKILL.md files:\n" + "\n".join(skill_paths)

    def _inject_plugin_skills(self) -> str | None:
        if not self.workspace:
            return None
        from relaydeck import skills as _skills
        _user, runtime = _skills.injection_dirs(self._config_home, self.workspace)
        if not runtime:
            return None
        skill_paths = [str(d / "SKILL.md") for d in runtime]
        return (
            "Plugin-contributed skills are available in these SKILL.md files:\n"
            + "\n".join(skill_paths)
        )

    def _inject_forbidden_tools(self) -> str | None:
        forbidden = self.config.get("forbidden_tools")
        if not forbidden:
            return None
        values = self._as_list(forbidden)
        return (
            "Avoid these tools or command families unless explicitly instructed: "
            + ", ".join(str(v) for v in values)
        )

    def _get_fleet_context_path(self) -> Path | None:
        if not self.workspace:
            return None
        return (
            self._config_home
            / "workspaces"
            / self.workspace
            / "runtime"
            / "fleet-context"
            / f"{self.agent_id}.md"
        )
