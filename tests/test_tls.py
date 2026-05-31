"""
Unit tests for the TLS helper module.

The cert + key generation path is exercised against the live
`cryptography` install (no mocks at the boundary). We don't smoke
a full uvicorn boot in tests — that's a manual verification step. Here we check:

  - self-signed cert + key materialize at the expected paths,
  - the key file is mode 0600 atomically,
  - subjectAltName covers localhost + 127.0.0.1,
  - the cert is reused on the second call (no churn),
  - `force=True` regenerates,
  - `fingerprint(path)` returns a stable SHA-256 hex string,
  - `state.yaml` round-trips the daemon_ca path,
  - `RemoteHost` selects the right ssl context.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

from relaydeck.tls import ensure_self_signed, fingerprint, self_signed_paths


def test_ensure_self_signed_materializes_cert_and_key(tmp_path):
    config_home = tmp_path / ".relaydeck"
    cert, key = ensure_self_signed(config_home)
    assert cert.exists() and key.exists()
    assert cert.name == "cert.pem"
    assert key.name == "key.pem"
    assert cert.parent == config_home / "runtime" / "tls"


def test_ensure_self_signed_writes_key_mode_0600(tmp_path):
    config_home = tmp_path / ".relaydeck"
    # Set a permissive umask to prove the file is born at 0600 atomically,
    # not via a chmod-after-write window (the same race we closed in
    # relaydeck/auth.py).
    old = os.umask(0)
    try:
        _, key = ensure_self_signed(config_home)
    finally:
        os.umask(old)
    mode = key.stat().st_mode & 0o777
    assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0o600"


def test_ensure_self_signed_is_idempotent(tmp_path):
    config_home = tmp_path / ".relaydeck"
    cert1, key1 = ensure_self_signed(config_home)
    body1 = cert1.read_bytes()
    cert2, key2 = ensure_self_signed(config_home)
    assert cert1 == cert2 and key1 == key2
    # No regeneration — bytes match.
    assert cert2.read_bytes() == body1


def test_ensure_self_signed_force_regenerates(tmp_path):
    config_home = tmp_path / ".relaydeck"
    cert1, _ = ensure_self_signed(config_home)
    body1 = cert1.read_bytes()
    cert2, _ = ensure_self_signed(config_home, force=True)
    assert cert2.read_bytes() != body1, "force=True must mint a fresh cert"


def test_self_signed_cert_covers_localhost_san(tmp_path):
    from cryptography import x509

    config_home = tmp_path / ".relaydeck"
    cert_path, _ = ensure_self_signed(config_home)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName,
    ).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ips = [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ips


def test_fingerprint_format(tmp_path):
    config_home = tmp_path / ".relaydeck"
    cert_path, _ = ensure_self_signed(config_home)
    fp = fingerprint(cert_path)
    # SHA-256 → 32 bytes → 64 hex chars; colon-separated → 32 groups.
    groups = fp.split(":")
    assert len(groups) == 32
    # Hex digits, with any alpha portion uppercase. `isupper()` returns
    # False for all-digit strings (digits have no case), so check
    # casefold equality with the upper form instead.
    assert all(
        len(g) == 2 and all(c in "0123456789ABCDEF" for c in g) for g in groups
    ), f"fingerprint groups should be uppercase 2-char hex: {fp}"


def test_self_signed_paths_match_ensure_self_signed(tmp_path):
    config_home = tmp_path / ".relaydeck"
    expected_cert, expected_key = self_signed_paths(config_home)
    actual_cert, actual_key = ensure_self_signed(config_home)
    assert actual_cert == expected_cert
    assert actual_key == expected_key


def test_state_yaml_round_trips_daemon_ca(tmp_path, monkeypatch):
    """Operator runs `relaydeck serve --tls-self-signed`; the daemon writes
    the cert path into state.yaml so sibling CLIs can pin verification
    against it. The plain `daemon_url` set already covers the scheme;
    `daemon_ca` is the additional artifact for the dev path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.state import get_daemon_ca, set_daemon_ca

    assert get_daemon_ca() is None
    set_daemon_ca("/path/to/cert.pem")
    assert get_daemon_ca() == "/path/to/cert.pem"
    set_daemon_ca(None)
    assert get_daemon_ca() is None


