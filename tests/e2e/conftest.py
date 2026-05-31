"""
Fixtures for `pytest -m e2e`.

E2E tests need a real harness binary on PATH and a real LLM endpoint.
Skip cleanly when either is missing -- on a contributor's laptop with
no API key, the dedicated e2e workflow on a fork without secrets, or
a local `pytest -m e2e` invocation that the operator forgot to set up.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        pytest.skip(f"{name} not on PATH; install it to run this E2E test")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} not set; export it or add as a CI secret")
    return value


@pytest.fixture
def pi_binary() -> str:
    """Skip if the pi harness binary isn't available."""
    _require_binary("pi")
    return "pi"


@pytest.fixture
def openrouter_key() -> str:
    """Skip if no OpenRouter API key is set."""
    return _require_env("OPENROUTER_API_KEY")


@pytest.fixture
def e2e_model() -> str:
    """Free-tier OpenRouter model used by the smoke tests.

    Override with RELAYDECK_E2E_MODEL for local experimentation. Default is
    the cheapest free-tier model we've verified responds in a single
    turn -- if it changes upstream, this is the one knob to twist."""
    return os.environ.get("RELAYDECK_E2E_MODEL", "mistralai/mistral-7b-instruct:free")


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin Path.home() to tmp_path so the test never touches the
    operator's ~/.relaydeck. Required because the orchestrator
    derives DB / agents / runtime paths from $HOME."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    # The orchestrator + plugin registry are module-level singletons. Reset
    # them per test so each e2e test binds them to ITS isolated home —
    # otherwise whichever e2e test runs first pins the config_home for the
    # rest (a stale orchestrator then can't find a later test's agent spec).
    import relaydeck.orchestrator as _orch
    import relaydeck.plugin as _plug
    monkeypatch.setattr(_orch, "_orchestrator", None)
    monkeypatch.setattr(_plug, "_registry", None)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    return cfg


# ── Browser E2E (Playwright) ────────────────────────────────────────────
# The web e2e tests (test_web_harness_e2e.py) drive a REAL dashboard in a
# headless Chromium against a REAL daemon on an isolated $HOME. They reuse
# the same skip-cleanly discipline: no playwright / no browser / no daemon
# → skip, never fail. Install with `uv sync --group e2e` +
# `uv run playwright install chromium`.

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def daemon_home(tmp_path: Path) -> Path:
    """Isolated ``$HOME`` for the ``live_daemon`` fixture (``tmp_path/home``)."""
    return tmp_path / "home"


