"""
Daemon auth boundary tests.

The conftest fixture auto-attaches the test bearer token to every
TestClient. To exercise the auth path itself we either:
  - call `.get(url, headers={"Authorization": ...})` which overrides
    the default per-request, or
  - read `_TEST_TOKEN` from conftest and pass it explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relaydeck import auth
from relaydeck.transports.api import create_app


# Conftest auto-pins this; mirror it here so we can build mismatched
# headers when we need to test the boundary.
_TEST_TOKEN = "test-token-relaydeck-deadbeef-cafef00d"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    cfg_home.mkdir()
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(cfg_home))
    app = create_app(cfg_home)
    return TestClient(app)


def _unauth(client: TestClient, method: str, url: str, **kw):
    """Make a request that explicitly clears the auto-attached token."""
    headers = dict(kw.pop("headers", {}) or {})
    headers["Authorization"] = ""
    return client.request(method, url, headers=headers, **kw)


def test_healthz_is_public(client):
    r = _unauth(client, "GET", "/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dashboard_index_is_public(client):
    r = _unauth(client, "GET", "/")
    # 200 (bundle present) or 404 (stripped checkout). Not 401.
    assert r.status_code != 401


def test_api_route_requires_token(client):
    r = _unauth(client, "GET", "/api/agents")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_api_route_rejects_wrong_token(client):
    r = client.get("/api/agents", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_api_route_accepts_correct_token(client):
    # Default header from conftest is the right token.
    r = client.get("/api/agents")
    assert r.status_code == 200


def test_api_route_accepts_token_query(client):
    """SSE / curl clients that can't set headers fall back to ?token=."""
    r = _unauth(client, "GET", f"/api/agents?token={_TEST_TOKEN}")
    assert r.status_code == 200


def test_bootstrap_returns_token_on_loopback(client):
    """TestClient sets Host: testserver, which our _is_loopback_request
    treats as loopback. Bootstrap is unauthenticated by design."""
    r = _unauth(client, "GET", "/api/auth/bootstrap")
    assert r.status_code == 200
    assert r.json()["token"] == _TEST_TOKEN


def test_bootstrap_refuses_remote_host(client):
    """A reverse proxy that rewrites Host must not be able to extract
    the token. Spoof a non-loopback Host header and expect 403."""
    r = _unauth(client, "GET", "/api/auth/bootstrap", headers={"Host": "evil.com"})
    assert r.status_code == 403


def test_verify_accepts_correct_token(client):
    """The dashboard hits /api/auth/verify after pulling a token from
    localStorage to detect rotated/stale values. Valid tokens get 200."""
    r = client.get("/api/auth/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_verify_rejects_wrong_token(client):
    """Stale tokens 401 here so the dashboard can clear localStorage
    and re-prompt instead of silently rendering empty panels."""
    r = client.get("/api/auth/verify", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_verify_requires_token(client):
    r = _unauth(client, "GET", "/api/auth/verify")
    assert r.status_code == 401


# ── CLI: `relaydeck auth token` / `relaydeck auth rotate` ──────────────────────


def test_cli_auth_token_prints_token(tmp_path, monkeypatch):
    """`relaydeck auth token` should print the on-disk token to stdout
    (or mint one if none exists), with no decorations — pipe-friendly."""
    from click.testing import CliRunner
    from relaydeck.transports.cli import main as cli

    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "token"])
    assert result.exit_code == 0, result.output
    printed = result.output.strip()
    assert printed, "token should be printed"

    # Same token on second call — idempotent.
    result2 = runner.invoke(cli, ["auth", "token"])
    assert result2.output.strip() == printed


def test_auth_commands_skip_plugin_bootstrap():
    from relaydeck import _skip_plugin_cli_bootstrap

    assert _skip_plugin_cli_bootstrap(["auth", "token"])
    assert _skip_plugin_cli_bootstrap(["auth", "show"])
    assert _skip_plugin_cli_bootstrap(["--verbose", "auth", "token"])
    assert not _skip_plugin_cli_bootstrap(["github", "status"])
    assert not _skip_plugin_cli_bootstrap(["workspace", "list"])


