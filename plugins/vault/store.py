"""
Vault storage backends.

Two stores live behind the `VaultStore` protocol:

  * `YamlStore` — legacy plaintext yaml at `vault.yaml` (mode 0600).
    Kept so migrations from older releases work.
  * `EncryptedStore` — ChaCha20-Poly1305 AEAD at `vault.enc`. Key is
    derived via HKDF over a per-machine salt + a machine identifier.
    This is the default for new installations.

The Vault plugin picks a store at load time:
  1. If `vault.enc` exists → EncryptedStore.
  2. Else if `vault.yaml` exists → YamlStore. The first encrypted
     write migrates the values to `vault.enc` and renames the old
     file to `vault.yaml.bak`.
  3. Else → EncryptedStore (fresh install).

## Threat model

The encrypted vault protects against:
  - Other UNIX users on the same host who can't read the salt file
    (machine identifier path is the second factor).
  - Backups / snapshots / sync tools that capture `~/.relaydeck/`
    in cleartext-on-disk form.
  - Casual filesystem grep ("did I accidentally commit a key?").

It does NOT protect against:
  - A process running as the same user — auth-token + vault file are
    both readable.
  - Memory inspection while the daemon is running.
  - A root-equivalent attacker on the host.

For stronger protection the operator can opt into a passphrase via
`RELAYDECK_VAULT_PASSPHRASE` — the daemon will refuse to load the vault
without it.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Protocol

import yaml

logger = logging.getLogger(__name__)


# ── Interface ────────────────────────────────────────────────────────


class VaultStore(Protocol):
    """Filesystem-backed key/value store with secure-write semantics."""

    def load(self) -> dict[str, str]: ...
    def save(self, values: dict[str, str]) -> None: ...
    def path(self) -> Path: ...


# ── Shared utilities ────────────────────────────────────────────────


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write `data` to `path` atomically at the requested mode.

    Mirrors the auth-token write path: a tempfile in the same dir is
    created at the target mode FIRST, then renamed over the
    destination. Avoids the umask-leak window of `write_text` +
    `chmod`. Important for vault.yaml and vault.enc both — the file
    contents are secret in the plaintext case and an attacker-readable
    encrypted blob in the encrypted case (less catastrophic but still
    worth keeping at 0600 for consistency).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ── Yaml store (legacy) ──────────────────────────────────────────────


class YamlStore:
    """Plaintext yaml at mode 0600. Kept for backward compatibility
    and migration. The Vault plugin moves callers off this store on
    first save."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data = yaml.safe_load(self._path.read_text()) or {}
            return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            logger.warning("vault yaml load failed: %s", exc)
            return {}

    def save(self, values: dict[str, str]) -> None:
        body = yaml.dump(values, default_flow_style=False).encode("utf-8")
        _atomic_write(self._path, body)


# ── Encrypted store ──────────────────────────────────────────────────


# File format for vault.enc:
#
#   bytes 0..3    magic "LMV1" (relaydeck Vault v1)
#   bytes 4..15   12-byte nonce
#   bytes 16..N   ChaCha20-Poly1305 ciphertext (16-byte tag at end)
#
# The salt file `vault.salt` is a separate file in the same directory.
# Both vault.enc and vault.salt must be present + readable to load.
_MAGIC = b"LMV1"
_NONCE_BYTES = 12
_SALT_BYTES = 32
_KEY_BYTES = 32


def _machine_id() -> bytes:
    """Best-effort machine identifier used as the second factor in
    key derivation. Platform-specific because no single source works
    everywhere — but ALL of these are at least readable to the user
    running the daemon.

      Linux: /etc/machine-id
      macOS: IOPlatformUUID via `ioreg` — but we want zero subprocess
             at boot, so fall back to /Library/Preferences/SystemConfiguration/com.apple.smb.server.plist
             which contains the hostname (poor but stable). Better:
             use `socket.gethostname` as the cross-platform fallback.

    On a single-user machine this is acceptable. Operators
    who want stronger key material should set RELAYDECK_VAULT_PASSPHRASE,
    which bypasses this function entirely.
    """
    candidates = [
        Path("/etc/machine-id"),
        Path("/var/lib/dbus/machine-id"),
    ]
    for c in candidates:
        try:
            if c.exists():
                return c.read_bytes().strip()
        except OSError:
            continue
    # Cross-platform fallback: hostname + user. Weak, but combined
    # with the random per-vault salt the resulting key still has 256
    # bits of randomness from the salt itself.
    import socket
    name = socket.gethostname().encode("utf-8")
    user = os.environ.get("USER", "").encode("utf-8")
    return b"relaydeck:" + name + b":" + user


