"""
`relaydeck view` — built-in textual TUI.

A single-window viewer for an entire relaydeck fleet: workspaces sidebar
on the left, focused-agent PTY pane on the right, message tail
below. One process, no tmux required.

Documented as ONE of several viewers — `relaydeck workspace view` still
supports the tmux and ghostty backends for users who prefer those
window managers. `relaydeck view` is the built-in default.

## Architecture

The TUI is a `textual.App` running in the foreground; the daemon
runs in its own process as usual. The two communicate over the
same HTTP/SSE/WS surface every other client uses:

  - `GET /api/agents`         — initial fleet list
  - `GET /api/events`        — live daemon event stream
  - `WS  /api/agents/{id}/term`           — bidirectional PTY pipe

Pure rendering goes through `pyte` (already a dependency for the
screen-snapshot endpoint), so the same VT-100 emulator that powers
`relaydeck agent screen` also powers the live view pane. One emulator,
two surfaces.

## Detach

`Ctrl+B` followed by `D` quits the app (tmux muscle memory). The PTY child
keeps running in the daemon; this is a viewer, not a session.

## Resize

Textual handles SIGWINCH and dispatches `on_resize`. The PTY pane
forwards new geometry to the daemon as a `\\x01 "<cols> <rows>"`
frame so the harness's child sees a real SIGWINCH (existing
behavior on the `/term` endpoint).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Pure helpers (testable without textual) ────────────────────────


SEMANTIC_STATUS_GLYPH: dict[str | None, str] = {
    "working": "●",          # solid dot = doing something
    "awaiting-input": "◉",   # ringed dot = waiting on human
    "complete-unread": "◆",  # diamond = result not yet seen
    "idle": "◌",             # hollow dot = quiet
    None: "·",               # no signal yet
}

SEMANTIC_STATUS_COLOR: dict[str | None, str] = {
    "working": "cyan",
    "awaiting-input": "yellow",
    "complete-unread": "green",
    "idle": "dim",
    None: "dim",
}


def status_badge(semantic: str | None) -> tuple[str, str]:
    """Return (glyph, color) for a semantic status. The CLI's
    `workspace list` and the TUI sidebar render the same badge —
    same visual language across surfaces."""
    return (
        SEMANTIC_STATUS_GLYPH.get(semantic, "·"),
        SEMANTIC_STATUS_COLOR.get(semantic, "dim"),
    )


# Common textual key-name → PTY byte sequence. Pulled into a
# module-level dict so the mapping is one place to audit when we
# add support for more keys. Not exhaustive — printable characters
# and most ctrl+letter chords are derived in `key_to_bytes` rather
# than listed here.
_KEY_TO_BYTES: dict[str, bytes] = {
    "enter": b"\r",
    "return": b"\r",
    "tab": b"\t",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
    "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}


def key_to_bytes(key: str, character: str | None = None) -> bytes | None:
    """Translate a textual key event (a name + optional character)
    into the PTY byte sequence the harness's child terminal expects.

    Returns `None` for keys the TUI itself consumes (the detach
    prefix, the quit shortcut) — the caller is responsible for
    filtering those before invoking us.

    Pure, exhaustively-tested in `tests/test_view.py`. The harness
    is sensitive to byte-perfect input (claude-code's editor mode
    looks at \\x7f vs \\x08 for backspace, codex expects \\x1b[A
    for arrow-up, etc.) — a regex-style "close enough" mapping
    would silently break TUI navigation.
    """
    # Special-case the known names first (most specific → least).
    if key in _KEY_TO_BYTES:
        return _KEY_TO_BYTES[key]

    # ctrl+letter: textual names these "ctrl+a"…"ctrl+z" (lowercase).
    if key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
        return bytes([ord(key[5].lower()) - ord("a") + 1])

    # A printable single character — send it verbatim. The
    # `character` argument is what textual would have appended to
    # an input field; use it when present so e.g. shift+a delivers
    # "A" not "a".
    if character and len(character) == 1 and character.isprintable():
        return character.encode("utf-8")
    if len(key) == 1 and key.isprintable():
        return key.encode("utf-8")

    return None


def detach_state(prev: str, key: str) -> tuple[str, bool]:
    """Two-key detach state machine — tmux prefix + d.

    `prev` is the previous prefix state (`""` or `"prefix"`). Given
    the new `key`, returns `(new_prev, should_quit)`. Pure so we
    can pin the entire keybinding contract in tests rather than
    boot a real terminal app.
    """
    if prev == "prefix":
        if key in ("d", "D"):
            return "", True
        # Any other key cancels the prefix without acting.
        return "", False
    if key == "ctrl+b":
        return "prefix", False
    return prev, False


@dataclass(frozen=True)
class AgentRow:
    """Flat row for the sidebar — one per agent. Workspace
    grouping happens in the rendering layer; this just carries
    the data."""

    id: str
    workspace: str
    status: str            # process-level (running/stopped/...)
    semantic_status: str | None
    purpose: str = ""
    type: str = ""
    name: str = ""
    tags: tuple[str, ...] = ()

    @property
    def is_running(self) -> bool:
        return self.status == "running"


def group_by_workspace(rows: list[AgentRow]) -> dict[str, list[AgentRow]]:
    """Stable workspace grouping for the sidebar. Sorted by
    workspace name then agent id so the tree doesn't reshuffle
    every refresh."""
    grouped: dict[str, list[AgentRow]] = {}
    for r in rows:
        grouped.setdefault(r.workspace, []).append(r)
    for ws in grouped:
        grouped[ws].sort(key=lambda r: r.id)
    return dict(sorted(grouped.items()))


def _rel_time(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = max(0, int(time.time() - float(ts)))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _compact_int(value: Any) -> str:
    try:
        n = int(value or 0)
    except Exception:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _truncate(value: str, width: int) -> str:
    value = str(value or "")
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


# ── Textual app (optional import — only loaded when `relaydeck view`
#    actually runs, so the bare `relaydeck` import doesn't drag in the
#    textual dependency for users who never open the TUI) ─────────


def _import_textual() -> Any:
    """Lazy import so unit tests of the pure helpers above don't
    need textual installed."""
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "`relaydeck view` requires textual. Install with: "
            "`uv pip install textual` or use the bundled "
            "extra: `pip install 'relaydeck[tui]'`"
        ) from exc
    from textual.app import App
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.widget import Widget
    from textual.widgets import Header, RichLog, Static, Tree

    return {
        "App": App,
        "Binding": Binding,
        "Horizontal": Horizontal,
        "Vertical": Vertical,
        "Header": Header,
        "RichLog": RichLog,
        "Static": Static,
        "Tree": Tree,
        "Widget": Widget,
        "reactive": reactive,
    }


def run_view(workspace: str | None = None) -> int:
    """Entry point used by `relaydeck view`. Returns a process exit
    code so the CLI can pass it through to the shell."""
    from relaydeck.sdk import RemoteHost

    try:
        host = RemoteHost.from_local()
    except RuntimeError as exc:
        # No daemon running / no token on disk. Tell the user.
        print(f"relaydeck view: {exc}", flush=True)
        print("Hint: start the daemon first with `relaydeck serve`.", flush=True)
        return 2

    initial_workspace = workspace
    scoped_to_workspace = bool(workspace)
    if initial_workspace is None:
        initial_workspace, source = resolve_initial_workspace()
        scoped_to_workspace = source in {"env", "cwd"}

    t = _import_textual()
    app = _build_app(
        t,
        host,
        initial_workspace=initial_workspace,
        scoped_to_workspace=scoped_to_workspace,
    )
    app.run()
    return 0


def resolve_initial_workspace(cwd: Any = None) -> tuple[str | None, str]:
    """Resolve the workspace `relaydeck view` should initially show.

    Returns `(workspace, source)`. When source is `cwd` or `env`, the
    TUI treats that as an explicit operator gesture and scopes the
    view to only that workspace. A durable `workspace set` default
    focuses the workspace but still leaves the whole fleet visible.
    """
    try:
        from relaydeck.state import resolve_workspace_source

        return resolve_workspace_source(cwd)
    except Exception:
        return None, "unset"


def _build_app(
    t: dict,
    host: Any,
    *,
    initial_workspace: str | None,
    scoped_to_workspace: bool = False,
):
    """Construct the textual App. Split out so a test can drive
    the constructor without `run()`ing into the terminal."""

    app_cls = t["App"]
    binding_cls = t["Binding"]
    horizontal_cls = t["Horizontal"]
    vertical_cls = t["Vertical"]
    rich_log_cls = t["RichLog"]
    static_cls = t["Static"]
    tree_cls = t["Tree"]

    class RelaydeckView(app_cls):
        TITLE = "relaydeck view"

        CSS = """
        Screen {
            layout: vertical;
            background: #11101b;
            color: #d7d3ef;
        }
        #topbar {
            height: 3;
            background: #171525;
            color: #9288ce;
            padding: 0 2;
            border-bottom: solid #302b48;
        }
        #body {
            height: 1fr;
        }
        #sidebar {
            width: 32;
            background: #141321;
            border-right: solid #2c2940;
            padding: 1 1;
        }
        .rail-title {
            height: 1;
            color: #7c8fdc;
            text-style: bold;
            margin: 0 0 1 0;
        }
        .rail-hint {
            height: 2;
            color: #706a92;
            margin: 1 0;
        }
        #spaces {
            height: 8;
            background: #141321;
        }
        #agents {
            height: 1fr;
            background: #141321;
        }
        #side_stats {
            height: 8;
            border-top: solid #2c2940;
            color: #9d98bc;
            padding: 1 0 0 0;
        }
        #main {
            width: 1fr;
            layout: vertical;
            background: #11101b;
        }
        #tabbar {
            height: 3;
            background: #171525;
            color: #9f94dd;
            padding: 0 2;
            border-bottom: solid #29253b;
        }
        #hero {
            height: 7;
            background: #11101b;
            padding: 1 2;
            border-bottom: solid #2c2940;
        }
        #avatar {
            width: 12;
            color: #ff875f;
            text-style: bold;
        }
        #agent_meta {
            width: 1fr;
            color: #d7d3ef;
        }
        #agent_stats {
            width: 32;
            color: #b9b2d8;
        }
        #pty {
            height: 1fr;
            background: #0d0c14;
            border: solid #403a5d;
            padding: 0 1;
        }
        #msgs {
            height: 7;
            background: #12111d;
            border-top: solid #403a5d;
            padding: 0 1;
        }
        #status {
            height: 3;
            background: #171525;
            color: #9d98bc;
            padding: 0 2;
            border-top: solid #2c2940;
        }
        """

        BINDINGS = [
            binding_cls("ctrl+b", "_prefix", "tmux-style prefix", show=False),
        ]

        def __init__(self, host_: Any, initial_workspace: str | None = None):
            super().__init__()
            self._host = host_
            self._initial_workspace = initial_workspace
            self._scoped_to_workspace = scoped_to_workspace
            self._focused_agent: str | None = None
            self._focused_workspace: str | None = initial_workspace
            self._detach_prefix = ""
            self._ws_tasks: list[asyncio.Task] = []
            self._events_task: asyncio.Task | None = None
            self._events_stop: threading.Event | None = None
            self._pty_task: asyncio.Task | None = None
            self._pty_agent: str | None = None
            self._pty_send_queue: asyncio.Queue | None = None
            self._pty_screen: Any = None
            self._pty_cols: int = 0
            self._pty_rows: int = 0
            self._agents: list[AgentRow] = []
            self._worker_rows: list[dict[str, Any]] = []
            self._presets: list[dict[str, Any]] = []
            self._plugins: list[dict[str, Any]] = []
            self._focused_agent_stats: dict[str, Any] = {}
            self._compose_mode = False
            self._compose_buffer = ""
            self._refresh_pending = False

        def compose(self):
            yield static_cls(self._render_topbar(), id="topbar")
            with horizontal_cls(id="body"):
                with vertical_cls(id="sidebar"):
                    yield static_cls("spaces", classes="rail-title")
                    yield tree_cls("relaydeck", id="spaces")
                    yield static_cls("detach ^B D\nmessage ^B M  help ^B ?", classes="rail-hint")
                    yield static_cls("agents", classes="rail-title")
                    yield tree_cls("fleet", id="agents")
                    yield static_cls("", id="side_stats")
                with vertical_cls(id="main"):
                    yield static_cls("", id="tabbar")
                    with horizontal_cls(id="hero"):
                        yield static_cls("", id="avatar")
                        yield static_cls("", id="agent_meta")
                        yield static_cls("", id="agent_stats")
                    yield rich_log_cls(id="pty", wrap=False, markup=False, highlight=False)
                    yield rich_log_cls(id="msgs", wrap=True, markup=True, highlight=False)
            yield static_cls("", id="status")

        async def on_mount(self) -> None:
            await self._refresh_snapshot()
            self._events_task = asyncio.create_task(self._event_driver())
            # Safety heartbeat only; the normal path is the daemon event stream.
            self.set_interval(30.0, self._refresh_snapshot)

        async def on_unmount(self) -> None:
            if self._events_stop is not None:
                self._events_stop.set()
            if self._events_task is not None and not self._events_task.done():
                self._events_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._events_task
            await self._clear_pty()

        def _render_topbar(self) -> str:
            return (
                "[b #d9d4ff]relaydeck[/]  local orchestration console"
                "    [#706a92]agents · workspaces · plugins · models · workers[/]\n"
                "[#56516f]prefix[/] [#c8beff]Ctrl+B[/] "
                "[#56516f]for commands  ·  daemon[/] "
                f"[#9de1ff]{getattr(self._host, 'daemon_url', 'local')}[/]"
            )

        async def _refresh_snapshot(self) -> None:
            try:
                infos = await asyncio.to_thread(self._host.agents.list)
            except Exception as exc:
                self.query_one("#status", static_cls).update(f"[red]daemon read failed:[/] {exc}")
                return
            self._agents = [
                AgentRow(
                    id=info.id,
                    workspace=info.workspace or "(none)",
                    status=info.status,
                    semantic_status=info.semantic_status,
                    purpose=info.purpose,
                    type=info.type,
                    name=info.name,
                    tags=info.tags,
                )
                for info in infos
                if (
                    not self._scoped_to_workspace
                    or not self._focused_workspace
                    or (info.workspace or "(none)") == self._focused_workspace
                )
            ]
            self._worker_rows = await asyncio.to_thread(self._api_list, "/api/workers")
            self._presets = await asyncio.to_thread(self._api_list, "/api/presets")
            self._plugins = await asyncio.to_thread(self._api_list, "/api/plugins")
            if self._focused_workspace is None:
                if self._initial_workspace:
                    self._focused_workspace = self._initial_workspace
                elif self._agents:
                    self._focused_workspace = self._agents[0].workspace
            if self._focused_agent is None:
                candidates = [
                    a for a in self._agents
                    if not self._focused_workspace or a.workspace == self._focused_workspace
                ]
                if candidates:
                    running = [a for a in candidates if a.is_running]
                    self._focused_agent = (running or candidates)[0].id
            self._render_sidebars()
            await self._render_agent_context()
            if self._focused_agent and (self._pty_task is None or self._pty_task.done()):
                await self._start_pty(self._focused_agent)

        async def _event_driver(self) -> None:
            """Bridge daemon SSE events into Textual's event loop."""
            q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            stop = threading.Event()
            self._events_stop = stop
            loop = asyncio.get_running_loop()

            def _push(event: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(q.put_nowait, event)

            thread = threading.Thread(
                target=_sse_worker,
                args=(self._host, "/api/events", _push, stop),
                name="relaydeck-view-events",
                daemon=True,
            )
            thread.start()

            while True:
                event = await q.get()
                typ = str(event.get("type") or event.get("event") or "")
                if typ == "relaydeck.view.sse_down":
                    if not self._compose_mode:
                        self.query_one("#status", static_cls).update(
                            f"[yellow]event stream reconnecting:[/] {event.get('error', '')}"
                        )
                    continue
                if typ.startswith(("agent.", "workspace.", "worktree.", "worker.", "skills.")):
                    self._schedule_snapshot_refresh()
                elif typ in {"usage.record", "model.invocation", "harness.assistant_message"}:
                    self._schedule_snapshot_refresh(delay=1.0)
                elif typ == "agent.message":
                    data = event.get("payload") or event.get("data") or {}
                    if (
                        not self._focused_workspace
                        or data.get("workspace") == self._focused_workspace
                    ):
                        self._schedule_snapshot_refresh(delay=0.2)

        def _schedule_snapshot_refresh(self, delay: float = 0.35) -> None:
            if self._refresh_pending:
                return
            self._refresh_pending = True

            async def _run() -> None:
                try:
                    await asyncio.sleep(delay)
                    await self._refresh_snapshot()
                finally:
                    self._refresh_pending = False

            asyncio.create_task(_run())

        def _api_list(self, path: str) -> list[dict[str, Any]]:
            try:
                out = self._host._request(path)
                if isinstance(out, list):
                    return [dict(v) for v in out if isinstance(v, dict)]
                if isinstance(out, dict):
                    # Tasks endpoint returns {"tasks": [...]}.
                    for key in ("tasks", "items", "data"):
                        val = out.get(key)
                        if isinstance(val, list):
                            return [dict(v) for v in val if isinstance(v, dict)]
                return []
            except Exception:
                return []

        def _render_sidebars(self) -> None:
            spaces = self.query_one("#spaces", tree_cls)
            agents_tree = self.query_one("#agents", tree_cls)
            spaces.clear()
            agents_tree.clear()
            spaces.root.label = "workspaces"
            agents_tree.root.label = "agents"
            spaces.root.expand()
            agents_tree.root.expand()

            grouped = group_by_workspace(self._agents)
            for ws, rows in grouped.items():
                running = sum(1 for r in rows if r.is_running)
                marker = "●" if ws == self._focused_workspace else "○"
                color = "green" if running else "dim"
                spaces.root.add_leaf(
                    f"[{color}]{marker}[/] {ws} [dim]{running}/{len(rows)}[/]",
                    data=f"ws:{ws}",
                )

            shown = [
                a for a in self._agents
                if not self._focused_workspace or a.workspace == self._focused_workspace
            ]
            for ag in shown:
                glyph, color = status_badge(ag.semantic_status)
                active = "▸" if ag.id == self._focused_agent else " "
                typ = ag.type or "agent"
                agents_tree.root.add_leaf(
                    f"{active} [{color}]{glyph}[/] [b]{ag.id}[/]\n"
                    f"    [dim]{ag.status or '-'} · {typ}[/]",
                    data=f"agent:{ag.id}",
                )

            running_workers = sum(1 for w in self._worker_rows if w.get("status") == "running")
            loaded_plugins = len(self._plugins)
            presets = len(self._presets)
            self.query_one("#side_stats", static_cls).update(
                "[#7c8fdc]system[/]\n"
                f"workers   [#9de1ff]{running_workers}/{len(self._worker_rows)}[/]\n"
                f"plugins   [#d5ff83]{loaded_plugins}[/]\n"
                f"models    [#ffcc66]{presets} profiles[/]\n"
                f"focus     [#c8beff]{self._focused_workspace or '-'}[/]\n"
                f"scope     [#c8beff]{'cwd workspace' if self._scoped_to_workspace else 'fleet'}[/]"
            )

        async def action_refresh(self) -> None:
            await self._refresh_snapshot()

        def action__prefix(self) -> None:
            self._detach_prefix = "prefix"
            self.query_one("#status", static_cls).update(
                "Ctrl+B armed — press D to detach"
            )

        def action_help(self) -> None:
            self.query_one("#status", static_cls).update(
                "Ctrl+B D detach · Ctrl+B M message focused agent · "
                "Ctrl+B S start focused agent · Ctrl+B R refresh · "
                "normal keys go to terminal"
            )

        def _begin_compose(self) -> None:
            self._compose_mode = True
            self._compose_buffer = ""
            self.query_one("#status", static_cls).update(
                "[#c8beff]message mode[/] type message, Enter sends, Esc cancels"
            )

        async def _send_compose(self) -> None:
            body = self._compose_buffer.strip()
            self._compose_mode = False
            self._compose_buffer = ""
            agent = next((a for a in self._agents if a.id == self._focused_agent), None)
            if not body:
                self.query_one("#status", static_cls).update("message cancelled")
                return
            if agent is None:
                self.query_one("#status", static_cls).update("[red]no focused agent[/]")
                return
            try:
                result = await asyncio.to_thread(
                    self._host.agents.send_message,
                    agent.id,
                    body,
                    workspace=agent.workspace,
                    from_="user:tui",
                )
            except Exception as exc:
                self.query_one("#status", static_cls).update(f"[red]message failed:[/] {exc}")
                return
            pending = len(getattr(result, "pending", ()) or ())
            injected = len(getattr(result, "injected", ()) or ())
            self.query_one("#status", static_cls).update(
                f"[green]sent[/] to {agent.id} "
                f"([#d5ff83]{injected} injected[/], [#ffcc66]{pending} queued[/])"
            )
            await self._render_messages(agent)

        async def _start_focused_agent(self) -> None:
            agent = next((a for a in self._agents if a.id == self._focused_agent), None)
            if agent is None:
                self.query_one("#status", static_cls).update("[red]no focused agent[/]")
                return
            if agent.is_running:
                self.query_one("#status", static_cls).update(f"{agent.id} is already running")
                return
            try:
                await asyncio.to_thread(
                    self._host._request,
                    f"/api/agents/{agent.id}/start",
                    method="POST",
                )
            except Exception as exc:
                self.query_one("#status", static_cls).update(f"[red]start failed:[/] {exc}")
                return
            self.query_one("#status", static_cls).update(f"[green]started[/] {agent.id}")
            await self._refresh_snapshot()

        async def on_tree_node_selected(self, event: Any) -> None:
            data = getattr(event.node, "data", None)
            if not data:
                return
            raw = str(data)
            if raw.startswith("ws:"):
                self._focused_workspace = raw[3:]
                candidates = [a for a in self._agents if a.workspace == self._focused_workspace]
                if candidates:
                    running = [a for a in candidates if a.is_running]
                    self._focused_agent = (running or candidates)[0].id
                else:
                    self._focused_agent = None
                self._render_sidebars()
                await self._render_agent_context()
                # Re-point the PTY at the now-focused agent. Without this the
                # header/sidebar could show agent B while keystrokes still
                # flowed to the previously-attached agent A's websocket.
                if self._focused_agent:
                    await self._start_pty(self._focused_agent)
                else:
                    await self._clear_pty()
                return

            self._focused_agent = raw[6:] if raw.startswith("agent:") else raw
            found = next((a for a in self._agents if a.id == self._focused_agent), None)
            if found:
                self._focused_workspace = found.workspace
            self._render_sidebars()
            await self._render_agent_context()
            await self._start_pty(self._focused_agent)

        async def _start_pty(self, agent_id: str) -> None:
            if (
                self._pty_agent == agent_id
                and self._pty_task is not None
                and not self._pty_task.done()
            ):
                return
            # Drop any previous WS pump and start a new one on the
            # focused agent.
            if self._pty_task is not None and not self._pty_task.done():
                self._pty_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pty_task
            self._pty_send_queue = asyncio.Queue()
            self._pty_agent = agent_id
            self._pty_task = asyncio.create_task(
                self._stream_pty(agent_id)
            )

        async def _clear_pty(self) -> None:
            """Detach the PTY entirely (e.g. an empty workspace is focused),
            so keystrokes can't route to a stale, no-longer-focused agent."""
            if self._pty_task is not None and not self._pty_task.done():
                self._pty_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pty_task
            self._pty_task = None
            self._pty_agent = None
            self._pty_send_queue = None

        async def _render_agent_context(self) -> None:
            agent = next((a for a in self._agents if a.id == self._focused_agent), None)
            if agent is None:
                self.query_one("#tabbar", static_cls).update("[#7c8fdc]no agent selected[/]")
                self.query_one("#avatar", static_cls).update("")
                self.query_one("#agent_meta", static_cls).update(
                    "[b]No agent selected[/]\n"
                    "[dim]Pick a workspace and agent from the left rail.[/]"
                )
                self.query_one("#agent_stats", static_cls).update("")
                if not self._compose_mode:
                    self.query_one("#status", static_cls).update(self._render_status(agent=None))
                return

            self.query_one("#tabbar", static_cls).update(
                f"[#c8beff]focus[/] {agent.workspace}/{agent.id}"
                "    [#56516f]+ tabs from plugins later: terminal · messages · events · tasks[/]"
            )
            self.query_one("#avatar", static_cls).update(
                "[#ff875f]   __\n"
                "  / /\\\n"
                " /_/  \\\n"
                " \\_\\__/"
            )
            tags = " ".join(f"[#7c8fdc]#{t}[/]" for t in agent.tags[:4]) or "[dim]no tags[/]"
            purpose = _truncate(agent.purpose or "No purpose set.", 92)
            glyph, color = status_badge(agent.semantic_status)
            self.query_one("#agent_meta", static_cls).update(
                f"[b #d7d3ef]{agent.name or agent.id}[/]  [dim]{agent.type or 'agent'}[/]\n"
                f"[{color}]{glyph}[/] {agent.semantic_status or agent.status or 'unknown'}"
                f"    [dim]workspace[/] [#9de1ff]{agent.workspace}[/]\n"
                f"{purpose}\n"
                f"{tags}"
            )

            stats = await asyncio.to_thread(self._agent_stats, agent.id)
            self._focused_agent_stats = stats
            model = stats.get("model") or self._model_hint(agent)
            self.query_one("#agent_stats", static_cls).update(
                "[#7c8fdc]run stats[/]\n"
                f"model    [#9de1ff]{_truncate(str(model or '-'), 20)}[/]\n"
                f"tokens   [#d5ff83]{_compact_int(stats.get('tokens_24h'))}[/] [dim]24h[/]\n"
                f"events   [#ffcc66]{_compact_int(stats.get('events_total'))}[/]\n"
                f"last     [#c8beff]{_rel_time(stats.get('last_event_ts'))}[/]"
            )
            await self._render_messages(agent)
            if not self._compose_mode:
                self.query_one("#status", static_cls).update(self._render_status(agent=agent))

        def _agent_stats(self, agent_id: str) -> dict[str, Any]:
            try:
                out = self._host._request(f"/api/agents/{agent_id}/stats")
                return dict(out) if isinstance(out, dict) else {}
            except Exception:
                return {}

        def _model_hint(self, agent: AgentRow) -> str:
            # AgentInfo intentionally exposes only public metadata; until
            # RemoteHost includes config.model, show the first profile as
            # an honest "profile available" hint instead of inventing one.
            if self._presets:
                return self._presets[0].get("name") or self._presets[0].get("model") or "-"
            return "-"

        async def _render_messages(self, agent: AgentRow) -> None:
            log = self.query_one("#msgs", rich_log_cls)
            log.clear()
            rows: list[dict[str, Any]] = []
            if agent.workspace:
                rows = await asyncio.to_thread(
                    self._api_list,
                    f"/api/workspaces/{agent.workspace}/inbox?agent={agent.id}&limit=6",
                )
            log.write("[#7c8fdc]messages[/] [dim]recent inbox for focused agent[/]")
            if not rows:
                log.write("[dim]no recent messages[/]")
                return
            for msg in reversed(rows[-6:]):
                sender = msg.get("from_id") or msg.get("sender") or msg.get("from") or "?"
                body = _truncate(str(msg.get("body") or ""), 110)
                ts = _rel_time(msg.get("ts"))
                log.write(f"[dim]{ts:>4}[/] [#d5ff83]{sender}[/] {body}")

        def _render_status(self, *, agent: AgentRow | None) -> str:
            workers = [w for w in self._worker_rows if w.get("status") == "running"]
            crashes = [w for w in self._worker_rows if w.get("status") == "crash_loop"]
            model = self._focused_agent_stats.get("model") if agent else None
            return (
                f"[#56516f]workspace[/] [#9de1ff]{self._focused_workspace or '-'}[/]"
                f"   [#56516f]agent[/] [#c8beff]{agent.id if agent else '-'}[/]"
                f"   [#56516f]model[/] [#9de1ff]{_truncate(str(model or '-'), 24)}[/]"
                f"   [#56516f]workers[/] [#d5ff83]{len(workers)} running[/]"
                + (f"   [red]{len(crashes)} crash-loop[/]" if crashes else "")
                + "\n[#706a92]Ctrl+B D detach · Ctrl+B M message · "
                "Ctrl+B ? help · terminal keys forward raw[/]"
            )

        async def _stream_pty(self, agent_id: str) -> None:
            """Live WS-attached PTY for the focused agent. The
            daemon's `/term` endpoint is bidirectional: `\\x00`
            frames are PTY bytes either way, `\\x01` frames are
            JSON events from the server and resize commands from
            the client.

            We feed inbound bytes into a per-stream pyte Screen
            (so terminal escapes resolve to a flat grid the
            RichLog can display) and pump key events from
            `self._pty_send_queue` outbound.
            """
            log = self.query_one("#pty", rich_log_cls)
            log.clear()
            try:
                ws = await _connect_pty_ws(self._host, agent_id)
            except Exception as exc:
                log.write(f"[red]could not connect to {agent_id}: {exc}[/]")
                return

            import pyte

            from relaydeck.screen import _snapshot_screen
            cols = max(40, log.size.width or 80)
            rows = max(10, log.size.height or 24)
            # Tolerant Screen: opencode (and other TUIs) emit the private
            # Device Status Report (`CSI ? n`) which pyte's base Screen chokes
            # on — see relaydeck/screen.py. Reuse the same subclass so the live
            # TUI doesn't crash on those harnesses.
            screen = _snapshot_screen(cols, rows)
            stream = pyte.ByteStream(screen)
            self._pty_screen = screen
            self._pty_cols = cols
            self._pty_rows = rows

            # Send our pane size as the initial resize so the
            # harness redraws to the right geometry rather than
            # whatever the daemon spawned at.
            await _ws_send_resize(ws, cols, rows)

            stop = asyncio.Event()

            async def pump_out() -> None:
                try:
                    async for frame in ws:
                        if not isinstance(frame, (bytes, bytearray)) or not frame:
                            continue
                        kind = frame[0:1]
                        payload = bytes(frame[1:])
                        if kind == b"\x00":
                            stream.feed(payload)
                            _render_screen(log, screen)
                        elif kind == b"\x01":
                            if b"pty_closed" in payload or b"agent_not_running" in payload:
                                log.write("[dim](pty closed)[/]")
                                stop.set()
                                return
                finally:
                    stop.set()

            async def pump_in() -> None:
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(
                            self._pty_send_queue.get(), timeout=0.25
                        )
                    except TimeoutError:
                        continue
                    if msg is None:
                        return
                    # Queue protocol: (`"input"`, bytes) for PTY
                    # input, (`"resize"`, cols, rows) for resize.
                    # The string tag keeps the channel single, no
                    # need for a second queue.
                    try:
                        if isinstance(msg, tuple) and msg and msg[0] == "resize":
                            _, cols, rows = msg
                            await _ws_send_resize(ws, cols, rows)
                        else:
                            await ws.send(b"\x00" + msg)
                    except Exception:
                        return

            try:
                await asyncio.gather(pump_out(), pump_in(), return_exceptions=True)
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()

        async def on_key(self, event: Any) -> None:  # type: ignore[override]
            # Prefix commands run first. Normal printable keys are
            # forwarded to the PTY; only Ctrl+B enters relaydeck command
            # mode so `q`, `r`, `?`, etc. remain usable inside agent
            # TUIs.
            key = str(event.key)
            if self._detach_prefix == "prefix":
                self._detach_prefix = ""
                if key in ("d", "D"):
                    self.exit()
                elif key in ("r", "R"):
                    await self._refresh_snapshot()
                    self.query_one("#status", static_cls).update("refreshed")
                elif key in ("m", "M"):
                    self._begin_compose()
                elif key in ("s", "S"):
                    await self._start_focused_agent()
                elif key in ("?", "f1"):
                    self.action_help()
                elif key == "ctrl+b":
                    self._detach_prefix = "prefix"
                    self.query_one("#status", static_cls).update(
                        "Ctrl+B armed — D detach, M message, S start, R refresh, ? help"
                    )
                event.stop()
                return

            if self._compose_mode:
                character = getattr(event, "character", None)
                if key in ("escape", "ctrl+c"):
                    self._compose_mode = False
                    self._compose_buffer = ""
                    self.query_one("#status", static_cls).update("message cancelled")
                elif key in ("enter", "return"):
                    await self._send_compose()
                elif key == "backspace":
                    self._compose_buffer = self._compose_buffer[:-1]
                    self.query_one("#status", static_cls).update(
                        "[#c8beff]message[/] " + _truncate(self._compose_buffer, 120)
                    )
                elif character and len(character) == 1 and character.isprintable():
                    self._compose_buffer += character
                    self.query_one("#status", static_cls).update(
                        "[#c8beff]message[/] " + _truncate(self._compose_buffer, 120)
                    )
                event.stop()
                return

            if key == "ctrl+b":
                self._detach_prefix = "prefix"
                self.query_one("#status", static_cls).update(
                    "Ctrl+B armed — D detach, M message, S start, R refresh, ? help"
                )
                event.stop()
                return

            if self._focused_agent is None or self._pty_send_queue is None:
                return

            character = getattr(event, "character", None)
            payload = key_to_bytes(key, character)
            if payload is None:
                return
            await self._pty_send_queue.put(payload)
            event.stop()

        async def on_resize(self, event: Any) -> None:  # type: ignore[override]
            """Forward textual's resize to the daemon so the
            harness's child sees a real SIGWINCH and redraws."""
            if self._pty_task is None or self._pty_task.done():
                return
            try:
                log = self.query_one("#pty", rich_log_cls)
                cols = max(40, log.size.width or 80)
                rows = max(10, log.size.height or 24)
            except Exception:
                return
            await self._pty_send_queue.put(("resize", cols, rows))

    return RelaydeckView(host, initial_workspace=initial_workspace)


def _render_screen(log: Any, screen: Any) -> None:
    """Re-paint the pyte screen into the RichLog. Cheap clear +
    re-write; pyte already collapses ANSI escapes to a flat grid,
    so this is just text."""
    log.clear()
    for line in screen.display:
        log.write(line.rstrip())


async def _connect_pty_ws(host: Any, agent_id: str) -> Any:
    """Open a WS to /api/agents/{id}/term with our auth + TLS
    pinning. Mirrors the protocol used by `relaydeck attach` so the
    harness sees identical client behavior."""
    import ssl as _ssl

    import websockets

    base = host.daemon_url.rstrip("/")
    if base.startswith("https://"):
        ws_url = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_url = "ws://" + base[len("http://"):]
    else:
        raise RuntimeError(f"unsupported daemon URL: {base}")
    ws_url = f"{ws_url}/api/agents/{agent_id}/term?token={host.token or ''}"

    ssl_ctx = None
    if ws_url.startswith("wss://"):
        verify = getattr(host, "verify", True)
        if isinstance(verify, str):
            ssl_ctx = _ssl.create_default_context(cafile=verify)
        elif verify is False:
            ssl_ctx = _ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE
        else:
            ssl_ctx = _ssl.create_default_context()

    return await websockets.connect(
        ws_url,
        ssl=ssl_ctx,
        max_size=4 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    )


async def _ws_send_resize(ws: Any, cols: int, rows: int) -> None:
    """Send a `\\x01 "<cols> <rows>"` frame so the daemon resizes
    the child PTY (the harness then gets SIGWINCH and redraws)."""
    with contextlib.suppress(Exception):
        await ws.send(b"\x01" + f"{cols} {rows}".encode("ascii"))


def _sse_worker(host: Any, path: str, on_event: Any, stop: threading.Event) -> None:
    """Blocking SSE reader for the Textual app.

    Textual owns the asyncio loop, so this uses a daemon thread and
    hands parsed event dicts back through `on_event`.
    """
    ctx = _ssl_context_for_host(host)
    backoff = 0.5
    while not stop.is_set():
        try:
            req = urllib.request.Request(
                host.daemon_url.rstrip("/") + path,
                headers=_event_headers(host),
            )
            with urllib.request.urlopen(req, context=ctx) as resp:
                backoff = 0.5
                _consume_sse_response(resp, on_event, stop)
        except urllib.error.HTTPError as exc:
            on_event({"type": "relaydeck.view.sse_down", "error": f"HTTP {exc.code}"})
            return
        except Exception as exc:
            on_event({"type": "relaydeck.view.sse_down", "error": str(exc)})
            stop.wait(backoff)
            backoff = min(backoff * 2, 10.0)


def _event_headers(host: Any) -> dict[str, str]:
    headers = {"Accept": "text/event-stream"}
    token = getattr(host, "token", None)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _ssl_context_for_host(host: Any) -> Any:
    if not str(getattr(host, "daemon_url", "")).startswith("https://"):
        return None
    import ssl as _ssl

    verify = getattr(host, "verify", True)
    if isinstance(verify, str):
        return _ssl.create_default_context(cafile=verify)
    if verify is False:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        return ctx
    return _ssl.create_default_context()


def _consume_sse_response(resp: Any, on_event: Any, stop: threading.Event) -> None:
    buf = ""
    for raw in resp:
        if stop.is_set():
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if buf:
                with contextlib.suppress(Exception):
                    event = json.loads(buf)
                    if isinstance(event, dict):
                        on_event(event)
            buf = ""
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            if buf:
                buf += "\n"
            buf += line[5:].lstrip()




# ── Best-effort SSE consumer (used by sidebar live updates) ───────


async def stream_status_changes(host: Any, on_change: Any) -> None:
    """Subscribe to `/api/events` and dispatch semantic-status changes.

    Kept as a small compatibility helper for tests/sidecar consumers; the
    TUI app itself uses the same worker directly and refreshes whole
    snapshots for the event types it cares about.
    """
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    stop = threading.Event()
    loop = asyncio.get_running_loop()

    def _push(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(q.put_nowait, event)

    thread = threading.Thread(
        target=_sse_worker,
        args=(host, "/api/events", _push, stop),
        name="relaydeck-view-status-events",
        daemon=True,
    )
    thread.start()
    try:
        while True:
            event = await q.get()
            if event.get("type") != "agent.status_changed":
                continue
            data = event.get("payload") or event.get("data") or {}
            on_change(
                data.get("agent_id"),
                data.get("to") or data.get("semantic_status") or data.get("status"),
            )
    finally:
        stop.set()


__all__ = [
    "AgentRow",
    "detach_state",
    "group_by_workspace",
    "run_view",
    "status_badge",
    "stream_status_changes",
    "SEMANTIC_STATUS_GLYPH",
    "SEMANTIC_STATUS_COLOR",
]
