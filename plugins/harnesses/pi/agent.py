"""
Pi harness — wraps the `pi` coding agent CLI.

Per-agent session isolation: each pi agent gets its own --session-dir
under the workspace's runtime dir so parallel agents don't fight over
the global session file.

Injections are driven by plugins, not a workspace mode:
  - fleet-context plugin enabled → inject generated fleet context
  - skills plugin enabled → load workspace skills via --skill

Each injection is gated on the workspace having that plugin enabled.
No "agent mode" — just plugins you opt into.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import suppress
from pathlib import Path

from relaydeck.harness import HarnessAgent

logger = logging.getLogger(__name__)


def _extract_assistant_text(content) -> str:
    """Pull visible response text from pi's content blocks.
    Skips reasoning/tool_use blocks — only `text` blocks make it through."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts).strip()


class PiAgent(HarnessAgent):
    """Wraps the `pi` CLI coding harness.

    Pi is relaydeck's first-class harness and the autonomy reference: it runs
    its built-in tools (read / bash / edit / write) with NO per-command
    approval prompts, so a relaydeck agent works unattended out of the box —
    the `relaydeck` CLI for peer messaging just runs via pi's bash tool. That's
    why pi↔pi messaging worked before the other harnesses (claude/codex/
    opencode) had their approval/sandbox flags wired through
    `HarnessAgent._autonomy_mode`. Pi therefore needs no approval-bypass flags;
    its only gating is the tool-name allowlist (`config.tools` →
    `--tools a,b`, or `config.no_tools`), which stays operator-driven. There is
    no per-command lock in pi, so `config.autonomy` levels are a no-op here
    (pi is always autonomous) — don't bolt approval flags onto pi that its CLI
    doesn't have.
    """

    CLI = "pi"
    HARNESS_TYPE = "pi"
    # pi prompts inline for tool/command approval when autonomy is gated.
    AWAITING_INPUT_PATTERNS = HarnessAgent.AWAITING_INPUT_PATTERNS + (
        r"\b(?:allow|approve|run|apply)\b.*\?\s*$",
        r"\bpermission\b.*\?",
    )
    DEFAULT_ARGS: list[str] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config_home = Path.home() / ".relaydeck"
        self._usage_worker = None  # set when run() registers it
        self._usage_seen_offsets: dict[str, int] = {}  # path → byte offset

    # ── Session-log tailing for usage metering ──────────────────

    def _session_dir(self) -> Path | None:
        if not (self.workspace and self.agent_id):
            return None
        return (self._config_home / "workspaces" / self.workspace /
                "runtime" / "pi-sessions" / self.agent_id)

    def log_path(self) -> Path | None:
        # Pi writes one JSONL per session; the directory is the
        # debug surface (caller can `ls` to find the active one).
        return self._session_dir()

    def run(self) -> None:
        """Start the harness AND register a Worker that mines pi's
        session JSONL for usage records, emitting them as `usage.record`
        events. The metering plugin subscribes and writes usage_records;
        the Worker is visible in /api/workers and `relaydeck workers list`."""
        sd = self._session_dir()
        if sd is not None:
            from relaydeck.workers import register_worker
            # Seed offsets to current sizes so an agent restart doesn't
            # replay historical usage. We do this once before the loop
            # rather than inside _tick.
            if sd.exists():
                for path in sd.glob("*.jsonl"):
                    with suppress(OSError):
                        self._usage_seen_offsets[str(path)] = path.stat().st_size
            self._usage_worker = register_worker(
                name="pi.usage-tailer",
                plugin="pi",
                target=lambda w: self._usage_tick(w, sd),
                interval_s=1.0,
                config={"session_dir": str(sd), "harness": "pi"},
                agent_id=self.agent_id,
                description=(
                    "Tails this pi agent's session JSONL once a second and "
                    "emits a usage.record event for every new token/cost line "
                    "(the metering plugin writes them to usage_records). A tick "
                    "is one sweep of new bytes — quiet when the agent is idle."
                ),
            )
            self._usage_worker.log(f"watching {sd}")
        try:
            super().run()
        finally:
            # Final sweep — pi may have flushed a usage record right
            # before exit that the periodic tick hasn't picked up.
            if sd:
                with suppress(Exception):
                    self._scan_session_dir(sd)
            if self._usage_worker:
                self._usage_worker.stop()

    def _usage_tick(self, worker, sd):
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
        for path in sorted(session_dir.glob("*.jsonl")):
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
            # Parse complete JSONL lines only — leave any trailing partial
            # line for next sweep.
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
                self._handle_record(rec)
            self._usage_seen_offsets[key] = offset + consumed

    def _handle_record(self, rec: dict) -> None:
        """Route one pi JSONL record to the right semantic-event emitter.
        Pi packs both the assistant text and the usage block into a single
        `type: message` row, so one record can fan out into multiple events."""
        if rec.get("type") != "message":
            return
        msg = rec.get("message") or {}
        if msg.get("role") != "assistant":
            return
        self._emit_assistant_message(rec, msg)
        self._emit_usage(rec, msg)

    def _emit_assistant_message(self, rec: dict, msg: dict) -> None:
        """Publish the assistant turn's visible text on the plugin event
        bus. Subscribers (emote, future summarizers) react without owning
        their own JSONL tailer."""
        text = _extract_assistant_text(msg.get("content"))
        if not text:
            return
        import time as _t
        self._emit_bus("harness.assistant_message", {
            "agent_id": self.agent_id,
            "harness": "pi",
            "text": text,
            "ts": _t.time(),
            "session_id": rec.get("id"),
        })

    def _emit_usage(self, rec: dict, msg: dict) -> None:
        """Pi assistant messages carry a `usage` block with input/output
        token counts and a pre-computed cost. Emit on the SSE bus (live
        UI) and the plugin bus (metering subscribes)."""
        usage = msg.get("usage") or {}
        if not usage:
            return
        prompt = int(usage.get("input", 0) or 0)
        completion = int(usage.get("output", 0) or 0)
        if prompt == 0 and completion == 0:
            return
        cost = (usage.get("cost") or {}).get("total")
        payload = {
            "agent_id": self.agent_id,
            "model": str(msg.get("model") or "unknown"),
            "provider": str(msg.get("provider") or "unknown"),
            "prompt": prompt,
            "completion": completion,
            "cost_usd": float(cost) if cost is not None else None,
            "session_id": rec.get("id"),
            "harness": "pi",
        }
        with suppress(Exception):
            self.emit("usage.record", payload)
        self._emit_bus("usage.record", payload)

    # ── Plugin-gated injections ─────────────────────────────────

    # Map of plugin name → injection method. Each returns a list of
    # extra CLI args to append, or None if the injection is a no-op
    # (plugin not enabled, already present, user explicitly disabled).

    _INJECTORS: dict[str, str] = {
        "fleet-context": "_inject_fleet_context",
        "skills": "_inject_skills",
        "forbidden-tools": "_inject_forbidden_tools",
    }

    def _workspace_plugins(self) -> list[str]:
        """Return the list of plugins enabled for this agent's workspace."""
        if not self.workspace:
            return []
        try:
            toml_path = (
                self._config_home / "workspaces" / self.workspace / "agent.toml"
            )
            if toml_path.exists():
                from relaydeck.config import load_config_file
                data = load_config_file(toml_path)
                return data.get("workspace", {}).get("plugins", [])
        except Exception:
            pass
        return []

    def _build_command(self) -> list[str]:
        cmd = [self.CLI]

        # Resolve model → --model (harness-type aware)
        cli_model = self._resolve_cli_model()
        if cli_model:
            cmd.extend(["--model", cli_model])

        # Per-agent session isolation (always on)
        if self.workspace and self.agent_id:
            session_dir = (
                self._config_home / "workspaces" / self.workspace /
                "runtime" / "pi-sessions" / self.agent_id
            )
            session_dir.mkdir(parents=True, exist_ok=True)
            cmd.extend(["--session-dir", str(session_dir)])

        # Spec-level system prompt — auto-on. Combines the identity
        # preamble (purpose + peers) with any user-provided
        # `system_prompt` text on the agent spec. Goes via pi's native
        # `--append-system-prompt`, stacking with workspace-level
        # fleet-context addenda below if enabled.
        composed = self._compose_system_prompt()
        if composed:
            cmd.extend(["--append-system-prompt", composed])

        # Plugin-gated injections
        enabled = set(self._workspace_plugins())
        for plugin_name, method_name in self._INJECTORS.items():
            if plugin_name in enabled:
                method = getattr(self, method_name, None)
                if method:
                    extra = method()
                    if extra:
                        cmd.extend(extra)

        # Plugin-contributed skills are always injected — the
        # contributing plugin (messaging, future cognitive-skills, etc.)
        # gates materialization on its own workspace enablement, so
        # presence of `runtime/skills/<dir>/SKILL.md` already implies
        # the owning plugin chose to ship it for this workspace.
        extra = self._inject_plugin_skills()
        if extra:
            cmd.extend(extra)

        # User command overrides (always last — user wins)
        if "command" in self.config:
            user_cmd = self.config["command"]
            if isinstance(user_cmd, list):
                cmd = user_cmd  # Full override
            elif isinstance(user_cmd, str):
                cmd = user_cmd.split()

        # Additive `config.args` — appended last so they're the
        # rightmost flags on the command line. Same contract as
        # base.HarnessAgent: list of strings or shell-style string.
        extra = self.config.get("args")
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
        elif isinstance(extra, str) and extra.strip():
            import shlex
            cmd.extend(shlex.split(extra))

        return cmd

    # ── Individual injectors (each gated by a plugin) ───────────

    def _inject_fleet_context(self) -> list[str] | None:
        """Inject fleet context via --append-system-prompt.
        Plugin: fleet-context"""
        fleet_path = self._get_fleet_context_path()
        if not fleet_path or not fleet_path.exists():
            return None
        content = fleet_path.read_text().strip()
        if not content:
            return None
        return ["--append-system-prompt", content]

    def _inject_skills(self) -> list[str] | None:
        """Inject user-authored workspace skills via --skill.
        Plugin: skills. Discovery + validation are shared with the
        dashboard Skills lens via `relaydeck.skills`, so invalid SKILL.md
        files never reach the model and the lens reports what's injected."""
        if not self.workspace:
            return None
        from relaydeck import skills as _skills
        user, _runtime = _skills.injection_dirs(self._config_home, self.workspace)
        args: list[str] = []
        for skill_dir in user:
            args.extend(["--skill", str(skill_dir)])
        return args if args else None

    def _inject_plugin_skills(self) -> list[str] | None:
        """Inject plugin-contributed skills from
        `workspaces/<ws>/runtime/skills/`. Always-on — the contributing
        plugin gates materialization on its own workspace enablement,
        so a SKILL.md only exists here if its owning plugin decided
        this workspace should have it (e.g. messaging plugin writes
        `relaydeck-cli/SKILL.md` only when `messaging` is in the workspace's
        `plugins = [...]` list)."""
        if not self.workspace:
            return None
        from relaydeck import skills as _skills
        _user, runtime = _skills.injection_dirs(self._config_home, self.workspace)
        args: list[str] = []
        for skill_dir in runtime:
            args.extend(["--skill", str(skill_dir)])
        return args if args else None

    def _inject_forbidden_tools(self) -> list[str] | None:
        """Inject forbidden tools list.
        Plugin: forbidden-tools"""
        forbidden = self.config.get("forbidden_tools")
        if not forbidden:
            return None
        if isinstance(forbidden, list):
            return ["--forbidden-tools", ",".join(forbidden)]
        return ["--forbidden-tools", str(forbidden)]

    def _get_fleet_context_path(self) -> Path | None:
        """Path to generated fleet context for this agent."""
        if not self.workspace:
            return None
        return (
            self._config_home / "workspaces" / self.workspace /
            "runtime" / "fleet-context" / f"{self.agent_id}.md"
        )

    def _preflight_fingerprint(self) -> str:
        """Hash of config knobs that trigger pre-flight re-confirmation."""
        parts = [
            self.config.get("model", ""),
            self.config.get("preset", ""),
            str(self.config.get("command", "")),
            ",".join(self._workspace_plugins()),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
