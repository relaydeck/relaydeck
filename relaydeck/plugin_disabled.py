"""
Plugin enable/disable persistence.

A separate concern from `plugin_settings`: this controls whether a
plugin loads at all, not how it's configured. Disabled plugins are
listed in `~/.relaydeck/plugins-disabled.yaml` (a single
`disabled: [name, ...]` list) and the `PluginRegistry` skips them at
load time.

The dashboard's "Disable" toggle also calls a runtime unload so the
user sees event-driven plugins (emote, file-watcher, ...) react
immediately. CLI subcommands and API routes registered at startup
will linger until the daemon restarts — the API surfaces that via a
flag in the response.

Disable state is global per machine, not per workspace. The
workspace-level `plugins = [...]` list in agent.toml controls which
plugins' injections apply to that workspace's agents; that's a
different concept.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


_LOCK = threading.RLock()


def _store_path() -> Path:
    return Path.home() / ".relaydeck" / "plugins-disabled.yaml"


def _load() -> set[str]:
    p = _store_path()
    if not p.exists():
        return set()
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            return set()
        out = data.get("disabled") or []
        if not isinstance(out, list):
            return set()
        return {str(x) for x in out if x}
    except Exception as exc:
        logger.warning("plugins-disabled: load failed: %s", exc)
        return set()


def _save(disabled: set[str]) -> None:
    import yaml
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(
        {"disabled": sorted(disabled)},
        sort_keys=True, default_flow_style=False,
    ))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def disabled_set() -> set[str]:
    """Return the set of plugin names the operator has disabled."""
    with _LOCK:
        return _load()


def is_disabled(name: str) -> bool:
    return name in disabled_set()


def set_disabled(name: str, disabled: bool) -> None:
    """Toggle a plugin's disabled flag in the persistent store."""
    with _LOCK:
        current = _load()
        if disabled:
            current.add(name)
        else:
            current.discard(name)
        _save(current)
