"""
TLS helpers for the daemon.

Production: operator points `relaydeck serve` at a real cert + key
(`--tls-cert /etc/letsencrypt/.../fullchain.pem`,
`--tls-key /etc/letsencrypt/.../privkey.pem`). We don't manage the
cert lifecycle for them; that's an OS-level concern.

Dev: `relaydeck serve --tls-self-signed` generates a localhost cert into
`~/.relaydeck/runtime/tls/` (mode 0600 on the key) and prints the
SHA-256 fingerprint so the operator can verify the dashboard URL.
The cert is valid for `127.0.0.1` and `localhost`; if the operator
binds to a LAN address they need to provide their own cert.

`RemoteHost` accepts a `verify=` arg (bool or path). For the
self-signed dev path we write the cert path into `state.yaml` so
sibling CLIs can pick it up — they verify against that file
specifically (not the system trust store, which doesn't know
about the dev cert).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def tls_dir(config_home: Path) -> Path:
    return config_home / "runtime" / "tls"


def self_signed_paths(config_home: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path) for the dev self-signed pair."""
    base = tls_dir(config_home)
    return base / "cert.pem", base / "key.pem"


def fingerprint(cert_path: Path) -> str:
    """SHA-256 fingerprint of the certificate (DER form), formatted as
    colon-separated uppercase hex like openssl prints."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def ensure_self_signed(
    config_home: Path,
    *,
    hostnames: list[str] | None = None,
    valid_days: int = 365,
    force: bool = False,
) -> tuple[Path, Path]:
    """Generate (or reuse) a self-signed cert + key under
    `<config_home>/runtime/tls/`. Returns (cert_path, key_path).

    The cert is RSA-3072 valid for `hostnames` (default
    `["localhost", "127.0.0.1"]`) and `valid_days` days from now.
    Existing files are reused unless `force=True` or one of them is
    missing. The key file is written mode 0600 atomically via
    tempfile.mkstemp + os.replace (no umask race).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path, key_path = self_signed_paths(config_home)
    base = tls_dir(config_home)
    base.mkdir(parents=True, exist_ok=True)

    if not force and cert_path.exists() and key_path.exists():
        return cert_path, key_path

    hostnames = hostnames or ["localhost", "127.0.0.1"]

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "relaydeck-daemon"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "relaydeck self-signed"),
    ])

    san_entries: list[x509.GeneralName] = []
    import ipaddress
    for h in hostnames:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san_entries.append(x509.DNSName(h))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    _atomic_write_0600(cert_path, cert_pem)
    _atomic_write_0600(key_path, key_pem)
    return cert_path, key_path


def _atomic_write_0600(path: Path, data: bytes) -> None:
    """Same pattern as vault/auth: tempfile.mkstemp inherits 0600,
    fsync, then os.replace for atomic visibility — no umask race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
