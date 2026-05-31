"""
OpenCode harness - wraps the `opencode` coding agent CLI.

OpenCode is not flag-compatible with pi. This adapter maps relaydeck's
shared harness expectations onto OpenCode's native surfaces:
  - workspace cwd through the project positional argument
  - model presets through `--model provider/model`
  - optional OpenCode agent selection through `--agent`
  - relaydeck prompt addenda through a generated opencode.json instructions file
  - plugin-contributed skills as instruction-file references
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from relaydeck.harness import HarnessAgent

logger = logging.getLogger(__name__)


class OpenCodeAgent(HarnessAgent):
    """Wraps the OpenCode CLI TUI."""

    CLI = "opencode"
    HARNESS_TYPE = "opencode-cli"
    DEFAULT_ARGS: list[str] = []
    # OpenCode surfaces a permission prompt for tool runs.
    AWAITING_INPUT_PATTERNS = HarnessAgent.AWAITING_INPUT_PATTERNS + (
        r"\b(?:allow|permit|approve)\b.*\?",
        r"\bpermission\b.*\?",
    )

    # Token metering: NOT wired yet. pi/codex/claude tail an on-disk session
    # transcript for per-call token usage, but opencode 1.4.7 keeps its global
    # storage (~/.local/share/opencode/storage) as session_diff snapshots with
    # no readily-tailable per-message token record, and it's shared across
    # agents (no per-agent data dir). So opencode agents honestly report 0
    # tokens rather than a fabricated/double-counted number. The future fix is
    # per-agent XDG_DATA_HOME isolation (opencode honors it) + a tailer once a
    # token-bearing record is locatable. Same gap means no
    # `harness.assistant_message` bus event either (pi/codex/claude emit it
    # from their transcript JSONL) — without a tail-able structured turn the
    # only signal is raw PTY bytes, which don't classify into clean message
    # boundaries. Honest gap, not a fabricated event.

    # OpenCode runs a full-screen TUI; split the body and Enter into two
    # writes so a paste-debouncing input widget can't swallow the CR
    # (same failure class as Claude Code's Ink). Harmless without paste
    # detection. Override per-agent via config submit_split/submit_delay_s.
    SUBMIT_SPLIT = True
    SUBMIT_DELAY_S = 0.08
    # OpenCode is a dense full-screen TUI. Replaying relaydeck's raw byte
    # ring buffer on dashboard attach can start mid-escape-sequence and
    # corrupt xterm's parser state (visible tails like "10;10m"). Keep
    # replay for visibility, but have the WebSocket bootstrap trim
    # unsafe leading fragments before xterm sees them.
    SANITIZE_PTY_REPLAY = True

    _PROMPT_INJECTORS: dict[str, str] = {
        "fleet-context": "_inject_fleet_context",
        "skills": "_inject_skills",
        "forbidden-tools": "_inject_forbidden_tools",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config_home = Path.home() / ".relaydeck"

    # -- OpenCode runtime files -----------------------------------------

    def _opencode_home(self) -> Path:
        base = (
            self._config_home / "workspaces" / self.workspace / "runtime"
            if self.workspace
            else self._config_home / "runtime"
        )
        return base / "opencode-homes" / self.agent_id

    def _relaydeck_instructions_file(self) -> Path:
        return self._opencode_home() / "relaydeck-instructions.md"

    def _relaydeck_config_file(self) -> Path:
        return self._opencode_home() / "opencode.json"

    def log_path(self) -> Path | None:
        return Path.home() / ".local" / "share" / "opencode" / "log"

    # -- Command / env ---------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        # `or` (not setdefault): an inherited COLORTERM="" / FORCE_COLOR=""
        # is present-but-empty, which setdefault wouldn't override — opencode
        # would then render without truecolor.
        env["COLORTERM"] = env.get("COLORTERM") or "truecolor"
        # Only fill in when unset/empty — respect an explicit FORCE_COLOR=0.
        if env.get("FORCE_COLOR", "") == "":
            env["FORCE_COLOR"] = "3"
        env["TERM_PROGRAM"] = str(self.config.get("term_program") or "xterm.js")
        cfg = self._write_opencode_config()
        if cfg is not None:
            env["OPENCODE_CONFIG"] = str(cfg)
        return env

    def _build_command(self) -> list[str]:
        cmd = [self.CLI]

        if self.config.get("pure"):
            cmd.append("--pure")
        if self.config.get("print_logs"):
            cmd.append("--print-logs")
        if self.config.get("log_level"):
            cmd.extend(["--log-level", str(self.config["log_level"])])

        model = self._resolve_opencode_model()
        if model:
            cmd.extend(["--model", model])

        opencode_agent = self.config.get("opencode_agent") or self.config.get("agent")
        if opencode_agent:
            cmd.extend(["--agent", str(opencode_agent)])

        if self.config.get("continue"):
            cmd.append("--continue")
        if self.config.get("session"):
            cmd.extend(["--session", str(self.config["session"])])
        if self.config.get("fork"):
            cmd.append("--fork")
        if self.config.get("port"):
            cmd.extend(["--port", str(self.config["port"])])
        if self.config.get("hostname"):
            cmd.extend(["--hostname", str(self.config["hostname"])])

        initial = self.config.get("initial_prompt") or self.config.get("prompt")
        if initial:
            cmd.extend(["--prompt", str(initial)])

        cwd = self._get_cwd()
        if cwd:
            cmd.append(cwd)

        if "command" in self.config:
            user_cmd = self.config["command"]
            if isinstance(user_cmd, list):
                cmd = [str(part) for part in user_cmd]
            elif isinstance(user_cmd, str):
                cmd = user_cmd.split()

        # Additive `config.args` — extra CLI flags appended last, matching
        # pi/codex/base so the new-agent modal's "extra flags" field works
        # here too.
        extra = self.config.get("args")
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
        elif isinstance(extra, str) and extra.strip():
            import shlex
            cmd.extend(shlex.split(extra))

        return cmd

    def _resolve_opencode_model(self) -> str | None:
        return self._resolve_cli_model()

    # -- Generated OpenCode config --------------------------------------

    def _write_opencode_config(self) -> Path | None:
        instructions = self._model_instructions_text()
        user_config = self._read_user_config()

        home = self._opencode_home()

        config = dict(user_config)
        if "opencode_config" in self.config and isinstance(self.config["opencode_config"], dict):
            config.update(self.config["opencode_config"])
        injected_model_config = self._inject_model_provider_config(config)

        # Autonomy: a relaydeck agent runs unattended, so opencode must not stop
        # at an approval prompt — and it must be allowed to run the `relaydeck`
        # CLI for peer messaging. Inject a permission block unless the operator
        # already set one.
        if "permission" not in config:
            permission = self._autonomy_permission()
            if permission is not None:
                config["permission"] = permission

        has_content = bool(
            instructions or user_config or injected_model_config or config.get("permission")
        )
        if not has_content:
            return None

        home.mkdir(parents=True, exist_ok=True)

        if instructions:
            instructions_path = self._relaydeck_instructions_file()
            instructions_path.write_text(instructions.rstrip() + "\n")
            existing = config.get("instructions")
            values = self._as_list(existing)
            path_s = str(instructions_path)
            if path_s not in values:
                values.append(path_s)
            config["instructions"] = values

        config.setdefault("$schema", "https://opencode.ai/config.json")
        path = self._relaydeck_config_file()
        path.write_text(json.dumps(config, indent=2, sort_keys=True))
        return path

    def _autonomy_permission(self) -> dict | None:
        """OpenCode `permission` block for the configured autonomy posture.

        OpenCode has no safety classifier, so "auto" can't be a true guarded
        mode the way claude's classifier or codex's sandbox are — it maps to
        "allow, with the relaydeck CLI explicitly allowlisted". "locked"
        restricts execution to just the relaydeck CLI (everything else denied).
        Returns None for "manual" (operator drives opencode's own permission
        config). `Bash(relaydeck *)`-style coordination always works because
        the `relaydeck *` pattern is allowed in every non-manual mode."""
        mode = self._autonomy_mode()
        if mode == "manual":
            return None
        if mode == "locked":
            return {
                "edit": "deny",
                "bash": {"relaydeck *": "allow", "*": "deny"},
                "webfetch": "deny",
            }
        # "auto" + "bypass": permissive, relaydeck CLI explicitly allowed.
        return {
            "edit": "allow",
            "bash": {"relaydeck *": "allow", "*": "allow"},
            "webfetch": "allow",
        }

    def _inject_model_provider_config(self, config: dict[str, Any]) -> bool:
        """Teach opencode about relaydeck-selected local models.

        Opencode validates `--model provider/model` against its own model
        catalog/config. relaydeck's provider presets can point at any locally
        pulled Ollama model, so a valid relaydeck preset like `gemma4:latest`
        still fails opencode unless that model is declared under
        `provider.ollama.models`. Add the minimal compatible provider
        stanza to the per-agent config while leaving user-provided values
        intact.
        """
        model_ref = self._resolve_opencode_model()
        if not model_ref or "/" not in model_ref:
            return False
        provider, model = model_ref.split("/", 1)
        if provider != "ollama" or not model:
            return False

        providers = config.setdefault("provider", {})
        if not isinstance(providers, dict):
            return False
        ollama = providers.setdefault("ollama", {})
        if not isinstance(ollama, dict):
            return False
        ollama.setdefault("name", "Ollama")
        ollama.setdefault("npm", "@ai-sdk/openai-compatible")
        options = ollama.setdefault("options", {})
        if isinstance(options, dict):
            options.setdefault("baseURL", "http://127.0.0.1:11434/v1")
        models = ollama.setdefault("models", {})
        if not isinstance(models, dict):
            return False
        existing = models.get(model)
        if isinstance(existing, dict):
            existing.setdefault("name", model)
            existing.setdefault("_launch", True)
        else:
            models[model] = {"name": model, "_launch": True}
        return True

    def _read_user_config(self) -> dict[str, Any]:
        raw = self.config.get("opencode_config_file")
        if not raw:
            return {}
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            cwd = self._get_cwd()
            path = Path(cwd or ".") / path
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read OpenCode config file %s: %s", path, exc)
            return {}

    def _model_instructions_text(self) -> str | None:
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

        return "\n\n".join(part for part in parts if part) or None

    # -- Plugin-gated prompt injections ---------------------------------

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
        if not self.workspace:
            return None
        from relaydeck import skills as _skills

        user, _runtime = _skills.injection_dirs(self._config_home, self.workspace)
        if not user:
            return None
        paths = [str(path / "SKILL.md") for path in user]
        return "Workspace skills are available in these SKILL.md files:\n" + "\n".join(paths)

    def _inject_plugin_skills(self) -> str | None:
        if not self.workspace:
            return None
        from relaydeck import skills as _skills

        _user, runtime = _skills.injection_dirs(self._config_home, self.workspace)
        if not runtime:
            return None
        paths = [str(path / "SKILL.md") for path in runtime]
        return "Plugin-contributed skills are available in these SKILL.md files:\n" + "\n".join(
            paths
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

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        return [value]
