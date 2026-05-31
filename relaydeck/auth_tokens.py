"""
Scoped Bearer tokens.

Each row in `auth_tokens` is one named credential the daemon will
accept. The plaintext token only exists in memory at issue time —
the DB stores `sha256(token)` so a leaked database file doesn't grant
access. Issuance prints the plaintext exactly once.

## Scopes

  - `root`             — full read+write access. Equivalent to the
                          implicit on-disk auth-token file.
  - `read-only`        — GET allowed; mutating verbs return 403.
  - `agent:<id>`       — scoped to one agent; for future use.
  - `plugin:<name>`    — scoped to one plugin's API surface; future.

The middleware enforces `root` vs `read-only` today. Agent/plugin
scopes are accepted and stored but treated like read-only on the
existing routes — the per-route declarations land alongside each
route's own audit emission. We don't grow the scope vocabulary
without a need.

## Compatibility

The existing on-disk auth-token file is unchanged. `verify_token`
in `relaydeck.auth` still recognizes it as the implicit root token —
that's the daemon's startup credential and the dashboard
bootstrap path. The `auth_tokens` table is *additional* — operators
add named credentials via `relaydeck auth issue --scope ...`.

`AuthIdentity` is what the middleware attaches to the request:
either `IDENTITY_ROOT_FILE` (the on-disk token) or a record from
this table. Route handlers don't typically inspect it — the scope
check happens in the middleware — but audit emission references
`identity.token_id` so the audit log can answer "which token did
this".
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from relaydeck.db import _PooledConnection, open_db  # noqa: F401 (re-exported for tests)

logger = logging.getLogger(__name__)


# Scope vocabulary. Kept tight on purpose — only add when a route
# needs the distinction and the audit story has been thought through.
SCOPE_ROOT = "root"
SCOPE_READ_ONLY = "read-only"


# In-memory marker for the on-disk root token. Has no DB row.
IDENTITY_ROOT_FILE = "root-file"


@dataclass(frozen=True)
class AuthIdentity:
    """The authenticated principal for one request.

    `token_id` is the auth_tokens.id of the credential that passed
    verification, or the sentinel `IDENTITY_ROOT_FILE` for the
    on-disk auth-token file (which has no row). `label` is a
    human-readable name for audit log + UI. `scope` is one of the
    SCOPE_* constants.
    """
    token_id: str
    label: str
    scope: str

    @property
    def is_root(self) -> bool:
        return self.scope == SCOPE_ROOT

    @property
    def is_read_only(self) -> bool:
        return self.scope == SCOPE_READ_ONLY

    @property
    def is_file_root(self) -> bool:
        """True for the implicit on-disk root token (no DB row)."""
        return self.token_id == IDENTITY_ROOT_FILE


_FILE_ROOT_IDENTITY = AuthIdentity(
    token_id=IDENTITY_ROOT_FILE,
    label="root-file",
    scope=SCOPE_ROOT,
)


def file_root_identity() -> AuthIdentity:
    return _FILE_ROOT_IDENTITY


# ── Hashing ─────────────────────────────────────────────────────────


def hash_token(plaintext: str) -> str:
    """sha256 of the token, lowercased hex. Stable across hosts; no
    salt because the token itself is 256-bit secret. A leaked DB file
    is uncrackable without the plaintext (which we never store)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# ── Issuance ────────────────────────────────────────────────────────


def issue_token(
    *,
    label: str,
    scope: str,
    expires_at: Optional[float] = None,
    db_path: Optional[str] = None,
) -> tuple[str, str]:
    """Mint a new scoped token. Returns `(token_id, plaintext)`.

    The plaintext is shown to the operator exactly once. Subsequent
    lookups happen via the sha256 hash. `label` is the operator-
    facing name; `scope` is one of the SCOPE_* constants or a
    prefixed scope like `agent:<id>`.
    """
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    if not _is_valid_scope(scope):
        raise ValueError(f"invalid scope: {scope!r}")
    plaintext = secrets.token_hex(32)
    tok_id = "tok_" + secrets.token_urlsafe(12)
    digest = hash_token(plaintext)
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        conn.execute(
            """INSERT INTO auth_tokens
                   (id, label, scope, hashed_token, created_at, last_used_at,
                    expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)""",
            (tok_id, label, scope, digest, time.time(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return tok_id, plaintext


def revoke_token(token_id: str, *, db_path: Optional[str] = None) -> bool:
    """Mark a token revoked. Returns True if an unrevoked row was
    flipped, False otherwise."""
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        cursor = conn.execute(
            "UPDATE auth_tokens SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (time.time(), token_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ── Verification ────────────────────────────────────────────────────


def verify_db_token(plaintext: str, *, db_path: Optional[str] = None) -> Optional[AuthIdentity]:
    """Look up a presented Bearer token in the auth_tokens table.

    Returns the matching identity on success, or None for:
      - no matching row,
      - the row exists but is revoked,
      - the row exists but has expired.

    Also updates `last_used_at` on success — cheap UPDATE, runs once
    per request, gives operators visibility in `relaydeck auth list`.
    """
    if not plaintext:
        return None
    digest = hash_token(plaintext)
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        row = conn.execute(
            "SELECT id, label, scope, expires_at, revoked_at "
            "FROM auth_tokens WHERE hashed_token = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            return None
        conn.execute(
            "UPDATE auth_tokens SET last_used_at = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
        conn.commit()
        return AuthIdentity(
            token_id=row["id"], label=row["label"], scope=row["scope"],
        )
    finally:
        conn.close()


# ── Listing ─────────────────────────────────────────────────────────


def list_tokens(*, db_path: Optional[str] = None) -> list[dict]:
    """All token rows for `relaydeck auth list`. Hashes are excluded —
    operators see the label, scope, last-used timestamp, and expiry."""
    conn = open_db(db_path) if db_path else _default_conn()
    try:
        rows = conn.execute(
            "SELECT id, label, scope, created_at, last_used_at, expires_at, revoked_at "
            "FROM auth_tokens ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── Helpers ─────────────────────────────────────────────────────────


def _is_valid_scope(scope: str) -> bool:
    if scope in (SCOPE_ROOT, SCOPE_READ_ONLY):
        return True
    if scope.startswith("agent:") and len(scope) > len("agent:"):
        return True
    if scope.startswith("plugin:") and len(scope) > len("plugin:"):
        return True
    return False


def _default_conn():
    """Resolve the default DB path from the daemon's config home so
    callers without an explicit `db_path` (CLI commands, tests using
    Path.home() monkey-patches) hit the right file."""
    from pathlib import Path
    return open_db(str(Path.home() / ".relaydeck" / "runtime" / "relaydeck.db"))
