"""Per-operator dashboard UI preferences.

Stores things like tile-system assignments, density toggle, accent
color, last-active lens, last-active workspace. Anything the dashboard
wants to persist that doesn't belong in a workspace config.

File: ``~/.relaydeck/preferences.yaml`` (mode 0600).

The contract is intentionally minimal — the dashboard pushes the whole
blob on every coalesced save, and we do an atomic rename. No JSON
schema, no migration: dashboards that don't recognize a key just leave
it alone, so adding new preference keys is forward-compatible.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def _prefs_path(home: Path) -> Path:
    return home / "preferences.yaml"


def read_preferences(home: Path) -> dict[str, Any]:
    """Return the on-disk preferences blob, or {} if missing/empty."""
    p = _prefs_path(home)
    if not p.exists():
        return {}
    try:
        raw = p.read_text()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return {}
    return data


def write_preferences(home: Path, prefs: dict[str, Any]) -> None:
    """Atomically write the preferences blob (mode 0600).

    Uses ``mkstemp`` so the file lands at the final mode immediately
    rather than briefly sitting at the umask-derived default. The
    ``os.replace`` is atomic on POSIX so concurrent readers always see
    either the old or new content, never a half-written file.
    """
    if not isinstance(prefs, dict):
        raise TypeError("preferences must be a mapping")
    home.mkdir(parents=True, exist_ok=True)
    p = _prefs_path(home)
    fd, tmp_name = tempfile.mkstemp(prefix=".preferences.", dir=str(home))
    tmp = Path(tmp_name)
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        try:
            os.write(fd, yaml.safe_dump(prefs, sort_keys=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


# ── Appearance (theme + density + glow + dashboard layout) ──────────
#
# Appearance is a sub-blob of preferences with a two-level shape: a
# `global` default and a `workspaces` map of per-workspace overrides. A
# workspace inherits each key from global until it sets its own; global
# falls back to the package defaults below. This is the resolution the
# user chose: "per-workspace, with a global default".
#
#   appearance:
#     theme: base          # global default theme name
#     density: regular
#     glow: "on"
#     dashboard: [ {id,key,x,y,w,h}, ... ]   # null = client default
#     workspaces:
#       prod: { theme: amber, density: compact, dashboard: [...] }

_APPEARANCE_KEYS = ("theme", "density", "glow", "dashboard")
_APPEARANCE_DEFAULTS: dict[str, Any] = {
    "theme": "base",
    "density": "regular",
    "glow": "on",
    "dashboard": None,
}


def read_appearance(home: Path) -> dict[str, Any]:
    """The raw `appearance` sub-blob (global keys + `workspaces` map)."""
    ap = read_preferences(home).get("appearance")
    return ap if isinstance(ap, dict) else {}


def resolve_appearance(home: Path, workspace: str | None = None) -> dict[str, Any]:
    """Effective appearance for a workspace: per-workspace override →
    global → package default, key by key. `workspace=None` (or unknown)
    yields the global view."""
    ap = read_appearance(home)
    ws_map = ap.get("workspaces")
    ws_over = ws_map.get(workspace) if isinstance(ws_map, dict) and workspace else None
    if not isinstance(ws_over, dict):
        ws_over = {}
    out: dict[str, Any] = {}
    for k in _APPEARANCE_KEYS:
        if k in ws_over:
            out[k] = ws_over[k]
        elif k in ap:
            out[k] = ap[k]
        else:
            out[k] = _APPEARANCE_DEFAULTS[k]
    out["scope"] = "workspace" if (workspace and ws_over) else "global"
    return out


def set_appearance(
    home: Path, patch: dict[str, Any], workspace: str | None = None
) -> dict[str, Any]:
    """Merge `patch` into the global appearance (workspace=None) or a
    per-workspace override. Only the four appearance keys are accepted;
    a key set to None is *removed* (so a workspace can fall back to the
    global value). Returns the resolved appearance after the write."""
    clean = {k: v for k, v in patch.items() if k in _APPEARANCE_KEYS}
    prefs = read_preferences(home)
    ap = prefs.get("appearance")
    if not isinstance(ap, dict):
        ap = {}
    if workspace:
        ws_map = ap.get("workspaces")
        if not isinstance(ws_map, dict):
            ws_map = {}
        target = ws_map.get(workspace)
        if not isinstance(target, dict):
            target = {}
        for k, v in clean.items():
            if v is None:
                target.pop(k, None)
            else:
                target[k] = v
        if target:
            ws_map[workspace] = target
        else:
            ws_map.pop(workspace, None)
        ap["workspaces"] = ws_map
    else:
        for k, v in clean.items():
            if v is None:
                ap.pop(k, None)
            else:
                ap[k] = v
    prefs["appearance"] = ap
    write_preferences(home, prefs)
    return resolve_appearance(home, workspace)


def clear_appearance_theme(home: Path, name: str) -> list[str]:
    """Drop every appearance `theme` ref (global + per-workspace) that
    points at `name`, so deleting a theme makes the scopes that used it
    fall back (workspace → global, global → package default `base`)
    instead of leaving a dangling reference. Returns the list of scopes
    cleared (`"global"` and/or workspace names) — empty if nothing
    referenced it."""
    prefs = read_preferences(home)
    ap = prefs.get("appearance")
    if not isinstance(ap, dict):
        return []
    cleared: list[str] = []
    if ap.get("theme") == name:
        ap.pop("theme", None)
        cleared.append("global")
    ws_map = ap.get("workspaces")
    if isinstance(ws_map, dict):
        for ws, over in list(ws_map.items()):
            if isinstance(over, dict) and over.get("theme") == name:
                over.pop("theme", None)
                cleared.append(ws)
                if not over:
                    ws_map.pop(ws, None)
    if cleared:
        prefs["appearance"] = ap
        write_preferences(home, prefs)
    return cleared
