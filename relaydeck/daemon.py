"""
Daemon lifecycle helpers for `relaydeck daemon start/stop/status`.

`relaydeck serve` is the foreground daemon. This module wraps it so the
operator can background, stop, and inspect the daemon without needing
a system service manager.

Files (under ~/.relaydeck/):
  daemon.pid   — PID of the running daemon process (mode 0600)
  daemon.log   — combined stdout/stderr of the backgrounded daemon
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


def pid_file_path(home: Path) -> Path:
    return home / "daemon.pid"


def log_file_path(home: Path) -> Path:
    return home / "daemon.log"


# daemon.log is the raw stdout/stderr of the backgrounded daemon
# (uvicorn access logs, tracebacks, agent noise). It's append-only across
# restarts, so a long-lived daemon would grow it without bound. We rotate
# on `daemon start` when it crosses the cap, keeping a few backups —
# `daemon.log.1` .. `daemon.log.{_LOG_BACKUPS}`. Mid-run rotation isn't
# possible here: the daemon inherits the open fd at the OS level, so
# renaming the file out from under it would keep writes flowing to the
# now-misnamed inode and break `daemon logs` tailing. Rotate-on-start
# bounds the common case (restart on upgrade/config change) cleanly.
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_LOG_BACKUPS = 3  # → ~40 MiB worst case across daemon.log + 3 backups


def _rotate_log_if_large(
    log_path: Path,
    *,
    max_bytes: int = _LOG_MAX_BYTES,
    backups: int = _LOG_BACKUPS,
) -> bool:
    """Roll daemon.log over when it exceeds `max_bytes`. Returns True if
    a rotation happened. Idempotent + best-effort: any rename failure is
    swallowed so it never blocks daemon startup. Mirrors the shift
    semantics of logging.handlers.RotatingFileHandler — oldest backup is
    discarded, each older backup shifts up one, the live log becomes .1."""
    try:
        if log_path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    for i in range(backups, 0, -1):
        src = log_path if i == 1 else Path(f"{log_path}.{i - 1}")
        dst = Path(f"{log_path}.{i}")
        if src.exists():
            try:
                os.replace(src, dst)  # atomic; overwrites the oldest backup
            except OSError:
                pass
    return True


def read_pid(home: Path) -> int | None:
    """Read the PID file. Returns None if missing, malformed, OR the
    process is genuinely dead (ProcessLookupError → stale-PID cleanup).

    A pid that exists but we can't signal (PermissionError — reused by
    another user, or a sandbox) is returned as-is WITHOUT deleting the
    pidfile: a liveness probe must never remove a live daemon's pidfile
    out from under it."""
    p = pid_file_path(home)
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # No such pid — the daemon is genuinely gone. Clean up the
        # stale pidfile so a fresh `daemon start` isn't blocked.
        try:
            p.unlink()
        except OSError:
            pass
        return None
    except PermissionError:
        # The pid exists but we can't signal it. It is NOT stale, so we
        # must NOT delete the pidfile — doing so on a transient EPERM is
        # exactly how a live daemon lost its pidfile mid-run.
        # The process is demonstrably alive; report it as running.
        return pid
    except OSError:
        return None
    return pid


def _daemon_url(host: str, port: int) -> str:
    # http only -- TLS variants of `relaydeck serve` are out of scope for
    # the simple lifecycle wrapper. Operators using TLS already have a
    # process manager.
    return f"http://{host}:{port}"


def _healthcheck(host: str, port: int, timeout: float) -> bool:
    try:
        with urlopen(_daemon_url(host, port) + "/healthz", timeout=timeout):
            return True
    except (URLError, OSError):
        return False


def start_daemon(
    home: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    wait_seconds: float = 5.0,
) -> dict[str, Any]:
    """Spawn `relaydeck serve` detached. Returns dict with `pid`, `log_path`,
    and `healthy` (whether /healthz responded within `wait_seconds`)."""
    existing = read_pid(home)
    if existing is not None:
        return {"pid": existing, "log_path": str(log_file_path(home)),
                "healthy": _healthcheck(host, port, timeout=1.0),
                "already_running": True}

    home.mkdir(parents=True, exist_ok=True)
    log_path = log_file_path(home)
    # Bound unbounded growth across restarts before re-opening in append
    # mode (no-op when the log is still small).
    _rotate_log_if_large(log_path)
    # Mode 0600 since the log can contain auth-bearing URLs / agent
    # output. Append mode -- restarts accumulate context without
    # losing the previous run's tail.
    log_fd = os.open(str(log_path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # sys.executable + -m so we hit the same interpreter the CLI
        # is running under. start_new_session=True detaches the child
        # from this TTY so closing the parent shell doesn't SIGHUP it.
        proc = subprocess.Popen(
            [sys.executable, "-m", "relaydeck", "serve",
             "--host", host, "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
            start_new_session=True, close_fds=True,
        )
    finally:
        os.close(log_fd)

    pid_path = pid_file_path(home)
    pid_path.write_text(f"{proc.pid}\n")
    try:
        pid_path.chmod(0o600)
    except OSError:
        pass

    deadline = time.monotonic() + wait_seconds
    healthy = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            # Daemon died during startup -- surface that instead of
            # falsely reporting a pid that's already gone.
            try:
                pid_path.unlink()
            except OSError:
                pass
            return {"pid": None, "log_path": str(log_path),
                    "healthy": False, "exited_during_startup": True,
                    "returncode": proc.returncode}
        if _healthcheck(host, port, timeout=0.5):
            healthy = True
            break
        time.sleep(0.2)
    return {"pid": proc.pid, "log_path": str(log_path),
            "healthy": healthy, "already_running": False}


def stop_daemon(home: Path, timeout: float = 5.0) -> dict[str, Any]:
    """Send SIGTERM, wait up to `timeout`, escalate to SIGKILL.
    Returns dict with `pid`, `signal` ("term" | "kill" | None), and
    `was_running`."""
    pid = read_pid(home)
    if pid is None:
        return {"pid": None, "signal": None, "was_running": False}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            pid_file_path(home).unlink()
        except OSError:
            pass
        return {"pid": pid, "signal": None, "was_running": False}

    deadline = time.monotonic() + timeout
    sent = "term"
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        # Still alive after timeout -- escalate.
        try:
            os.kill(pid, signal.SIGKILL)
            sent = "kill"
        except ProcessLookupError:
            pass

    try:
        pid_file_path(home).unlink()
    except OSError:
        pass
    return {"pid": pid, "signal": sent, "was_running": True}


def daemon_status(
    home: Path, host: str = "127.0.0.1", port: int = 8765,
) -> dict[str, Any]:
    """Distinguish four states so the CLI can render them precisely:

    - managed    : pid file points at a live process, /healthz responds
    - sick       : pid file points at a live process, /healthz does NOT
    - foreign    : no pid file (or stale), but /healthz still responds
                   (someone is running `relaydeck serve` in foreground, or
                   via systemd / launchd / docker outside our wrapper)
    - down       : neither

    `running` is True for managed/sick/foreign so simple callers can
    just check that one flag and reach the daemon. `state` is the
    precise label for prettier reporting.
    """
    pid = read_pid(home)
    healthy = _healthcheck(host, port, timeout=1.0)
    if pid is not None:
        state = "managed" if healthy else "sick"
    else:
        state = "foreign" if healthy else "down"
    return {
        "pid": pid,
        "running": state != "down",
        "healthy": healthy,
        "state": state,
        "pid_file": str(pid_file_path(home)),
        "log_file": str(log_file_path(home)),
    }
