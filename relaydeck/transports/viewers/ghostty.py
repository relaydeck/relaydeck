"""
Ghostty viewer — single-window splits on macOS, separate
windows elsewhere.

Ghostty (https://ghostty.org) ships a GUI terminal with first-
class split panes. The trouble is Ghostty's CLI has no
`+new-split` action — splits are GUI actions bound to keystrokes
(Cmd+D, Cmd+Shift+D). The only way to drive them programmatically
is via AppleScript on macOS, sending keystrokes after the window
opens.

## Layouts

**Single-window splits** (default on macOS):

    ┌──────────────┬──────────────┐
    │ agent 1      │ agent 2      │   ← Cmd+D splits right
    ├──────────────┴──────────────┤
    │ inbox -f --full             │   ← Cmd+Shift+D splits down
    └─────────────────────────────┘

One Ghostty window, splits arranged the same way tmux's pane
grid is. Built via `osascript` that:
  1. Opens a new Ghostty window (Cmd+N).
  2. In each split, types `relaydeck attach <id>` and presses Enter.
  3. Sends Cmd+D / Cmd+Shift+D to spawn the next split.

The keystroke approach needs Accessibility permission for
"System Events" (macOS will prompt the first time). It's also
mildly timing-sensitive — the delays between split-and-type are
tuned for a typical machine and should be fine on anything since
~2018.

**Separate windows** (Linux/non-macOS, or `RELAYDECK_GHOSTTY_LAYOUT=windows`):

One Ghostty window per agent + one for the inbox. Cmd-` between
them. Less elegant but more reliable when AppleScript isn't an
option.

## Trade-off vs tmux

Splits: same visual structure, native font rendering, no
SIGWINCH redraw quirks, no key collisions with `relaydeck attach`'s
detach sequence (because Ghostty's Cmd+D/Cmd+Shift+D are
intercepted before any pane sees them).

Cost: AppleScript-driven setup is fragile compared to tmux's
declarative pane recipe. If you have GUI-modal dialogs open or
switch apps mid-setup, the keystrokes can land in the wrong
place. The fallback windows mode is always available as
`--print-only` or `RELAYDECK_GHOSTTY_LAYOUT=windows`.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

from relaydeck.transports.viewers import TerminalViewer, ViewerContext, ViewerResult


# Tunable delays for the AppleScript split path. Adjust upward if
# you see commands landing in the wrong split (e.g. Ghostty's
# startup is slow on your machine and the next Cmd+D fires before
# the new pane is ready). Conservative defaults; total time for a
# 3-agent layout is roughly (3 × _PER_SPLIT_DELAY + 0.5 setup) ≈ 1.5s.
_INITIAL_WINDOW_DELAY = 0.6   # after Cmd+N, before first keystroke
_PER_SPLIT_DELAY = 0.4        # after split keystroke, before typing
_POST_TYPE_DELAY = 0.15       # after typing the command, before Enter
_POST_ENTER_DELAY = 0.25      # after Enter, before next split keystroke


def _layout_mode() -> str:
    """Which layout to use: `splits` (single window, Cmd+D splits)
    or `windows` (one per agent). Default: splits on macOS where
    AppleScript can drive the keystrokes; windows everywhere else."""
    explicit = os.environ.get("RELAYDECK_GHOSTTY_LAYOUT", "").strip().lower()
    if explicit in ("splits", "windows"):
        return explicit
    return "splits" if platform.system() == "Darwin" else "windows"


class GhosttyViewer:
    name = "ghostty"
    description = (
        "Ghostty splits: one window with Cmd+D splits per agent "
        "(macOS); separate windows on other OSes"
    )

    def is_available(self) -> bool:
        # Binary on PATH is the universal check. macOS users
        # frequently install Ghostty as an app bundle without
        # symlinking the binary, so also accept "Ghostty.app exists"
        # via osascript — but keep it cheap.
        if shutil.which("ghostty"):
            return True
        if platform.system() == "Darwin":
            try:
                rc = subprocess.run(
                    ["osascript", "-e",
                     'tell application "Finder" to exists '
                     'application file id "com.mitchellh.ghostty"'],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
                return rc == "true"
            except (subprocess.SubprocessError, FileNotFoundError):
                return False
        return False

    def launch(self, ctx: ViewerContext) -> ViewerResult:
        mode = _layout_mode()

        if mode == "splits" and platform.system() == "Darwin":
            return self._launch_splits(ctx)
        return self._launch_windows(ctx)

    # ── splits (single window) ──────────────────────────────────────

    def _launch_splits(self, ctx: ViewerContext) -> ViewerResult:
        """macOS path: open one Ghostty window and use AppleScript
        keystrokes to spawn splits + type each `relaydeck attach` /
        inbox command. Falls back to the windows path if osascript
        fails."""
        script = _build_split_applescript(ctx)

        if ctx.print_only:
            return ViewerResult(
                success=True,
                message=(
                    "# AppleScript that would drive Ghostty:\n"
                    f"{script}\n"
                    "# To run by hand: pipe the above into "
                    "`osascript -`"
                ),
            )

        if not self.is_available():
            return ViewerResult(
                success=False,
                error=(
                    "Ghostty isn't installed (neither `ghostty` on PATH "
                    "nor the Ghostty.app bundle). "
                    "Install from https://ghostty.org or pass "
                    "[bold]--viewer tmux[/]."
                ),
            )

        try:
            subprocess.run(
                ["osascript", "-"],
                input=script,
                text=True,
                check=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            # The most common error is missing Accessibility
            # permission for the launching app (your shell / iTerm /
            # whatever ran `relaydeck`). Detect and explain.
            if "not allowed assistive access" in stderr or "1002" in stderr:
                return ViewerResult(
                    success=False,
                    error=(
                        "Accessibility permission missing — the app "
                        "running `relaydeck` needs to be allowed to control "
                        "Ghostty via System Events.\n"
                        "[dim]System Settings → Privacy & Security → "
                        "Accessibility, then re-add your terminal app. "
                        "Or use [bold]--viewer ghostty[/] with "
                        "[bold]RELAYDECK_GHOSTTY_LAYOUT=windows[/] to skip "
                        "AppleScript.[/]"
                    ),
                )
            return ViewerResult(
                success=False,
                error=f"osascript failed: {stderr or exc}",
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            return ViewerResult(success=False, error=f"osascript: {exc}")

        return ViewerResult(
            success=True,
            message=(
                f"Opened one Ghostty window with "
                f"{len(ctx.agents)} agent split(s) + 1 inbox split.\n"
                "[dim]Cmd+[ / Cmd+] cycle splits, Cmd+Shift+Enter "
                "zooms one to full window.[/]"
            ),
        )

    # ── windows (one per agent) ─────────────────────────────────────

    def _launch_windows(self, ctx: ViewerContext) -> ViewerResult:
        """Fallback path: N+1 separate windows. Used on non-macOS
        and when RELAYDECK_GHOSTTY_LAYOUT=windows is set."""
        commands = self._build_window_commands(ctx)

        if ctx.print_only:
            lines = [" ".join(_quote(p) for p in c) for c in commands]
            return ViewerResult(success=True, message="\n".join(lines))

        if not self.is_available():
            return ViewerResult(
                success=False,
                error=(
                    "Ghostty isn't installed (neither `ghostty` on PATH "
                    "nor the Ghostty.app bundle). "
                    "Install from https://ghostty.org or pass "
                    "[bold]--viewer tmux[/]."
                ),
            )

        failures = 0
        for cmd in commands:
            try:
                subprocess.run(
                    cmd, check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                failures += 1

        n = len(commands)
        if failures == n:
            return ViewerResult(
                success=False,
                error="Couldn't spawn any Ghostty windows.",
            )
        if failures:
            return ViewerResult(
                success=True,
                message=f"Spawned {n - failures} of {n} Ghostty windows ({failures} failed).",
            )
        return ViewerResult(
            success=True,
            message=(
                f"Spawned {n} Ghostty windows "
                f"({len(ctx.agents)} agent + 1 inbox)."
            ),
        )

    def _build_window_commands(self, ctx: ViewerContext) -> list[list[str]]:
        commands: list[list[str]] = []
        for ag in ctx.agents:
            commands.append(_spawn_window(ctx.attach_command_for(ag["id"])))
        commands.append(_spawn_window(ctx.inbox_command))
        return commands


def _spawn_window(shell_cmd: str) -> list[str]:
    """Build the argv that opens a new Ghostty window running
    `shell_cmd`. macOS prefers `open -na Ghostty.app --args -e`
    per Ghostty's official guidance (the CLI binary's `-e` path
    is documented as unsupported on macOS); Linux uses the binary
    directly."""
    if platform.system() == "Darwin":
        return [
            "open", "-na", "Ghostty.app",
            "--args", "-e", "bash", "-lc", shell_cmd,
        ]
    if shutil.which("ghostty"):
        return ["ghostty", "-e", "bash", "-lc", shell_cmd]
    # Last resort: try `open -na` even off-macOS — won't work but
    # gives a single coherent failure.
    return ["open", "-na", "Ghostty.app", "--args", "-e", "bash", "-lc", shell_cmd]


def _quote(s: str) -> str:
    """Shell-quote for the `--print-only` view."""
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./=:%@,+")
    if all(c in safe for c in s):
        return s
    escaped = s.replace('"', '\\"')
    return f'"{escaped}"'


# ── AppleScript builder ────────────────────────────────────────────


def _build_split_applescript(ctx: ViewerContext) -> str:
    """Produce the osascript program that opens one Ghostty window
    and lays out splits matching the tmux layout:

        ┌──────────────┬──────────────┐
        │ agent 1      │ agent 2      │
        ├──────────────┴──────────────┤
        │ agent 3      │ agent 4      │  (if N ≥ 3 agents)
        ├──────────────┴──────────────┤
        │ inbox tail (full width)     │
        └─────────────────────────────┘

    Split direction logic:
      - 1st agent: opens in the initial pane (no split)
      - subsequent agents: alternate Cmd+D (right) and
        Cmd+Shift+D (down) for a roughly-balanced grid
      - inbox: always Cmd+Shift+D (split down) after the last
        agent so it spans full width at the bottom

    Kept as a pure function so the unit test can pin the script
    shape without invoking osascript."""
    commands_to_run = [
        ctx.attach_command_for(ag["id"]) for ag in ctx.agents
    ] + [ctx.inbox_command]

    lines: list[str] = [
        '-- Auto-generated by relaydeck workspace view (Ghostty splits)',
        'tell application "Ghostty" to activate',
        f'delay {_INITIAL_WINDOW_DELAY}',
        'tell application "System Events"',
        '  tell process "Ghostty"',
        '    -- Fresh window — keystrokes after this land here.',
        '    keystroke "n" using {command down}',
        f'    delay {_INITIAL_WINDOW_DELAY}',
    ]

    for i, cmd in enumerate(commands_to_run):
        is_inbox = (i == len(commands_to_run) - 1)
        if i > 0:
            if is_inbox:
                # Inbox always at the bottom, full width — split
                # down from the most-recently-active split.
                lines.append('    -- split down for inbox')
                lines.append('    keystroke "d" using {command down, shift down}')
            else:
                # Alternate right/down for agent panes, matching
                # the rough balance tmux's `tiled` layout produces.
                direction = "right" if i % 2 == 1 else "down"
                modifier = (
                    '{command down}'
                    if direction == "right"
                    else '{command down, shift down}'
                )
                lines.append(f'    -- split {direction}')
                lines.append(f'    keystroke "d" using {modifier}')
            lines.append(f'    delay {_PER_SPLIT_DELAY}')

        # Type the command and Enter.
        escaped = _escape_for_applescript(cmd)
        lines.append(f'    keystroke "{escaped}"')
        lines.append(f'    delay {_POST_TYPE_DELAY}')
        lines.append('    keystroke return')
        lines.append(f'    delay {_POST_ENTER_DELAY}')

    lines.append('  end tell')
    lines.append('end tell')
    return "\n".join(lines)


def _escape_for_applescript(s: str) -> str:
    """Escape a string for embedding inside `keystroke "..."`.
    AppleScript string literals use backslash escapes for `\\`
    and `"`. We don't try to handle every edge case — `relaydeck
    attach <id>` and inbox commands are simple ASCII."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


_: TerminalViewer = GhosttyViewer()  # type: ignore[assignment]