def test_cli_auth_token_reflects_rotation(tmp_path, monkeypatch):
    """After `relaydeck auth rotate`, `relaydeck auth token` should print the
    new value, not the old one. Belt-and-suspenders against caching
    creeping into either command.
    """
    from click.testing import CliRunner
    from relaydeck.transports.cli import main as cli

    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    runner = CliRunner()
    before = runner.invoke(cli, ["auth", "token"]).output.strip()
    assert before
    rotated = runner.invoke(cli, ["auth", "rotate", "--yes"])
    assert rotated.exit_code == 0, rotated.output
    after = runner.invoke(cli, ["auth", "token"]).output.strip()
    assert after and after != before


def test_mutation_requires_token(client):
    """Verify a POST mutation (workspace create-style) 401s without
    the token — GET-only coverage isn't enough for an auth boundary."""
    r = _unauth(client, "POST", "/api/workspaces", json={"name": "x", "path": "/tmp/x"})
    assert r.status_code == 401


# ── Token persistence (relaydeck/auth.py) ──────────────────────────────────


def test_token_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    t1 = auth.get_or_create_token()
    assert t1 and len(t1) >= 32
    t2 = auth.get_or_create_token()
    assert t1 == t2

    token_file = fake_home / ".relaydeck" / "auth-token"
    assert token_file.exists()
    assert (token_file.stat().st_mode & 0o777) == 0o600


def test_token_write_is_atomic_and_never_world_readable(tmp_path, monkeypatch):
    """Regression for the find: `write_text` + `chmod` opened a brief
    window where the token sat at the umask-derived mode (often 0644).
    The fix uses os.fchmod on a freshly-created FD plus an atomic
    rename. Verify by writing under an *explicitly permissive umask*
    and confirming the final file is still 0600 — pre-fix, the
    umask would have leaked through.
    """
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Force a permissive umask so any inherited-perms path becomes
    # 0o666 instead of the usual 0o644 — easier to see the bug if it
    # comes back.
    prev_umask = os.umask(0)
    try:
        auth.write_token("regression-token")
    finally:
        os.umask(prev_umask)

    token_file = fake_home / ".relaydeck" / "auth-token"
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600, f"token wrote at {oct(mode)}, expected 0o600"
    assert token_file.read_text() == "regression-token"


def test_token_env_overrides_file(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    auth.write_token("on-disk-value")
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "env-value")

    assert auth.read_token() == "env-value"


def test_token_rotation_invalidates_old(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    old = auth.get_or_create_token()
    new = auth.regenerate_token()
    assert old != new
    assert auth.verify_token(new) is True
    assert auth.verify_token(old) is False


def test_verify_token_rejects_empty_and_none(monkeypatch):
    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "abc")
    assert auth.verify_token("abc") is True
    assert auth.verify_token("") is False
    assert auth.verify_token(None) is False
    assert auth.verify_token("ab") is False
    assert auth.verify_token("abcd") is False


# ── RemoteHost.from_local ────────────────────────────────────────────


def test_remotehost_from_local_reads_token(monkeypatch):
    from relaydeck.sdk import RemoteHost

    monkeypatch.setenv("RELAYDECK_AUTH_TOKEN", "remote-token-1")
    monkeypatch.setenv("RELAYDECK_DAEMON_URL", "http://127.0.0.1:9999")
    h = RemoteHost.from_local()
    assert h.token == "remote-token-1"
    assert h.daemon_url == "http://127.0.0.1:9999"


def test_remotehost_from_local_raises_without_token(tmp_path, monkeypatch):
    from relaydeck.sdk import RemoteHost

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="no daemon token"):
        RemoteHost.from_local()


def test_static_paths_are_public(client):
    r = _unauth(client, "GET", "/static/plugins/nonexistent/x.js")
    assert r.status_code != 401


def test_provider_logo_path_is_public(client, monkeypatch):
    """Logos load via plain <img src> (no Bearer header possible), so the
    proxy must be exempt from auth — otherwise every logo 401s in the
    browser. Regression: caught in a Playwright smoke test."""
    monkeypatch.setattr("relaydeck.models_dev.fetch_logo",
                        lambda name, ch=None: b"<svg/>")
    r = _unauth(client, "GET", "/api/providers/anthropic/logo")
    assert r.status_code != 401
    assert r.status_code == 200

    # The exemption is scoped to the /logo suffix — a sibling provider
    # route still requires a token.
    r2 = _unauth(client, "GET", "/api/providers/anthropic/models")
    assert r2.status_code == 401
