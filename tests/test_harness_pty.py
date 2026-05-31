"""
Harness PTY geometry — the terminal must NOT start at 80 columns.

`pty.openpty()` is born 0×0, so a TUI harness falls back to its built-in
80 cols on its first frame ("narrow terminal that keeps coming back").
HarnessAgent.run seeds a real winsize before the fork; pin that here with
a real `sleep` child (no TUI needed — we read the PTY winsize directly).
"""

from __future__ import annotations

import fcntl
import struct
import termios
import threading
import time
from pathlib import Path

from relaydeck.db import open_db
from relaydeck.harness import HarnessAgent


class _SleepHarness(HarnessAgent):
    CLI = "sleep"
    DEFAULT_ARGS = ("5",)


def _mk(tmp_path: Path, **config) -> _SleepHarness:
    db = str(tmp_path / "d.db")
    open_db(db).close()
    return _SleepHarness(
        agent_id="a", name="a", config=config, workspace=None,
        db_path=db, stop_flag=threading.Event(),
    )


def _run_until_pty(agent) -> threading.Thread:
    t = threading.Thread(target=agent.run, daemon=True)
    t.start()
    for _ in range(60):                      # ~3s for the fork to land
        if agent._master_fd is not None:
            break
        time.sleep(0.05)
    return t


def _winsize(fd) -> tuple[int, int]:
    rows, cols, _x, _y = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return rows, cols


def test_pty_seeded_with_initial_winsize(tmp_path):
    a = _mk(tmp_path)
    t = _run_until_pty(a)
    try:
        assert a._master_fd is not None, "child never forked"
        rows, cols = _winsize(a._master_fd)
        assert (rows, cols) == (50, 200)     # NOT (0, 0) → no 80-col fallback
    finally:
        a.stop_flag.set(); t.join(timeout=3)


def test_initial_winsize_config_override(tmp_path):
    a = _mk(tmp_path, init_cols=120, init_rows=40)
    t = _run_until_pty(a)
    try:
        rows, cols = _winsize(a._master_fd)
        assert (rows, cols) == (40, 120)
    finally:
        a.stop_flag.set(); t.join(timeout=3)


def test_resize_after_spawn_updates_winsize(tmp_path):
    """The dashboard's resize path still works on top of the seed."""
    a = _mk(tmp_path)
    t = _run_until_pty(a)
    try:
        assert a.resize(177, 44) is True
        assert _winsize(a._master_fd) == (44, 177)
    finally:
        a.stop_flag.set(); t.join(timeout=3)


def test_build_env_strips_columns_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    a = _mk(tmp_path)
    env = a._build_env()
    assert "COLUMNS" not in env and "LINES" not in env   # can't pin width
    assert env.get("TERM")                                # still a real terminal
