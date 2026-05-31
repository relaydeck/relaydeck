"""tests/docker/test_zero_install_scripts.py — B1 shell-script smokes.

`scripts/install-smoke.sh` and `scripts/install-behavior.sh` are the
end-to-end install probes Phase 1 ports from the old `install.yml`
workflow. They each manage their own daemon lifecycle (start, wait for
healthz, stop), so they MUST NOT share a module with API tests that
depend on a long-lived daemon fixture — the scripts would tear it down
mid-run.

Keeping them in a sibling module gives them a clean process space and
makes the test ordering insensitive to pytest's collection order.
"""
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.bucket("zero")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("timeout", 180)
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


@pytest.fixture(autouse=True)
def _daemon_quiesced():
    """Each script does its own daemon start/stop. Ensure no daemon is
    running before each test, and clean up afterwards."""
    _run(["relaydeck", "daemon", "stop", "--timeout", "5"])
    yield
    _run(["relaydeck", "daemon", "stop", "--timeout", "5"])


def test_install_smoke_script_passes():
    """Boot the daemon, hit /healthz + the API, run `relaydeck doctor`,
    stop. The original install.yml smoke, now part of the bucket tests."""
    r = _run(["bash", "/usr/local/bin/install-smoke.sh"], timeout=120)
    assert r.returncode == 0, (
        f"install-smoke.sh failed (rc={r.returncode})\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )


def test_install_behavior_script_runs():
    """'Fresh Ubuntu' degradation probe — no harness CLIs. Informational
    (doesn't exit non-zero on expected degradations), so we just assert it
    reaches the success banner."""
    r = _run(["bash", "/usr/local/bin/install-behavior.sh"], timeout=180)
    assert "behavior probe complete" in r.stdout, (
        f"install-behavior.sh didn't reach the success banner\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