def _e2e_browser_headed(request: pytest.FixtureRequest, slow_mo: int) -> bool:
    """Headless by default (safe for ``pytest -m e2e`` batch runs). Headed when
    explicitly opted in, slowmo is set (signal to watch), or a single test id
    is targeted (``path::test_name``). ``RELAYDECK_E2E_HEADLESS=1`` always wins."""
    if os.environ.get("RELAYDECK_E2E_HEADLESS", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("RELAYDECK_E2E_HEADED", "").lower() in ("1", "true", "yes"):
        return True
    if slow_mo > 0:
        return True
    args = [a for a in request.config.args if a and not a.startswith("-")]
    return len(args) == 1 and "::" in args[0]


@pytest.fixture
def browser(request):
    """A Chromium for the e2e tests, or skip if Playwright / its browser is
    absent. Headless by default; headed when ``RELAYDECK_E2E_HEADED=1``,
    ``RELAYDECK_E2E_SLOWMO>0``, or a single ``file.py::test`` target is
    selected. Set ``RELAYDECK_E2E_HEADLESS=1`` for CI. Optional
    ``RELAYDECK_E2E_SLOWMO=<ms>`` slows each action. (We launch our own
    browser, so the pytest-playwright ``--headed`` flag does not apply.)"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed -- `uv sync --group e2e`")
    try:
        slow_mo = int(os.environ.get("RELAYDECK_E2E_SLOWMO", "0") or "0")
    except ValueError:
        slow_mo = 0
    headed = _e2e_browser_headed(request, slow_mo)
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=not headed, slow_mo=slow_mo)
    except Exception as exc:  # browser binary not installed
        pw.stop()
        pytest.skip(
            "chromium not available -- `uv run playwright install chromium` "
            f"({exc})"
        )
    try:
        yield b
    finally:
        b.close()
        pw.stop()


def _spawn_live_daemon(daemon_home: Path, *, path_override: str | None = None):
    """Yield a daemon base URL; internal helper for ``live_daemon*`` fixtures."""
    relaydeck = shutil.which("relaydeck")
    if relaydeck is None:
        pytest.skip("relaydeck not on PATH -- run inside the project venv")

    home = daemon_home
    home.mkdir()
    port = _free_port()
    env = dict(os.environ)
    env["HOME"] = str(home)
    if path_override is not None:
        env["PATH"] = path_override
    env.pop("RELAYDECK_DAEMON_URL", None)

    proc = subprocess.Popen(
        [relaydeck, "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode(errors="replace")
                pytest.fail(f"daemon exited during startup (rc={proc.returncode}):\n{out}")
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.3)
        else:
            pytest.fail("daemon did not answer /healthz within 30s")
        yield base
    finally:
        try:
            agents = json.loads(
                urllib.request.urlopen(base + "/api/agents", timeout=3).read()
            )
        except Exception:
            agents = []
        for a in agents if isinstance(agents, list) else []:
            with contextlib.suppress(Exception):
                req = urllib.request.Request(
                    f"{base}/api/agents/{a.get('id')}/stop", method="POST", data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=3).read()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        with contextlib.suppress(Exception):
            subprocess.run(["pkill", "-9", "-f", str(home)], capture_output=True, timeout=5)


@pytest.fixture
def live_daemon(daemon_home: Path):
    """Spawn a real `relaydeck serve` on an ephemeral port with an isolated
    $HOME (so it never touches the operator's ~/.relaydeck) and yield its
    base URL. $HOME isolation also means harnesses spawn without their normal
    auth -- fine for the web smoke, which asserts the PTY *renders*, not that
    a model replies. Skips if the relaydeck entrypoint isn't on PATH."""
    yield from _spawn_live_daemon(daemon_home)


def _live_daemon_url() -> str | None:
    if os.environ.get("RELAYDECK_E2E_LIVE_DAEMON", "").lower() not in ("1", "true", "yes"):
        return None
    return os.environ.get("RELAYDECK_DAEMON_URL", "http://127.0.0.1:8765").rstrip("/")


def _healthz_ok(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _operator_daemon_up() -> bool:
    return _healthz_ok("http://127.0.0.1:8765")


@pytest.fixture
def telegram_e2e_base(live_daemon):
    """Daemon URL for Telegram live e2e.

    Default: isolated ephemeral ``live_daemon`` (clean $HOME).

    ``RELAYDECK_E2E_LIVE_DAEMON=1`` → reuse the operator's running daemon
    (``RELAYDECK_DAEMON_URL``, default ``http://127.0.0.1:8765``). Required
    when the same bot token is already polling there — Telegram allows only
    ONE getUpdates consumer per bot."""
    live = _live_daemon_url()
    if live:
        if not _healthz_ok(live):
            pytest.fail(f"RELAYDECK_E2E_LIVE_DAEMON=1 but {live}/healthz is down")
        yield live
        return
    if _operator_daemon_up():
        pytest.fail(
            "Your relaydeck daemon is already running on :8765 with this bot token. "
            "Telegram delivers getUpdates to only ONE poller — an isolated e2e "
            "daemon will never see your DMs (the test looks 'stuck').\n\n"
            "Re-run with:\n"
            "  RELAYDECK_E2E_LIVE_DAEMON=1 RELAYDECK_E2E_HEADED=1 … pytest …\n"
            "Or stop the live daemon first:\n"
            "  uv run relaydeck daemon stop"
        )
    yield live_daemon


@pytest.fixture
def live_daemon_no_pi(daemon_home: Path):
    """Like ``live_daemon`` but the daemon's PATH hides ``pi``.

    Proves harness-catalog / status probes use live ``shutil.which`` — no
    daemon restart after installing pi. Skips when pi isn't installed on the
    runner (nothing to strip)."""
    if shutil.which("pi") is None:
        pytest.skip("pi not on PATH — install pi to exercise PATH-stripping")
    from ._webutil import path_without_binaries

    stripped = path_without_binaries("pi")
    assert shutil.which("pi", path=stripped) is None, "PATH strip did not hide pi"
    yield from _spawn_live_daemon(daemon_home, path_override=stripped)
