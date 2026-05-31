"""
PTY resize / SIGWINCH delivery — the controlling-terminal contract.

A TUI harness (pi / claude-code / codex) reads its terminal geometry at
startup and then relies on **SIGWINCH** to learn it changed. The kernel
only delivers SIGWINCH to the foreground process group of a terminal's
*controlling* terminal. We spawn harnesses with ``start_new_session=True``
(own session/pgrp), which leaves the child with NO controlling tty — so a
``TIOCSWINSZ`` on the master updates the winsize silently and the harness
never reflows, drawing its TUI at the spawn width forever (it wraps/ghosts
in a smaller dashboard pane). ``base._acquire_controlling_tty`` (wired as
the spawn ``preexec_fn``) claims the slave as the controlling tty so
SIGWINCH is delivered.

These tests use a real pty + real subprocess + real signals — no mocks —
and pin both halves of the contract: without the fix no SIGWINCH arrives;
with it every resize is delivered with the right geometry.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from relaydeck.harness.base import _acquire_controlling_tty

# Child: install a SIGWINCH handler that prints the new geometry it reads
# back from its own stdin, so the parent can observe delivery + value.
_CHILD = r"""
import signal, os, fcntl, termios, struct, sys, time
def on_winch(sig, frm):
    rows, cols, _, _ = struct.unpack("HHHH",
        fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
    sys.stdout.write(f"WINCH {cols} {rows}\n"); sys.stdout.flush()
signal.signal(signal.SIGWINCH, on_winch)
try:
    os.close(os.open("/dev/tty", os.O_RDWR)); ctty = 1
except OSError:
    ctty = 0
sys.stdout.write(f"READY ctty={ctty}\n"); sys.stdout.flush()
time.sleep(5)
"""


def _spawn(preexec):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    kw = dict(
        stdin=slave, stdout=slave, stderr=slave,
        close_fds=True, start_new_session=True,
    )
    if preexec is not None:
        kw["preexec_fn"] = preexec
    proc = subprocess.Popen([sys.executable, "-c", _CHILD], **kw)
    os.close(slave)
    return master, proc


def _drive(preexec):
    """Spawn, wait until the child is READY, push two resizes on the
    master, and return ALL accumulated child output (so both the
    `ctty=` line and any `WINCH` lines are captured in one buffer)."""
    master, proc = _spawn(preexec)
    out = b""
    t_ready = t_r1 = t_r2 = None
    deadline = time.time() + 6
    try:
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            now = time.time()
            if t_ready is None and b"READY" in out:
                t_ready = now
            if t_ready and t_r1 is None and now - t_ready > 0.2:
                fcntl.ioctl(master, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 30, 116, 0, 0))
                t_r1 = now
            if t_r1 and t_r2 is None and now - t_r1 > 0.4:
                fcntl.ioctl(master, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 40, 90, 0, 0))
                t_r2 = now
            # Done when the last resize is observed (fix path), or shortly
            # after issuing it with nothing arriving (baseline path).
            if t_r2 and (b"WINCH 90" in out or now - t_r2 > 1.0):
                break
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=3)
        try:
            os.close(master)
        except OSError:
            pass
    return out.decode("utf-8", "replace")


def test_controlling_tty_delivers_sigwinch():
    """With _acquire_controlling_tty the child has a ctty and receives
    every resize with the exact geometry pushed on the master."""
    out = _drive(_acquire_controlling_tty)
    assert "ctty=1" in out, f"child should own a controlling tty, got: {out!r}"
    assert "WINCH 116 30" in out, f"first resize not delivered: {out!r}"
    assert "WINCH 90 40" in out, f"second resize not delivered: {out!r}"


def test_without_fix_no_sigwinch_is_delivered():
    """The contrast case that proves WHY the fix is needed: the prior
    spawn (no preexec) leaves the child with no controlling tty, so
    TIOCSWINSZ on the master delivers no SIGWINCH at all."""
    out = _drive(None)
    assert "ctty=0" in out, f"baseline child unexpectedly had a ctty: {out!r}"
    assert "WINCH" not in out, f"baseline should get no SIGWINCH, got: {out!r}"
