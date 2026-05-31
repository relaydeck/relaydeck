"""Curated community plugin registry.

relaydeck recommends third-party plugins through a maintainer-curated catalog
(``plugins/registry.yaml``) rather than vendoring community code into the repo.
An entry pins a package/git spec and a version; ``relaydeck plugin search``
surfaces curated entries first, and ``relaydeck plugin install <name>`` resolves
a registry name to its pinned install spec and records it at the ``curated``
trust tier.

This module is read-only and fail-open: a missing or malformed registry yields
no entries rather than raising.

Trust ladder (highest → lowest):
  bundled  — official plugins shipped in the relaydeck wheel
  curated  — community plugins approved in this registry, pinned in plugins.lock
  local    — operator-approved path/git/package install
  untrusted — discovered (e.g. entry point) but not approved; skipped by default
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CURATED_TRUST = "curated"


@dataclass
class RegistryEntry:
    name: str
    summary: str = ""
    package: str = ""
    version: str = ""
    repo: str = ""
    git_ref: str = ""
    maintainer: str = ""
    categories: list[str] = field(default_factory=list)
    sha256: str = ""

    def install_spec(self) -> str:
        """The argument to pass to `relaydeck plugin install` for this entry.

        PyPI package (optionally version-constrained) when ``package`` is set;
        otherwise a pinned git URL (``repo`` + ``@git_ref``)."""
        if self.package:
            return f"{self.package}{self.version}" if self.version else self.package
        if self.repo:
            return f"git+{self.repo}@{self.git_ref}" if self.git_ref else f"git+{self.repo}"
        return self.name


def _registry_yaml_path() -> Path | None:
    """Locate plugins/registry.yaml next to the installed ``plugins`` package."""
    try:
        spec = importlib.util.find_spec("plugins")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    for root in spec.submodule_search_locations:
        cand = Path(root) / "registry.yaml"
        if cand.is_file():
            return cand
    return None


def load_registry() -> list[RegistryEntry]:
    """Parse the curated registry. Returns [] on missing/invalid file."""
    path = _registry_yaml_path()
    if path is None:
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("registry: failed to parse %s", path, exc_info=True)
        return []
    raw = data.get("plugins") or []
    if not isinstance(raw, list):
        return []
    out: list[RegistryEntry] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append(RegistryEntry(
            name=str(item["name"]),
            summary=str(item.get("summary") or ""),
            package=str(item.get("package") or ""),
            version=str(item.get("version") or ""),
            repo=str(item.get("repo") or ""),
            git_ref=str(item.get("git_ref") or ""),
            maintainer=str(item.get("maintainer") or ""),
            categories=[str(c) for c in (item.get("categories") or [])],
            sha256=str(item.get("sha256") or ""),
        ))
    return out


def get_entry(name: str) -> RegistryEntry | None:
    for e in load_registry():
        if e.name == name:
            return e
    return None


def search(query: str) -> list[RegistryEntry]:
    """Curated entries whose name/summary/categories match ``query`` (case-
    insensitive substring). Empty query returns the whole registry."""
    q = (query or "").lower().strip()
    entries = load_registry()
    if not q:
        return entries
    return [
        e for e in entries
        if q in e.name.lower()
        or q in e.summary.lower()
        or any(q in c.lower() for c in e.categories)
    ]


def is_curated(name: str) -> bool:
    return get_entry(name) is not None
