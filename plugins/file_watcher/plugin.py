"""
File watcher plugin — emits workspace.file.* events.

Watches registered workspace directories for file changes (create,
modify, delete) and emits events on the plugin event bus. Other
plugins (cognitive, fleet-context, auto-compilers) subscribe to these
events.

Declared capability gates (in plugin.toml; the gate refuses the call at
runtime without them):

  * `events.subscribe` — workspace.added / .removed / shutdown
  * `events.emit`      — workspace.file.* notifications
  * `workers.spawn`    — per-workspace polling worker

## Configuration (per workspace, in agent.toml)

    [file_watcher]
    watch_patterns = ["**/*.py", "**/*.md", "**/*.yaml", "AGENTS.md"]
    ignore_patterns = [".git/**", "__pycache__/**", ".venv/**"]
    debounce_ms = 500

Polling uses os.walk on a configurable interval. The pluggable
backend (native kqueue/inotify via watchdog) was envisioned in the
original design; that lands as a follow-up since the polling
implementation is already production-acceptable on small workspaces.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import time
from pathlib import Path
from typing import Any

from relaydeck.sdk import Event, EventType, Plugin, PluginContext, PluginEventBus, PluginHost

PLUGIN_NAME = "file-watcher"

logger = logging.getLogger(__name__)


_DEFAULT_WATCH = [
    "**/*.py", "**/*.md", "**/*.yaml", "**/*.yml",
    "**/*.toml", "**/*.json", "**/*.ts", "**/*.js",
    "AGENTS.md", "*.md",
]
_DEFAULT_IGNORE = [
    ".git/**", "__pycache__/**", ".venv/**", "node_modules/**",
    ".pytest_cache/**", ".ruff_cache/**", "dist/**", "build/**",
    "*.pyc", ".DS_Store",
]


class FileWatcher:
    """Snapshot-diff watcher for one workspace root.

    Decoupled from the plugin lifecycle: the FileWatcherPlugin
    instantiates one of these per active workspace, holds them in a
    dict, and stops them on workspace.removed or system.shutdown.
    The class itself doesn't reach into globals — `bus` and `worker`
    spawner are passed in, which makes it unit-testable without a
    daemon.
    """

    def __init__(
        self,
        root: Path,
        *,
        bus: Any,
        worker_spawner: Any,
        workspace: str | None = None,
        watch_patterns: list[str] | None = None,
        ignore_patterns: list[str] | None = None,
        debounce_ms: int = 500,
    ):
        self.root = root
        # Workspace name this watcher belongs to. Defaults to the dir
        # basename, but the plugin passes the registry name so the
        # spawned worker can be scoped to the right workspace in the
        # dashboard (otherwise it reads as an unscoped infra worker and
        # shows in every workspace's Workers lens).
        self.workspace = workspace or root.name
        self._bus = bus
        self._worker_spawner = worker_spawner
        self.watch_patterns = watch_patterns or list(_DEFAULT_WATCH)
        self.ignore_patterns = ignore_patterns or list(_DEFAULT_IGNORE)
        self.debounce_ms = debounce_ms
        self._snapshot: dict[str, float] = {}
        self._pending: dict[str, float] = {}
        self._worker = None
        self._primed = False

    def start(self) -> None:
        """Spawn the polling worker via the SDK's worker host.

        `worker_spawner` is host.workers — the capability-gated
        version of register_worker. The interval is the debounce
        window, since a finer cadence than that adds no value: any
        change within debounce_ms collapses into one event.
        """
        self._worker = self._worker_spawner.spawn(
            name=f"{PLUGIN_NAME}:{self.workspace}",
            fn=self._tick,
            interval=max(0.1, self.debounce_ms / 1000.0),
            config={
                "workspace": self.workspace,
                "root": str(self.root),
                "watch_patterns": list(self.watch_patterns),
                "ignore_patterns": list(self.ignore_patterns),
                "debounce_ms": self.debounce_ms,
            },
            description=(
                "Watches this workspace's files for changes (debounced) and "
                "emits workspace.file.* events other plugins subscribe to. "
                "A tick is one snapshot diff against the watch patterns — "
                "quiet until a watched file is added, changed, or removed."
            ),
        )

    def stop(self) -> None:
        """Stop watching + flush any debounced events still pending."""
        if self._worker is not None:
            try:
                self._worker.stop()
            except Exception:
                pass
        self._flush_pending()

    # ── Worker tick ───────────────────────────────────────────────

    def _tick(self, worker) -> None:
        """One sweep — called on the worker's interval. First tick
        primes the snapshot without emitting; subsequent ticks diff
        against the previous snapshot and emit changes."""
        if not self._primed:
            self._snapshot = self._scan()
            self._primed = True
            try:
                worker.log(f"primed initial snapshot: {len(self._snapshot)} files")
            except Exception:
                pass
            return
        try:
            current = self._scan()
            old_pending = len(self._pending)
            self._compare(self._snapshot, current)
            self._snapshot = current
            changes = len(self._pending) - old_pending
            if changes > 0:
                try:
                    worker.log(f"detected {changes} change(s)")
                except Exception:
                    pass
                # Emit at the end of the tick — debounce window is
                # collapsed within this snapshot diff, so it's safe
                # to flush at tick boundary.
                self._flush_pending()
        except Exception as exc:
            try:
                worker.log(f"scan error: {exc}", level="warn")
            except Exception:
                pass

    # ── Snapshot + diff ──────────────────────────────────────────

    def _scan(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        if not self.root.exists():
            return snapshot
        try:
            for dirpath, dirnames, filenames in os.walk(str(self.root)):
                rel_dir = Path(dirpath).relative_to(self.root)
                # Prune ignored dirs in-place so os.walk skips descent.
                dirnames[:] = [
                    d for d in dirnames
                    if not self._is_ignored(str(rel_dir / d))
                ]
                for fname in filenames:
                    rel_path = (
                        str(rel_dir / fname) if str(rel_dir) != "." else fname
                    )
                    if self._is_ignored(rel_path):
                        continue
                    if not self._matches_watch(rel_path):
                        continue
                    full_path = Path(dirpath) / fname
                    try:
                        snapshot[rel_path] = full_path.stat().st_mtime
                    except OSError:
                        pass
        except Exception:
            pass
        return snapshot

    def _compare(self, old: dict[str, float], new: dict[str, float]) -> None:
        now = time.time()
        for path, old_mtime in old.items():
            if path not in new:
                self._debounce(path, EventType.WORKSPACE_FILE_DELETED, now)
            elif new[path] != old_mtime:
                self._debounce(path, EventType.WORKSPACE_FILE_CHANGED, now)
        for path in new:
            if path not in old:
                self._debounce(path, EventType.WORKSPACE_FILE_CREATED, now)

    def _debounce(self, rel_path: str, event_type: str, now: float) -> None:
        key = f"{rel_path}:{event_type}"
        if key in self._pending:
            if now - self._pending[key] < self.debounce_ms / 1000.0:
                return
        self._pending[key] = now

    def _flush_pending(self) -> None:
        if self._bus is None:
            self._pending.clear()
            return
        for key, _first in list(self._pending.items()):
            rel_path, event_type = key.rsplit(":", 1)
            full_path = self.root / rel_path
            self._pending.pop(key, None)
            try:
                # `self._bus` may be the raw PluginEventBus (tests) or the
                # SDK EventBusHost adapter (production via host.events).
                # Both accept a pre-built Event: the raw bus natively, the
                # adapter via its isinstance(Event) guard. Emitting the
                # Event directly is the idiom used across the codebase and
                # works regardless of which bus was injected.
                self._bus.emit(Event(
                    type=event_type,
                    data={
                        "path": str(full_path),
                        "relative_path": rel_path,
                        "root": str(self.root),
                    },
                    source_plugin=PLUGIN_NAME,
                ))
            except Exception:
                pass

    # ── Pattern matching ─────────────────────────────────────────

    def _is_ignored(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, p) for p in self.ignore_patterns)

    def _matches_watch(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, p) for p in self.watch_patterns)


# ── SDK Plugin ──────────────────────────────────────────────────────


class FileWatcherPlugin(Plugin):
    """SDK-style plugin. Spawns one FileWatcher per workspace it sees
    in workspace.added events; tears them down on workspace.removed
    or system.shutdown."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self._watchers: dict[str, FileWatcher] = {}
        host.events.subscribe("workspace.added", self._on_workspace_added)
        host.events.subscribe("workspace.removed", self._on_workspace_removed)
        host.events.subscribe("system.shutdown", self._on_shutdown)

    def on_unload(self) -> None:
        for w in list(self._watchers.values()):
            try:
                w.stop()
            except Exception:
                pass
        self._watchers.clear()

    # ── Event handlers ───────────────────────────────────────────

    def _on_workspace_added(self, event: Event) -> None:
        ws_name = event.data.get("name", "")
        ws_path = event.data.get("path", "")
        if not ws_name or not ws_path:
            return
        if ws_name in self._watchers:
            return
        root = Path(ws_path)
        if not root.exists():
            return
        bus = self.host.events  # has .emit
        watcher = FileWatcher(
            root=root,
            workspace=ws_name,
            bus=bus,
            worker_spawner=self.host.workers,
        )
        try:
            watcher.start()
        except Exception:
            logger.exception("file-watcher: failed to start for %s", ws_name)
            return
        self._watchers[ws_name] = watcher
        logger.info("file-watcher: started for workspace %s", ws_name)

    def _on_workspace_removed(self, event: Event) -> None:
        ws_name = event.data.get("name", "")
        w = self._watchers.pop(ws_name, None)
        if w is None:
            return
        try:
            w.stop()
        except Exception:
            pass
        logger.info("file-watcher: stopped for workspace %s", ws_name)

    def _on_shutdown(self, _event: Event) -> None:
        for w in list(self._watchers.values()):
            try:
                w.stop()
            except Exception:
                pass
        self._watchers.clear()


PLUGIN = FileWatcherPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compatibility shim so the existing PluginRegistry loader can
    boot us via the same `PLUGIN.on_load(ctx)` shape the other SDK
    pilots use until the loader natively understands new-style
    plugins everywhere."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe",
            "events.emit",
            "workers.spawn",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
