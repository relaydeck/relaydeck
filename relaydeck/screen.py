"""
Server-side terminal rendering — turn a byte stream from a
harness PTY into a plain-text snapshot of its current screen.

`relaydeck agent screen <id>` and the corresponding API endpoint use
this to let one agent (or an operator) ask "what's currently on
the reviewer's screen?" without raw ANSI noise. The bytes go
through a real terminal emulator (pyte) which interprets cursor
moves, scrolls, color codes, alt-screen toggles, and the like —
the same way your local terminal does — and we read the
post-rendered grid of cells.

## Why this needs a real emulator, not regex

Modern coding harnesses (pi, claude-code, codex) use
alt-screen mode with extensive cursor manipulation. Trying to
"strip ANSI" by regex would leave a meaningless soup of letters
since the actual visible state depends on which cells the
harness chose to overwrite. pyte runs the same VT100/VT220
state machine real terminals use, so a snapshot matches what
the operator sees.

## Sizing

The renderer needs a canvas size. We pick 200x50 by default —
wide enough to capture full-width logs, tall enough for a
typical TUI. The actual rendering trims trailing blank lines
before returning, so a small TUI doesn't pad the output with
empty rows.
"""

from __future__ import annotations


DEFAULT_COLS = 200
DEFAULT_ROWS = 50


def _snapshot_screen(cols: int, rows: int):
    """A pyte Screen that tolerates the *private* Device Status Report
    (`CSI ? n`, e.g. DECXCPR). opencode — and other modern TUIs — emit it
    on startup to probe the terminal. pyte's parser dispatches private CSI
    sequences with `private=True`, but its base `report_device_status(mode)`
    doesn't accept that kwarg, so the whole `feed()` raised `TypeError` and
    the snapshot 500'd. A snapshot has no PTY back-channel to answer DSR on,
    so we just swallow it and keep rendering."""
    import pyte

    class _SnapshotScreen(pyte.Screen):
        def report_device_status(self, *args, **kwargs):  # noqa: D401
            return None

    return _SnapshotScreen(max(1, cols), max(1, rows))


def render(buf: bytes, *, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> str:
    """Feed `buf` through a pyte VT terminal of size cols×rows and
    return the resulting screen as a plain-text string.

    Trailing all-whitespace lines are trimmed because most TUIs
    only paint the rows they need; padding with blanks would
    obscure the actual content.

    Pure function — no I/O. Tests can pin behavior by feeding
    raw byte sequences and asserting on the rendered output.
    """
    import re

    # ONLCR-style normalization: when ingesting bytes that didn't
    # come through a real PTY (e.g. test fixtures, captured logs),
    # bare `\n` doesn't carry a CR — pyte then renders the next
    # line indented to the previous column. Real PTY output always
    # has `\r\n` (the kernel's line discipline adds it), so adding
    # the translation here is a no-op for production traffic and
    # makes the function symmetric for both real and synthesized
    # input.
    buf = re.sub(rb"(?<!\r)\n", b"\r\n", buf)

    import pyte

    screen = _snapshot_screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(buf)

    lines = screen.display
    # Drop trailing all-whitespace lines so a short TUI doesn't
    # come back as 50 lines of " " * 200.
    while lines and not lines[-1].strip():
        lines.pop()
    # Right-strip each surviving line; pyte pads to `cols` width
    # with spaces, which is fine for cursor math but ugly to
    # display.
    return "\n".join(ln.rstrip() for ln in lines)
