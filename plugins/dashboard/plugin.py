"""
Dashboard plugin — the skill-shipping surface for live dashboard control.

The control surface itself is core: the validator
(`relaydeck/dashboard_commands.py`), the endpoint
(`POST /api/dashboard/command`), and the CLI (`relaydeck dashboard …`). The
browser applies the emitted `dashboard.command` events live. This plugin
exists for one reason: to materialize the `relaydeck-dashboard` SKILL.md into
workspaces via the generic `[plugin.skills]` mechanism, so an agent on ANY
harness can reshape the dashboard through the `relaydeck dashboard` CLI — the
same capability the native `dashboard` tool gives the relaydeck-native agent.

Daemon-wide (not workspace-scoped) and opt-out via the `inject_skill`
setting; mirrors the theme plugin.
"""

from __future__ import annotations

import logging
from typing import Any

from relaydeck.sdk import Plugin, PluginHost

logger = logging.getLogger(__name__)

PLUGIN_NAME = "dashboard"


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


class DashboardPlugin(Plugin):
    """Daemon-wide plugin that ships the `relaydeck-dashboard` skill."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self._config_home = host.config_home

    def on_unload(self) -> None:
        # The skills plugin strips materialized skills on plugin unload.
        pass

    def on_settings_changed(self, new_values: dict[str, Any]) -> None:
        del new_values
        try:
            self.host.events.emit("plugin.skills.changed", {"plugin": PLUGIN_NAME})
        except Exception:
            pass

    def skill_target_workspaces(self, all_workspaces: list[str]) -> list[str]:
        """Dashboard control is universal — ship to every workspace unless the
        operator turned `inject_skill` off."""
        if not _inject_skill_enabled():
            return []
        return list(all_workspaces)


PLUGIN = DashboardPlugin()
