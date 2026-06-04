"""
Harness plugin — base for CLI-harness-wrapping agents.

Each harness plugin (pi, claude-code, codex, cursor, opencode)
extends HarnessAgent to wrap a CLI tool in a PTY, fan PTY bytes out
to per-agent subscriber queues (consumed by the dashboard's terminal
WebSocket), and forward keystrokes + resize back to the child.

PTY bytes are NOT persisted to SQLite — volume is too high. A small
ring buffer keeps recent output so a reconnecting browser tab sees
context. Only lifecycle (harness.spawn / harness.exit / harness.error)
goes to the events table.
"""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import queue
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from contextlib import suppress
from pathlib import Path

from relaydeck.agents_base import BaseAgent

logger = logging.getLogger(__name__)


def _acquire_controlling_tty() -> None:
    """`preexec_fn`: make the PTY slave (already dup'd to fd 0/1/2 by the
    time this runs) the child's *controlling terminal*.

    Why this matters: we spawn with ``start_new_session=True`` so the
    harness gets its own session/process group. But `setsid()` leaves the
    new session with **no controlling terminal**, and `TIOCSWINSZ` on the
    master only delivers `SIGWINCH` to the foreground process group *of the
    pty's controlling terminal*. With no controlling tty, every resize we
    push from the dashboard updates the winsize silently — the harness
    (pi / claude-code / codex) reads its geometry once at spawn and never
    reflows, so the terminal "keeps coming back" stuck at the spawn width
    and its TUI wraps/ghosts in a smaller pane. `TIOCSCTTY` on fd 0 (the
    slave) claims it as the controlling tty so SIGWINCH is delivered.

    Kept deliberately tiny — a single ioctl — because `preexec_fn` runs
    post-fork in a multithreaded daemon, where only minimal work is safe.
    """
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


# ── Process-tree reaping ─────────────────────────────────────────────
#
# Why a tree walk instead of a single `killpg`: some harnesses (notably
# `pi`) fork worker subprocesses that call `setsid()`, detaching into
# their OWN session + process group. `killpg(getpgid(child))` only signals
# the direct child's group, so those setsid'd workers survive, get
# reparented to init (PPID 1), and leak — we accumulated 100+ stray `pi`
# processes this way. Walking the descendant tree (by PPID) while the
# parent is still alive captures every worker so we can signal each one.
#
# Dependency-free on purpose (no psutil): a single `ps -Ao pid=,ppid=`
# snapshot works on both macOS and Linux.


def _descendant_pids(root_pid: int) -> set[int]:
    """All transitive descendants of `root_pid` (excluding root itself).

    One `ps` snapshot → child-map → BFS. Returns an empty set on any
    failure (ps missing, parse error) — callers still handle the root
    pid directly, so a failed walk degrades to the old single-process
    behaviour rather than raising.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            # Snapshot is taken up to twice (terminate + _reap); keep the
            # per-call cap tight so worst-case terminate+reap stays inside
            # the orchestrator's 10s thread-join window (≈7.5s with the
            # grace + kill waits below).
            capture_output=True, text=True, timeout=1.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _pid_alive(pid: int) -> bool:
    """True if `pid` still exists. `signal 0` probes without delivering."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another uid — treat as alive (not ours to reap).
        return True


def _signal_pids(pids, sig: int) -> None:
    """Best-effort `sig` to every pid; already-gone / not-ours are ignored."""
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_pids_gone(pids, deadline: float, interval: float = 0.05) -> set[int]:
    """Poll until every pid in `pids` has exited or `deadline` passes.
    Returns the set still alive at the end (empty == clean)."""
    alive = set(pids)
    while alive and time.time() < deadline:
        alive = {pid for pid in alive if _pid_alive(pid)}
        if not alive:
            break
        time.sleep(interval)
    return alive


# Cache of preset_name → "provider/model" resolved once at startup.
# Shared across all harness agents so presets are only loaded once.
_RESOLVED_PRESETS: dict[str, str] = {}

