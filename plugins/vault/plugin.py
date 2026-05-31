"""
Vault plugin — encrypted secrets store.

Two on-disk formats are supported:

  * `vault.enc` (default, new installs) — ChaCha20-Poly1305 AEAD with
    HKDF-derived key. See `plugins/vault/store.py` for the
    threat model and file format.
  * `vault.yaml` (legacy) — plaintext yaml at mode 0600. Migrating
    away from this is the operator's choice via `relaydeck vault migrate`;
    the daemon never silently re-writes the format.

The plugin picks the right store at load via `store.pick_store()` —
existing yaml installs keep working unchanged, new installs get
encrypted, and migration is explicit. Vault values are resolved at
agent start via ${vault:KEY} substitution and never round-trip to
disk in plaintext. The API returns key names only.

The routes live under `/api/vault/...` (`top_level_api = true`) because the
settings overlay and the plugin's own panel fetch them by absolute path —
shifting to `/api/plugins/vault/...` would break the dashboard. Declared
capabilities: api.register, cli.register, ui.register. `vault.read` is
*provided* to other plugins via `host.vault.get(...)`, gated on the
consumer's manifest rather than here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from relaydeck.plugin import PluginContext, PluginEventBus
from relaydeck.sdk import Plugin, PluginHost

from .store import (
    EncryptedStore,
    VaultError,
    VaultStore,
    migrate_to_encrypted,
    pick_store,
)

PLUGIN_NAME = "vault"
logger = logging.getLogger(__name__)


class Vault:
    """In-memory cache backed by a VaultStore on disk.

    Values are loaded once at startup and held in memory. The
    /api/vault/keys endpoint returns names only — values never leave
    the daemon process.
    """

    def __init__(self, store: VaultStore):
        self._store = store
        self._values: dict[str, str] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._store.path()

    @property
    def store(self) -> VaultStore:
        return self._store

    def _load(self) -> None:
        try:
            self._values = self._store.load()
        except VaultError:
            # Re-raise so daemon boot surfaces the problem instead of
            # silently presenting an empty vault — that path would
            # break agents that rely on ${vault:KEY} substitutions.
            raise
        except Exception:
            self._values = {}

    def _save(self) -> None:
        self._store.save(self._values)

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        if key in self._values:
            del self._values[key]
            self._save()
            return True
        return False

    def keys(self) -> list[str]:
        return sorted(self._values.keys())

    def resolve(self, text: str) -> str:
        """Replace ${vault:KEY} placeholders with values."""
        import re

        def _replace(match):
            key = match.group(1)
            return self._values.get(key, match.group(0))

        return re.sub(r'\$\{vault:([^}]+)\}', _replace, text)


# ── Module-level singleton + helpers (legacy API) ───────────────────


_vault: Vault | None = None


def get_vault() -> Vault | None:
    return _vault


def get_secret(key: str) -> str | None:
    if _vault is None:
        return None
    return _vault.get(key)


def _sync_pi_auth_after_vault_change(key: str) -> None:
    """Push provider-key changes into relaydeck-native's pi auth.json store.

    Emits `relaydeck-native.credentials_rotated` with the list of running
    agents whose PTY child has stale env baked in — the dashboard surfaces
    a "Restart these agents" toast so the operator knows their rotation
    won't reach the live pi process until restart (auth.json + HTTP chat
    pick it up automatically; only the PTY env is sticky)."""
    if _vault is None:
        return
    try:
        from plugins.harnesses.relaydeck_native.pi_engine import (
            _VAULT_KEY_TO_PI,
            sync_native_pi_auth,
        )
        if key not in _VAULT_KEY_TO_PI:
            return
        sync_native_pi_auth(_vault.path.parent)
        try:
            from relaydeck.orchestrator import get_orchestrator
            orch = get_orchestrator(_vault.path.parent)
            stale = orch.invalidate_relaydeck_native_spawn_caches()
            if stale:
                try:
                    bus = getattr(orch, "_plugin_event_bus", None)
                    if bus is not None:
                        from relaydeck.plugin import Event
                        bus.emit(Event(
                            type="relaydeck-native.credentials_rotated",
                            data={"key": key, "stale_agent_ids": stale},
                        ))
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        logger.debug("vault: pi auth sync skipped", exc_info=True)


def set_secret(key: str, value: str) -> None:
    """Write a secret to the daemon vault.

    The companion to `get_secret`, used by other plugins to persist tokens
    (e.g. the telegram setup endpoint storing `TELEGRAM_BOT_TOKEN`). In the
    daemon `_vault` is already initialized by `VaultPlugin.on_load`; if a
    caller reaches this before that (or in a vault-less context) we lazily
    initialize it from the default config home so the write still lands on
    disk rather than raising `ImportError`/`NoneType`."""
    global _vault
    if _vault is None:
        from pathlib import Path
        _vault = Vault(pick_store(Path.home() / ".relaydeck"))
    _vault.set(key, value)
    _sync_pi_auth_after_vault_change(key)


def delete_secret(key: str) -> bool:
    """Remove a secret from the daemon vault. Companion to `set_secret`;
    returns True if the key existed. Lazily initializes the vault like
    `set_secret` so a caller in a vault-less context still operates on disk."""
    global _vault
    if _vault is None:
        from pathlib import Path
        _vault = Vault(pick_store(Path.home() / ".relaydeck"))
    deleted = _vault.delete(key)
    if deleted:
        _sync_pi_auth_after_vault_change(key)
    return deleted


def _route_through_daemon(
    method: str, key: str, value: str | None = None,
) -> tuple[bool, str | None]:
    """POST/DELETE vault changes through the running daemon if reachable.

    Returns (handled, error). When handled=True the daemon owns the write
    (in-memory cache stays in sync; the CLI exits clean). When handled=False
    the daemon is unreachable and the caller falls back to a local-disk
    write — operator still gets correctness, but the daemon will see the
    new value only on next restart.

    Without this, `relaydeck vault set` from CLI writes vault.yaml + the
    pi auth.json on disk, but the daemon's `_vault._values` in-memory cache
    stays stale. On the daemon's next relaydeck-native spawn, `_sync_native_pi_auth`
    reads stale credentials and overwrites the CLI's correct auth.json write."""
    import json
    import urllib.error
    import urllib.request

    from relaydeck.auth import read_token
    from relaydeck.state import get_daemon_ca, get_daemon_url

    try:
        base = get_daemon_url().rstrip("/")
    except Exception:
        return False, "daemon url unset"
    url = f"{base}/api/vault/keys/{key}"
    headers = {"Content-Type": "application/json"}
    tok = read_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    ctx = None
    if base.startswith("https://"):
        import ssl
        ca = get_daemon_ca()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    data = json.dumps({"value": value}).encode() if value is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            r.read()
        return True, None
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ── SDK Plugin ──────────────────────────────────────────────────────


