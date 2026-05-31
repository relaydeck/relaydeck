"""
Theme plugin — the skill-shipping surface for the theme engine.

The engine itself (registry, resolver, token contract, HTTP API, CLI) is
core: `relaydeck/themes.py`, `relaydeck/transports/api.py` (`/api/themes`,
`/api/appearance`), `relaydeck/transports/cli.py` (`relaydeck theme …`), and
the dashboard Appearance lens. This plugin exists for one reason: to
materialize the `theme` SKILL.md into workspaces via the generic
`[plugin.skills]` mechanism, so an agent on ANY harness learns how to
author themes through the `relaydeck theme` CLI.

It's daemon-wide (not workspace-scoped) — theming is universal — so it
opts every workspace in via `skill_target_workspaces`, gated by the
`inject_skill` setting. Materialization + re-sync is owned by the bundled
`skills` plugin; this plugin just declares the skill and answers the two
provider hooks.
"""

from __future__ import annotations

import logging
from typing import Any

from relaydeck.sdk import Plugin, PluginHost

logger = logging.getLogger(__name__)

PLUGIN_NAME = "theme"


def _setting(key: str, default: Any) -> Any:
    from relaydeck.plugin_settings import get_setting
    return get_setting(PLUGIN_NAME, key, default=default)


def _inject_skill_enabled() -> bool:
    v = _setting("inject_skill", True)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


class ThemePlugin(Plugin):
    """Daemon-wide plugin that ships the `theme` authoring skill."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self._config_home = host.config_home

    def on_unload(self) -> None:
        # The skills plugin strips materialized skills on plugin unload.
        pass

    def on_settings_changed(self, new_values: dict[str, Any]) -> None:
        """Toggling `inject_skill` changes the target set — nudge the
        skills manager to re-sync."""
        del new_values
        try:
            self.host.events.emit("plugin.skills.changed", {"plugin": PLUGIN_NAME})
        except Exception:
            pass

    # ── Skill provider hooks (called by the bundled skills manager) ──

    def skill_target_workspaces(self, all_workspaces: list[str]) -> list[str]:
        """Theming is universal — ship the skill to every workspace,
        unless the operator turned `inject_skill` off."""
        if not _inject_skill_enabled():
            return []
        return list(all_workspaces)


PLUGIN = ThemePlugin()
