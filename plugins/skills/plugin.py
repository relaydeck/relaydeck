"""
Bundled Skills plugin — inventory, import, and the generic consumer
of `[plugin.skills]`.

This is the orchestrator layer the raw harness injection paths never had:
it answers *what skills exist, where they came from, which agents will
see them, and what changed*. The materialization
primitives live in `relaydeck.skills` (pure, harness-shared); this plugin
drives them on lifecycle events and exposes a lens, CLI, API, and a
periodic rescan worker.

## Responsibilities

  - **Materialize** every loaded plugin's declared skills into the
    workspaces that want them (`manager.sync_all`), with ownership
    sidecars so two plugins can't clobber each other. Re-runs on
    workspace + plugin lifecycle events and on `plugin.skills.changed`
    (emitted by messaging/telegram when their content or targets shift).
  - **Inventory** the filesystem (workspace user skills, plugin runtime
    skills, read-only codex skills) into the `skills_cache` mirror and
    emit `skills.changed` / `skills.invalid` / `skills.removed`.
  - **Surface** all of it via `relaydeck skills ...`, `/api/plugins/skills/*`,
    and the dashboard Skills lens.

## Disable semantics

`on_unload` deliberately does NOT strip materialized skills — disabling
the manager stops *future* re-sync but never silently removes a
capability an agent already depends on. Re-enable (or restart) to resume
syncing. Uninstalling a *contributing* plugin (messaging, telegram) is
what removes its skills, handled via `system.plugin.unloaded`.
"""

from __future__ import annotations

import logging
from contextlib import suppress

from relaydeck.sdk import Event, Plugin, PluginHost

from . import commands, manager, routes

PLUGIN_NAME = "skills"
logger = logging.getLogger(__name__)


