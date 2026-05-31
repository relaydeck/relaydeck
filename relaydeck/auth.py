"""
Local daemon auth token.

The daemon ships a single shared-secret Bearer token at
`~/.relaydeck/auth-token` (mode 0600). Every mutation HTTP call and
every SSE/WebSocket stream must present it via `Authorization: Bearer`
or `?token=` query string. The CLI and `RemoteHost` read it from the
file (or `RELAYDECK_AUTH_TOKEN`) and forward it transparently — users
operating on the same machine never see the token.

Why a Bearer token when the daemon binds to 127.0.0.1 only?

  - File mode 0600 stops other UNIX users on the same machine.
  - The token stops cross-origin browser tabs from poking the API.
    A malicious page at evil.com can fetch http://127.0.0.1:8765 from
    the user's browser, but it cannot read the Authorization header
    or the response (CORS is locked to loopback origins) and it cannot
    upgrade to a WS without the token query.
  - Provides a clean place for cross-machine federation later — a
    user can copy the token to another machine and `RELAYDECK_DAEMON_URL` +
    `RELAYDECK_AUTH_TOKEN` is enough to drive a remote daemon.

The token is regenerated on demand via `relaydeck auth rotate` — running
clients must re-read the file.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_TOKEN_BYTES = 32  # 256-bit


def _token_path() -> Path:
    return Path.home() / ".relaydeck" / "auth-token"


def read_token() -> str | None:
    """Return the daemon token, or None if no token is configured.

    Order, highest first:
      1. `RELAYDECK_AUTH_TOKEN` env var (tests, CI, ad-hoc shells)
      2. `~/.relaydeck/auth-token` file
      3. None — caller decides whether to mint one
    """
    env = os.environ.get("RELAYDECK_AUTH_TOKEN")
    if env:
        s = env.strip()
        if s:
            return s

    p = _token_path()
    if not p.exists():
        return None
    try:
        with _LOCK:
            s = p.read_text().strip()
        return s or None
    except OSError as exc:
        logger.warning("auth-token: read failed: %s", exc)
        return None


def get_or_create_token() -> str:
    """Return the daemon token, generating + persisting one if needed.

    Called by `relaydeck serve` at startup. Idempotent — if a token already
    exists, returns it untouched.
    """
    existing = read_token()
    if existing:
        return existing

    new = secrets.token_hex(_TOKEN_BYTES)
    write_token(new)
    return new


def write_token(token: str) -> None:
    """Persist `token` to disk at mode 0600 atomically.

    Critical: the file must be created with mode 0600 from the start.
    A naive `write_text` + `chmod(0o600)` opens a window where the
    file exists at the inherited-from-umask mode (typically 0644)
    before the chmod fires, defeating the "only this user can read
    the token" guarantee. We:

      1. Write to a temp file in the same directory, created with
         `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)` so the FD is
         born with restrictive perms and umask is bypassed.
      2. fsync the FD.
      3. Atomic rename over the destination — POSIX guarantees the
         rename is atomic on the same filesystem.

    The temp file lives in the same directory so the rename is on the
    same filesystem (cross-fs renames degenerate to copy+unlink, which
    re-introduces a permission window).
    """
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".auth-token.", dir=str(p.parent),
        )
        # mkstemp returns 0600 on POSIX. Make that explicit anyway in
        # case the implementation ever loosens — we don't want a
        # silent regression here.
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        try:
            os.write(fd, token.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, p)


def regenerate_token() -> str:
    """Mint and persist a new token, returning it.

    `relaydeck auth rotate` exposes this to operators. Any running clients
    (CLI, dashboards) with the old token will start getting 401s and
    must re-read the file.
    """
    new = secrets.token_hex(_TOKEN_BYTES)
    write_token(new)
    return new


def verify_token(presented: str | None) -> bool:
    """Constant-time compare `presented` against the on-disk token.

    Returns False for missing tokens, missing files, and mismatched
    tokens. Used by the FastAPI auth middleware.
    """
    if not presented:
        return False
    expected = read_token()
    if not expected:
        # No token configured → fail closed. `relaydeck serve` mints on boot
        # so this state should only happen in tests that haven't set
        # RELAYDECK_AUTH_TOKEN.
        return False
    return hmac.compare_digest(presented, expected)
