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
import os
import sqlite3
from contextlib import suppress
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
        self._usage_worker = None
        self._usage_watermarks: dict[str, int] = {}  # db path → last message.time_created

    # -- OpenCode runtime files -----------------------------------------

    def _opencode_xdg_data(self) -> Path:
        return self._opencode_home() / "xdg-data"

    def _opencode_db_path(self, xdg_data: Path) -> Path:
        return xdg_data / "share" / "opencode" / "opencode.db"

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
        isolated = self._opencode_db_path(self._opencode_xdg_data())
        if isolated.exists():
            return isolated.parent / "log"
        return Path.home() / ".local" / "share" / "opencode" / "log"

    # -- Usage metering (opencode.db assistant rows) --------------------

    def run(self) -> None:
        self._seed_usage_watermarks()
        from relaydeck.workers import register_worker

        self._usage_worker = register_worker(
            name="opencode.usage-tailer",
            plugin="opencode",
            target=lambda w: self._usage_tick(w),
            interval_s=1.0,
            config={"agent_id": self.agent_id, "harness": "opencode"},
            agent_id=self.agent_id,
            description=(
                "Polls OpenCode's SQLite message store for assistant turns with "
                "token/cost JSON and emits usage.record (metering writes "
                "usage_records). Tails the per-agent DB (XDG_DATA_HOME) and, when "
                "a workspace cwd is known, the global DB filtered to that directory."
            ),
        )
        sources = [str(p) for p, _ in self._usage_sources()]
        self._usage_worker.log(f"watching {', '.join(sources) or 'no db'}")
        try:
            super().run()
        finally:
            with suppress(Exception):
                self._scan_usage_dbs()
            if self._usage_worker:
                self._usage_worker.stop()

    def _usage_tick(self, worker) -> None:
        try:
            self._scan_usage_dbs()
        except Exception as exc:
            worker.log(f"sweep error: {exc}", level="warn")

    def _usage_sources(self) -> list[tuple[Path, str | None]]:
        """(db path, session.directory filter). None filter = whole DB."""
        out: list[tuple[Path, str | None]] = []
        isolated = self._opencode_db_path(self._opencode_xdg_data())
        out.append((isolated, None))
        cwd = self._get_cwd()
        if cwd:
            global_db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
            try:
                directory = os.path.realpath(cwd)
            except OSError:
                directory = cwd
            if global_db != isolated:
                out.append((global_db, directory))
        return out

    def _seed_usage_watermarks(self) -> None:
        # Seed to the newest row already in each DB so a restart only emits
        # assistant turns created *after* this agent (re)starts — parity with
        # pi/codex seeding tail offsets to current file size. The watermark
        # lives only in memory, so a 24h backfill here would re-emit history on
        # every restart, and `record_usage` is an append-only INSERT with no
        # message-id dedup → that would double-count tokens/cost per restart.
        for db_path, directory in self._usage_sources():
            key = str(db_path)
            self._usage_watermarks[key] = _opencode_usage_watermark(db_path, directory)

    def _scan_usage_dbs(self) -> None:
        for db_path, directory in self._usage_sources():
            self._scan_usage_db(db_path, directory)

    def _scan_usage_db(self, db_path: Path, directory: str | None) -> None:
        key = str(db_path)
        after = self._usage_watermarks.get(key, 0)
        rows = _opencode_fetch_assistant_rows(db_path, directory, after)
        if not rows:
            return
        for msg_id, session_id, raw, created in rows:
            self._usage_watermarks[key] = max(self._usage_watermarks.get(key, 0), int(created))
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("role") != "assistant":
                continue
            self._emit_usage_row(msg_id, session_id, data)

    def _emit_usage_row(self, msg_id: str, session_id: str, data: dict) -> None:
        tok = data.get("tokens") or {}
        prompt = int(tok.get("input") or 0)
        completion = int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
        if prompt == 0 and completion == 0:
            total = int(tok.get("total") or 0)
            if total <= 0:
                return
            completion = max(0, total - prompt)
        cost = data.get("cost")
        payload = {
            "agent_id": self.agent_id,
            "model": str(data.get("modelID") or "unknown"),
            "provider": str(data.get("providerID") or "unknown"),
            "prompt": prompt,
            "completion": completion,
            "cost_usd": float(cost) if cost is not None else None,
            "session_id": session_id or msg_id,
            "harness": "opencode",
        }
        with suppress(Exception):
            self.emit("usage.record", payload)
        self._emit_bus("usage.record", payload)

    # -- Command / env ---------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        env["XDG_DATA_HOME"] = str(self._opencode_xdg_data())
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


def _opencode_connect_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _opencode_usage_watermark(db_path: Path, directory: str | None) -> int:
    conn = _opencode_connect_ro(db_path)
    if conn is None:
        return 0
    try:
        if directory:
            row = conn.execute(
                "SELECT MAX(m.time_created) FROM message m "
                "JOIN session s ON s.id = m.session_id WHERE s.directory = ?",
                (directory,),
            ).fetchone()
        else:
            row = conn.execute("SELECT MAX(time_created) FROM message").fetchone()
        return int((row[0] if row else 0) or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _opencode_fetch_assistant_rows(
    db_path: Path, directory: str | None, after: int,
) -> list[tuple[str, str, str, int]]:
    conn = _opencode_connect_ro(db_path)
    if conn is None:
        return []
    try:
        if directory:
            return conn.execute(
                "SELECT m.id, m.session_id, m.data, m.time_created FROM message m "
                "JOIN session s ON s.id = m.session_id "
                "WHERE s.directory = ? AND m.time_created > ? "
                "ORDER BY m.time_created ASC",
                (directory, after),
            ).fetchall()
        return conn.execute(
            "SELECT id, session_id, data, time_created FROM message "
            "WHERE time_created > ? ORDER BY time_created ASC",
            (after,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
