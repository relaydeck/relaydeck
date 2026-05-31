"""
Daemon lifecycle helpers (`relaydeck daemon start/stop/status`).

The unit tests here cover the in-process functions in `relaydeck.daemon`
that don't actually spawn `relaydeck serve` -- they stub the subprocess
boundary so we can verify the PID-file + signal + status logic
without paying the cost of booting uvicorn in pytest.

A separate end-to-end test boots `relaydeck daemon start` against a tiny
fake server (a Python `-c` one-liner) to verify the start/stop loop
on a real PID without TLS / FastAPI in the picture.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.daemon import (
    daemon_status,
    log_file_path,
    pid_file_path,
    read_pid,
    start_daemon,
    stop_daemon,
)


def test_read_pid_returns_none_when_missing(tmp_path):
    assert read_pid(tmp_path) is None


def test_read_pid_cleans_up_stale_pid(tmp_path):
    """A pid file pointing at a process that no longer exists is a
    stale leftover -- pretend it's not there and unlink it so the
    next `daemon start` doesn't refuse to launch."""
    pf = pid_file_path(tmp_path)
    # PID 1 _exists_ but we can't signal it without root, which
    # triggers the same "not ours" branch. Use a fresh PID we just
    # fork-and-reaped instead so this works as an unprivileged user.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pf.write_text(str(proc.pid))

    assert read_pid(tmp_path) is None
    assert not pf.exists(), "stale pid file should be cleaned up"


def test_read_pid_keeps_live_pidfile_on_permission_error(tmp_path, monkeypatch):
    """A pid we can't signal (EPERM) is ALIVE, not stale — read_pid must
    return it and leave the pidfile intact. Deleting it on a transient
    PermissionError is exactly how a running daemon lost its pidfile
    mid-session; a liveness probe must never remove a live
    daemon's pidfile."""
    import relaydeck.daemon as daemon_mod

    pf = pid_file_path(tmp_path)
    pf.write_text("4242")

    def _eperm(pid, sig):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(daemon_mod.os, "kill", _eperm)

    assert read_pid(tmp_path) == 4242, "EPERM pid exists → reported as running"
    assert pf.exists(), "live daemon's pidfile must NOT be unlinked on EPERM"


def test_stop_daemon_no_pidfile_reports_was_running_false(tmp_path):
    result = stop_daemon(tmp_path, timeout=0.5)
    assert result["was_running"] is False
    assert result["signal"] is None


def test_daemon_status_reports_not_running_with_no_pidfile(tmp_path):
    s = daemon_status(tmp_path, host="127.0.0.1", port=1)  # port 1 = closed
    assert s["running"] is False
    assert s["pid"] is None
    assert s["healthy"] is False
    assert s["state"] == "down"


def test_daemon_status_detects_foreign_daemon(tmp_path, monkeypatch):
    """A daemon started outside our wrapper (foreground `relaydeck serve`,
    systemd, docker) has no pid file. We must still report it as
    running so `relaydeck daemon status` doesn't lie."""
    # Stub the HTTP probe to simulate /healthz being reachable.
    import relaydeck.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "_healthcheck", lambda *a, **kw: True)

    s = daemon_status(tmp_path)
    assert s["running"] is True
    assert s["state"] == "foreign"
    assert s["pid"] is None
    assert s["healthy"] is True


def test_daemon_status_reports_sick_when_pid_alive_http_down(tmp_path, monkeypatch):
    """PID alive but HTTP not responding -- a stuck or crashed-but-
    not-yet-reaped daemon. Operators need this distinct from "down"
    or they'll start a duplicate."""
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    pid_file_path(tmp_path).write_text(str(proc.pid))
    try:
        import relaydeck.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod, "_healthcheck", lambda *a, **kw: False)
        s = daemon_status(tmp_path)
        assert s["state"] == "sick"
        assert s["running"] is True
        assert s["healthy"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)


def test_pid_file_path_is_inside_home(tmp_path):
    # Pinned so a future refactor that "tidies" pid file location
    # doesn't quietly orphan running daemons across versions.
    assert pid_file_path(tmp_path) == tmp_path / "daemon.pid"
    assert log_file_path(tmp_path) == tmp_path / "daemon.log"


# ── End-to-end with a real subprocess ─────────────────────────────


def test_stop_signals_real_process_and_removes_pidfile(tmp_path):
    """End-to-end: write a pid file for a real subprocess we control,
    confirm stop_daemon kills it and cleans up. We use a long-running
    `sleep` instead of `relaydeck serve` so the test doesn't depend on
    uvicorn binding a port."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    pid_file_path(tmp_path).write_text(str(proc.pid))
    try:
        result = stop_daemon(tmp_path, timeout=2.0)
        assert result["was_running"] is True
        assert result["pid"] == proc.pid
        # Either SIGTERM was enough (most likely for a `sleep`) or we
        # escalated to SIGKILL -- both count as "stopped."
        assert result["signal"] in ("term", "kill")
        assert not pid_file_path(tmp_path).exists()

        # Process should be reapable now.
        proc.wait(timeout=2.0)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1.0)


def test_start_daemon_idempotent_when_already_running(tmp_path):
    """If a pid file points at a live process, start_daemon must
    return its pid instead of spawning a duplicate."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    pid_file_path(tmp_path).write_text(str(proc.pid))
    try:
        # port 1 is closed, so healthy=False, but the function should
        # still recognize the existing pid and not double-spawn.
        result = start_daemon(tmp_path, host="127.0.0.1", port=1,
                              wait_seconds=0.1)
        assert result.get("already_running") is True
        assert result["pid"] == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)
