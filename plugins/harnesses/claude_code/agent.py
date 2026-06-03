"""Claude Code harness agent class.

Wraps Anthropic's `claude` CLI. Claude Code 2.x has a native Agent Skills
system, so relaydeck materializes its workspace + plugin skills into a
session-scoped Claude Code *plugin dir* and passes `--plugin-dir`. That makes
them load the native way — they appear in `/skills`, are namespaced
`/skills:<name>` (a neutral namespace, so imported/third-party skills aren't
branded as relaydeck's), and use progressive disclosure (the model sees each
skill's name + description, loading the body on demand) instead of every
SKILL.md body being inlined into `--append-system-prompt` (the old workaround,
from before `claude` had a skill flag — it bloated the prompt and never showed
in the native skills UI).

`--append-system-prompt` still carries the identity preamble + user
system_prompt (which also nudges the messaging contract, so peer replies work
even before the messaging skill is disclosed). Everything else is inherited
from the base HarnessAgent.
"""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from pathlib import Path

from relaydeck.harness import HarnessAgent

logger = logging.getLogger(__name__)


def _relaydeck_config_home() -> Path:
    """Match the relaydeck-wide convention: env override > ~/.relaydeck.
    The base HarnessAgent doesn't expose a `_config_home` attribute
    (only pi does), so we resolve it directly here."""
    override = os.environ.get("RELAYDECK_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".relaydeck"


def _extract_assistant_text(content) -> str:
    """Pull the visible response text from claude's transcript content
    blocks (Anthropic format: a list of `{type, ...}` blocks). Only `text`
    blocks make it through — `tool_use`/`thinking` are skipped."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts).strip()


class ClaudeCodeAgent(HarnessAgent):
    CLI = "claude"
    HARNESS_TYPE = "claude-code"

    # Claude Code's TUI is Ink-based. Ink debounces stdin reads to
    # detect bracketed-paste / fast paste bursts: if a chunk of text
    # arrives in one read() with the Enter byte attached, Ink treats
    # the whole blob as a paste, deposits it in the input box, and
    # waits for a *subsequent* Enter to submit. Pi/codex don't do
    # this — they take whatever you push immediately. So Claude Code
    # opts into the base's split-write submit: the body lands in the
    # input buffer, then a separate `\r` arrives ~80ms later as an
    # unambiguous Enter keystroke. Without this, peer messages and
    # drained inbox items sit in the box waiting for a manual Enter.
    SUBMIT_SPLIT = True
    SUBMIT_DELAY_S = 0.08

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usage_worker = None
        self._usage_seen_offsets: dict[str, int] = {}

    def run(self) -> None:
        # Auto-install the Claude Code status hook just before spawning, so
        # live semantic status (working / awaiting-input / idle) works out of
        # the box for claude-code agents — no manual `relaydeck integration
        # install claude` needed. In run() (not _build_command) so it fires
        # only on a real spawn, never when callers build the command for tests.
        self._ensure_state_hook()
        self._start_usage_tailer()
        try:
            super().run()
        finally:
            tdir = self._transcript_dir()
            if tdir is not None:
                with suppress(Exception):
                    self._scan_transcript_dir(tdir)
            if self._usage_worker:
                self._usage_worker.stop()

    # ── Token metering (tail claude's transcript JSONL) ──────────
    #
    # claude-code writes a transcript per session under
    # `<config>/projects/<munged-cwd>/<session>.jsonl`, where each assistant
    # line carries `message.usage` (input/output/cache tokens) + `message.model`.
    # We tail it and emit `usage.record` so claude agents show real tokens in the
    # stat strip / usage rollup (cost stays None — claude is subscription-billed,
    # the transcript has no per-call cost, same honest-$0 as codex).
    #
    # The transcript dir is keyed by CWD, not agent, so it's shared by every
    # claude agent in the same workspace. To stay ACCURATE rather than
    # double-count, we only tail when this is the SOLE claude agent in the
    # workspace; otherwise we leave usage unreported (honest gap, not a wrong
    # number). Proper per-agent isolation (CLAUDE_CONFIG_DIR, like codex's
    # CODEX_HOME) is the future fix that lifts this restriction.

    def _claude_config_home(self) -> Path:
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(base).expanduser() if base else Path.home() / ".claude"

    def _transcript_dir(self) -> Path | None:
        import re

        cwd = self._get_cwd()
        if not cwd:
            return None
        munged = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        d = self._claude_config_home() / "projects" / munged
        return d if d.exists() else None

    def _sole_claude_in_workspace(self) -> bool:
        if not self.workspace:
            return False
        try:
            from relaydeck.db import open_db
            conn = open_db(self.db_path)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM agents WHERE workspace=? "
                    "AND type IN ('claude-code','claude')",
                    (self.workspace,),
                ).fetchone()[0]
            finally:
                conn.close()
            return int(n or 0) <= 1
        except Exception:
            return False  # ambiguous → don't risk mis-attribution

    def _start_usage_tailer(self) -> None:
        tdir = self._transcript_dir()
        if tdir is None or not self._sole_claude_in_workspace():
            if tdir is not None:
                logger.debug(
                    "claude usage tailer off for %s: not the sole claude agent in %s",
                    self.agent_id, self.workspace,
                )
            return
        from relaydeck.workers import register_worker

        # Seed offsets to current sizes so a restart doesn't replay history.
        for path in tdir.glob("*.jsonl"):
            with suppress(OSError):
                self._usage_seen_offsets[str(path)] = path.stat().st_size
        self._usage_worker = register_worker(
            name="claude-code.usage-tailer",
            plugin="claude-code",
            target=lambda w: self._usage_tick(w, tdir),
            interval_s=1.0,
            config={"transcript_dir": str(tdir), "harness": "claude-code"},
            agent_id=self.agent_id,
            description=(
                "Tails this claude agent's transcript JSONL once a second and "
                "emits a usage.record for every new assistant message's token "
                "usage (the metering plugin writes them to usage_records). Cost "
                "is None — claude is subscription-billed."
            ),
        )
        self._usage_worker.log(f"watching {tdir}")

    def _usage_tick(self, worker, tdir: Path) -> None:
        try:
            before = sum(self._usage_seen_offsets.values())
            self._scan_transcript_dir(tdir)
            after = sum(self._usage_seen_offsets.values())
            if after > before:
                worker.log(f"sweep advanced {after - before} bytes")
        except Exception as exc:
            worker.log(f"sweep error: {exc}", level="warn")

    def _scan_transcript_dir(self, tdir: Path) -> None:
        if not tdir.exists():
            return
        for path in sorted(tdir.glob("*.jsonl")):
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
                if line.strip():
                    self._handle_transcript_line(line)
            self._usage_seen_offsets[key] = offset + consumed

    def _handle_transcript_line(self, line: bytes) -> None:
        import json

        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return

        # Publish the assistant turn's visible text on the plugin bus so
        # classifier/summarizer subscribers (e.g. a semantic-status
        # classifier, HITL escalation) get the same signal pi/codex already
        # emit — without owning their own JSONL tailer. Independent of the
        # usage block below: a text-only turn still emits the message.
        if rec.get("type") == "assistant" or msg.get("role") == "assistant":
            text = _extract_assistant_text(msg.get("content"))
            if text:
                import time as _t
                self._emit_bus("harness.assistant_message", {
                    "agent_id": self.agent_id,
                    "harness": "claude-code",
                    "text": text,
                    "ts": _t.time(),
                    "session_id": rec.get("sessionId") or rec.get("uuid"),
                })

        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return
        # Prompt side = the FULL-PRICED input: fresh input + cache creation.
        # We deliberately EXCLUDE cache_read_input_tokens: the metering plugin
        # prices prompt_tokens at the full input rate, but cached reads bill at
        # ~0.1x — folding them in inflated cost ~6-9x (they're often the bulk of
        # the context). Excluding them keeps BOTH the token count and the
        # derived cost honest. Completion = output_tokens.
        prompt = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        completion = int(usage.get("output_tokens", 0) or 0)
        if prompt == 0 and completion == 0:
            return
        payload = {
            "agent_id": self.agent_id,
            "model": str(msg.get("model") or "claude"),
            "provider": "anthropic",
            "prompt": prompt,
            "completion": completion,
            "cost_usd": None,
            "harness": "claude-code",
        }
        with suppress(Exception):
            self.emit("usage.record", payload)
        self._emit_bus("usage.record", payload)

    def _ensure_state_hook(self) -> None:
        """Idempotently install the Claude Code semantic-status hook. Best-effort
        and opt-out (`RELAYDECK_DISABLE_HOOK_AUTOINSTALL=1`); safe to leave on —
        the hook no-ops for Claude sessions launched outside relaydeck (no agent
        id in env). Reversible via `relaydeck integration uninstall claude`."""
        if os.environ.get("RELAYDECK_DISABLE_HOOK_AUTOINSTALL"):
            return
        try:
            from relaydeck.integrations.claude import ClaudeIntegration
            it = ClaudeIntegration()
            # Re-render when missing OR when the on-disk script is stale (an
            # older build keyed the event off $1, which Claude never sets, so
            # status never fired). install() always re-writes the script.
            if not it.is_installed() or not it.script_is_current():
                it.install()
                logger.info("Installed/updated Claude Code status hook (agent %s)", self.agent_id)
        except Exception as exc:  # never block a spawn on hook setup
            logger.debug("Claude hook auto-install skipped: %s", exc)

    def _build_command(self) -> list[str]:
        cmd = super()._build_command()
        composed = self._compose_system_prompt()  # identity preamble + system_prompt
        if composed:
            cmd.extend(["--append-system-prompt", composed])
        plugin_dir = self._materialize_claude_plugin()
        if plugin_dir is not None:
            cmd.extend(["--plugin-dir", str(plugin_dir)])
        self._apply_autonomy(cmd)
        return cmd

    # ── External-model routing (OpenRouter / deepseek-anthropic / proxy) ──
    #
    # Claude Code speaks the Anthropic Messages API but honors ANTHROPIC_BASE_URL
    # + ANTHROPIC_AUTH_TOKEN (Bearer), so it can be pointed at any
    # Anthropic-compatible endpoint (or a translating proxy for OpenAI-style
    # providers like ollama/vllm). The operator opts in via the `claude_base_url`
    # config param; the model id (e.g. `deepseek/deepseek-chat`) rides the normal
    # `claude_model` override. relaydeck never replaces the operator's Anthropic
    # account unless `claude_base_url` is explicitly set.

    # endpoint-host → vault key, so the operator can leave `claude_key_env` blank
    # for the common hosts. Not a model pick — just a token-name convenience.
    _ROUTE_KEY_BY_HOST = {
        "openrouter.ai": "OPENROUTER_API_KEY",
        "api.deepseek.com": "DEEPSEEK_API_KEY",
        "api.together.xyz": "TOGETHER_API_KEY",
        "api.groq.com": "GROQ_API_KEY",
        "api.mistral.ai": "MISTRAL_API_KEY",
        "api.x.ai": "XAI_API_KEY",
        "api.fireworks.ai": "FIREWORKS_API_KEY",
    }

    def _infer_key_env(self, base_url: str) -> str | None:
        try:
            from urllib.parse import urlparse
            host = (urlparse(base_url).hostname or "").lower()
        except Exception:
            return None
        return self._ROUTE_KEY_BY_HOST.get(host)

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        base_url = str(self.config.get("claude_base_url") or "").strip()
        if not base_url:
            return env  # default: the operator's own ~/.claude Anthropic auth
        env["ANTHROPIC_BASE_URL"] = base_url
        # Token: explicit vault key wins, else infer from the endpoint host.
        key_env = str(self.config.get("claude_key_env") or "").strip() or self._infer_key_env(base_url)
        token = None
        if key_env:
            with suppress(Exception):
                from relaydeck.vault import get_secret
                token = get_secret(key_env)
        if token:
            # Bearer token ONLY (ANTHROPIC_AUTH_TOKEN). We deliberately do NOT
            # also set ANTHROPIC_API_KEY (x-api-key), even though both "work"
            # against e.g. deepseek's endpoint — setting both is actively
            # hostile to an UNATTENDED agent:
            #   1. claude 2.x flags both-set as an auth conflict ("Both a token
            #      and an API key are set … may lead to unexpected behavior").
            #   2. ANTHROPIC_API_KEY trips claude's interactive "Detected a
            #      custom API key — use it? [No]" gate. No human sits at the
            #      harness TUI to answer it; the default No means the key is
            #      ignored and the agent silently falls back to the operator's
            #      Anthropic account. ANTHROPIC_AUTH_TOKEN needs no approval.
            # Bearer is the path OpenRouter / LiteLLM / deepseek-anthropic all
            # accept (verified live against api.deepseek.com/anthropic).
            env["ANTHROPIC_AUTH_TOKEN"] = token
        else:
            logger.warning(
                "claude-code %s: external endpoint %s set but no token resolved "
                "(claude_key_env=%r) — requests may 401",
                getattr(self, "agent_id", "?"), base_url, key_env,
            )
        # We're routing at an external endpoint, so any ANTHROPIC_API_KEY
        # inherited from the daemon env (the operator's own Anthropic key) is
        # irrelevant here AND would re-trigger the both-set conflict above —
        # drop it so only our Bearer token (or the proxy's own auth) is used.
        env.pop("ANTHROPIC_API_KEY", None)
        # Mirror the model id into ANTHROPIC_MODEL too (some proxies read it
        # when --model isn't forwarded). The --model flag is still the primary.
        model = str(self.config.get("claude_model") or "").strip()
        if model:
            env["ANTHROPIC_MODEL"] = model
        return env

    def _apply_autonomy(self, cmd: list[str]) -> None:
        """Translate the cross-harness autonomy posture into claude permission
        flags. relaydeck agents run unattended: an unlisted tool that would
        *prompt* blocks forever, so we must pick a non-prompting permission
        mode. The relaydeck CLI is ALWAYS allowlisted (peer messaging / replies
        / status) so fleet coordination works even under the fail-safe
        `dontAsk` deny. Operator-pinned `--permission-mode` / `--allowedTools` /
        `--dangerously-skip-permissions` (via config.args) win — we stay out of
        the way."""
        mode = self._autonomy_mode()
        if mode == "manual":
            return
        has_mode = any(
            a in ("--permission-mode", "--dangerously-skip-permissions") for a in cmd
        )
        has_allow = any(a in ("--allowedTools", "--allowed-tools") for a in cmd)
        if not has_allow:
            # `Bash(relaydeck *)` matches every relaydeck subcommand; Read is
            # harmless and avoids stalling on context reads under dontAsk.
            cmd.extend(["--allowedTools", "Bash(relaydeck *) Read"])
        if not has_mode:
            # auto: classifier auto-approves safe work, blocks dangerous, never
            # prompts (needs a recent model). bypass: no checks. locked: only
            # the allowlist runs, everything else auto-denies (fail-safe).
            permission_mode = {
                "auto": "auto",
                "bypass": "bypassPermissions",
                "locked": "dontAsk",
            }.get(mode, "auto")
            cmd.extend(["--permission-mode", permission_mode])

    def _materialize_claude_plugin(self) -> Path | None:
        """Build a session-scoped Claude Code plugin dir wrapping relaydeck's
        skills so `claude` loads them natively via `--plugin-dir`:

            runtime/claude-plugin/
              .claude-plugin/plugin.json   {"name": "skills", ...}
              skills/<name>/  ->  symlink to the materialized skill dir

        Skill sources match pi/codex/opencode so an operator gets the SAME set
        on every harness: user-authored `skills/` (GATED by `skills` in
        agent.toml) + plugin-contributed `runtime/skills/` (always). Returns
        None when there are no valid skills (no flag added). Rebuilt fresh each
        spawn so a removed skill doesn't linger.
        """
        if not self.workspace:
            return None
        import json
        import shutil

        from relaydeck import skills as _skills

        home = _relaydeck_config_home()
        include_user = "skills" in self._workspace_plugins()
        refs = _skills.discover_workspace_skills(
            home, self.workspace, include_user=include_user, include_runtime=True,
        )
        valid = [r for r in refs if r.valid and (Path(r.path) / "SKILL.md").exists()]
        if not valid:
            return None

        plugin_dir = home / "workspaces" / self.workspace / "runtime" / "claude-plugin"
        shutil.rmtree(plugin_dir, ignore_errors=True)
        (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        skills_dir = plugin_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        # Plugin name = the `/skills` namespace prefix Claude Code stamps on
        # every skill here. Use a neutral "skills" so injected third-party /
        # user-authored skills (grill-me, …) aren't branded as relaydeck's —
        # relaydeck only *delivers* them, it doesn't own them.
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({
            "name": "skills",
            "description": "Skills delivered into this workspace by relaydeck (relaydeck-managed + user-authored + imported).",
            "version": "0.1.0",
        }, indent=2))

        seen: set[str] = set()
        for ref in valid:
            if ref.name in seen:
                continue
            seen.add(ref.name)
            dest = skills_dir / ref.name
            try:
                dest.symlink_to(Path(ref.path), target_is_directory=True)
            except OSError:
                shutil.copytree(Path(ref.path), dest, dirs_exist_ok=True)
        return plugin_dir

    def _workspace_plugins(self) -> list[str]:
        """Plugins enabled for this agent's workspace (read from agent.toml).
        Drives the `skills` gate for user-authored skills — same source of
        truth pi/codex/opencode read."""
        if not self.workspace:
            return []
        import tomllib
        toml = _relaydeck_config_home() / "workspaces" / self.workspace / "agent.toml"
        try:
            data = tomllib.loads(toml.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return []
        plugins = data.get("workspace", {}).get("plugins", [])
        return plugins if isinstance(plugins, list) else []
