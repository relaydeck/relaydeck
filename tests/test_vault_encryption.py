"""
Tests for vault encryption at rest.

  - EncryptedStore round-trips values through ChaCha20-Poly1305.
  - On-disk vault.enc contents are unintelligible without the key.
  - Tampered ciphertext raises VaultError (not "returns empty dict").
  - Missing salt → explicit error, not silent empty load.
  - rotate_key preserves values under a fresh salt.
  - migrate_to_encrypted moves yaml → enc + leaves .bak.
  - pick_store selects the right backend based on what exists.
  - RELAYDECK_VAULT_PASSPHRASE participates in key derivation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.vault.store import (
    EncryptedStore,
    VaultError,
    YamlStore,
    migrate_to_encrypted,
    pick_store,
)


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Each test gets a clean config_home with no passphrase set."""
    monkeypatch.delenv("RELAYDECK_VAULT_PASSPHRASE", raising=False)
    home = tmp_path / "cfg"
    home.mkdir()
    return home


# ── Round-trip ──────────────────────────────────────────────────────


def test_encrypted_round_trip(cfg_home):
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"OPENAI_KEY": "sk-test", "ANTHROPIC_KEY": "sk-ant"})
    fresh = EncryptedStore(cfg_home / "vault.enc")
    values = fresh.load()
    assert values["OPENAI_KEY"] == "sk-test"
    assert values["ANTHROPIC_KEY"] == "sk-ant"


def test_load_returns_empty_for_fresh_install(cfg_home):
    """No vault.enc yet → empty dict, no error. Critical: this is the
    fresh-install path and must not require a passphrase."""
    store = EncryptedStore(cfg_home / "vault.enc")
    assert store.load() == {}


def test_save_creates_files_at_mode_0600(cfg_home):
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"K": "V"})
    enc_mode = (cfg_home / "vault.enc").stat().st_mode & 0o777
    salt_mode = (cfg_home / "vault.enc.salt").stat().st_mode & 0o777
    assert enc_mode == 0o600, f"vault.enc mode {oct(enc_mode)}"
    assert salt_mode == 0o600, f"vault.enc.salt mode {oct(salt_mode)}"


# ── Confidentiality ─────────────────────────────────────────────────


def test_on_disk_contents_do_not_contain_plaintext(cfg_home):
    """The cipher text must not contain the values verbatim — this is
    the entire point of the encryption pass."""
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({
        "SECRET": "highly-specific-value-12345",
        "TOKEN": "another-distinctive-string",
    })
    raw = (cfg_home / "vault.enc").read_bytes()
    assert b"highly-specific-value-12345" not in raw
    assert b"another-distinctive-string" not in raw
    # Magic + structure is fine to leak.
    assert raw.startswith(b"LMV1")


def test_salt_is_random_per_install(cfg_home, tmp_path):
    """Two fresh installs in different config_homes must produce
    different salts. Same-host doesn't mean same key."""
    other = tmp_path / "other"
    other.mkdir()
    s1 = EncryptedStore(cfg_home / "vault.enc")
    s2 = EncryptedStore(other / "vault.enc")
    s1.save({"K": "V"})
    s2.save({"K": "V"})
    salt1 = (cfg_home / "vault.enc.salt").read_bytes()
    salt2 = (other / "vault.enc.salt").read_bytes()
    assert salt1 != salt2


# ── Tampering / corruption ──────────────────────────────────────────


def test_tampered_ciphertext_raises(cfg_home):
    """Flipping one byte in the ciphertext must surface a clear
    authentication failure — never silently return an empty dict or
    bogus values."""
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"K": "V"})

    path = cfg_home / "vault.enc"
    raw = bytearray(path.read_bytes())
    # Flip a byte well past the magic + nonce header.
    raw[-5] ^= 0xFF
    path.write_bytes(bytes(raw))

    fresh = EncryptedStore(path)
    with pytest.raises(VaultError, match="authentication failed"):
        fresh.load()


def test_missing_salt_raises_clear_error(cfg_home):
    """If the salt file is missing the operator gets a clear error,
    not a silent fall-through. Recovery path is documented in the
    error message."""
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"K": "V"})
    (cfg_home / "vault.enc.salt").unlink()

    fresh = EncryptedStore(cfg_home / "vault.enc")
    with pytest.raises(VaultError, match="salt file missing"):
        fresh.load()


def test_corrupt_salt_raises(cfg_home):
    """A salt file of wrong size means the install is broken — surface,
    don't try to derive a key from partial input."""
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"K": "V"})
    (cfg_home / "vault.enc.salt").write_bytes(b"too-short")

    fresh = EncryptedStore(cfg_home / "vault.enc")
    with pytest.raises(VaultError, match="corrupt"):
        fresh.load()


def test_truncated_blob_raises(cfg_home):
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"K": "V"})
    path = cfg_home / "vault.enc"
    path.write_bytes(path.read_bytes()[:8])  # too small for magic+nonce+tag

    fresh = EncryptedStore(path)
    with pytest.raises(VaultError, match="truncated"):
        fresh.load()


