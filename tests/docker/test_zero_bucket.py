"""tests/docker/test_zero_bucket.py — Bucket B1 (zero) tests.

The "fresh Ubuntu / fresh laptop" path: relaydeck is installed via the real
installer but NO harness CLI is on PATH, no providers configured, no
workspaces, no vault keys. We assert the platform installs cleanly, the
daemon boots, the dashboard auto-auths over loopback, and `relaydeck
doctor` is honest about every missing dep (warns but never crashes).

These tests run only inside `relaydeck-test:base` with
`RELAYDECK_TEST_BUCKET=zero`. The `conftest.py` skip-gate enforces that —
no need for runtime checks here.

Phase 1 ships this file at tier 1 (valid-invocation smokes). Tier 2
(destructive / stateful) coverage lands in Phase 2.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.bucket("zero")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a CLI command, capture output, never raise — tests assert on rc."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kwargs)


def _wait_healthz(timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=2):
                return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.3)
    return False


def _bootstrap_token() -> str:
    with urllib.request.urlopen("http://127.0.0.1:8765/api/auth/bootstrap") as resp:
        return json.loads(resp.read())["token"]


def _api_get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:8765{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def daemon():
    """Boot the daemon once per module; tear down at the end. Yields the
    auth token so each test can call the API without re-bootstrapping."""
    # Ensure ~/.relaydeck doesn't pre-exist from a previous run. We can't
    # `rm -rf` it during the container's lifetime if the daemon is up, but
    # at module scope this is the right moment.
    home_relaydeck = os.path.expanduser("~/.relaydeck")
    if os.path.exists(home_relaydeck):
        shutil.rmtree(home_relaydeck)

    start = _run(["relaydeck", "daemon", "start"])
    assert start.returncode == 0, f"daemon start failed: {start.stderr}"
    assert _wait_healthz(), "daemon never answered /healthz"
    token = _bootstrap_token()
    try:
        yield token
    finally:
        _run(["relaydeck", "daemon", "stop", "--timeout", "5"])


# ─── Tier-1 install / version / on-PATH ─────────────────────────────────────

def test_relaydeck_on_path():
    """The shim is installed under /root/.local/bin/ (uv tool default)."""
    assert shutil.which("relaydeck"), "relaydeck not on PATH after install"


def test_rdk_shim_is_alias():
    """`rdk` is the short alias; both should report the same version."""
    rd = _run(["relaydeck", "--version"])
    rdk = _run(["rdk", "--version"])
    assert rd.returncode == 0 and rdk.returncode == 0
    assert rd.stdout == rdk.stdout, f"version mismatch: rd={rd.stdout!r} rdk={rdk.stdout!r}"


def test_help_does_not_crash():
    """Tier-0 self-check for the root command."""
    r = _run(["relaydeck", "--help"])
    assert r.returncode == 0
    assert "daemon" in r.stdout  # group should be listed
    assert "agent" in r.stdout


# ─── Tier-1 doctor honesty ──────────────────────────────────────────────────

def test_doctor_reports_no_harness_clis_honestly():
    """B1 has NO harness CLI. `doctor` must surface that — not crash."""
    r = _run(["relaydeck", "doctor"])
    # doctor exits non-zero when there are issues; that's an expected,
    # honest signal here, not a test failure.
    assert "Harness CLIs" in r.stdout, "doctor output missing harness section"
    # At least pi should be reported as missing.
    assert "pi" in r.stdout
    # And the install hint we care about must be there.
    assert "@mariozechner/pi-coding-agent" in r.stdout or \
        "@mariozechner" in r.stdout, \
        "doctor should suggest the canonical pi install hint"


def test_doctor_reports_no_workspaces():
    r = _run(["relaydeck", "doctor"])
    assert "Workspaces: 0" in r.stdout, \
        f"expected 'Workspaces: 0' in doctor output, got: {r.stdout}"


def test_doctor_exits_nonzero_when_daemon_missing():
    """`doctor` flags the missing daemon as an issue. Exit code is non-zero
    when there are issues; honest signal, not crash."""
    r = _run(["relaydeck", "doctor"])
    assert r.returncode != 0
    assert "Daemon" in r.stdout or "daemon" in r.stdout


# ─── Tier-1 daemon lifecycle ────────────────────────────────────────────────

def test_daemon_start_idempotent(daemon):
    """Second `daemon start` while one is running reports 'already running'."""
    r = _run(["relaydeck", "daemon", "start"])
    assert r.returncode == 0
    assert "already running" in r.stdout.lower() or "already" in r.stdout.lower()


def test_healthz_responds_with_version(daemon):
    with urllib.request.urlopen("http://127.0.0.1:8765/healthz") as resp:
        body = json.loads(resp.read())
    assert "version" in body, f"/healthz missing version: {body}"


def test_loopback_auth_bootstrap_works(daemon):
    """The loopback auto-auth path that the dashboard uses."""
    assert daemon  # the fixture's token
    assert len(daemon) >= 16, "bootstrap token looks too short"


def test_api_agents_empty_after_fresh_install(daemon):
    body = _api_get("/api/agents", daemon)
    assert body == [] or body == {"agents": []}, \
        f"fresh install should have 0 agents, got: {body}"


def test_dashboard_html_serves(daemon):
    with urllib.request.urlopen("http://127.0.0.1:8765/") as resp:
        html = resp.read().decode("utf-8", errors="replace")
    assert "relaydeck" in html.lower(), "dashboard HTML missing 'relaydeck'"


# ─── Tier-1 harness catalog honesty ─────────────────────────────────────────

def test_harness_catalog_marks_all_clis_missing(daemon):
    """No CLI is on PATH in B1; every harness entry must reflect that."""
    body = _api_get("/api/harnesses", daemon)
    # /api/harnesses returns a list of harness entries (or a dict with one).
    entries = body if isinstance(body, list) else body.get("harnesses", [])
    assert entries, "harness catalog is empty"
    # Pi-backed harnesses (pi + relaydeck-native) both report missing pi.
    pi_entries = [h for h in entries if h.get("cli") == "pi"]
    assert pi_entries, "no pi-backed harness in catalog"
    for h in pi_entries:
        assert h.get("cli_installed") is False, \
            f"{h.get('type')} should report cli_installed=False in B1, got: {h}"


def test_relaydeck_native_status_reports_pi_missing(daemon):
    """The status endpoint the new-agent modal + Context tile read."""
    body = _api_get("/api/plugins/relaydeck-native/status", daemon)
    assert body.get("pi_installed") is False
    hint = body.get("install_hint", "")
    assert "@mariozechner/pi-coding-agent" in hint, \
        f"install_hint should reference canonical package, got: {hint!r}"


# ─── Tier-1 onboarding wizard path (API-level — no Playwright) ──────────────

def test_providers_endpoint_empty_or_unconfigured(daemon):
    """B1 has no provider keys; providers endpoint must report none usable.
    Mirror of the wizard's `usableProviders()` check."""
    body = _api_get("/api/providers", daemon)
    providers = body if isinstance(body, list) else body.get("providers", [])
    # All providers should be either: no key, or detected-but-empty-catalog.
    for p in providers:
        if p.get("has_key"):
            pytest.fail(f"B1 should have no provider keys, but {p.get('name')} has_key=True")


def test_providers_detect_returns_empty_or_unregistered(daemon):
    """Local-provider detection on a container without ollama/vllm/lmstudio
    reachable. Must respond cleanly, never 500."""
    try:
        body = _api_get("/api/providers/detect", daemon)
    except urllib.error.HTTPError as e:  # pragma: no cover — only on regression
        pytest.fail(f"/api/providers/detect should not 5xx in B1, got {e.code}")
        return  # unreachable; satisfies the type checker that body is bound below
    candidates = body.get("candidates", []) if isinstance(body, dict) else body
    # Even if a host-side ollama leaks through Docker's bridge network,
    # detect should still return a list — never null or an error shape.
    assert isinstance(candidates, list), f"detect candidates should be a list: {body}"
