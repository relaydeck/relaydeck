"""
tmux viewer — the original `workspace view` implementation,
reshaped as a `TerminalViewer`.

Spawns one window with a tiled grid of `relaydeck attach <id>` panes
plus a bottom pane running the inbox tail. The layout:

    ┌──────────────┬──────────────┐
    │ agent 1      │ agent 2      │
    ├──────────────┼──────────────┤
    │ agent 3      │ agent 4      │
    ├──────────────┴──────────────┤
    │ inbox -f --full             │
    └─────────────────────────────┘

Tested behaviors live with the viewer (recipe shape, layout
choices, `remain-on-exit`, the `-l N%` percent-length flag for
tmux 3.4 compatibility) so future viewers don't have to know any
tmux quirks.
"""

from __future__ import annotations

import shutil
import subprocess

from relaydeck.transports.viewers import TerminalViewer, ViewerContext, ViewerResult


class TmuxViewer:
    name = "tmux"
    description = "tmux pane grid: one pane per agent + bottom inbox tail"

    def is_available(self) -> bool:
        return shutil.which("tmux") is not None

    def launch(self, ctx: ViewerContext) -> ViewerResult:
        session = ctx.session_name
        recipe = build_recipe(ctx)

        if ctx.print_only:
            lines = [" ".join(c) for c in recipe]
            lines.append("")
            lines.append(f"tmux attach -t {session}")
            return ViewerResult(
                success=True,
                message="\n".join(lines),
                attach_command=f"tmux attach -t {session}",
            )

        if not self.is_available():
            return ViewerResult(
                success=False,
                error="tmux not on PATH",
            )

        # Refuse to clobber an existing session unless --force. A
        # previous partial run leaves the session in a state where
        # re-running this command would attach a second initial pane
        # to the same window and confuse the layout.
        has_session = subprocess.run(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if has_session:
            if not ctx.force:
                return ViewerResult(
                    success=False,
                    error=(
                        f"tmux session [bold]{session}[/] already exists. "
                        f"Attach with [bold]tmux attach -t {session}[/], "
                        f"or pass [bold]--force[/] to rebuild it."
                    ),
                    attach_command=f"tmux attach -t {session}",
                )
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        for cmd in recipe:
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as exc:
                return ViewerResult(
                    success=False,
                    error=f"tmux command failed: {' '.join(cmd)}\n[dim]{exc}[/]",
                )

        # Verify the session survived. Even with `remain-on-exit on`,
        # an immediate `new-session` failure can leave us with nothing
        # to attach to.
        still_alive = subprocess.run(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not still_alive:
            return ViewerResult(
                success=False,
                error=(
                    f"tmux session [bold]{session}[/] died before we could attach. "
                    f"Run [bold]relaydeck doctor[/] to check for zombie agents; the "
                    f"`{ctx.attach_command_for(ctx.agents[0]['id'])}` pane likely "
                    f"hit a daemon-side failure on connect."
                ),
            )

        return ViewerResult(
            success=True,
            message=(
                f"Started tmux session [bold]{session}[/] with "
                f"{len(ctx.agents)} agent pane(s) + inbox tail."
            ),
            attach_command=f"tmux attach -t {session}",
        )


def build_recipe(ctx: ViewerContext) -> list[list[str]]:
    """Return the list of argv tuples that, run in order, materialize
    the workspace-view layout. Kept pure (no subprocess) so the
    `--print-only` mode shows exactly what would run, and unit tests
    can pin the recipe shape without spawning tmux.

    Lives at module scope (not a method) so tests don't need a
    TmuxViewer instance to exercise the recipe shape — that
    keeps the unit boundary tight.
    """
    session = ctx.session_name
    ws_name = ctx.workspace
    agents = ctx.agents

    commands: list[list[str]] = []
    first_agent_id = agents[0]["id"]

    # Initial window with the first agent attached.
    commands.append([
        "tmux", "new-session", "-d", "-s", session, "-n", ws_name,
        ctx.attach_command_for(first_agent_id),
    ])

    # `remain-on-exit on` so a failed `relaydeck attach` (e.g. zombie
    # agent) leaves the pane visible with its error message
    # instead of collapsing the whole window — see the long
    # explanation in dev-workflow.md / commit log.
    commands.append([
        "tmux", "set-window-option", "-t", f"{session}:{ws_name}",
        "remain-on-exit", "on",
    ])

    # One pane per remaining agent.
    for ag in agents[1:]:
        commands.append([
            "tmux", "split-window", "-t", f"{session}:{ws_name}",
            ctx.attach_command_for(ag["id"]),
        ])

    # Bottom horizontal inbox pane. `-l 30%` works on tmux 2.x→3.x;
    # the older `-p 30` was deprecated in 3.1 and removed in 3.4.
    commands.append([
        "tmux", "split-window", "-v", "-l", "30%",
        "-t", f"{session}:{ws_name}",
        ctx.inbox_command,
    ])

    # Balance the grid.
    commands.append([
        "tmux", "select-layout", "-t", f"{session}:{ws_name}", "tiled",
    ])

    return commands


# Re-export as the protocol so the registry's static-type hints
# stay accurate (TmuxViewer is structurally a TerminalViewer).
_: TerminalViewer = TmuxViewer()  # type: ignore[assignment]
