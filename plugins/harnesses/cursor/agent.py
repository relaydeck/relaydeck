"""
Cursor harness — wraps the `cursor-agent` CLI (Cursor's terminal agent).

cursor-agent takes a **positional** initial prompt (like codex, not a
`--append-system-prompt` flag), authenticates via the operator's own Cursor
account (`~/.cursor`, no provider API keys), and reads its permission posture
from a `cli-config.json`. This adapter keeps relaydeck's cross-harness
capabilities where Cursor exposes an equivalent surface:

  - per-agent isolation via ``CURSOR_CONFIG_DIR`` + ``CURSOR_DATA_DIR`` (the
    auth token is keychain-backed, so relocating both keeps the Pro login)
  - auto-trust: pre-seed Cursor's per-workspace trust file so an unattended
    agent never blocks on the "Workspace Trust Required" prompt
  - autonomy posture written into the isolated ``cli-config.json``
    (``approvalMode`` + ``sandbox`` + a ``permissions.allow`` list that always
    includes the ``relaydeck`` CLI so peer messaging can persist to ~/.relaydeck)
  - model selection via ``--model`` (Cursor's own namespace, e.g. ``sonnet-4``)
  - identity preamble + plugin context + skills delivered via the positional
    prompt (the only injection point Cursor exposes for headless setup)

No per-call token metering: Cursor is a flat-rate subscription (Composer/Pro)
and only records token usage in an opaque per-session ``store.db`` (SQLite
blobs) — there is no tail-able JSONL like codex/claude, so usage renders an
honest 0 / $0.00. (A future iteration could parse ``store.db`` or the headless
``--print --output-format stream-json`` event stream.)

Same root cause means this harness also emits no ``harness.assistant_message``
bus event (pi/codex/claude do, from their transcript JSONL): with no tail-able
structured assistant turn, the only signal is raw PTY bytes, which can't be
classified into clean message boundaries reliably. Honest gap, not a fake event.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from relaydeck.harness import HarnessAgent

logger = logging.getLogger(__name__)

# Always-allowed shell entries so peer messaging / `relaydeck reply` never
# stalls and can persist to ~/.relaydeck outside any sandbox (the hard rule).
_RELAYDECK_ALLOW = ["Shell(relaydeck)", "Shell(rdk)"]


class CursorAgent(HarnessAgent):
    """Wraps the Cursor CLI (`cursor-agent`)."""

    CLI = "cursor-agent"
    HARNESS_TYPE = "cursor-cli"
    DEFAULT_ARGS: list[str] = []
    # cursor-agent prompts for command/edit approval in interactive mode.
    AWAITING_INPUT_PATTERNS = HarnessAgent.AWAITING_INPUT_PATTERNS + (
        r"\b(?:run|apply|allow|approve)\b.*\?\s*$",
        r"\baccept (?:edit|change|command)s?\b.*\?",
    )

    # Cursor runs a full-screen TUI; submit the body and Enter as two separate
    # writes so a paste-debouncing input widget can't swallow the trailing CR
    # (same failure class as Claude Code's Ink). Override per-agent via config
    # submit_split / submit_delay_s.
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

    # -- Cursor config-home isolation -----------------------------------

    def _cursor_home(self) -> Path:
        base = (
            self._config_home / "workspaces" / self.workspace / "runtime"
            if self.workspace
            else self._config_home / "runtime"
        )
        return base / "cursor-homes" / self.agent_id

    def _cursor_config_path(self) -> Path:
        return self._cursor_home() / "cli-config.json"

    def _prepare_cursor_home(self) -> Path:
        home = self._cursor_home()
        home.mkdir(parents=True, exist_ok=True)
        with suppress(Exception):
            self._write_cursor_config()
        return home

    def _autonomy_overlay(self) -> dict | None:
        """The autonomy fields to merge into cli-config.json, or None for
        `manual` (inject nothing — the operator's own config drives).

        Cursor expresses approval posture in the config file, not flags:
          - approvalMode: "unrestricted" (never prompt) | "allowlist" (only
            allow-listed commands run)
          - sandbox.mode: "enabled" | "disabled"
        Cursor has no trustable filesystem writable-root escape, so every
        non-manual mode runs UN-sandboxed — otherwise the relaydeck CLI
        couldn't write ~/.relaydeck and peer messaging would silently fail.
        An operator who pins `approval_mode`/`sandbox` in config is respected.
        """
        mode = self._autonomy_mode()
        if mode == "manual":
            return None

        op_approval = self.config.get("approval_mode")
        op_sandbox = self.config.get("sandbox")

        if op_approval:
            approval = str(op_approval)
        elif mode == "locked":
            approval = "allowlist"
        else:  # auto, bypass
            approval = "unrestricted"

        sandbox_mode = str(op_sandbox) if op_sandbox else "disabled"
        return {"approvalMode": approval, "sandbox": {"mode": sandbox_mode}}

    def _write_cursor_config(self) -> Path | None:
        """Materialize the per-agent cli-config.json: the operator's existing
        ~/.cursor settings (model, privacy) merged with relaydeck's autonomy
        posture + the always-allowed relaydeck CLI. Returns the path written,
        or None when nothing was written. Public enough to drive from tests."""
        path = self._cursor_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Start from the operator's real config so model/privacy/auth metadata
        # carry into the isolated home (auth itself is keychain-backed).
        base: dict[str, Any] = {}
        user_cfg = Path.home() / ".cursor" / "cli-config.json"
        if user_cfg.exists():
            try:
                base = json.loads(user_cfg.read_text())
            except (OSError, json.JSONDecodeError):
                base = {}

        overlay = self._autonomy_overlay()
        if overlay is None:
            # manual: preserve the operator's config verbatim; inject nothing.
            path.write_text(json.dumps(base, indent=2))
            return path

        perms = base.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        allow = list(perms.get("allow") or [])
        for entry in _RELAYDECK_ALLOW:
            if entry not in allow:
                allow.append(entry)
        perms["allow"] = allow
        perms.setdefault("deny", [])
        base["permissions"] = perms
        base["approvalMode"] = overlay["approvalMode"]
        base["sandbox"] = {**(base.get("sandbox") or {}), **overlay["sandbox"]}

        path.write_text(json.dumps(base, indent=2))
        return path

    # -- Command / env ---------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        home = self._prepare_cursor_home()
        # Isolate BOTH config + data per-agent (auth is keychain-backed and
        # survives the relocation). The data dir holds Cursor's per-workspace
        # trust store, which we pre-seed in _ensure_workspace_trusted().
        env["CURSOR_CONFIG_DIR"] = str(home)
        env["CURSOR_DATA_DIR"] = str(home)
        self._ensure_workspace_trusted()
        return env

    @staticmethod
    def _munge_path(p: str) -> str:
        import re
        return re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-")

    def _ensure_workspace_trusted(self) -> Path | None:
        """Pre-trust the workspace directory so an unattended agent never blocks
        on Cursor's "Workspace Trust Required" prompt (there's no human to press
        [a], so it would hang forever). Cursor records trust at
        ``<data_dir>/projects/<munged-abs-path>/.workspace-trusted`` and treats
        mere existence as trust. Skipped for `manual` autonomy (operator drives).

        Returns the trust file path, or None when nothing was written.
        """
        if self._autonomy_overlay() is None:  # manual: don't inject trust
            return None
        cwd = self._get_cwd()
        if not cwd:
            return None
        abspath = str(Path(cwd).resolve())
        trust_file = (
            self._cursor_home() / "projects" / self._munge_path(abspath) / ".workspace-trusted"
        )
        if not trust_file.exists():
            import datetime
            trust_file.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            with suppress(OSError):
                trust_file.write_text(json.dumps({"trustedAt": ts, "workspacePath": abspath}))
        return trust_file

    def _build_command(self) -> list[str]:
        cmd = [self.CLI]

        model = self._resolve_cursor_model()
        if model:
            cmd.extend(["--model", model])

        # Autonomy flags belt-and-suspenders the config file (approvalMode +
        # permissions live there; the flags enforce the sandbox posture).
        mode = self._autonomy_mode()
        op_sandbox = self.config.get("sandbox")
        op_force = self.config.get("force")
        operator_pinned = bool(
            op_sandbox or op_force is not None or self.config.get("approval_mode")
        )
        if mode != "manual" and not operator_pinned:
            # Un-sandboxed so the relaydeck CLI can write ~/.relaydeck.
            cmd.extend(["--sandbox", "disabled"])
            if mode == "bypass":
                cmd.append("--force")
        else:
            if op_force:
                cmd.append("--force")
            if op_sandbox:
                cmd.extend(["--sandbox", str(op_sandbox)])

        cwd = self._get_cwd()
        if cwd:
            cmd.extend(["--workspace", cwd])

        # Extra/launch-option flags go BEFORE the positional prompt — Cursor's
        # prompt is variadic ([prompt...]) so trailing flags would be eaten
        # as prompt words.
        extra = self.config.get("args")
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
        elif isinstance(extra, str) and extra.strip():
            import shlex
            cmd.extend(shlex.split(extra))

        prompt = self._initial_prompt()
        if prompt:
            cmd.append(prompt)

        if "command" in self.config:
            user_cmd = self.config["command"]
            if isinstance(user_cmd, list):
                cmd = [str(part) for part in user_cmd]
            elif isinstance(user_cmd, str):
                cmd = user_cmd.split()

        return cmd

    def _resolve_cursor_model(self) -> str | None:
        return self._resolve_cli_model()

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        return [value]

    # -- Positional-prompt context injection ----------------------------

    def _initial_prompt(self) -> str | None:
        """Compose the model-visible context (identity preamble + plugin
        contributions + skills) and the operator's optional first prompt into
        a single positional argument — Cursor's only headless injection point.

        Newlines survive: this is one argv element, not PTY-submitted text.
        """
        context = self._composed_context()
        user = self.config.get("initial_prompt")
        user = str(user).strip() if user else ""

        if context and user:
            return f"{context}\n\n---\n\n{user}"
        return context or user or None

    def _composed_context(self) -> str:
        """Identity preamble + user system_prompt + plugin prompt contributions
        + skills, mirroring the codex adapter's instructions assembly."""
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

    def _inject_fleet_context(self) -> str | None:
        fleet_path = self._get_fleet_context_path()
        if not fleet_path or not fleet_path.exists():
            return None
        content = fleet_path.read_text().strip()
        return content or None

    def _inject_skills(self) -> str | None:
        """User-authored skills (plugin: skills). List the SKILL.md paths
        rather than inlining bodies — a large skill would bloat the prompt.
        Discovery/validation is shared with the Skills lens via
        `relaydeck.skills` (invalid skills are skipped)."""
        if not self.workspace:
            return None
        from relaydeck import skills as _skills
        user, _runtime = _skills.injection_dirs(self._config_home, self.workspace)
        if not user:
            return None
        skill_paths = [str(d / "SKILL.md") for d in user]
        return "Workspace skills are available in these SKILL.md files:\n" + "\n".join(skill_paths)

    def _inject_plugin_skills(self) -> str | None:
        """Plugin-contributed skills from `workspaces/<ws>/runtime/skills/` —
        always on (the owning plugin gates materialization on its own workspace
        enablement, so presence implies it shipped the skill here)."""
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
