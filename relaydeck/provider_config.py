"""
Operator-set provider configuration — the non-secret half of the Providers
settings section.

API keys live in the **vault** (values never leave the daemon); this store
holds only the safe-to-persist bits: per-provider **base-URL overrides**
and (next increment) **user-defined custom providers**. Backed by
`~/.relaydeck/providers.yaml`:

    overrides:
      openai: { base_url: "https://my-proxy/v1" }
    custom: []   # reserved for user-defined OpenAI-compatible providers

A `ProviderPlugin` reads its override via `resolved_base_url()`. Built-in
providers need only the API key (vault); the base-URL override is for
proxies / self-hosted gateways.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _path(config_home: Path | None = None) -> Path:
    home = config_home or (Path.home() / ".relaydeck")
    return home / "providers.yaml"


def _load(config_home: Path | None = None) -> dict[str, Any]:
    p = _path(config_home)
    if not p.exists():
        return {"overrides": {}, "custom": []}
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {"overrides": {}, "custom": []}
    data.setdefault("overrides", {})
    data.setdefault("custom", [])
    return data


def _save(data: dict[str, Any], config_home: Path | None = None) -> None:
    p = _path(config_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False))


# ── base-url overrides ────────────────────────────────────────────────


def get_base_url(provider: str, config_home: Path | None = None) -> str | None:
    ov = _load(config_home).get("overrides", {}).get(provider) or {}
    val = ov.get("base_url")
    return str(val) if val else None


def set_base_url(provider: str, base_url: str | None, config_home: Path | None = None) -> None:
    """Set (or clear, with None/empty) a provider's base-URL override."""
    data = _load(config_home)
    overrides = data.setdefault("overrides", {})
    entry = overrides.setdefault(provider, {})
    if base_url:
        entry["base_url"] = base_url.strip()
    else:
        entry.pop("base_url", None)
        if not entry:
            overrides.pop(provider, None)
    _save(data, config_home)


def all_overrides(config_home: Path | None = None) -> dict[str, dict[str, Any]]:
    return dict(_load(config_home).get("overrides", {}))


# ── custom (user-defined) providers — data layer; registration lands
#    in the next increment ───────────────────────────────────────────


def list_custom(config_home: Path | None = None) -> list[dict[str, Any]]:
    return list(_load(config_home).get("custom", []))


def upsert_custom(spec: dict[str, Any], config_home: Path | None = None) -> None:
    """Create/replace a custom provider definition keyed by `name`."""
    data = _load(config_home)
    custom = data.setdefault("custom", [])
    key = spec.get("name") or spec.get("id")
    custom[:] = [c for c in custom if (c.get("name") or c.get("id")) != key]
    custom.append(spec)
    _save(data, config_home)


def remove_custom(name: str, config_home: Path | None = None) -> bool:
    data = _load(config_home)
    custom = data.get("custom", [])
    before = len(custom)
    data["custom"] = [c for c in custom if (c.get("name") or c.get("id")) != name]
    _save(data, config_home)
    return len(data["custom"]) < before
