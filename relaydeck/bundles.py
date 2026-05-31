"""Recommended bundles of official relaydeck plugins.

A *bundle* is a named, pinned set of official plugin names (e.g. ``default``,
``minimal``). It is the one source of truth for "which official plugins should
be present", consumed by ``relaydeck doctor`` and the dashboard to flag a
missing recommended plugin, and by an eventual split ``relaydeck-plugins`` wheel
to know what it must ship. The bundle definitions live in ``plugins/bundle.toml``
so they travel with the plugins, not the engine.

Fail-open: a missing or malformed ``bundle.toml`` yields no bundles rather than
raising — the daemon never depends on this file to boot.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BUNDLE = "default"


@dataclass
class Bundle:
    name: str
    description: str = ""
    plugins: list[str] = field(default_factory=list)


def _bundle_toml_path() -> Path | None:
    """Locate plugins/bundle.toml next to the installed ``plugins`` package."""
    try:
        spec = importlib.util.find_spec("plugins")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    for root in spec.submodule_search_locations:
        cand = Path(root) / "bundle.toml"
        if cand.is_file():
            return cand
    return None


def load_bundles() -> dict[str, Bundle]:
    """Parse ``plugins/bundle.toml`` into ``{name: Bundle}``. Empty on error."""
    path = _bundle_toml_path()
    if path is None:
        return {}
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("bundles: failed to parse %s", path, exc_info=True)
        return {}
    out: dict[str, Bundle] = {}
    for name, body in (data.get("bundle") or {}).items():
        if not isinstance(body, dict):
            continue
        plugins = body.get("plugins") or []
        if not isinstance(plugins, list):
            continue
        out[name] = Bundle(
            name=name,
            description=str(body.get("description") or ""),
            plugins=[str(p) for p in plugins],
        )
    return out


def get_bundle(name: str = DEFAULT_BUNDLE) -> Bundle | None:
    return load_bundles().get(name)


def missing_from_bundle(present: set[str], name: str = DEFAULT_BUNDLE) -> list[str]:
    """Bundle plugin names not in ``present`` (e.g. discovered plugin names).
    Returns [] when the bundle is unknown — absence of a manifest is not an
    error, just "nothing to check"."""
    bundle = get_bundle(name)
    if bundle is None:
        return []
    return [p for p in bundle.plugins if p not in present]