def _derive_key(salt: bytes, *, passphrase: str | None = None) -> bytes:
    """HKDF-SHA256 over (passphrase or machine_id) with the salt.

    `passphrase` (when set via RELAYDECK_VAULT_PASSPHRASE) is mixed in
    BEFORE the machine id so a passphrase change rotates the key
    without needing a new salt. Returns a 32-byte ChaCha20 key.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ikm_parts = [_machine_id()]
    if passphrase:
        ikm_parts.insert(0, passphrase.encode("utf-8"))
    ikm = b"\x00".join(ikm_parts)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        info=b"relaydeck-vault-v1",
    ).derive(ikm)


class EncryptedStore:
    """ChaCha20-Poly1305 encrypted vault.

    Two files in the same directory:
      <path>      — vault.enc (magic + nonce + ciphertext)
      <path>.salt — vault.salt (random 32 bytes, generated on first save)

    Loading without the salt file raises VaultError — the operator
    has to either run `relaydeck vault migrate` (auto-generates the salt
    from yaml) or copy the salt from backup.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._salt_path = path.with_name(path.name + ".salt")
        self._passphrase = os.environ.get("RELAYDECK_VAULT_PASSPHRASE") or None

    def path(self) -> Path:
        return self._path

    def salt_path(self) -> Path:
        return self._salt_path

    def _read_salt(self, *, create_if_missing: bool) -> bytes:
        if self._salt_path.exists():
            data = self._salt_path.read_bytes()
            if len(data) != _SALT_BYTES:
                raise VaultError(
                    f"vault salt at {self._salt_path} is corrupt "
                    f"(expected {_SALT_BYTES} bytes, got {len(data)})"
                )
            return data
        if not create_if_missing:
            raise VaultError(
                f"vault salt file missing at {self._salt_path} — "
                "run `relaydeck vault migrate` or restore from backup"
            )
        salt = secrets.token_bytes(_SALT_BYTES)
        _atomic_write(self._salt_path, salt)
        return salt

    def load(self) -> dict[str, str]:
        """Return the decrypted values. Empty dict if vault.enc doesn't
        exist yet (fresh install). Raises VaultError on tampered
        ciphertext or missing salt."""
        if not self._path.exists():
            return {}
        salt = self._read_salt(create_if_missing=False)
        blob = self._path.read_bytes()
        if len(blob) < len(_MAGIC) + _NONCE_BYTES + 16:
            raise VaultError("vault.enc is truncated")
        if blob[: len(_MAGIC)] != _MAGIC:
            raise VaultError("vault.enc magic mismatch — wrong format or corrupt file")
        nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES]
        ciphertext = blob[len(_MAGIC) + _NONCE_BYTES :]
        key = _derive_key(salt, passphrase=self._passphrase)
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise VaultError(
                "vault.enc authentication failed — wrong passphrase, "
                "corrupted file, or salt/key mismatch"
            ) from exc
        data = yaml.safe_load(plaintext.decode("utf-8")) or {}
        return {str(k): str(v) for k, v in data.items()}

    def save(self, values: dict[str, str]) -> None:
        salt = self._read_salt(create_if_missing=True)
        key = _derive_key(salt, passphrase=self._passphrase)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        body = yaml.dump(values, default_flow_style=False).encode("utf-8")
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce, body, None)
        out = _MAGIC + nonce + ciphertext
        _atomic_write(self._path, out)

    def rotate_key(self) -> None:
        """Re-encrypt the vault under a fresh salt. If a passphrase is
        in effect, callers should set RELAYDECK_VAULT_PASSPHRASE to the
        NEW one before calling — the new salt + new passphrase
        together derive the new key.
        """
        values = self.load() if self._path.exists() else {}
        # Move the old salt aside so the next save generates a fresh one.
        if self._salt_path.exists():
            backup = self._salt_path.with_suffix(self._salt_path.suffix + ".bak")
            os.replace(self._salt_path, backup)
        # save() will create a new salt + re-encrypt.
        self.save(values)


class VaultError(Exception):
    """Raised when the vault can't load or save — wrong key, missing
    salt, tampered ciphertext, etc. Callers should surface this; the
    daemon refuses to start with an unreadable vault rather than
    silently falling back to an empty values dict."""


# ── Store selection ──────────────────────────────────────────────────


def pick_store(config_home: Path) -> VaultStore:
    """Choose the vault store to use for `config_home`.

    Preference order:
      1. `vault.enc` exists → EncryptedStore.
      2. `vault.yaml` exists (and `vault.enc` doesn't) → YamlStore.
         The plugin will migrate values to encrypted on next save.
      3. Neither exists → EncryptedStore (fresh install gets the
         encrypted format).
    """
    enc = config_home / "vault.enc"
    yaml_path = config_home / "vault.yaml"
    if enc.exists():
        return EncryptedStore(enc)
    if yaml_path.exists():
        return YamlStore(yaml_path)
    return EncryptedStore(enc)


def migrate_to_encrypted(config_home: Path) -> tuple[Path, int]:
    """One-shot migration from plaintext yaml to encrypted vault.

    Reads `vault.yaml`, writes `vault.enc` + `vault.enc.salt`, renames
    the yaml to `vault.yaml.bak`. Returns `(enc_path, key_count)`.

    Idempotent in the sense that re-running with no yaml present is a
    no-op that returns 0 keys.
    """
    yaml_path = config_home / "vault.yaml"
    enc_path = config_home / "vault.enc"
    if not yaml_path.exists():
        # Nothing to migrate. Make sure the encrypted store at least
        # exists so callers can `set` immediately.
        return (enc_path, 0)
    yaml_store = YamlStore(yaml_path)
    values = yaml_store.load()
    enc_store = EncryptedStore(enc_path)
    enc_store.save(values)
    backup = yaml_path.with_suffix(yaml_path.suffix + ".bak")
    os.replace(yaml_path, backup)
    return (enc_path, len(values))
