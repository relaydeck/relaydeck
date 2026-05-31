"""
Codex harness - wraps the `codex` coding agent CLI.

Codex is not flag-compatible with pi. This adapter keeps the same relaydeck
capabilities where Codex exposes an equivalent surface:
  - per-agent session isolation through CODEX_HOME
  - workspace cwd through `-C`
  - model presets through `-m`
  - plugin-gated prompt addenda through Codex's model_instructions_file
  - token metering by tailing Codex session JSONL `token_count` events
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from relaydeck.harness import HarnessAgent

logger = logging.getLogger(__name__)


def _extract_codex_assistant_text(rec: dict) -> str:
    """Pull visible assistant text from a Codex JSONL record.
    Codex has two shapes carrying it:
      - `response_item` with payload.type=message, role=assistant, output_text blocks
      - `event_msg` with payload.type=agent_message, payload.message is plain text
    Reasoning/tool_use blocks are skipped so we only forward speech-acts."""
    rec_type = rec.get("type")
    payload = rec.get("payload") or {}

    if rec_type == "response_item":
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return ""
        content = payload.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") in ("text", "output_text")
            ]
            return "\n".join(parts).strip()
        return ""

    if rec_type == "event_msg" and payload.get("type") == "agent_message":
        return str(payload.get("message") or "").strip()

    return ""


class CodexAgent(HarnessAgent):
    """Wraps the OpenAI Codex CLI."""

    CLI = "codex"
    HARNESS_TYPE = "codex-cli"
    # Codex shows an inline approval prompt ("Allow command?", a numbered
    # Yes/No menu) when `--ask-for-approval` gates a tool. The semantic-status
    # engine flips the agent to awaiting-input when one is on screen.
    AWAITING_INPUT_PATTERNS = HarnessAgent.AWAITING_INPUT_PATTERNS + (
        r"\ballow (?:this )?command\b.*\?",
        r"\bapprove\b.*\?",
        r"\b1\.\s*yes\b.*\b2\.\s*no\b",
    )
    DEFAULT_ARGS: list[str] = []

    # Codex runs a full-screen TUI; submit the body and Enter as two
    # separate writes so a paste-debouncing input widget can't swallow
    # the trailing CR (same failure class as Claude Code's Ink). Harmless
    # for builds without paste detection — the body lands, then the CR
    # submits. Override per-agent via config submit_split/submit_delay_s.
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
        self._usage_worker = None
        self._usage_seen_offsets: dict[str, int] = {}
        self._usage_context: dict[str, dict[str, str]] = {}

    # -- Codex home / session isolation ---------------------------------

    def _codex_home(self) -> Path:
        base = (
            self._config_home / "workspaces" / self.workspace / "runtime"
            if self.workspace
            else self._config_home / "runtime"
        )
        return base / "codex-homes" / self.agent_id

    def _prepare_codex_home(self) -> Path:
        home = self._codex_home()
        home.mkdir(parents=True, exist_ok=True)

        user_codex = Path.home() / ".codex"
        for name in ("auth.json", "config.toml"):
            src = user_codex / name
            dst = home / name
            if not src.exists():
                continue
            # Dangling symlink (target no longer resolves). Path.exists()
            # follows symlinks and returns False on dangling targets, so
            # without this the dead link stays and codex fails with ENOENT
            # reading auth.json.
            if dst.is_symlink() and not dst.exists():
                with suppress(OSError):
                    dst.unlink()
            if dst.exists():
                continue
            with suppress(OSError):
                dst.symlink_to(src)
        return home

    def _session_dir(self) -> Path:
        return self._codex_home() / "sessions"

    def _relaydeck_instructions_file(self) -> Path:
        return self._codex_home() / "relaydeck-instructions.md"

    def log_path(self) -> Path | None:
        return self._codex_home() / "log" / "codex-tui.log"

    # -- Command / env ---------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        env["CODEX_HOME"] = str(self._prepare_codex_home())
        return env

    def _build_command(self) -> list[str]:
        cmd = [self.CLI]

        # Continue the most recent session. Codex has no `--continue` flag —
        # it's the `codex resume --last` subcommand, which does NOT accept the
        # --model/--cd/--sandbox flags we'd otherwise add (resume restores the
        # session's own model/cwd/sandbox). So build a clean resume command
        # and return early. `command` override still wins below.
        if self.config.get("resume_last") and "command" not in self.config:
            cmd = [self.CLI, "resume", "--last"]
            extra = self.config.get("args")
            if isinstance(extra, list):
                cmd.extend(str(a) for a in extra)
            elif isinstance(extra, str) and extra.strip():
                import shlex
                cmd.extend(shlex.split(extra))
            return cmd

        subcommand = self.config.get("subcommand")
        if subcommand:
            # Supported for Codex agent-like subcommands such as exec/resume/review.
            # Other Codex subcommands may reject the agent flags appended below.
            if isinstance(subcommand, list):
                cmd.extend(str(part) for part in subcommand)
            else:
                cmd.extend(str(subcommand).split())

        model = self._resolve_codex_model()
        if model:
            cmd.extend(["--model", model])

        # Local OSS provider routing (ollama / LM Studio). Codex ships these as
        # BUILT-IN providers that carry their own localhost base_url + wire
        # protocol, so we just select one — the model id rides --model above.
        # This is deliberately NOT a generic base_url override: codex 0.134
        # dropped custom chat-completions providers (config requires
        # wire_api="responses", the OpenAI Responses API), so cloud
        # chat-completions endpoints (DeepSeek, OpenRouter, vLLM) 404 — the
        # built-in local providers are the only working non-OpenAI route.
        # Whitelisted to the two built-in OSS ids to keep the -c value safe.
        local_provider = str(self.config.get("codex_local_provider") or "").strip()
        if local_provider in ("ollama", "lmstudio"):
            cmd.extend(["--config", f'model_provider="{local_provider}"'])

        profile = self.config.get("profile")
        if profile:
            cmd.extend(["--profile", str(profile)])

        # Autonomy: a relaydeck-orchestrated codex agent runs unattended, so it
        # can't answer approval prompts, and it must run `relaydeck` CLI
        # commands that write ~/.relaydeck (outside the workspace). Translate
        # the cross-harness autonomy posture into codex flags — unless the
        # operator pinned sandbox/approval/bypass explicitly (then respect
        # those verbatim and stay out of the way).
        sandbox = self.config.get("sandbox")
        approval = self.config.get("ask_for_approval") or self.config.get("approval_policy")
        bypass = self.config.get("dangerously_bypass_approvals_and_sandbox")
        operator_pinned = bool(sandbox or approval or bypass is not None)
        mode = self._autonomy_mode()
        writable_relaydeck = False
        if not operator_pinned and mode != "manual":
            if mode == "bypass":
                bypass = True
            else:
                # "auto" and "locked": never prompt; sandbox writes to the
                # workspace; keep ~/.relaydeck writable so the relaydeck CLI
                # works. codex has no per-command allowlist, so "locked"
                # collapses to the same sandbox — the guardrail is the sandbox
                # boundary, not a command list.
                sandbox = "workspace-write"
                approval = "never"
                writable_relaydeck = True

        if bypass:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            if sandbox:
                cmd.extend(["--sandbox", str(sandbox)])
            if approval:
                cmd.extend(["--ask-for-approval", str(approval)])
            if writable_relaydeck:
                roots = json.dumps([str(self._relaydeck_config_home())])
                cmd.extend(["--config", f"sandbox_workspace_write.writable_roots={roots}"])
                # Coding agents need network for deps/git; the sandbox boundary
                # (no writes outside workspace + ~/.relaydeck) is the guardrail.
                cmd.extend(["--config", "sandbox_workspace_write.network_access=true"])

        for add_dir in self._as_list(self.config.get("add_dirs") or self.config.get("add_dir")):
            cmd.extend(["--add-dir", str(add_dir)])

        if self.config.get("search"):
            cmd.append("--search")
        if self.config.get("no_alt_screen", True):
            cmd.append("--no-alt-screen")

        instructions = self._model_instructions_text()

        for key, value in (self.config.get("codex_config") or {}).items():
            # relaydeck owns developer_instructions when it has generated
            # context. `model_instructions_file` is a DEAD key in current
            # Codex (it silently ignores unknown config), so it's kept in the
            # skip set only so a stale user config can't re-introduce it.
            if instructions and key in ("developer_instructions", "model_instructions_file"):
                continue
            cmd.extend(["--config", f"{key}={value}"])

        if instructions:
            # Codex has no model_instructions_file. Deliver relaydeck's
            # identity preamble + plugin context as a developer message via
            # `developer_instructions` — additive, does NOT replace Codex's
            # base system prompt. Codex parses the `-c` value as TOML and
            # falls back to the raw string, so multi-line prose lands intact.
            cmd.extend(["--config", f"developer_instructions={instructions}"])

        cwd = self._get_cwd()
        if cwd:
            cmd.extend(["--cd", cwd])

        prompt = self._initial_prompt()
        if prompt:
            # With subcommands, Codex receives this as that subcommand's positional prompt.
            cmd.append(prompt)

        if "command" in self.config:
            user_cmd = self.config["command"]
            if isinstance(user_cmd, list):
                cmd = [str(part) for part in user_cmd]
            elif isinstance(user_cmd, str):
                cmd = user_cmd.split()

        # Additive `config.args` — appended last (rightmost on the
        # command line). Same contract as base.HarnessAgent. Note
        # codex receives an initial positional prompt; user args land
        # *after* that prompt, which is the right place for flags
        # like `--fast` that don't consume the positional.
        extra = self.config.get("args")
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
        elif isinstance(extra, str) and extra.strip():
            import shlex
            cmd.extend(shlex.split(extra))

        return cmd

    def _resolve_codex_model(self) -> str | None:
        return self._resolve_cli_model()

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        return [value]

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

    def _initial_prompt(self) -> str | None:
        """Explicit first user prompt only.

        relaydeck identity, peer context, and plugin prompt contributions go
        through Codex's `model_instructions_file` config instead of the
        positional prompt. That keeps the TUI from auto-submitting a fake
        first user turn before the operator has a chance to change model
        or session settings.
        """
        initial = self.config.get("initial_prompt")
        if initial:
            return str(initial).strip() or None
        return None

    def _model_instructions_text(self) -> str | None:
        """Build Codex model instructions from the existing relaydeck prompt
        extension points.

        This is deliberately Codex-local adapter plumbing: plugins still
        contribute through the same `_PROMPT_INJECTORS` methods used
        before; only the transport changes from positional prompt text
        to Codex's instructions-file config.
        """
        parts: list[str] = []

        configured = self._read_configured_model_instructions_file()
        if configured:
            parts.append(configured.strip())

        # Spec-level system prompt (identity preamble + user append).
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

        # Plugin-contributed skills are always included. The contributing
        # plugin gates materialization on its own workspace enablement,
        # so presence of `runtime/skills/<dir>/SKILL.md` implies the
        # owning plugin chose to ship it for this workspace.
        plugin_skills = self._inject_plugin_skills()
        if plugin_skills:
            parts.append(plugin_skills.strip())

        return "\n\n".join(part for part in parts if part)

    def _inject_fleet_context(self) -> str | None:
        fleet_path = self._get_fleet_context_path()
        if not fleet_path or not fleet_path.exists():
            return None
        content = fleet_path.read_text().strip()
        return content or None

    def _inject_skills(self) -> str | None:
        """Inject user-authored skills via model instructions.
        Plugin: skills. List the SKILL.md paths instead of inlining
        bodies; large skills would bloat the instructions payload.
        Discovery + validation are shared with the dashboard Skills lens
        via `relaydeck.skills` (invalid skills are skipped)."""
        if not self.workspace:
            return None
        from relaydeck import skills as _skills
        user, _runtime = _skills.injection_dirs(self._config_home, self.workspace)
        if not user:
            return None
        skill_paths = [str(d / "SKILL.md") for d in user]
        return "Workspace skills are available in these SKILL.md files:\n" + "\n".join(skill_paths)

    def _inject_plugin_skills(self) -> str | None:
        """List plugin-contributed skills from
        `workspaces/<ws>/runtime/skills/`. Always-on — see
        PiAgent._inject_plugin_skills for the rationale."""
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

    def _read_configured_model_instructions_file(self) -> str | None:
        """Fold a user-specified model_instructions_file into relaydeck's
        generated file instead of dropping it when relaydeck adds its own
        instructions-file override.

        This handles the common agent-spec shape:
          config:
            codex_config:
              model_instructions_file: '"/path/to/file.md"'

        Profile-level Codex config is not visible to relaydeck; if an agent
        uses relaydeck-generated instructions, the generated override wins.
        """
        raw = (self.config.get("codex_config") or {}).get("model_instructions_file")
        if raw is None:
            raw = self.config.get("model_instructions_file")
        if raw is None:
            return None

        path_s = self._parse_tomlish_string(raw)
        if not path_s:
            return None
        path = Path(path_s).expanduser()
        if not path.is_absolute():
            cwd = self._get_cwd()
            path = Path(cwd or ".") / path
        try:
            target = path.resolve()
            if target == self._relaydeck_instructions_file().resolve():
                return None
            return target.read_text().strip() or None
        except OSError as exc:
            logger.warning("Could not read Codex model_instructions_file %s: %s", path, exc)
            return None

    @staticmethod
    def _parse_tomlish_string(value: Any) -> str:
        if not isinstance(value, str):
            return str(value)
        raw = value.strip()
        if not raw:
            return ""
        try:
            import tomllib
            parsed = tomllib.loads(f"x = {raw}")
            out = parsed.get("x")
            if isinstance(out, str):
                return out
        except Exception:
            pass
        return raw

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

    # -- Session-log tailing for usage metering -------------------------

    def run(self) -> None:
        sd = self._session_dir()
        self._prepare_codex_home()
        from relaydeck.workers import register_worker

        if sd.exists():
            for path in sd.rglob("*.jsonl"):
                with suppress(OSError):
                    self._usage_seen_offsets[str(path)] = path.stat().st_size
        self._usage_worker = register_worker(
            name="codex.usage-tailer",
            plugin="codex",
            target=lambda w: self._usage_tick(w, sd),
            interval_s=1.0,
            config={"session_dir": str(sd), "harness": "codex"},
            agent_id=self.agent_id,
            description=(
                "Tails this codex agent's session JSONL once a second and "
                "emits a usage.record event for every new token/cost line "
                "(the metering plugin writes them to usage_records). A tick "
                "is one sweep of new bytes — quiet when the agent is idle."
            ),
        )
        self._usage_worker.log(f"watching {sd}")
        try:
            super().run()
        finally:
            with suppress(Exception):
                self._scan_session_dir(sd)
            if self._usage_worker:
                self._usage_worker.stop()

    def _usage_tick(self, worker, sd: Path) -> None:
        try:
            before = sum(self._usage_seen_offsets.values())
            self._scan_session_dir(sd)
            after = sum(self._usage_seen_offsets.values())
            if after > before:
                worker.log(f"sweep advanced {after - before} bytes")
        except Exception as exc:
            worker.log(f"sweep error: {exc}", level="warn")

    def _scan_session_dir(self, session_dir: Path) -> None:
        if not session_dir.exists():
            return
        for path in sorted(session_dir.rglob("*.jsonl")):
            key = str(path)
            offset = self._usage_seen_offsets.get(key, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= offset:
                continue
            try:
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
            except OSError:
                continue
            lines = chunk.split(b"\n")
            consumed = 0
            for line in lines[:-1]:
                consumed += len(line) + 1
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_record(rec, key)
            self._usage_seen_offsets[key] = offset + consumed

    def _handle_record(self, rec: dict, key: str) -> None:
        """Route one Codex JSONL record. Codex spreads its turn across
        many record types — `session_meta`/`turn_context` carry model+
        session_id state that later `event_msg` rows reference, and
        assistant text appears in either `response_item` or
        `event_msg/agent_message` form depending on Codex version."""
        ctx = self._usage_context.setdefault(
            key, {"model": "unknown", "provider": "openai", "session_id": ""}
        )
        rec_type = rec.get("type")
        event_payload = rec.get("payload") or {}

        if rec_type == "session_meta":
            ctx["session_id"] = str(event_payload.get("id") or ctx.get("session_id") or "")
            ctx["provider"] = str(
                event_payload.get("model_provider") or ctx.get("provider") or "openai"
            )
            if event_payload.get("model"):
                ctx["model"] = str(event_payload["model"])
            return

        if rec_type == "turn_context" and event_payload.get("model"):
            ctx["model"] = str(event_payload["model"])
            return

        # Assistant message — emit on the plugin bus so subscribers
        # (emote, future summarizers) don't have to tail JSONL.
        text = _extract_codex_assistant_text(rec)
        if text:
            import time as _t
            self._emit_bus("harness.assistant_message", {
                "agent_id": self.agent_id,
                "harness": "codex",
                "text": text,
                "ts": _t.time(),
                "session_id": ctx.get("session_id") or None,
            })

        if rec_type != "event_msg" or event_payload.get("type") != "token_count":
            return
        info = event_payload.get("info") or {}
        usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
        prompt = int(usage.get("input_tokens", 0) or 0)
        completion = int(usage.get("output_tokens", 0) or 0)
        if prompt == 0 and completion == 0:
            return

        usage_payload = {
            "agent_id": self.agent_id,
            "model": str(ctx.get("model") or "unknown"),
            "provider": str(ctx.get("provider") or "openai"),
            "prompt": prompt,
            "completion": completion,
            "cost_usd": None,
            "session_id": ctx.get("session_id") or None,
            "harness": "codex",
        }
        with suppress(Exception):
            self.emit("usage.record", usage_payload)
        self._emit_bus("usage.record", usage_payload)
