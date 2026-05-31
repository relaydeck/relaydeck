"""Installed-plugin lockfile support.

The lockfile records provenance and the manifest the user approved. It
does not load plugins; it gives support/debugging and future install
commands a stable source of truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py312+ in project metadata
    import tomli as tomllib  # type: ignore

from relaydeck.plugin_manifest import PluginManifest

HOST_API_VERSION = 1


@dataclass
class PluginLockEntry:
    name: str
    source: str
    version: str
    manifest_hash: str
    scope: str
    state: str = "enabled"
    host_api_version: int = 1
    installed_at: str = ""
    installed_via: str = "local"
    declared_capabilities: list[str] = field(default_factory=list)
    block_reason: str = ""
    git_url: str = ""
    git_ref: str = ""
    git_commit: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "source": self.source,
            "version": self.version,
            "manifest_hash": self.manifest_hash,
            "scope": self.scope,
            "state": self.state,
            "host_api_version": self.host_api_version,
            "installed_at": self.installed_at,
            "installed_via": self.installed_via,
            "declared_capabilities": list(self.declared_capabilities),
        }
        if self.block_reason:
            out["block_reason"] = self.block_reason
        if self.git_url:
            out["git_url"] = self.git_url
        if self.git_ref:
            out["git_ref"] = self.git_ref
        if self.git_commit:
            out["git_commit"] = self.git_commit
        return out


def lock_path(config_home: Path) -> Path:
    return config_home / "plugins.lock"


def load_lock(config_home: Path) -> dict[str, PluginLockEntry]:
    path = lock_path(config_home)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    plugins = data.get("plugins") or {}
    out: dict[str, PluginLockEntry] = {}
    if not isinstance(plugins, dict):
        return out
    for name, raw in plugins.items():
        if not isinstance(raw, dict):
            continue
        out[str(name)] = PluginLockEntry(
            name=str(name),
            source=str(raw.get("source") or ""),
            version=str(raw.get("version") or ""),
            manifest_hash=str(raw.get("manifest_hash") or ""),
            scope=str(raw.get("scope") or "user"),
            state=str(raw.get("state") or "enabled"),
            host_api_version=int(raw.get("host_api_version") or 1),
            installed_at=str(raw.get("installed_at") or ""),
            installed_via=str(raw.get("installed_via") or "local"),
            declared_capabilities=[str(v) for v in raw.get("declared_capabilities") or []],
            block_reason=str(raw.get("block_reason") or ""),
            git_url=str(raw.get("git_url") or ""),
            git_ref=str(raw.get("git_ref") or ""),
            git_commit=str(raw.get("git_commit") or ""),
        )
    return out


def save_lock(config_home: Path, entries: dict[str, PluginLockEntry]) -> None:
    path = lock_path(config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plugins": {name: entry.to_dict() for name, entry in sorted(entries.items())}}
    path.write_text(tomli_w.dumps(payload))


def entry_from_manifest(
    manifest: PluginManifest,
    *,
    source: str,
    scope: str,
    installed_via: str,
    state: str = "enabled",
    block_reason: str = "",
    git_url: str = "",
    git_ref: str = "",
    git_commit: str = "",
) -> PluginLockEntry:
    return PluginLockEntry(
        name=manifest.name,
        source=source,
        version=manifest.version,
        manifest_hash=manifest.manifest_hash,
        scope=scope,
        state=state,
        host_api_version=manifest.host_api_version,
        installed_at=_utc_now(),
        installed_via=installed_via,
        declared_capabilities=list(manifest.declared_capabilities),
        block_reason=block_reason,
        git_url=git_url,
        git_ref=git_ref,
        git_commit=git_commit,
    )


def verify_lock(config_home: Path, manifests: list[PluginManifest]) -> dict[str, PluginLockEntry]:
    """Rebuild lock entries for discovered manifests while preserving state.

    Installed plugins move to ``state=blocked`` when their manifest hash
    drifts from the previously approved lock entry or when they target a
    Host API major this runtime cannot satisfy.
    """
    existing = load_lock(config_home)
    rebuilt: dict[str, PluginLockEntry] = {}
    for manifest in manifests:
        old = existing.get(manifest.name)
        state = old.state if old else "enabled"
        block_reason = old.block_reason if old else ""
        if manifest.host_api_version != HOST_API_VERSION:
            state = "blocked"
            block_reason = (
                f"host_api_version {manifest.host_api_version} is not supported "
                f"by runtime Host API {HOST_API_VERSION}"
            )
        elif (
            old
            and old.installed_via not in ("builtin", "editable", "editable-package")
            and old.manifest_hash
            and old.manifest_hash != manifest.manifest_hash
        ):
            state = "blocked"
            block_reason = "manifest hash changed since approval"
        elif state == "blocked" and block_reason.startswith("host_api_version"):
            state = "enabled"
            block_reason = ""

        entry = entry_from_manifest(
            manifest,
            source=old.source if old else _source_for_manifest(manifest),
            scope=old.scope if old else "builtin",
            installed_via=old.installed_via if old else "builtin",
            state=state,
            block_reason=block_reason,
            git_url=old.git_url if old else "",
            git_ref=old.git_ref if old else "",
            git_commit=old.git_commit if old else "",
        )
        if old and old.installed_at:
            entry.installed_at = old.installed_at
        rebuilt[manifest.name] = entry
    save_lock(config_home, rebuilt)
    return rebuilt


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _source_for_manifest(manifest: PluginManifest) -> str:
    if manifest.path is None:
        return "unknown"
    return str(manifest.path.parent)