class VaultPlugin(Plugin):
    """Secrets vault — daemon-local key-value store with optional
    ChaCha20-Poly1305 encryption at rest."""

    def on_load(self, host: PluginHost) -> None:
        global _vault
        self.host = host
        self._config_home = host.config_home
        store = pick_store(host.config_home)
        _vault = Vault(store)
        # Register as core's secret backend so relaydeck.vault delegates here
        # instead of importing this plugin's module path directly.
        from relaydeck.vault import register_secret_backend
        register_secret_backend(self)
        self._register_routes()
        self._register_cli()

    def on_unload(self) -> None:
        # Drop the in-memory store but leave the backend registered: the
        # backend delegates to module state, so with _vault None it correctly
        # reads empty (and lazily re-inits on write). Clearing the registration
        # here would strand callers — the import-time registration fires once,
        # and on_load (which re-registers) may not run again.
        global _vault
        _vault = None

    # ── SecretBackend protocol (consumed by relaydeck.vault) ─────────

    def get_secret(self, key: str) -> str | None:
        return get_secret(key)

    def set_secret(self, key: str, value: str) -> None:
        set_secret(key, value)

    def delete_secret(self, key: str) -> bool:
        return delete_secret(key)

    # ── API routes ──────────────────────────────────────────────────

    def _register_routes(self) -> None:
        host = self.host

        @host.api.route("/api/vault/keys", methods=["GET"], top_level=True)
        async def vault_keys():
            """Return key names only — values never leave the daemon."""
            if _vault is None:
                return []
            return _vault.keys()

        @host.api.route("/api/vault/keys/{key}", methods=["POST"], top_level=True)
        async def vault_set(key: str, body: dict[str, str]):
            from fastapi import HTTPException

            if _vault is None:
                raise HTTPException(500, "Vault not initialized")
            set_secret(key, body.get("value", ""))
            return {"key": key, "status": "set"}

        @host.api.route("/api/vault/keys/{key}", methods=["DELETE"], top_level=True)
        async def vault_delete(key: str):
            from fastapi import HTTPException

            if _vault is None:
                raise HTTPException(404)
            if delete_secret(key):
                return {"key": key, "status": "deleted"}
            raise HTTPException(404, f"Key {key} not found")

    # ── CLI subgroup ────────────────────────────────────────────────

    def _register_cli(self) -> None:
        host = self.host
        import click

        @host.cli.command("list", help="List vault keys (values never shown).")
        def _list():
            from rich.console import Console

            console = Console()
            if _vault is None:
                console.print("[dim]Vault not initialized[/]")
                return
            keys = _vault.keys()
            if not keys:
                console.print("[dim]Vault is empty[/]")
                return
            for k in keys:
                console.print(f"  • {k}")

        @host.cli.command("set", help="Set a vault secret.")
        @click.argument("key")
        @click.argument("value")
        def _set(key: str, value: str):
            from rich.console import Console

            console = Console()
            handled, err = _route_through_daemon("POST", key, value)
            if handled:
                console.print(f"[green]✓[/] Set [bold]{key}[/] [dim](via daemon)[/]")
                return
            if _vault is None:
                console.print("[red]Vault not initialized[/]")
                return
            set_secret(key, value)
            if err:
                console.print(
                    f"[yellow]·[/] daemon unreachable ({err}); wrote disk + auth.json. "
                    f"[dim]Daemon will pick up the new value on next restart.[/]"
                )
            console.print(f"[green]✓[/] Set [bold]{key}[/]")

        @host.cli.command("get", help="Get a vault secret (displays masked).")
        @click.argument("key")
        def _get(key: str):
            from rich.console import Console

            console = Console()
            if _vault is None:
                console.print("[red]Vault not initialized[/]")
                return
            val = _vault.get(key)
            if val is None:
                console.print(f"[yellow]Key '{key}' not found[/]")
            else:
                console.print(f"[bold]{key}[/] = {'*' * min(len(val), 20)}")

        @host.cli.command("rm", help="Delete a vault secret.")
        @click.argument("key")
        def _rm(key: str):
            from rich.console import Console

            console = Console()
            handled, err = _route_through_daemon("DELETE", key)
            if handled:
                console.print(f"[red]✗[/] Deleted [bold]{key}[/] [dim](via daemon)[/]")
                return
            if _vault is None:
                console.print("[red]Vault not initialized[/]")
                return
            if delete_secret(key):
                if err:
                    console.print(
                        f"[yellow]·[/] daemon unreachable ({err}); wrote disk. "
                        f"[dim]Daemon's in-memory cache will refresh on next restart.[/]"
                    )
                console.print(f"[red]✗[/] Deleted [bold]{key}[/]")
            else:
                console.print(f"[yellow]Key '{key}' not found[/]")

        @host.cli.command("info", help="Show vault format + path information.")
        def _info():
            from rich.console import Console

            console = Console()
            if _vault is None:
                console.print("[red]Vault not initialized[/]")
                return
            backend = type(_vault.store).__name__
            console.print(f"[bold]Backend:[/] {backend}")
            console.print(f"[bold]Path:[/] {_vault.path}")
            if isinstance(_vault.store, EncryptedStore):
                console.print(f"[bold]Salt:[/] {_vault.store.salt_path()}")
                passphrase_set = bool(os.environ.get("RELAYDECK_VAULT_PASSPHRASE"))
                console.print(
                    f"[bold]Passphrase:[/] "
                    f"{'set (RELAYDECK_VAULT_PASSPHRASE)' if passphrase_set else 'not set'}",
                )
            console.print(f"[bold]Keys:[/] {len(_vault.keys())}")

        @host.cli.command("migrate", help="Migrate legacy vault.yaml → vault.enc.")
        @click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
        def _migrate(yes: bool):
            """Migrate legacy vault.yaml → encrypted vault.enc.

            Reads every key out of the plaintext yaml, writes the
            encrypted file + a fresh salt, and renames the old yaml
            to `vault.yaml.bak`. The plugin reloads automatically on
            next access. Idempotent: running with no yaml present is
            a no-op.
            """
            global _vault
            from rich.console import Console

            console = Console()
            if _vault is None:
                console.print("[red]Vault not initialized[/]")
                return
            config_home = _vault.path.parent
            yaml_path = config_home / "vault.yaml"
            if not yaml_path.exists():
                console.print("[dim]No vault.yaml to migrate.[/]")
                return
            if not yes:
                click.confirm(
                    f"Migrate {yaml_path} → encrypted vault.enc?",
                    abort=True,
                )
            enc_path, count = migrate_to_encrypted(config_home)
            _vault = Vault(pick_store(config_home))
            console.print(
                f"[green]✓[/] Migrated [bold]{count}[/] keys to {enc_path}\n"
                f"[dim]Legacy file kept as {yaml_path}.bak — delete when satisfied.[/]"
            )

        @host.cli.command("rotate-key", help="Rotate the vault encryption key.")
        @click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
        def _rotate(yes: bool):
            """Rotate the vault encryption key.

            Generates a fresh salt, re-derives the key, and re-encrypts
            the vault under the new key. If a passphrase is in effect,
            set RELAYDECK_VAULT_PASSPHRASE to the NEW value before running
            this command so the new derivation uses the new secret.
            """
            global _vault
            from rich.console import Console

            console = Console()
            if _vault is None or not isinstance(_vault.store, EncryptedStore):
                console.print(
                    "[red]Rotate requires an encrypted vault. Run "
                    "`relaydeck vault migrate` first.[/]"
                )
                return
            if not yes:
                click.confirm(
                    "Rotate the vault key? Existing salt will be moved to .bak.",
                    abort=True,
                )
            try:
                _vault.store.rotate_key()
            except VaultError as exc:
                console.print(f"[red]Rotation failed:[/] {exc}")
                return
            config_home = _vault.path.parent
            _vault = Vault(pick_store(config_home))
            console.print(
                "[green]✓[/] Rotated — vault re-encrypted with a fresh salt"
            )


PLUGIN = VaultPlugin()

# Register the secret backend at import time as well as in on_load. Core's
# relaydeck.vault never imports this module; it delegates to whatever backend
# is registered. Registering on import preserves the historical contract that
# importing the vault plugin module makes get_secret/set_secret resolve here —
# the daemon and CLI both import every plugin module during discovery, and
# tests import it directly. on_load re-registers (idempotent); on_unload clears.
from relaydeck.vault import register_secret_backend as _register_secret_backend

_register_secret_backend(PLUGIN)


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compat shim for tests that build a PluginContext by hand."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "api.register",
            "cli.register",
            "ui.register",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        top_level_api=True,
    )
    PLUGIN.on_load(host)