# ── Key rotation ────────────────────────────────────────────────────


def test_rotate_key_preserves_values(cfg_home):
    """rotate_key generates a fresh salt + re-encrypts. The old
    ciphertext is unreadable after rotation; the new one decrypts to
    the same values."""
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"OPENAI_KEY": "rotated-value", "OTHER": "x"})
    old_salt = (cfg_home / "vault.enc.salt").read_bytes()

    store.rotate_key()
    new_salt = (cfg_home / "vault.enc.salt").read_bytes()
    assert new_salt != old_salt

    fresh = EncryptedStore(cfg_home / "vault.enc")
    values = fresh.load()
    assert values == {"OPENAI_KEY": "rotated-value", "OTHER": "x"}
    # Old salt sits next to it as a .bak — operator can roll back if
    # needed before deleting.
    assert (cfg_home / "vault.enc.salt.bak").exists()


# ── Migration ───────────────────────────────────────────────────────


def test_migrate_from_yaml_creates_enc_and_bak(cfg_home):
    yaml_store = YamlStore(cfg_home / "vault.yaml")
    yaml_store.save({"KEY1": "v1", "KEY2": "v2"})

    enc_path, count = migrate_to_encrypted(cfg_home)
    assert count == 2
    assert enc_path == cfg_home / "vault.enc"
    assert enc_path.exists()
    assert (cfg_home / "vault.yaml.bak").exists()
    assert not (cfg_home / "vault.yaml").exists()

    # Reading via EncryptedStore returns the migrated values.
    enc = EncryptedStore(enc_path).load()
    assert enc == {"KEY1": "v1", "KEY2": "v2"}


def test_migrate_with_no_yaml_is_noop(cfg_home):
    enc_path, count = migrate_to_encrypted(cfg_home)
    assert count == 0
    # No new file created.
    assert not enc_path.exists()


# ── Store picker ────────────────────────────────────────────────────


def test_pick_store_prefers_enc_when_both_exist(cfg_home):
    """If somehow both files exist, the encrypted one wins — never
    silently fall back to plaintext."""
    YamlStore(cfg_home / "vault.yaml").save({"K": "yaml"})
    EncryptedStore(cfg_home / "vault.enc").save({"K": "enc"})

    store = pick_store(cfg_home)
    assert isinstance(store, EncryptedStore)


def test_pick_store_uses_yaml_when_only_yaml_present(cfg_home):
    YamlStore(cfg_home / "vault.yaml").save({"K": "V"})
    store = pick_store(cfg_home)
    assert isinstance(store, YamlStore)


def test_pick_store_defaults_to_encrypted_for_fresh_install(cfg_home):
    store = pick_store(cfg_home)
    assert isinstance(store, EncryptedStore)


# ── Passphrase ──────────────────────────────────────────────────────


def test_passphrase_required_to_decrypt_after_set(cfg_home, monkeypatch):
    """A vault written with RELAYDECK_VAULT_PASSPHRASE='foo' must NOT
    decrypt without it (or with the wrong one)."""
    monkeypatch.setenv("RELAYDECK_VAULT_PASSPHRASE", "secret-passphrase")
    store = EncryptedStore(cfg_home / "vault.enc")
    store.save({"K": "V"})

    monkeypatch.delenv("RELAYDECK_VAULT_PASSPHRASE")
    fresh = EncryptedStore(cfg_home / "vault.enc")
    with pytest.raises(VaultError, match="authentication failed"):
        fresh.load()

    monkeypatch.setenv("RELAYDECK_VAULT_PASSPHRASE", "wrong-passphrase")
    fresh2 = EncryptedStore(cfg_home / "vault.enc")
    with pytest.raises(VaultError, match="authentication failed"):
        fresh2.load()

    # Correct passphrase recovers.
    monkeypatch.setenv("RELAYDECK_VAULT_PASSPHRASE", "secret-passphrase")
    fresh3 = EncryptedStore(cfg_home / "vault.enc")
    assert fresh3.load() == {"K": "V"}


# ── Vault wrapper integration ────────────────────────────────────────


def test_vault_class_uses_picked_store(cfg_home):
    from plugins.vault.plugin import Vault
    store = pick_store(cfg_home)
    v = Vault(store)
    v.set("OPENAI_KEY", "sk-real")
    # Bytes on disk are encrypted.
    raw = (cfg_home / "vault.enc").read_bytes()
    assert b"sk-real" not in raw
    # In-memory access works.
    assert v.get("OPENAI_KEY") == "sk-real"
    # Resolve still substitutes.
    assert v.resolve("${vault:OPENAI_KEY}") == "sk-real"


def test_vault_resolve_unknown_key_leaves_placeholder(cfg_home):
    from plugins.vault.plugin import Vault
    v = Vault(pick_store(cfg_home))
    assert v.resolve("${vault:MISSING}") == "${vault:MISSING}"
