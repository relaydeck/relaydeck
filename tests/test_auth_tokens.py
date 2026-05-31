"""
Scoped Bearer tokens.

These tests exercise:

  - issue → hash stored, plaintext returned exactly once,
  - verify → hash lookup, last_used_at updated,
  - revoke → subsequent verify returns None,
  - expiry → after expires_at, verify returns None,
  - middleware enforcement → read-only token blocked on POST,
                              read-only token allowed on GET,
                              implicit root file still works,
  - audit identity is attached to request.state.identity.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from relaydeck.auth_tokens import (
    SCOPE_READ_ONLY,
    SCOPE_ROOT,
    hash_token,
    issue_token,
    list_tokens,
    revoke_token,
    verify_db_token,
)
from relaydeck.db import open_db


# ── Issue / verify / revoke happy paths ─────────────────────────────


def test_issue_returns_token_id_and_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    tok_id, plaintext = issue_token(label="ci", scope=SCOPE_ROOT)
    assert tok_id.startswith("tok_")
    assert len(plaintext) >= 32, "token must have enough entropy"


def test_issue_stores_hash_not_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)
    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")

    tok_id, plaintext = issue_token(label="scraper", scope=SCOPE_READ_ONLY)

    conn = open_db(db)
    try:
        row = conn.execute(
            "SELECT hashed_token FROM auth_tokens WHERE id = ?", (tok_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["hashed_token"] == hash_token(plaintext)
    # And the plaintext is nowhere to be found.
    assert plaintext not in row["hashed_token"]


def test_verify_db_token_returns_identity_with_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    _, plaintext = issue_token(label="scraper", scope=SCOPE_READ_ONLY)
    identity = verify_db_token(plaintext)
    assert identity is not None
    assert identity.scope == SCOPE_READ_ONLY
    assert identity.label == "scraper"
    assert identity.is_read_only is True
    assert identity.is_root is False


def test_verify_db_token_updates_last_used_at(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)
    db = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")

    tok_id, plaintext = issue_token(label="ci", scope=SCOPE_ROOT)

    conn = open_db(db)
    try:
        before = conn.execute(
            "SELECT last_used_at FROM auth_tokens WHERE id = ?", (tok_id,),
        ).fetchone()
    finally:
        conn.close()
    assert before["last_used_at"] is None

    verify_db_token(plaintext)

    conn = open_db(db)
    try:
        after = conn.execute(
            "SELECT last_used_at FROM auth_tokens WHERE id = ?", (tok_id,),
        ).fetchone()
    finally:
        conn.close()
    assert after["last_used_at"] is not None


def test_verify_db_token_returns_none_for_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    assert verify_db_token("not-a-real-token") is None


def test_revoke_makes_subsequent_verify_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    tok_id, plaintext = issue_token(label="x", scope=SCOPE_ROOT)
    assert verify_db_token(plaintext) is not None
    assert revoke_token(tok_id) is True
    assert verify_db_token(plaintext) is None
    # Re-revoking is a no-op (False) — already revoked.
    assert revoke_token(tok_id) is False


def test_expired_token_does_not_verify(tmp_path, monkeypatch):
    """If `expires_at` is in the past, the row exists but verify_db_token
    returns None. Subsequent calls don't update last_used_at."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    past = time.time() - 60
    _, plaintext = issue_token(label="exp", scope=SCOPE_ROOT, expires_at=past)
    assert verify_db_token(plaintext) is None


# ── Validation ──────────────────────────────────────────────────────


def test_issue_rejects_invalid_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    with pytest.raises(ValueError):
        issue_token(label="bad", scope="superadmin")


def test_issue_accepts_agent_and_plugin_prefixed_scopes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    # Don't raise.
    issue_token(label="a", scope="agent:reviewer")
    issue_token(label="b", scope="plugin:metering")