class SkillsPlugin(Plugin):
    workspace_scoped = False
    description = (
        "Skill inventory + management: list, import, and "
        "materialize agent skills across workspaces and harnesses."
    )

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self.config_home = host.config_home
        self.db_path = str(host.config_home / "runtime" / "relaydeck.db")
        self._worker = None

        # Initial pass: materialize declared skills for the workspaces +
        # plugins already present at boot, then warm the inventory cache.
        self._safe(lambda: manager.sync_all(self.config_home), "initial sync")
        self._safe(self._rescan, "initial rescan")

        host.events.subscribe("workspace.added", self._on_ws_change)
        host.events.subscribe("workspace.updated", self._on_ws_change)
        host.events.subscribe("workspace.removed", self._on_ws_change)
        host.events.subscribe("system.plugin.loaded", self._on_plugin_loaded)
        host.events.subscribe("system.plugin.unloaded", self._on_plugin_unloaded)
        # A contributing plugin signals its skills need re-sync (settings
        # toggled, route table changed, etc.) without coupling to us.
        host.events.subscribe("plugin.skills.changed", self._on_skills_changed)

        self._start_worker()
        commands.register(self)
        routes.register(self)

    def on_unload(self) -> None:
        # Intentionally leave materialized skills in place — see module
        # docstring. Stop only our periodic inventory worker.
        worker = getattr(self, "_worker", None)
        if worker is not None:
            with suppress(Exception):
                worker.stop()
            with suppress(Exception):
                from relaydeck.workers import get_worker_registry
                live_worker = get_worker_registry().get(worker.id)
                if live_worker is not None:
                    live_worker.join(timeout=2.0)
        self._worker = None

    # ── settings ──────────────────────────────────────────────────

    def _scan_interval(self) -> float:
        try:
            return max(10.0, float(self.host.settings.get("scan_interval_seconds", 300.0)))
        except Exception:
            return 300.0

    def _managed_import_check_interval(self) -> float:
        try:
            return max(
                60.0,
                float(self.host.settings.get("managed_import_check_interval_seconds", 3600.0)),
            )
        except Exception:
            return 3600.0

    def _include_codex(self) -> bool:
        try:
            return bool(self.host.settings.get("include_codex", True))
        except Exception:
            return True

    def _include_claude(self) -> bool:
        try:
            return bool(self.host.settings.get("include_claude", True))
        except Exception:
            return True

    def _inject_plugin_authoring_skill(self) -> bool:
        try:
            value = self.host.settings.get("inject_plugin_authoring_skill")
        except Exception:
            return True
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)

    # ── Skill provider hook (called by the generic materializer) ──

    def skill_target_workspaces(self, all_workspaces: list[str]) -> list[str]:
        """Ship the plugin-authoring skill to every workspace by default.

        The skills plugin is daemon-wide, so without this hook its declared
        `[plugin.skills]` entry would have no default target.
        """
        if not self._inject_plugin_authoring_skill():
            return []
        return list(all_workspaces)

    def on_settings_changed(self, new_values: dict[str, object]) -> None:
        if "inject_plugin_authoring_skill" not in new_values:
            return
        with suppress(Exception):
            self.host.events.emit("plugin.skills.changed", {"plugin": PLUGIN_NAME})

    # ── worker ────────────────────────────────────────────────────

    def _start_worker(self) -> None:
        try:
            self._worker = self.host.workers.spawn(
                "inventory-sync",
                self._scan_tick,
                interval=self._scan_interval(),
                config={"scan_interval_s": self._scan_interval()},
                restart_policy="restart",
                description=(
                    "Re-syncs plugin-contributed skill files and refreshes the "
                    "skills_cache inventory each interval, so a SKILL.md edited "
                    "on disk or a skill installed out-of-band surfaces within "
                    "one tick. Managed Git imports are checked on a separate "
                    "low-frequency cadence for upstream updates."
                ),
            )
        except Exception:
            logger.exception("skills: failed to start rescan worker")

    def _scan_tick(self, _worker=None) -> None:
        # Each tick reconciles materialization (cheap + idempotent) then
        # refreshes the inventory cache — so a SKILL.md edited on disk,
        # a codex skill installed out-of-band, or a workspace that
        # appeared without an event all surface within one interval.
        manager.sync_all(self.config_home)
        manager.refresh_managed_imports(
            self.config_home,
            self.db_path,
            min_interval_s=self._managed_import_check_interval(),
        )
        self._rescan()

    def _rescan(self) -> dict:
        return manager.rescan(
            self.config_home, self.db_path,
            emit=self.host.events.emit, include_codex=self._include_codex(),
            include_claude=self._include_claude(),
        )

    # ── event handlers ────────────────────────────────────────────

    def _on_ws_change(self, _event: Event) -> None:
        self._safe(lambda: manager.sync_all(self.config_home), "ws-change sync")
        self._safe(self._rescan, "ws-change rescan")

    def _on_plugin_loaded(self, event: Event) -> None:
        name = (event.data or {}).get("name")
        if name == PLUGIN_NAME:
            return
        self._safe(lambda: manager.sync_all(self.config_home), "plugin-load sync")
        self._safe(self._rescan, "plugin-load rescan")

    def _on_plugin_unloaded(self, event: Event) -> None:
        name = (event.data or {}).get("name")
        if not name or name == PLUGIN_NAME:
            return
        # A contributing plugin went away — drop its materialized skills
        # so agents stop seeing a capability that's no longer wired.
        self._safe(lambda: manager.remove_plugin_skills(self.config_home, str(name)),
                   f"unload cleanup for {name}")
        self._safe(self._rescan, "plugin-unload rescan")

    def _on_skills_changed(self, _event: Event) -> None:
        self._safe(lambda: manager.sync_all(self.config_home), "skills-changed sync")
        self._safe(self._rescan, "skills-changed rescan")

    # ── util ──────────────────────────────────────────────────────

    @staticmethod
    def _safe(fn, label: str) -> None:
        try:
            fn()
        except Exception:
            logger.exception("skills: %s failed", label)


PLUGIN = SkillsPlugin()