# CSI / escape-sequence matcher for echo confirmation. We strip ANSI
# from a PTY snapshot before searching it for a message id, so cursor
# moves and colour codes a TUI sprays around the input box don't break
# the substring match.
_ANSI_RE = re.compile(r"\x1b[@-_][0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_WS_RE = re.compile(r"\s+")


def _flatten_for_match(raw: str) -> str:
    """Strip ANSI escapes and remove all whitespace from `raw`.

    Echo confirmation searches for a message id (e.g. `msg_6d4ec0bd2b10`)
    in the harness's rendered input box. The id has no internal
    whitespace, so removing whitespace from the snapshot makes the match
    survive line-wrapping the TUI applies to a long input line.
    """
    return _WS_RE.sub("", _ANSI_RE.sub("", raw))


def _resolve_model_preset(name: str) -> str | None:
    """Resolve a model preset name to 'provider/model' format."""
    from relaydeck.config import load_model_presets
    for preset in load_model_presets():
        if preset.name == name:
            resolved = f"{preset.provider}/{preset.model}"
            _RESOLVED_PRESETS[name] = resolved
            return resolved
    return None


def _configured_model_ref(config: dict) -> str:
    """Return the configured model/preset reference.

    Newer UI surfaces write the named reusable combo as `preset`; older
    CLI/spec paths used `model`. Keep both so existing agents continue to
    spawn while dashboard-created agents actually receive their preset.
    """
    return str(config.get("preset") or config.get("model") or "")


def compose_git_context_block(
    workspace_path: Path | str | None,
    *,
    config_home: Path | str | None = None,
) -> str:
    """Git/worktree awareness block for harness system prompts.

    Omitted for non-git workspaces — relaydeck never auto-inits git; a
    folder the operator `git init`s later picks up on the next agent start."""
    if not workspace_path:
        return ""
    from relaydeck.worktrees import workspace_git_info

    home = Path(config_home) if config_home else None
    info = workspace_git_info(Path(workspace_path), config_home=home)
    if not info["is_git"]:
        return ""

    lines: list[str] = ["", "Git checkout context:"]
    if info["kind"] == "worktree":
        lines.append(
            "- This workspace is a **linked git worktree** — a parallel branch "
            "checkout off a shared repo. Other agents may be on sibling branches."
        )
    else:
        lines.append(
            "- This workspace is the **main git checkout** for its repo."
        )
    branch = info.get("branch")
    if branch:
        detail = f"- Branch: `{branch}`"
        if info.get("dirty"):
            detail += " (**dirty** — uncommitted changes)"
        ahead, behind = info.get("ahead", 0), info.get("behind", 0)
        if ahead or behind:
            detail += f"; {ahead} ahead, {behind} behind upstream"
        nfiles = info.get("files_changed", 0)
        if nfiles:
            ins, dele = info.get("insertions", 0), info.get("deletions", 0)
            detail += f"; +{ins}/-{dele} across {nfiles} file(s)"
        lines.append(detail)
    if info.get("repo_root"):
        lines.append(f"- Repo root (main checkout): `{info['repo_root']}`")
    sibs = info.get("sibling_workspaces") or []
    if sibs:
        lines.append(
            "- Other relaydeck workspaces on this repo (stay in your tree unless delegated):"
        )
        for s in sibs:
            br = s.get("branch") or "?"
            dirty = " (dirty)" if s.get("dirty") else ""
            kind = "worktree" if s.get("is_worktree") else "main"
            lines.append(f"  - `{s['workspace']}` — {kind}, branch `{br}`{dirty}")
    lines.append(
        "- This working tree is SHARED with any peer agents in this same "
        "workspace: a `git checkout` / `git switch` / branch-create changes the "
        "branch for ALL of them at once. Don't switch branches here — for "
        "isolated branch work ask the operator for a worktree "
        "(`relaydeck worktree list` to inspect). Don't edit sibling workspace "
        "paths without explicit coordination."
    )
    return "\n".join(lines)


def compose_identity_preamble(
    agent_id: str,
    workspace: str | None,
    purpose: str,
    peers: list[dict],
    *,
    workspace_path: Path | str | None = None,
    config_home: Path | str | None = None,
) -> str:
    """Build the auto-generated "you are X, your peers are..." block.

    Pure — takes the already-resolved identity + peer list so the same
    text can be produced both at spawn (the live harness, via
    `_build_identity_preamble`) and as a pre-spawn preview (the new-agent
    modal, via `/api/agents/preview-prompt`). Omits sections that have
    no content.
    """
    lines: list[str] = []
    lines.append(f"You are agent `{agent_id}` in workspace `{workspace or '(unscoped)'}`.")
    if purpose:
        lines.append(f"Your purpose: {purpose}")

    if peers:
        lines.append("")
        lines.append("Peer agents in this workspace (use the relaydeck CLI to message them):")
        for p in peers:
            p_purpose = p.get("purpose") or "(no purpose set)"
            p_type = p.get("type") or "?"
            lines.append(f"  - `{p['id']}` ({p_type}) — {p_purpose}")
        lines.append("")
        lines.append(
            "Peer messaging:\n"
            "  A `[relay from=X id=msg_...] <body>` line in your input is a peer task\n"
            "  owing a durable reply — the sender reads the inbox, not your transcript.\n"
            "    relaydeck reply msg_... \"<reply>\"   # auto-routes + threads\n"
            "  Reply via this command BEFORE you end your turn — answering only in\n"
            "  your transcript does NOT reach the sender; the reply is lost.\n"
            "  Don't poll your inbox; new messages are pushed live as `[relay ...]` lines.\n"
            "  relaydeck agent find --tag <tag>     # discover peers\n"
            "  relaydeck workspace message \"<body>\"  # broadcast to the fleet"
        )

    git_block = compose_git_context_block(
        workspace_path, config_home=config_home,
    )
    if git_block:
        lines.append(git_block)

    return "\n".join(lines).strip()


class HarnessAgent(BaseAgent):
    """Wraps a CLI harness (pi, claude, codex, etc.) in a real PTY.

    Subclasses set:
      - CLI:          the binary name (e.g., "pi")
      - DEFAULT_ARGS: default CLI arguments
    """

    CLI: str = ""
    DEFAULT_ARGS: list[str] = []
    # Slash command the harness understands to summarize-and-trim its context
    # IN PLACE (without killing the process — so the prompt prefix stays stable
    # and the KV cache mostly survives). Empty = no known in-place compaction;
    # the orchestrator then reports it unsupported and the caller falls back to
    # `agent result` + a fresh session. Sent via the sanctioned input path
    # (send_message), never the read loop (terminal-untouchable).
    COMPACT_COMMAND: str = ""
    CHUNK_SIZE: int = 4096
    BUFFER_BYTES: int = 64 * 1024  # ring buffer for reconnect replay
    REPLAY_PTY_BUFFER: bool = True
    SANITIZE_PTY_REPLAY: bool = False
    # How often the read loop opportunistically re-drains the inbox
    # when the PTY is idle. Keeps "send during warmup race" + transient
    # write failures from getting stuck in `pending` until restart.
    _LIVE_DRAIN_INTERVAL: float = 2.0

    # ── Message-submit reliability knobs ──────────────────────────
    # SUBMIT_SPLIT: write the body and the Enter keystroke as two
    #   separate PTY writes (with SUBMIT_DELAY_S between) instead of one
    #   `body + \r`. Needed by paste-debouncing TUIs (Claude Code's Ink)
    #   that classify `body + \r` arriving in a single read() as a paste
    #   and swallow the Enter. Off by default — most harnesses submit a
    #   single write fine. Per-agent override: config `submit_split`.
    # SUBMIT_DELAY_S: gap between the two writes when SUBMIT_SPLIT is on.
    #   Per-agent override: config `submit_delay_s`.
    # SUBMIT_COLLAPSE_NEWLINES: collapse embedded CR/LF in a message into
    #   single spaces before injecting. A raw-mode TUI input widget
    #   treats a bare `\n` inconsistently (literal glyph in some, submit
    #   in others) — a multi-line body could partial-submit or mangle.
    #   The full body is preserved in the DB; only the PTY-injected form
    #   is collapsed.
    SUBMIT_SPLIT: bool = False
    SUBMIT_DELAY_S: float = 0.0
    SUBMIT_COLLAPSE_NEWLINES: bool = True

    # ── Cross-harness semantic-status: awaiting-input detection ────
    # Regexes (matched case-insensitively against the tail of the
    # rendered screen) that mean "this harness is blocked waiting on the
    # operator" — a permission/approval prompt, a y/N confirmation, etc.
    # The semantic-status engine reads the rendered screen and sets
    # `awaiting-input` when any pattern matches, making that state
    # available to EVERY harness, not just ones with a vendor hook.
    # Subclasses override with the prompts their CLI actually shows;
    # the base set covers common shells. Keep them anchored to prompt
    # shapes (trailing `?`, `[y/N]`) so ordinary prose doesn't trip them.
    AWAITING_INPUT_PATTERNS: tuple[str, ...] = (
        r"\[y/n\]\s*$",
        r"\(y/n\)\s*$",
        r"\bpress enter to continue\b",
        r"\bdo you want to (?:proceed|continue)\b.*\?",
        r"\bawaiting (?:your )?(?:input|response|approval)\b",
    )

    # ── Delivery-readiness + confirmation knobs (L2) ──────────────
    # The first inbox drain waits until the child has actually produced
    # output (TUI is up and accepting input) or this many seconds pass,
    # whichever comes first. Draining the instant the child is forked
    # races the TUI's startup: os.write succeeds but the bytes are eaten
    # before the input widget exists, and the message is marked delivered
    # while the agent never saw it.
    _READY_TIMEOUT_S: float = 3.0
    # Echo-confirm window: when delivery confirmation is enabled, how long
    # to watch the PTY ring buffer for the message's id marker before
    # giving up and leaving the message queued for the next live-drain.
    _ECHO_CONFIRM_TIMEOUT_S: float = 1.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._sub_lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._pty_buffer = bytearray()
        # Set True the first time the child emits any PTY output — the
        # readiness signal the startup drain waits on.
        self._seen_output: bool = False
        # (value, read_at) TTL cache for the confirm-delivery flag so the
        # push path doesn't re-read plugin settings from disk per message.
        self._confirm_cache: tuple[bool, float] | None = None
        # Backpressure counters. The PTY ring buffer already drops
        # oldest-bytes when it exceeds BUFFER_BYTES and subscriber
        # queues silently drop on Full — visibility was missing. These
        # counters surface in pty_stats() so relaydeck doctor can flag
        # runaway agents instead of letting them silently lose output.
        self._overflow_bytes: int = 0
        self._subscriber_drops: int = 0
        self._last_overflow_event_ts: float = 0.0
        # Descendant PIDs observed while the child was alive. Snapshotted
        # on terminate()/reap so setsid'd workers (which reparent to init
        # the instant their parent dies) are still reachable for SIGKILL.
        self._tracked_pids: set[int] = set()

    # ── Command / env ────────────────────────────────────────────

    def _build_command(self) -> list[str]:
        cmd = [self.CLI] + list(self.DEFAULT_ARGS)

        # Resolve model → --model (harness-type aware)
        cli_model = self._resolve_cli_model()
        if cli_model:
            cmd.extend(["--model", cli_model])

        # No `--workspace` injection: no shipped harness CLI (pi,
        # claude, codex, opencode) actually accepts that flag.
        # `pi` and `codex` reimplement _build_command from scratch so
        # they always skipped this anyway; `claude` and `opencode` inherit
        # the base, and `claude` in particular died with "error: unknown
        # option '--workspace'" until this was removed. Workspace-scoped
        # behavior is plumbed through CODEX_HOME, --session-dir, --cd,
        # or cwd on the spawned process — not a CLI flag.
        if "command" in self.config:
            user_cmd = self.config["command"]
            if isinstance(user_cmd, list):
                cmd = user_cmd
            elif isinstance(user_cmd, str):
                cmd = user_cmd.split()

        # `config.args` — extra CLI flags appended to whatever the
        # harness composed (or to `config.command` if it overrode the
        # whole thing). Accepted forms:
        #   args: ["--dangerously-skip-permissions", "-c"]   (list)
        #   args: "--continue --dangerously-skip-permissions" (str)
        # Mirrors the `command` override but additive instead of
        # destructive — every harness inherits it via the base
        # `_build_command`, so per-type subclasses (claude-code's
        # append-system-prompt extension, codex's positional-prompt
        # form, pi's direct-build) all pick it up.
        extra = self.config.get("args")
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
        elif isinstance(extra, str) and extra.strip():
            import shlex
            cmd.extend(shlex.split(extra))
        return cmd

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        # The PTY winsize is the single source of truth for terminal
        # geometry. A stale COLUMNS/LINES inherited from whatever launched
        # the daemon would pin a TUI to that width regardless of the PTY +
        # resize — drop them so the child always trusts the winsize.
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        ws_path = self._workspace_path()
        if self.workspace:
            env["RELAYDECK_WORKSPACE"] = self.workspace  # the name
        if ws_path:
            resolved = Path(ws_path).expanduser().resolve()
            env["RELAYDECK_WORKSPACE_PATH"] = str(resolved)
            ws_path = resolved
        self._inject_git_env(env, ws_path)
        if getattr(self, "agent_id", None):
            env["RELAYDECK_AGENT_ID"] = self.agent_id
        # Orchestration-depth marker. RELAYDECK_AGENT_ID already tells a
        # spawned agent "you are relaydeck-managed"; this adds "how deep".
        # In the usual single-daemon topology every managed agent is depth
        # 1; running a relaydeck daemon *inside* a managed agent (a
        # fleet-of-fleets) makes its agents depth 2+. The relaydeck
        # skill reads this to refuse bootstrapping a runaway nested fleet.
        try:
            parent_depth = int(os.environ.get("RELAYDECK_ORCHESTRATION_DEPTH", "0"))
        except ValueError:
            parent_depth = 0
        env["RELAYDECK_ORCHESTRATION_DEPTH"] = str(max(0, parent_depth) + 1)
        if "env" in self.config:
            env.update({str(k): str(v) for k, v in self.config["env"].items()})
        return env

    def _inject_git_env(self, env: dict[str, str], ws_path: Path | str | None) -> None:
        """Expose live git/worktree facts to harness children.

        Mirrors the RELAYDECK_WORKTREE_* vars used by setup/teardown hooks
        so agents spawned into linked worktrees know their branch context."""
        if not ws_path:
            return
        from relaydeck.worktrees import workspace_git_info

        config_home = Path(self.db_path).parent.parent
        info = workspace_git_info(Path(ws_path), config_home=config_home)
        if not info["is_git"]:
            return
        if info.get("repo_root"):
            env["RELAYDECK_GIT_REPO_ROOT"] = str(info["repo_root"])
        branch = info.get("branch")
        if branch:
            env["RELAYDECK_GIT_BRANCH"] = str(branch)
        env["RELAYDECK_GIT_DIRTY"] = "1" if info.get("dirty") else "0"
        if info["is_worktree"]:
            env["RELAYDECK_WORKTREE"] = "1"
            env["RELAYDECK_WORKTREE_PATH"] = str(Path(ws_path).expanduser().resolve())
            if branch:
                env["RELAYDECK_WORKTREE_BRANCH"] = str(branch)

    def _workspace_path(self):
        """Resolve self.workspace (a registry name) to its filesystem path.
        Returns None if the workspace can't be resolved."""
        if not self.workspace:
            return None
        # Already a path?
        p = Path(self.workspace)
        if p.is_absolute() and p.is_dir():
            return p
        # Look up by name in the registry.
        try:
            from relaydeck.config import load_workspace_registry
            for w in load_workspace_registry():
                if w.name == self.workspace:
                    return w.path
        except Exception:
            pass
        return None

    def log_path(self) -> Path | None:
        # Where the child process writes its own log. Surfaced on
        # harness.exit so operators can chase a crash without knowing
        # each harness's storage layout. Subclasses override; returning
        # None means "no known log file, look at the PTY ring buffer."
        return None

    def _get_cwd(self) -> str | None:
        if "cwd" in self.config:
            return self.config["cwd"]
        ws_path = self._workspace_path()
        if ws_path:
            return str(ws_path)
        return None

    # ── Autonomy posture ─────────────────────────────────────────

    def _autonomy_mode(self) -> str:
        """Cross-harness autonomy posture for an UNATTENDED relaydeck agent
        (no human sits at the harness TUI to answer approval prompts). Set
        per-agent via `config.autonomy`:

          - "auto"   (default) run safe work autonomously, guard dangerous ops
          - "bypass"           skip all approval/sandbox checks (no guardrails)
          - "locked"           fail-safe: only the allowlist runs, rest denied
          - "manual"           inject nothing — operator drives harness flags

        Regardless of mode, every harness auto-allows the relaydeck CLI
        (peer messaging / replies / status) so fleet coordination never
        stalls — those commands also need to write ~/.relaydeck, outside any
        workspace sandbox.
        """
        val = self.config.get("autonomy")
        mode = "auto" if val is None else str(val).strip().lower()
        return mode if mode in ("auto", "bypass", "locked", "manual") else "auto"

    def _relaydeck_config_home(self) -> Path:
        """env override > ~/.relaydeck. The relaydeck CLI writes its DB here,
        so a sandboxed harness must keep this path writable."""
        override = os.environ.get("RELAYDECK_CONFIG_HOME")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".relaydeck"

    def _agent_harness_type(self) -> str:
        spec = self._load_local_spec()
        if spec and getattr(spec, "type", ""):
            return str(spec.type)
        return str(getattr(self.__class__, "HARNESS_TYPE", "") or "")

    def _config_home_path(self) -> Path:
        return self._relaydeck_config_home()

    def _resolve_cli_model(self) -> str | None:
        from relaydeck.harness_model import cli_model_for_agent

        htype = self._agent_harness_type()
        if not htype:
            return None
        return cli_model_for_agent(
            htype, self.config, config_home=self._config_home_path(),
        )

    # ── Preset validation ────────────────────────────────────────

    def _validate_preset(self) -> tuple[bool, dict | None]:
        """Validate model config for this agent's harness type.

        Native harnesses (codex/claude/cursor) reject relaydeck presets
        that resolve to a different provider. relaydeck-native runs the
        catalog check for named presets."""
        htype = self._agent_harness_type()
        if not htype:
            return True, None
        try:
            from relaydeck.harness_model import validate_harness_model

            return validate_harness_model(
                htype, self.config, config_home=self._config_home_path(),
            )
        except Exception:
            return True, None

    # ── System prompt composition ───────────────────────────────

    def _compose_system_prompt(self) -> str:
        """Build the system-prompt addendum for this agent.

        Composition order:
          1. Auto-generated identity preamble (if `inject_identity_preamble`
             on the spec is True — default). Includes the agent's id,
             workspace, purpose, and a list of peers with their purposes
             so the model sees the messaging surface in context.
          2. User-provided `spec.system_prompt` (free-form append).

        Returns an empty string when both are off/empty — the harness
        callers check truthiness before adding an `--append-system-prompt`
        flag, so an empty result has no effect.

        Per-harness adapters call this once at `_build_command` time
        and inject the result via their native mechanism (pi/claude-code:
        `--append-system-prompt`; codex: model_instructions_file).
        """
        spec = self._load_local_spec()
        parts: list[str] = []

        # 1. Identity preamble
        if spec is None or getattr(spec, "inject_identity_preamble", True):
            preamble = self._build_identity_preamble()
            if preamble:
                parts.append(preamble)

        # 2. User-supplied append
        user_prompt = (getattr(spec, "system_prompt", "") or "").strip() if spec else ""
        if user_prompt:
            parts.append(user_prompt)

        return "\n\n".join(parts).strip()

    def _load_local_spec(self):
        """Read this agent's YAML spec from disk. Returns None on miss
        (e.g. tests that instantiate HarnessAgent without a real spec)."""
        try:
            from relaydeck.config import AgentSpec
            spec_path = (
                Path.home() / ".relaydeck" / "agents" / f"{self.agent_id}.yaml"
            )
            if not spec_path.exists():
                return None
            return AgentSpec.from_yaml(spec_path)
        except Exception:
            return None

    def _build_identity_preamble(self) -> str:
        """Auto-generated "you are X, your peers are..." block.

        Reads peers via the DB (live state — picks up newly-spawned
        agents) and includes their purposes so this agent knows who
        to delegate to. Omits sections that have no content
        (purposeless agent → just identity + peer list).
        """
        spec = self._load_local_spec()
        purpose = (getattr(spec, "purpose", "") or "") if spec else ""
        ws_path = self._workspace_path()
        config_home = Path(self.db_path).parent.parent
        return compose_identity_preamble(
            self.agent_id, self.workspace, purpose, self._list_peers(),
            workspace_path=ws_path, config_home=config_home,
        )

    def _list_peers(self) -> list[dict]:
        """Other agents in the same workspace, sorted alphabetically.
        Reads from DB so a freshly-created peer is visible without
        restarting this agent."""
        if not self.workspace or not self.agent_id:
            return []
        try:
            from relaydeck.db import open_db
            conn = open_db(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT id, type, purpose FROM agents "
                    "WHERE workspace = ? AND id != ? "
                    "ORDER BY id ASC",
                    (self.workspace, self.agent_id),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception:
            return []

    def reset_session(self) -> bool:
        """Start a fresh conversation/session for this agent IN PLACE, if the
        harness supports it. Return True iff an in-place reset happened.

        Default: False — most PTY harnesses have no in-place "new session"
        knob, so the orchestrator's `reset_agent_session` falls back to a
        process restart (a clean session for every harness). A harness with a
        native reset (e.g. clearing a session transcript without killing the
        process) overrides this and returns True. Triggered by Telegram's
        `/new` / `/clear` command and the future `relaydeck agent reset`."""
        return False

    def compact(self) -> bool:
        """Ask the harness to compact its context IN PLACE via COMPACT_COMMAND.
        Returns True iff a compaction command was sent — False when the harness
        has no known in-place compaction (COMPACT_COMMAND unset). KV-safer than
        a fresh session: the conversation prefix is summarized rather than
        discarded, so the cache mostly survives."""
        cmd = self.COMPACT_COMMAND
        if not cmd:
            return False
        try:
            return bool(self.send_message(cmd))
        except Exception:
            return False

    # ── Plugin event bus ─────────────────────────────────────────

    def _emit_bus(self, event_type: str, data: dict) -> None:
        """Publish a typed event on the in-process plugin event bus.

        Use this for harness-derived semantic events (`usage.record`,
        `harness.assistant_message`, ...) that other plugins consume.
        Bus-only — does NOT go through `BaseAgent.emit` so high-volume
        content events don't bloat the events table.

        Looks up the bus in two places, in order:
          1. `self._plugin_event_bus` — set directly by tests + by
             code paths that want to scope to a non-singleton bus.
          2. The global orchestrator's `_plugin_event_bus` — the
             normal production path where the orchestrator owns the
             bus and harnesses share it.
        """
        try:
            from relaydeck.plugin import Event
            bus = getattr(self, "_plugin_event_bus", None)
            if bus is None:
                from relaydeck.orchestrator import get_orchestrator
                bus = getattr(get_orchestrator(), "_plugin_event_bus", None)
            if bus is None:
                return
            source = f"{self.CLI}-harness" if self.CLI else "harness"
            bus.emit(Event(type=event_type, data=data, source_plugin=source))
        except Exception:
            pass

    # ── PTY lifecycle ────────────────────────────────────────────

    def run(self) -> None:
        ok, invalid_payload = self._validate_preset()
        if not ok and invalid_payload is not None:
            msg = invalid_payload.get("message")
            if not msg:
                sug = invalid_payload.get("suggestion")
                msg = (
                    f"preset '{invalid_payload.get('preset')}' uses unknown model "
                    f"'{invalid_payload.get('provider')}/{invalid_payload.get('model')}'"
                    + (f"  · did you mean "
                       f"'{invalid_payload['provider']}/{sug}'?" if sug else "")
                )
            logger.warning("Agent %s: %s", self.agent_id, msg)
            self.emit("preset.invalid", invalid_payload)
            self.emit("harness.error", {"error": msg})
            self.update_status("errored", msg)
            return

        cmd = self._build_command()
        env = self._build_env()
        cwd = self._get_cwd()

        logger.info("Agent %s launching: %s", self.agent_id, " ".join(cmd))
        self.emit("harness.spawn", {"command": cmd, "cwd": cwd})

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        # `pty.openpty()` is born 0×0 — a TUI harness (pi, claude-code,
        # codex) reads that winsize at startup, sees no geometry, and
        # falls back to its built-in 80 columns, drawing a cramped
        # rectangle on its FIRST frame. The dashboard's resize widens it
        # via SIGWINCH once a tab connects, but every spawn AND every
        # relaunch starts at 80 (and a mistimed first fit leaves it there)
        # — so the narrow terminal "keeps coming back". Seed a sensible
        # geometry BEFORE the fork so the child draws full-width from
        # frame 1; the dashboard then refines it to the exact pane.
        try:
            init_cols = max(20, int(self.config.get("init_cols") or 200))
            init_rows = max(5, int(self.config.get("init_rows") or 50))
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", init_rows, init_cols, 0, 0))
        except (OSError, ValueError, TypeError):
            pass

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=cwd, env=env,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
                # Claim the pty as the child's controlling terminal so
                # SIGWINCH is delivered on resize (see _acquire_controlling_tty).
                preexec_fn=_acquire_controlling_tty,
            )
        except FileNotFoundError:
            os.close(slave_fd); os.close(master_fd)
            self._master_fd = None
            # Popen raises FileNotFoundError for TWO distinct causes: the
            # command isn't on PATH, OR the cwd doesn't exist. They're easy to
            # confuse — a missing workspace dir would otherwise be misreported
            # as "command not found: <cli>". Disambiguate with a cwd check.
            if cwd and not os.path.isdir(str(cwd)):
                err = f"workspace directory not found: {cwd}"
            else:
                err = f"command not found: {cmd[0]}"
            self.emit("harness.error", {"error": err})
            self.update_status("errored", err)
            return
        except Exception as exc:
            os.close(slave_fd); os.close(master_fd)
            self._master_fd = None
            err = f"{type(exc).__name__}: {exc}"
            self.emit("harness.error", {"error": err})
            self.update_status("errored", err)
            return

        os.close(slave_fd)  # child owns the slave end

        # The startup drain is deferred into the read loop and gated on
        # readiness (first PTY output, or _READY_TIMEOUT_S). Draining
        # right here — the instant the child is forked — races the TUI's
        # startup: os.write succeeds but the input widget doesn't exist
        # yet, so the message is marked delivered while the agent never
        # sees it. See the `first drain` block below.
        spawn_ts = time.time()
        first_drain_done = False

        chunk_size = int(self.config.get("chunk_size") or self.CHUNK_SIZE)
        # Live-drain cadence: every `_LIVE_DRAIN_INTERVAL` seconds we
        # check the inbox for pending messages, REGARDLESS of whether
        # the PTY is currently emitting bytes. The earlier version
        # gated this on `if not ready` (PTY idle) — that fails for
        # interactive TUIs (pi, claude-code) which refresh the screen
        # constantly, so select() almost never returns idle and the
        # drain effectively never fires. Time-gated drains run cheaply
        # (one indexed SELECT) and on a predictable cadence whether
        # the harness is busy rendering or not.
        last_live_drain = time.time()

        try:
            while not self.stop_flag.is_set():
                if self._proc.poll() is not None:
                    self._drain(master_fd, chunk_size)
                    break

                now = time.time()
                if not first_drain_done:
                    # First drain: wait for the TUI to be input-ready
                    # (it has emitted output) or until the grace window
                    # expires, then drain once and fall into the normal
                    # live-drain cadence.
                    if self._seen_output or (now - spawn_ts) >= self._READY_TIMEOUT_S:
                        self._drain_pending_messages()
                        first_drain_done = True
                        last_live_drain = now
                elif now - last_live_drain >= self._LIVE_DRAIN_INTERVAL:
                    self._drain_pending_messages()
                    last_live_drain = now

                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                try:
                    data = os.read(master_fd, chunk_size)
                except OSError:
                    break
                if not data:
                    break
                self._broadcast(data)
        finally:
            self._reap()
            try:
                os.close(master_fd)
            except OSError:
                pass
            self._master_fd = None
            self._broadcast_eof()

        rc = self._proc.returncode if self._proc else None
        try:
            log_path = self.log_path()
        except Exception:
            log_path = None
        self.emit("harness.exit", {
            "returncode": rc,
            "log_path": str(log_path) if log_path else None,
        })

    def _drain(self, fd: int, chunk_size: int) -> None:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.0)
            if not ready:
                return
            try:
                data = os.read(fd, chunk_size)
            except OSError:
                return
            if not data:
                return
            self._broadcast(data)

    # How long to let the process tree exit gracefully after SIGTERM
    # before escalating to SIGKILL. Kept well under the orchestrator's
    # 10s thread-join so reap always finishes inside the join window.
    _REAP_GRACE_S: float = 4.0
    _REAP_KILL_S: float = 1.5

    def _snapshot_tree(self) -> set[int]:
        """Union the child's current descendants into `self._tracked_pids`
        and return the full tree (direct child + all tracked descendants).

        Called while the child is (ideally) still alive so setsid'd
        workers are still chained to it by PPID; once captured they stay
        tracked even after they reparent to init on the child's death."""
        if self._proc is None:
            return set(self._tracked_pids)
        root = self._proc.pid
        with suppress(Exception):
            self._tracked_pids |= _descendant_pids(root)
        return {root} | set(self._tracked_pids)

    def _reap(self) -> None:
        """Graceful, leak-free teardown of the whole harness process tree.

        SIGTERM the child group + every tracked descendant, wait for the
        tree to drain, then SIGKILL whatever survives. Walking the tree
        (not just the child's process group) is what stops pi's setsid'd
        workers from leaking — see `_descendant_pids`."""
        if self._proc is None:
            return
        tree = self._snapshot_tree()
        already_dead = self._proc.poll() is not None

        # Graceful pass: signal the child's process group (cheap, catches
        # same-group children) AND each tracked pid individually (catches
        # the setsid'd workers in their own groups).
        if not already_dead:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        _signal_pids(tree, signal.SIGTERM)

        deadline = time.time() + self._REAP_GRACE_S
        with suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=self._REAP_GRACE_S)
        survivors = _wait_pids_gone(tree, deadline)
        if not survivors and self._proc.poll() is not None:
            self._tracked_pids.clear()
            return

        # Escalation pass: SIGKILL the group + every survivor.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        _signal_pids(survivors or tree, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=self._REAP_KILL_S)
        _wait_pids_gone(survivors or tree, time.time() + self._REAP_KILL_S)
        self._tracked_pids.clear()

    def terminate(self) -> None:
        """External stop nudge (from the orchestrator). Snapshots the tree
        while the child is alive and SIGTERMs every member, so the read
        loop unblocks and `_reap` has the full descendant set to escalate
        on. Non-blocking: escalation/waiting happens in `_reap`."""
        if self._proc is None:
            return
        tree = self._snapshot_tree()
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        _signal_pids(tree, signal.SIGTERM)

    # ── Bidirectional PTY: input + resize ────────────────────────

    def send_input(self, data: bytes) -> bool:
        """Forward bytes to the child's stdin via the PTY master."""
        if self._master_fd is None:
            return False
        try:
            os.write(self._master_fd, data)
            return True
        except OSError:
            return False

    def resize(self, cols: int, rows: int) -> bool:
        """TIOCSWINSZ to tell the child the terminal geometry changed."""
        if self._master_fd is None:
            return False
        try:
            fcntl.ioctl(
                self._master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0),
            )
            return True
        except OSError:
            return False

    def _normalize_for_submit(self, message: str) -> str:
        """Flatten a message body for safe injection into a raw-mode TUI.

        Collapses runs of CR/LF into single spaces (gated on
        SUBMIT_COLLAPSE_NEWLINES) so a multi-line body can't partial-
        submit: a bare `\\n` is a submit keystroke in some input widgets
        and a literal glyph in others. The canonical body stays intact
        in the DB — this only shapes what we type into the PTY.
        """
        if self.SUBMIT_COLLAPSE_NEWLINES:
            message = re.sub(r"[\r\n]+", " ", message)
        return message.strip()

    def send_message(self, message: str) -> bool:
        """Deliver a line of text into the child's stdin and submit it
        as if the user pressed Enter.

        Returns True only when every PTY write succeeded — the drain and
        orchestrator-push paths check this so a write failure doesn't
        silently mark the message injected. (Note: a True here means the
        bytes reached the PTY, not that the harness's input widget
        accepted them; echo-confirm — opt-in — closes that gap.)

        Why `\\r` not `\\n`: pi / claude-code / codex run their TUIs in
        raw (cbreak) mode where the line discipline doesn't translate
        `\\r`↔`\\n`. The widget treats `\\r` (0x0D) as Enter and `\\n`
        (0x0A) as a literal newline glyph — sending `\\n` paints text
        into the box without submitting.

        Split vs single write: paste-debouncing TUIs (Claude Code's Ink)
        treat `body + \\r` arriving in one read() as a paste and swallow
        the Enter. SUBMIT_SPLIT writes the body and the CR separately
        (with SUBMIT_DELAY_S between) so the CR lands as its own keystroke.
        Both knobs are per-agent overridable via config.
        """
        text = self._normalize_for_submit(message)
        split = bool(self.config.get("submit_split", self.SUBMIT_SPLIT))
        if split:
            if not self.send_input(text.encode()):
                return False
            delay = float(self.config.get("submit_delay_s", self.SUBMIT_DELAY_S))
            if delay > 0:
                time.sleep(delay)
            return self.send_input(b"\r")
        return self.send_input(text.encode() + b"\r")

    def _confirm_delivery_enabled(self) -> bool:
        """Echo-confirm is opt-in: a per-agent config flag wins, else the
        `messaging.confirm_delivery` plugin setting (default off). When
        off, delivery is marked on a successful PTY write (the historical
        optimistic behaviour); when on, the *orchestrator push* path
        additionally waits for the message id to echo back in the PTY
        before marking it delivered. (Only the push path — never the
        read-loop drain — calls _confirm_echo; see _drain_pending_messages
        for why.)"""
        override = self.config.get("confirm_delivery")
        if override is not None:
            return bool(override)
        # This is consulted on every live push, and get_setting re-reads
        # the settings YAML each call (deliberately, so dashboard edits
        # take effect live). Cache the read for a few seconds so a chatty
        # fleet doesn't hit disk per message — an opt-in reliability flag
        # tolerates a few seconds of staleness.
        now = time.time()
        cached = getattr(self, "_confirm_cache", None)
        if cached is not None and now - cached[1] < 5.0:
            return cached[0]
        try:
            from relaydeck.plugin_settings import get_setting
            val = bool(get_setting("messaging", "confirm_delivery", default=False))
        except Exception:
            val = False
        self._confirm_cache = (val, now)
        return val

    def _confirm_echo(self, marker: str, *, timeout: float | None = None,
                      interval: float = 0.1) -> bool:
        """Watch the PTY ring buffer for `marker` (a message id) to appear
        in the harness's rendered output, i.e. the input widget actually
        accepted what we typed. Returns True as soon as it's seen, False
        if `timeout` elapses first.

        Best-effort and conservative: an empty/whitespace marker can't be
        confirmed, so we return True (nothing to look for) rather than
        wedging a message that simply has no id in its format template.
        """
        target = _flatten_for_match(marker)
        if not target:
            return True
        if timeout is None:
            timeout = self._ECHO_CONFIRM_TIMEOUT_S
        deadline = time.time() + max(0.0, timeout)
        while True:
            snap = _flatten_for_match(
                self.get_pty_buffer().decode("utf-8", "replace")
            )
            if target in snap:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    def _drain_pending_messages(self) -> int:
        """Push every still-pending message to this agent's PTY and
        mark each `injected_at` on success. Called once at spawn
        time, immediately after the PTY child becomes writable.

        Reads/writes through `self.db_path` — the same SQLite file
        the orchestrator's `send_message_to` uses. PTY-write failures
        leave `injected_at` NULL so the message survives and retries
        on the next start; never silent-loss.

        Returns the count of messages successfully injected. Public
        enough to be driven by tests without spawning a real child.
        """
        try:
            from relaydeck.messages import (
                drain_pending,
                mark_injected,
                record_delivery_failure,
                render_format,
            )
        except Exception as exc:
            logger.warning("Inbox drain import failed for %s: %s",
                           self.agent_id, exc)
            return 0

        # NB: no echo-confirm here on purpose. This method runs ON the
        # PTY read-loop thread (see run()), so blocking to watch the ring
        # buffer would deadlock: the buffer is only refilled by _broadcast
        # later in the same loop, so the echo we're waiting for can never
        # arrive while we wait. Echo-confirm lives in the orchestrator
        # push path (a different thread, with the read loop concurrently
        # filling the buffer). The drain stays optimistic — readiness-
        # gated at startup, retried every _LIVE_DRAIN_INTERVAL — and acts
        # as the best-effort fallback for whatever the push couldn't
        # confirm.
        injected = 0
        try:
            for msg in drain_pending(self.agent_id, db_path=self.db_path):
                text = render_format(msg, agent_config=self.config)
                try:
                    ok = self.send_message(text)
                except Exception as exc:
                    record_delivery_failure(
                        msg.id, f"send raised: {exc}", db_path=self.db_path,
                    )
                    logger.warning("Inbox drain send raised for %s: %s",
                                   self.agent_id, exc)
                    continue
                if ok:
                    mark_injected(msg.id, rendered_body=text, db_path=self.db_path)
                    injected += 1
                else:
                    # PTY write returned False — typically pre-fork or
                    # mid-shutdown. Record the failure so the retry cap
                    # eventually kicks in instead of looping forever
                    # on every restart.
                    record_delivery_failure(
                        msg.id, "send_message returned False",
                        db_path=self.db_path,
                    )
        except Exception as exc:
            logger.warning("Inbox drain failed for %s: %s", self.agent_id, exc)
        return injected

    # ── PTY subscriber pool ──────────────────────────────────────

    def subscribe_pty(self) -> queue.Queue:
        """Register a subscriber. Returns a Queue receiving `bytes` chunks;
        a final `None` is pushed when the child exits."""
        q: queue.Queue = queue.Queue(maxsize=1024)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe_pty(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def get_pty_buffer(self) -> bytes:
        """Recent PTY bytes (up to BUFFER_BYTES). Used to bootstrap a
        freshly-connected WebSocket so the user sees screen context."""
        return bytes(self._pty_buffer)

    def detect_awaiting_input(self, screen: str) -> bool:
        """Does the rendered screen show this harness blocked on the operator?

        Read-only classification used by the semantic-status engine — it never
        touches the PTY. Default matches `AWAITING_INPUT_PATTERNS` against the
        screen tail; subclasses can override for prompt shapes a regex can't
        capture. Returning False just means "no blocked-prompt detected", which
        the engine treats as working/idle as usual."""
        from relaydeck.semantic_engine import screen_matches_any

        return screen_matches_any(screen, self.AWAITING_INPUT_PATTERNS)

    def pty_stats(self) -> dict[str, int]:
        """Backpressure counters — bytes dropped from the ring buffer
        and chunks dropped from full subscriber queues. Operators can
        diff these snapshots to spot a runaway agent that's flooding
        the WebSocket pipe."""
        return {
            "buffer_bytes": len(self._pty_buffer),
            "buffer_cap": self.BUFFER_BYTES,
            "overflow_bytes": self._overflow_bytes,
            "subscriber_drops": self._subscriber_drops,
            "subscribers": len(self._subscribers),
        }

    def _broadcast(self, data: bytes) -> None:
        if data and not self._seen_output:
            # First bytes from the child — the TUI is up and the startup
            # drain may now safely fire.
            self._seen_output = True
        with self._sub_lock:
            subs = list(self._subscribers)
        sub_dropped = 0
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                self._subscriber_drops += 1
                sub_dropped += 1
        if sub_dropped:
            try:
                from relaydeck.metrics import record_pty_subscriber_drop
                record_pty_subscriber_drop(self.agent_id, sub_dropped)
            except Exception:
                pass
        self._pty_buffer.extend(data)
        overflow = len(self._pty_buffer) - self.BUFFER_BYTES
        if overflow > 0:
            del self._pty_buffer[:overflow]
            self._overflow_bytes += overflow
            try:
                from relaydeck.metrics import record_pty_overflow
                record_pty_overflow(self.agent_id, overflow)
            except Exception:
                pass
            # Rate-limit the bus event to one per 30s per agent — a
            # truly runaway producer would otherwise drown the bus
            # itself in overflow notifications. Operators don't need
            # the event for every overflow; they need to know the
            # condition is happening.
            now = time.time()
            if now - self._last_overflow_event_ts >= 30.0:
                self._last_overflow_event_ts = now
                try:
                    self._emit_bus("harness.pty_overflow", {
                        "agent_id": self.agent_id,
                        "overflow_bytes": self._overflow_bytes,
                        "subscriber_drops": self._subscriber_drops,
                    })
                except Exception:
                    pass

    def _broadcast_eof(self) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
