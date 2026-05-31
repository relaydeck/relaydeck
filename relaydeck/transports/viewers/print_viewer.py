"""
Print viewer — the universal fallback.

Doesn't spawn anything; just emits the commands the user could
copy-paste into terminals of their choice. Always available,
useful in scripted contexts (CI, restricted shells without GUI),
and serves as the documentation-by-example for what a viewer
is *for*: one `relaydeck attach <id>` per agent, plus one inbox tail.

When neither tmux nor Ghostty (nor any other registered viewer)
is available, auto-detect falls back to this. The CLI also
selects it explicitly via `--viewer print` or `--print-only`.
"""

from __future__ import annotations

from relaydeck.transports.viewers import TerminalViewer, ViewerContext, ViewerResult


class PrintViewer:
    name = "print"
    description = "print the recipe; spawn nothing (always available)"

    def is_available(self) -> bool:
        return True

    def launch(self, ctx: ViewerContext) -> ViewerResult:
        lines: list[str] = [
            "# Open one terminal per agent + one for the inbox tail.",
            "# Each `relaydeck attach` opens a WebSocket to the daemon; all",
            "# clients of the same agent see the same byte stream.",
            "",
        ]
        for ag in ctx.agents:
            lines.append(f"# agent: {ag['id']}")
            lines.append(ctx.attach_command_for(ag["id"]))
            lines.append("")
        lines.append("# inbox tail (message bus, scoped to this workspace)")
        lines.append(ctx.inbox_command)
        return ViewerResult(success=True, message="\n".join(lines))


_: TerminalViewer = PrintViewer()  # type: ignore[assignment]
