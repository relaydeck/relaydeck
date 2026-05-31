"""
`relaydeck attach <agent>` — CLI-side PTY attach over the daemon's WS.

The dashboard's terminal tab uses the same `/api/agents/{id}/term`
WebSocket; this is the CLI mirror. Devs already attach to local TUIs
all day (tmux, mosh, screen); attaching to a relaydeck-managed agent
should feel just as immediate.

## Protocol (mirrors relaydeck/transports/api.py:agent_terminal)

Binary frames in both directions. First byte = type.

  - server → client
      0x00 + bytes  →  PTY output (stdout/stderr from the agent's CLI)
      0x01 + json   →  status event (`agent_not_running`, `pty_closed`)

  - client → server
      0x00 + bytes  →  PTY input (keystrokes)
      0x01 + "C R"  →  terminal resize (cols + rows, ASCII text)
      0x02          →  ping (informational)

## Terminal lifecycle

We put the local TTY into cbreak mode with echo off — control chars
(arrow keys, Ctrl-C, Ctrl-D, etc.) reach the agent's CLI verbatim
instead of being intercepted by the kernel line discipline. The
old mode is restored on every exit path, including signals.

## Detach sequence

Default is Ctrl-B then D (matches tmux muscle memory). Configurable
via `--detach-key`. The watcher tracks a small state machine — one
prefix byte then one mark byte — and doesn't forward those two
bytes to the agent when the sequence completes.

## TLS

If the daemon URL is HTTPS, the WS upgrade is WSS. We honor the
same CA pin used elsewhere (state.yaml `daemon_ca`) so the dev
self-signed path keeps working.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import ssl
import struct
import sys
import termios
import tty

logger = logging.getLogger(__name__)


# Default detach sequence: Ctrl-B (prefix) then D (mark). Override with
# `--detach-key ctrl-x,q` etc.
DEFAULT_DETACH_KEY = "ctrl-b,d"

# How long to hold the prefix before timing out and forwarding the
# Ctrl-B as a real keystroke. Mirrors tmux's behavior — long enough
# for a deliberate detach, short enough that an accidental Ctrl-B
# inside the agent doesn't gobble the next character.
_PREFIX_TIMEOUT_S = 1.0


def attach_main(agent_id: str, detach_key: str = DEFAULT_DETACH_KEY) -> int:
    """Synchronous entry point. Returns the exit code for the CLI:
    0 on clean detach / agent exit, 2 for connection problems, etc.
    """
    prefix_byte, mark_byte = _parse_detach_key(detach_key)
    try:
        return asyncio.run(_run(agent_id, prefix_byte, mark_byte))
    except KeyboardInterrupt:
        # Ctrl-C *outside* the raw-mode region (e.g. before connect)
        # propagates as KeyboardInterrupt — exit cleanly.
        return 130


async def _run(agent_id: str, prefix_byte: int, mark_byte: int) -> int:
    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    token = read_token()
    if not token:
        _stderr_msg("No daemon token on disk. Run `relaydeck serve` first.")
        return 2

    daemon_url = get_daemon_url().rstrip("/")
    if daemon_url.startswith("https://"):
        ws_url = "wss://" + daemon_url[len("https://"):]
    elif daemon_url.startswith("http://"):
        ws_url = "ws://" + daemon_url[len("http://"):]
    else:
        _stderr_msg(f"Unsupported daemon URL scheme: {daemon_url}")
        return 2
    ws_url = f"{ws_url}/api/agents/{agent_id}/term?token={token}"

    ssl_ctx: ssl.SSLContext | None = None
    if ws_url.startswith("wss://"):
        ca = get_daemon_ca()
        ssl_ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    # Import websockets lazily so the module is cheap to import even
    # when attach isn't being used (the CLI loads every command on every
    # invocation; attach drags in ~150ms of websockets imports).
    import websockets

    try:
        ws = await websockets.connect(
            ws_url,
            ssl=ssl_ctx,
            # Tight buffers: the harness coalesces up to 32 KB into one
            # frame, so a generous read window keeps us flowing without
            # back-pressure pings.
            max_size=4 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
    except Exception as exc:
        _stderr_msg(f"Could not connect to daemon WS: {exc}")
        return 2

    # Show the masthead before raw mode so users see it even on a slow
    # connect. Once we drop into raw mode the prompt is suppressed.
    _stderr_msg(
        f"\033[2mattached to {agent_id}. detach: {_detach_label(prefix_byte, mark_byte)}\033[0m"
    )

    # Raw mode + signal-handler swap.
    old_attrs = _enter_raw_mode(sys.stdin.fileno()) if sys.stdin.isatty() else None
    stop = asyncio.Event()
    detach_requested = asyncio.Event()

    loop = asyncio.get_running_loop()

    # SIGWINCH → send a resize frame so the agent's CLI re-renders to
    # the new pane size. tmux relays this naturally; we relay it
    # ourselves over the WS.
    def _on_winch() -> None:
        cols, rows = _term_size()
        # `call_soon_threadsafe` lets the signal handler hand off to the
        # event loop. The send itself happens in an async task.
        loop.call_soon_threadsafe(asyncio.create_task, _send_resize(ws, cols, rows))

    if hasattr(signal, "SIGWINCH"):
        try:
            loop.add_signal_handler(signal.SIGWINCH, _on_winch)
        except (NotImplementedError, RuntimeError):
            pass

    # Single SIGWINCH at our pane size. The harness's response
    # depends on how its TUI is built:
    #
    #   - alt-screen TUIs (claude-code, codex) repaint in place at
    #     the new size — clean redraw.
    #   - scrollback-style TUIs (pi) treat SIGWINCH as "the next
    #     thing I print needs the new width" — they DON'T re-emit
    #     past output. The replay we got from the server has pi's
    #     banner + prompt sized for the daemon's spawn-time
    #     dimensions; that stays as scrollback above the live
    #     input area.
    #
    # An earlier version of this code did a double-pump (resize to
    # rows-1, sleep, resize to rows) to force a second SIGWINCH —
    # the trick tmux uses for alt-screen clients. For pi this
    # backfired: each SIGWINCH made pi PRINT a fresh prompt frame
    # at the new size, so a single attach left two stacked prompt
    # frames in scrollback. A single resize is the safe behavior
    # across the harness mix we ship; the modest cost is that
    # scrollback above the live area can look a bit wide-wrapped
    # for the first few lines until pi's next emission.
    cols, rows = _term_size()
    await _send_resize(ws, cols, rows)

    async def pump_out() -> None:
        """WS frames → stdout (and stderr for status events)."""
        try:
            async for frame in ws:
                if not isinstance(frame, (bytes, bytearray)):
                    continue
                if not frame:
                    continue
                kind = frame[0:1]
                payload = bytes(frame[1:])
                if kind == b"\x00":
                    # Most-common path. Write straight to stdout's fd;
                    # don't go through Python's buffered text stream
                    # since we need byte-exact output for ANSI codes.
                    try:
                        os.write(sys.stdout.fileno(), payload)
                    except BrokenPipeError:
                        return
                elif kind == b"\x01":
                    # Status event. We surface a couple known ones
                    # inline so users aren't left staring at a frozen
                    # screen wondering what happened.
                    _stderr_msg(_decode_status(payload))
                    if b'"pty_closed"' in payload or b'"agent_not_running"' in payload:
                        stop.set()
                        return
        finally:
            stop.set()

    async def pump_in() -> None:
        """stdin → WS, with detach-sequence interception."""
        if not sys.stdin.isatty():
            return  # No interactive input — attach is read-only here.
        state = 0  # 0 = normal, 1 = prefix seen, waiting for mark
        prefix_seen_at = 0.0
        reader = await _stdin_reader()

        while not stop.is_set():
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.25)
            except asyncio.TimeoutError:
                # Expire pending prefix if user took too long after Ctrl-B
                if state == 1 and (loop.time() - prefix_seen_at) > _PREFIX_TIMEOUT_S:
                    # Forward the swallowed prefix byte; we never sent it.
                    await _send_input(ws, bytes([prefix_byte]))
                    state = 0
                continue
            if not chunk:
                # stdin closed (Ctrl-D in cbreak still sends 0x04 as a byte,
                # so empty here really is "stdin gone").
                stop.set()
                return

            i = 0
            buf = bytearray()
            while i < len(chunk):
                b = chunk[i]
                if state == 0 and b == prefix_byte:
                    # Hold the prefix byte; don't forward yet.
                    state = 1
                    prefix_seen_at = loop.time()
                    i += 1
                    continue
                if state == 1:
                    if b == mark_byte:
                        # Detach! Flush pending payload first (in case the
                        # user typed stuff before Ctrl-B in the same chunk).
                        if buf:
                            await _send_input(ws, bytes(buf))
                        detach_requested.set()
                        stop.set()
                        return
                    else:
                        # False alarm — forward the prefix + this byte.
                        buf.append(prefix_byte)
                        buf.append(b)
                        state = 0
                        i += 1
                        continue
                buf.append(b)
                i += 1

            if buf:
                await _send_input(ws, bytes(buf))

    try:
        await asyncio.gather(pump_out(), pump_in(), return_exceptions=True)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        if old_attrs is not None:
            _restore_mode(sys.stdin.fileno(), old_attrs)
        # Carriage return + clear-to-eol so the next shell prompt
        # doesn't land midway through the last agent line.
        try:
            os.write(sys.stdout.fileno(), b"\r\n")
        except OSError:
            pass

    # Exit code distinguishes the three reasons we ended:
    #   0  — user pressed the detach sequence (Ctrl-B,D by default).
    #        Caller should NOT auto-reconnect.
    #   3  — WS closed without a detach request. Either the daemon
    #        sent `pty_closed` / `agent_not_running` or the
    #        connection dropped. Caller MAY auto-reconnect after a
    #        delay; the agent is gone or transient transport failure.
    #   2  — connect failure / no token. Caller almost certainly
    #        wants to surface this verbatim and stop.
    if detach_requested.is_set():
        _stderr_msg("detached.")
        return 0
    _stderr_msg("session ended (PTY closed or connection dropped).")
    return 3


# ── WS framing helpers ──────────────────────────────────────────────


async def _send_input(ws, payload: bytes) -> None:
    """Wrap PTY input in a 0x00 frame and send. Swallow disconnects so
    the pump_in loop can drain to the finally{} cleanup."""
    try:
        await ws.send(b"\x00" + payload)
    except Exception:
        pass


async def _send_resize(ws, cols: int, rows: int) -> None:
    try:
        await ws.send(b"\x01" + f"{cols} {rows}".encode("ascii"))
    except Exception:
        pass


def _decode_status(payload: bytes) -> str:
    """Make a JSON status event human-friendly. Best-effort — we don't
    want a malformed status to kill the attach session."""
    import json
    try:
        ev = json.loads(payload)
    except (ValueError, TypeError):
        return f"\033[2mstatus: {payload!r}\033[0m"
    kind = ev.get("event") or ev.get("type") or "status"
    extras = " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("event", "type"))
    return f"\033[2m\033[33m· {kind}{(' ' + extras) if extras else ''}\033[0m"


# ── Terminal mode helpers ───────────────────────────────────────────


def _enter_raw_mode(fd: int) -> list:
    """Switch to cbreak with echo off. Returns the prior termios attrs
    so the caller can restore on exit. Don't use `tty.setraw` —
    full raw kills newline translation and `\\r\\n` line endings start
    showing up at the wrong column. cbreak + echo-off is the
    canonical mode for terminal attach UIs."""
    attrs = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    new = termios.tcgetattr(fd)
    # Suppress local echo so we don't get duplicated keystrokes — the
    # agent's CLI echoes them when it processes input.
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    return attrs


def _restore_mode(fd: int, attrs) -> None:
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    except Exception:
        pass


def _term_size() -> tuple[int, int]:
    """Detect our local terminal size. Falls back to 80x24 if neither
    stdout nor stderr is a TTY (rare but possible under nohup)."""
    sz = shutil.get_terminal_size(fallback=(80, 24))
    return sz.columns, sz.lines


async def _stdin_reader():
    """Wrap stdin in an asyncio StreamReader so the input pump can
    `await reader.read(...)` with a timeout. We connect a pipe
    transport rather than polling — the read() returns immediately
    when the kernel has bytes."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


