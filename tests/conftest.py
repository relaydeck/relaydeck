"""
Test-suite-wide fixtures.

The daemon auth middleware (relaydeck/auth.py + relaydeck/transports/api.py)
requires every non-public HTTP request to carry a Bearer token. Tests
use `fastapi.testclient.TestClient` directly, so they would all 401
without help. Rather than touch every test file, this conftest:

  1. Sets `RELAYDECK_AUTH_TOKEN` to a deterministic test token via an
     autouse session fixture. The token middleware then accepts that
     token because `relaydeck.auth.read_token()` consults the env first.

  2. Monkey-patches `TestClient.__init__` to install the same token as
     a default `Authorization` header on every constructed client.
     Existing test code continues to read `TestClient(app)` without
     change.

Tests that want to verify auth behavior end-to-end (401 with no
token, rotation, etc.) construct an httpx client themselves and
bypass the patch.
"""

from __future__ import annotations

import os

import pytest


_TEST_TOKEN = "test-token-relaydeck-deadbeef-cafef00d"


@pytest.fixture(autouse=True, scope="session")
def _relaydeck_test_auth_token():
    """Pin RELAYDECK_AUTH_TOKEN for the whole test session.

    Any read_token() in the daemon resolves to this value, so the
    auto-injected Authorization headers below validate cleanly.
    """
    prev = os.environ.get("RELAYDECK_AUTH_TOKEN")
    os.environ["RELAYDECK_AUTH_TOKEN"] = _TEST_TOKEN
    yield _TEST_TOKEN
    if prev is None:
        os.environ.pop("RELAYDECK_AUTH_TOKEN", None)
    else:
        os.environ["RELAYDECK_AUTH_TOKEN"] = prev


# Env vars the harness sets on purpose — never strip these.
_AMBIENT_KEEP = {"RELAYDECK_AUTH_TOKEN"}


@pytest.fixture(autouse=True)
def _isolate_ambient_relaydeck_env(monkeypatch):
    """Hermeticity vs. the *ambient* environment.

    When the suite runs inside a relaydeck-managed agent (dogfooding), the
    daemon has injected RELAYDECK_WORKSPACE / RELAYDECK_AGENT_ID / git-context
    vars into the shell. CLI + workspace-resolution code reads those, so tests
    that assume "no active workspace / agent" spuriously fail (~12 of them)
    even though they pass in CI. Strip every RELAYDECK_* var except the ones
    the harness sets deliberately, so a test sees the same clean baseline
    whether run in CI or inside an agent. Tests that need a var set it
    themselves via monkeypatch (which runs after this fixture and wins).
    """
    for key in list(os.environ):
        if key.startswith("RELAYDECK_") and key not in _AMBIENT_KEEP:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _flush_db_pools_after_test():
    """The connection pool in relaydeck.db keeps connections alive across
    test runs by design (it's a daemon-lifetime cache). In the test
    suite that means every fresh tmp_path DB leaves stale fds pinned
    in the pool. Close them after each test so the suite stays under
    the open-fd ulimit and tests can't see each other's connections.
    """
    yield
    try:
        from relaydeck.db import _close_all_pools
        _close_all_pools()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _disable_models_dev_network():
    """Hard-disable models.dev live fetches for the whole suite so no test
    can hit the network (api.json or logos). Tests that exercise the fetch
    path (test_models_dev) re-enable it locally and inject a fixture via the
    monkeypatched `_fetch_raw` / `urlopen` seams."""
    from relaydeck import models_dev
    models_dev.set_fetch_disabled(True)
    models_dev.clear_cache()
    yield
    models_dev.set_fetch_disabled(True)


@pytest.fixture(autouse=True, scope="session")
def _testclient_auto_auth():
    """Auto-attach the bearer token to every TestClient.

    httpx clients merge default headers per request, so any test that
    overrides Authorization explicitly still wins. Tests verifying the
    no-token path should construct an httpx client directly with
    `app=app` to skip this wrapper.
    """
    from fastapi.testclient import TestClient

    orig = TestClient.__init__

    def wrapped_init(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Authorization", f"Bearer {_TEST_TOKEN}")
        kwargs["headers"] = headers
        orig(self, *args, **kwargs)

    TestClient.__init__ = wrapped_init
    yield
    TestClient.__init__ = orig