def test_issue_rejects_empty_label(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    with pytest.raises(ValueError):
        issue_token(label="   ", scope=SCOPE_ROOT)


# ── list_tokens ─────────────────────────────────────────────────────


def test_list_tokens_returns_rows_with_no_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    issue_token(label="a", scope=SCOPE_ROOT)
    issue_token(label="b", scope=SCOPE_READ_ONLY)
    rows = list_tokens()
    labels = {r["label"] for r in rows}
    assert labels == {"a", "b"}
    # Listing must not leak the hash either — operators see only what
    # `relaydeck auth list` should print.
    assert all("hashed_token" not in r for r in rows)


# ── Middleware enforcement ──────────────────────────────────────────


def _build_app() -> FastAPI:
    """Build a tiny FastAPI app with the real `_AuthMiddleware` so we
    can drive it via TestClient. We don't need any real routes — the
    middleware decides 401/403 before the route runs."""
    from relaydeck.transports.api import _AuthMiddleware

    app = FastAPI()
    app.add_middleware(_AuthMiddleware)

    @app.get("/api/secret")
    async def secret_get(request: Request):
        ident = request.state.identity
        return {"scope": ident.scope, "label": ident.label}

    @app.post("/api/secret")
    async def secret_post(request: Request):
        ident = request.state.identity
        return {"scope": ident.scope, "label": ident.label}

    return app


def test_middleware_attaches_identity_for_file_root(monkeypatch):
    """The implicit on-disk auth-token (the daemon's startup credential)
    maps to a root identity attached to request.state."""
    # conftest pins RELAYDECK_AUTH_TOKEN session-wide; the middleware
    # delegates to `verify_token` which reads that var.
    import os
    token = os.environ["RELAYDECK_AUTH_TOKEN"]
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/api/secret", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == SCOPE_ROOT


def _client_no_auth(app: FastAPI) -> TestClient:
    """Build a TestClient that does NOT auto-inject the session
    Bearer token (the conftest fixture installs one by default).
    Used by the negative-auth tests that need the request to land
    at the middleware with no credentials."""
    c = TestClient(app)
    c.headers.pop("Authorization", None)
    return c


def test_middleware_rejects_missing_bearer():
    app = _build_app()
    with _client_no_auth(app) as c:
        r = c.get("/api/secret")
    assert r.status_code == 401


def test_middleware_rejects_wrong_bearer():
    app = _build_app()
    with _client_no_auth(app) as c:
        r = c.get("/api/secret", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_read_only_token_allowed_on_get(tmp_path, monkeypatch):
    """A read-only scoped token can GET but not POST."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    _, plaintext = issue_token(label="ro", scope=SCOPE_READ_ONLY)

    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/api/secret", headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == SCOPE_READ_ONLY


def test_read_only_token_blocked_on_post(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    _, plaintext = issue_token(label="ro", scope=SCOPE_READ_ONLY)

    app = _build_app()
    with TestClient(app) as c:
        r = c.post(
            "/api/secret",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]


def test_root_scoped_token_can_post(tmp_path, monkeypatch):
    """A token issued with --scope root has the same powers as the
    file root token — POST is allowed."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    _, plaintext = issue_token(label="ci", scope=SCOPE_ROOT)

    app = _build_app()
    with TestClient(app) as c:
        r = c.post(
            "/api/secret",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert r.status_code == 200, r.text


def test_revoked_token_returns_401(tmp_path, monkeypatch):
    """After `relaydeck auth revoke`, the plaintext immediately stops
    working — 401, not 403."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    tok_id, plaintext = issue_token(label="x", scope=SCOPE_ROOT)
    revoke_token(tok_id)

    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/api/secret", headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 401


def test_query_token_works_for_sse_path(tmp_path, monkeypatch):
    """SSE/WS clients can't always set Authorization — `?token=` works
    too. This is exercised on the GET path. The conftest's auto-
    injected Bearer is removed so the only credential is the query."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    (tmp_path / ".relaydeck" / "runtime").mkdir(parents=True)

    _, plaintext = issue_token(label="dash", scope=SCOPE_ROOT)

    app = _build_app()
    with _client_no_auth(app) as c:
        r = c.get(f"/api/secret?token={plaintext}")
    assert r.status_code == 200