# ── Detach key parsing ──────────────────────────────────────────────


def _parse_detach_key(spec: str) -> tuple[int, int]:
    """Parse e.g. `ctrl-b,d` → (0x02, ord('d')). Lowercase letters
    only for the mark (we match by byte; the kernel sends 'd' as 0x64
    even when Caps Lock is on, so case-sensitivity doesn't help)."""
    parts = [p.strip().lower() for p in spec.split(",")]
    if len(parts) != 2:
        raise ValueError(f"detach key must be `prefix,mark` (e.g. ctrl-b,d): {spec!r}")
    prefix_s, mark_s = parts
    prefix_byte = _parse_key(prefix_s)
    mark_byte = _parse_key(mark_s)
    return prefix_byte, mark_byte


def _parse_key(spec: str) -> int:
    if spec.startswith("ctrl-") and len(spec) == 6:
        # Ctrl-A=1, Ctrl-B=2, …  Ctrl-_=31. Standard ASCII control
        # table: ord(letter)-ord('a')+1.
        ch = spec[5]
        if "a" <= ch <= "z":
            return ord(ch) - ord("a") + 1
    if len(spec) == 1:
        return ord(spec)
    raise ValueError(f"unrecognized key: {spec!r}")


def _detach_label(prefix: int, mark: int) -> str:
    """Render the parsed pair back as a human label for the masthead.
    Uppercases lowercase letters so `ctrl-b,d` renders as
    `Ctrl-B then D`."""
    def label(b: int) -> str:
        if 1 <= b <= 26:
            return f"Ctrl-{chr(b + ord('A') - 1)}"
        ch = chr(b)
        return ch.upper() if ch.isalpha() else ch
    return f"{label(prefix)} then {label(mark)}"


# ── Misc ────────────────────────────────────────────────────────────


def _stderr_msg(line: str) -> None:
    """Write a single line to stderr. Used for the masthead and
    status events — stdout is the agent's PTY mirror and must stay
    pristine for byte-accurate ANSI."""
    try:
        sys.stderr.write(line + "\r\n")
        sys.stderr.flush()
    except Exception:
        pass


# Silence unused-import lint (struct is reserved for a future
# resize wire format using packed binary; staying on ASCII for v1).
_ = struct