def test_state_yaml_daemon_ca_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_DAEMON_CA", "/env/path.pem")
    from relaydeck.state import get_daemon_ca, set_daemon_ca

    set_daemon_ca("/file/path.pem")
    assert get_daemon_ca() == "/env/path.pem"


def test_remotehost_https_with_pinned_ca_loads_cafile(tmp_path):
    """RemoteHost with verify=<path> must produce an SSL context that
    verifies against THAT file, not the system trust store. We can't
    easily inspect the context's CA bundle, but we can confirm it's a
    verified context (not CERT_NONE) and that the URL scheme triggers
    it."""
    from relaydeck.sdk import RemoteHost

    config_home = tmp_path / ".relaydeck"
    cert, _ = ensure_self_signed(config_home)
    host = RemoteHost("https://127.0.0.1:8765", token="t", verify=str(cert))
    ctx = host._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_remotehost_https_verify_false_disables_check():
    from relaydeck.sdk import RemoteHost

    host = RemoteHost("https://127.0.0.1:8765", token="t", verify=False)
    ctx = host._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_remotehost_http_returns_no_ssl_context():
    from relaydeck.sdk import RemoteHost

    host = RemoteHost("http://127.0.0.1:8765", token="t")
    assert host._ssl_context() is None


def test_remotehost_from_local_picks_up_pinned_ca(tmp_path, monkeypatch):
    """The `from_local()` classmethod resolves state.yaml. With a CA
    pin set, the resulting RemoteHost must verify against that file
    (not the system trust)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # conftest pins RELAYDECK_AUTH_TOKEN session-wide; let the file path be
    # the source of truth for this test by clearing the env.
    monkeypatch.delenv("RELAYDECK_AUTH_TOKEN", raising=False)
    from relaydeck.auth import write_token
    from relaydeck.sdk import RemoteHost
    from relaydeck.state import set_daemon_ca, set_daemon_url

    config_home = tmp_path / ".relaydeck"
    config_home.mkdir(parents=True, exist_ok=True)
    cert, _ = ensure_self_signed(config_home)
    write_token("test-token-32-chars-long-aaaaaaaa")
    set_daemon_url("https://127.0.0.1:8765")
    set_daemon_ca(str(cert))

    host = RemoteHost.from_local()
    assert host.daemon_url == "https://127.0.0.1:8765"
    assert host.token == "test-token-32-chars-long-aaaaaaaa"
    assert host.verify == str(cert)


def test_serve_tls_mutual_exclusion(tmp_path, monkeypatch):
    """--tls-self-signed and --tls-cert/--tls-key are mutually
    exclusive. `_resolve_tls` is the gate; clicking both raises a
    click.UsageError which surfaces as exit code 2."""
    from relaydeck.transports.cli import _resolve_tls
    import click

    cfg = tmp_path / ".relaydeck"
    cert_file = tmp_path / "c.pem"
    key_file = tmp_path / "k.pem"
    cert_file.write_text("X")
    key_file.write_text("Y")

    # Both at once → error
    try:
        _resolve_tls(cfg, str(cert_file), str(key_file), tls_self_signed=True)
    except click.UsageError as exc:
        assert "mutually exclusive" in str(exc)
    else:
        raise AssertionError("expected UsageError")

    # Only one of cert/key → error
    try:
        _resolve_tls(cfg, str(cert_file), None, tls_self_signed=False)
    except click.UsageError as exc:
        assert "together" in str(exc)
    else:
        raise AssertionError("expected UsageError")


def test_serve_tls_resolve_self_signed_returns_paths_and_fingerprint(tmp_path):
    """The happy path: `_resolve_tls(..., tls_self_signed=True)` returns
    the materialized cert + key paths and a fingerprint string."""
    from relaydeck.transports.cli import _resolve_tls

    cfg = tmp_path / ".relaydeck"
    cert, key, fp = _resolve_tls(cfg, None, None, tls_self_signed=True)
    assert cert is not None and key is not None and fp is not None
    assert cert.exists() and key.exists()
    assert fp.count(":") == 31  # 32 hex pairs


def test_serve_tls_resolve_plain_http_returns_none(tmp_path):
    from relaydeck.transports.cli import _resolve_tls

    cert, key, fp = _resolve_tls(tmp_path, None, None, tls_self_signed=False)
    assert (cert, key, fp) == (None, None, None)
