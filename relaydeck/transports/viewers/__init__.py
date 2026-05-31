"""
Pluggable terminal viewers for `relaydeck workspace view`.

A "viewer" is anything that can take a list of agents and a
message-bus tail command and arrange them on the operator's
screen — tmux panes, Ghostty windows, iTerm splits, a printed
recipe for them to copy by hand. Every viewer is interchangeable
because the only thing it actually launches is

    relaydeck attach <agent_id>

which is the single canonical way to put an agent's PTY in front
of a human. That command opens a WebSocket to the daemon and
relays bytes both ways — and the daemon broadcasts to every
subscriber, so a tmux pane attached to `alice`, a Ghostty window
attached to `alice`, and the dashboard's terminal panel for
`alice` all see the same stream in real time. The viewer plugin
choice is purely about how the windows are arranged on your
screen; the data layer is invariant.

## Why a registry instead of a giant if/elif

Two reasons:

  1. Adding a new viewer (kitty, alacritty, wezterm) should not
     require editing the workspace-view command. A plugin drops
     a `TerminalViewer` implementation into the registry from
     its `on_load`; the rest of the system picks it up.

  2. Auto-detection. The CLI knows it has N registered viewers
     and walks them in preference order asking `is_available()`.
     First yes wins. Users can override with `--viewer X`,
     `RELAYDECK_VIEWER`, or the `default_viewer` plugin setting.

## The protocol

A viewer implements two methods:

  - `is_available() -> bool` — whether the underlying terminal
    or multiplexer is installed and usable. Cheap to call;
    typically a `shutil.which(...)` and maybe an `--version`
    probe.

  - `launch(ctx: ViewerContext) -> ViewerResult` — actually
    arrange the windows. Returns a small dataclass with
    success state, a user-facing message, and (optionally)
    the command the user can run to enter the layout
    (e.g. `tmux attach -t relaydeck-foo`).

All viewers share the same `ViewerContext` so the registry can
hand any one of them the same input — agents to attach to, the
session name, the inbox tail command, etc.

## Built-in vs plugin-contributed

Built-in viewers live in this package (`tmux.py`, `ghostty.py`,
`print_viewer.py`). They get registered at CLI startup. Third-
party viewers register via the SDK from inside a plugin's
`on_load` — `host.viewers.register(MyViewer())` with the
`viewers.register` capability declared. The registration shape
is symmetric so adding a new built-in or a new plugin viewer is
mechanically the same operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class ViewerContext:
    """Everything a viewer needs to lay out a workspace.

    `attach_command_for(agent_id)` returns the shell command string
    that opens a PTY to that agent — usually `relaydeck attach <id>`,
    but we route it through a callable so a custom build of relaydeck
    (different binary name, wrapper script) still works.

    `inbox_command` is the shell command for the message-bus tail
    pane — usually `relaydeck workspace inbox -f --full --workspace <ws>`.
    """
    session_name: str
    workspace: str
    agents: list[dict]
    attach_command_for: Callable[[str], str]
    inbox_command: str
    print_only: bool = False
    force: bool = False
    extras: dict = field(default_factory=dict)


@dataclass
class ViewerResult:
    """What a viewer reports back after launching.

    `success` is True when the layout was created (or, for the
    print viewer, when the recipe was emitted). `attach_command`
    is what the operator types next to enter the layout
    (`tmux attach -t relaydeck-foo`, or empty for viewers that
    opened windows directly).

    `message` is a single line for the CLI to print on success;
    `error` is the corresponding line on failure. Both go through
    rich markup so the workspace-view command can format
    consistently regardless of which viewer ran.
    """
    success: bool = True
    message: str = ""
    error: str = ""
    attach_command: str = ""


class TerminalViewer(Protocol):
    """The interface every viewer implements.

    Implementations live in this package (built-ins) or in
    third-party plugins (via `host.viewers.register`). The CLI
    treats them identically.
    """
    name: str
    description: str

    def is_available(self) -> bool:
        """Return True if the underlying terminal/multiplexer can
        be launched. Should be cheap — `shutil.which` and maybe an
        environment probe, no subprocesses that take longer than
        a few ms."""
        ...

    def launch(self, ctx: ViewerContext) -> ViewerResult:
        """Materialize the layout. Returns the result; should not
        raise for ordinary failures (missing tool, user-input
        issue) — return success=False with a helpful `error`
        instead. Unexpected exceptions propagate."""
        ...


# Ordered list of viewer names checked by auto-detect. tmux wins
# when available because it's the most-tested in this codebase
# and works in any terminal. Ghostty is preferred on macOS for
# users who already use it. Print is the universal fallback.
AUTODETECT_ORDER = ("tmux", "ghostty", "print")


# Module-level registry. Populated at CLI startup by
# `register_builtin_viewers()` and by plugin SDK at plugin load.
# A dict keyed on viewer.name so explicit `--viewer X` lookups are
# O(1) and the auto-detect walk can iterate `AUTODETECT_ORDER`.
_registry: dict[str, TerminalViewer] = {}


def register(viewer: TerminalViewer) -> None:
    """Register a viewer. Second registration of the same name
    wins — lets a user-installed plugin override a built-in
    (e.g. a fancier tmux viewer with custom layouts)."""
    _registry[viewer.name] = viewer


def get(name: str) -> TerminalViewer | None:
    """Look up a viewer by name. Returns None if not registered."""
    return _registry.get(name)


def all_viewers() -> list[TerminalViewer]:
    """Snapshot of every currently-registered viewer. Order is
    insertion order; the CLI sorts by `AUTODETECT_ORDER` when it
    needs preference-aware iteration."""
    return list(_registry.values())


def auto_detect() -> TerminalViewer | None:
    """Pick the highest-preference viewer that's available right
    now. Returns None if literally nothing works — which means
    the print viewer wasn't registered (a programming error),
    not a normal end-user state."""
    for name in AUTODETECT_ORDER:
        v = _registry.get(name)
        if v is None:
            continue
        try:
            if v.is_available():
                return v
        except Exception:
            # A misbehaving is_available shouldn't bring down the
            # whole auto-detect walk; skip it and try the next.
            continue
    # Last resort: anything left in the registry that says it
    # works. Covers third-party viewers that aren't in
    # AUTODETECT_ORDER.
    for v in _registry.values():
        try:
            if v.is_available():
                return v
        except Exception:
            continue
    return None


def register_builtin_viewers() -> None:
    """Idempotent: register the in-tree viewers. Called once at
    CLI startup. Safe to call multiple times — each viewer module
    re-registers the same instance."""
    # Lazy imports so the viewers package stays cheap to import
    # for code that just wants the protocol definitions.
    from relaydeck.transports.viewers.tmux import TmuxViewer
    from relaydeck.transports.viewers.ghostty import GhosttyViewer
    from relaydeck.transports.viewers.print_viewer import PrintViewer

    register(TmuxViewer())
    register(GhosttyViewer())
    register(PrintViewer())
