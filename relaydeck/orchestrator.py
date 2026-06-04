"""
Orchestrator: agent lifecycle for the daemon.

Holds the single in-process registry of running agents. Each agent type
implements `BaseAgent`; the orchestrator spawns them in their own threads,
persists their state to the `agents` table, and routes lifecycle events
to the dashboard via the in-memory event bus.

The orchestrator is intentionally thin — per-type behavior lives in the
agent classes. Lifecycle: register type → instantiate → start → stop →
list → delete. Agent crashes are caught and reflected in status; they
never take down the daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from relaydeck import config as relaydeck_config
from relaydeck.agents_base import BaseAgent
from relaydeck.db import open_db, upsert_agent

logger = logging.getLogger(__name__)


# ─── In-memory event bus ─────────────────────────────────────────────

class _AsyncSub:
    """An asyncio-native event subscriber: an `asyncio.Queue` plus the loop
    it belongs to, so `publish` (called from any thread) can hand events to
    it with `loop.call_soon_threadsafe` instead of parking a pool thread on a
    blocking `queue.Queue.get`. Bounded; on overflow the oldest event is
    dropped (a stalled SSE client must not grow memory without bound)."""

    __slots__ = ("queue", "loop")

    def __init__(self, loop: "asyncio.AbstractEventLoop", maxsize: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.loop = loop


class EventBus:
    """Lightweight pub/sub for SSE streaming.

    Routes agent events to subscribers within milliseconds, avoiding
    SQLite polling for the live dashboard stream. SQLite is still
    written for durability and history replay.

    Two subscriber flavours share one `publish`:
      - thread queues (`subscribe`) — legacy/in-process consumers that drain
        from their own thread;
      - async queues (`subscribe_async`) — SSE generators on the daemon's
        single event loop. These are the scalable path: an idle stream costs
        no thread, so N concurrent viewers no longer exhaust the default
        ThreadPoolExecutor (which control-plane `asyncio.to_thread` calls,
        e.g. start/stop agent, also share).
    """

    def __init__(self):
        self._subscribers: dict[str, list[queue.Queue]] = defaultdict(list)
        self._async_subscribers: dict[str, list[_AsyncSub]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, agent_id: str) -> queue.Queue:
        """Return a queue that will receive events for agent_id.
        Caller drains the queue from an async generator."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers[agent_id].append(q)
        return q

    def unsubscribe(self, agent_id: str, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers[agent_id].remove(q)
            except ValueError:
                pass

    def subscribe_async(self, agent_id: str, maxsize: int = 2000) -> _AsyncSub:
        """Register an asyncio subscriber for `agent_id` (or "*"). Must be
        called from within the running event loop — it captures that loop so
        cross-thread `publish` can deliver without blocking a pool thread."""
        sub = _AsyncSub(asyncio.get_running_loop(), maxsize)
        with self._lock:
            self._async_subscribers[agent_id].append(sub)
        return sub

    def unsubscribe_async(self, agent_id: str, sub: _AsyncSub) -> None:
        with self._lock:
            try:
                self._async_subscribers[agent_id].remove(sub)
            except ValueError:
                pass

    @staticmethod
    def _deliver_async(sub: _AsyncSub, event: dict) -> None:
        """Drop-oldest enqueue, run on the subscriber's own loop thread."""
        q = sub.queue
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def publish(self, agent_id: str, event_type: str,
                payload: dict | None, event_id: int) -> None:
        event = {"id": event_id, "type": event_type,
                 "agent_id": agent_id, "payload": payload or {}, "ts": time.time()}
        with self._lock:
            thread_qs = list(self._subscribers.get(agent_id, []))
            thread_qs += list(self._subscribers.get("*", []))
            async_subs = list(self._async_subscribers.get(agent_id, []))
            async_subs += list(self._async_subscribers.get("*", []))
        # Fan out outside the lock so a publish never blocks on a consumer.
        for q in thread_qs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        for sub in async_subs:
            try:
                sub.loop.call_soon_threadsafe(self._deliver_async, sub, event)
            except RuntimeError:
                # Loop already closed (subscriber went away mid-shutdown).
                pass


# Module-level singleton bus
_bus = EventBus()


# ─── Type registry ──────────────────────────────────────────────────

# Populated by plugins via register_agent_type().
_TYPES: dict[str, type] = {}


def register_agent_type(name: str, cls: type) -> None:
    """Register an agent type. cls must subclass BaseAgent."""
    _TYPES[name] = cls


def unregister_agent_type(name: str) -> None:
    """Remove an agent type. Used by the SDK adapter when a plugin
    unloads — so disabling the pi-harness plugin makes `pi` stop
    showing up in `known_agent_types()` immediately.

    Idempotent: removing a missing name is a no-op."""
    _TYPES.pop(name, None)


def known_agent_types() -> list[str]:
    return sorted(_TYPES.keys())


def get_agent_type(name: str) -> type | None:
    return _TYPES.get(name)


# ─── Orchestrator ───────────────────────────────────────────────────

class Orchestrator:
    """Manages the lifecycle of all agents in the daemon.

    One instance per daemon. Spawns agents in threads, tracks their
    status in SQLite, and provides the public API that the CLI and
    web API call through.
    """

    # How often the daemon-side sweeper reconciles zombie DB rows
    # against `_instances`. Five seconds is a sweet spot: tight enough
    # that `relaydeck agent list` doesn't lie for long, loose enough that
    # the read load on SQLite stays negligible.
    SWEEPER_INTERVAL: float = 5.0
    # Total wall-clock budget for joining ALL agent threads at shutdown.
    # Every agent's stop_flag + terminate() fire before the join loop, so the
    # reaps proceed in parallel; we wait against ONE shared deadline rather
    # than 10s *per agent*, which would serialize to N×10s and overrun the
    # daemon supervisor's SIGKILL grace (so clean stops got force-killed).
    SHUTDOWN_JOIN_BUDGET_S: float = 8.0
    # How long start_agent waits for the runner thread to confirm the
    # agent actually came up before returning. If the child fails to
    # Popen (binary missing, perm denied) we want the CLI to know
    # immediately, not report success and have the user later wonder
    # why the agent doesn't respond.
    START_VERIFY_TIMEOUT: float = 1.0

    def __init__(self, config_home: Path | None = None):
        self.config_home = config_home or Path.home() / ".relaydeck"
        self.db_path = str(self.config_home / "runtime" / "relaydeck.db")
        self._running: dict[str, threading.Thread] = {}
        self._instances: dict[str, BaseAgent] = {}
        self._lock = threading.Lock()
        self._plugin_event_bus: Any = None  # set by serve() after plugin load
        self._sweeper_thread: threading.Thread | None = None
        self._sweeper_stop = threading.Event()
        # Cross-harness semantic-status engine (created when the daemon
        # starts). Derives working/idle/complete-unread/awaiting-input from
        # each running agent's rendered screen — see semantic_engine.py.
        self._semantic_engine: Any = None
        # `_is_daemon` flips on when `start()` runs — i.e. when this
        # orchestrator is the actual daemon process. CLI commands that
        # create their own Orchestrator (for direct-DB reads) don't
        # set this and therefore don't run the sweeper or reconcile
        # against `_instances` (which would always be empty there).
        self._is_daemon = False

    def set_event_bus(self, bus: Any) -> None:
        """Set the plugin event bus for emitting lifecycle events, and
        bridge its data-change events onto the SSE bus (`_bus`) so the
        dashboard updates in real time without polling.

        Only event domains the SSE feed is *missing* are forwarded —
        `agent.*`, `workspace.*`, `worktree.*`, `skills.changed`,
        `github.*`, `external_agent.*`, `dashboard.*`, `appearance.changed`,
        `themes.changed`, `telegram.*`. The high-churn signals
        (`usage.record`, `emote.update`, `harness.*`, `loop.*`) already reach
        `_bus` via `BaseAgent.log_event`, so they're deliberately not forwarded
        here (no duplicate SSE delivery)."""
        self._plugin_event_bus = bus
        if bus is None or getattr(self, "_sse_bridge_installed", False):
            return

        def _forward(event: Any) -> None:
            try:
                t = getattr(event, "type", "") or ""
                data = getattr(event, "data", {}) or {}
                agent_id = data.get("agent_id") or data.get("id") or "*"
                _bus.publish(agent_id, t, data, 0)
            except Exception:
                pass

        try:
            for pat in ("agent.*", "workspace.*", "worktree.*", "skills.changed", "github.*", "external_agent.*", "dashboard.*", "appearance.changed", "themes.changed", "telegram.*", "system.plugin.*", "relaydeck-native.*", "hitl.*", "prompt.*", "autopilot.*", "usage_limits.*", "manager.*"):
                bus.subscribe(pat, _forward)
            self._sse_bridge_installed = True
        except Exception:
            pass

    def _emit_plugin_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Emit an event on the plugin event bus, if available."""
        if self._plugin_event_bus:
            from relaydeck.plugin import Event
            self._plugin_event_bus.emit(Event(
                type=event_type, data=data or {},
                source_plugin="orchestrator",
            ))

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Sync YAML specs to DB, autostart flagged agents, and launch
        the background zombie sweeper.

        Called from `relaydeck serve` (the daemon). Marks `_is_daemon=True`
        so other code paths (e.g. the sweeper) know they're allowed
        to compare `_instances` against the DB."""
        self._is_daemon = True

        conn = open_db(self.db_path)
        try:
            # Any agents the DB still has as `running` are stale — their
            # previous daemon process is gone, so the in-memory instance
            # doesn't exist. Reset them so the UI reflects reality and the
            # WebSocket doesn't dead-end on `agent_not_running`.
            conn.execute(
                "UPDATE agents SET status='stopped', last_error=NULL "
                "WHERE status IN ('running','starting')"
            )
            conn.commit()

            for spec in relaydeck_config.load_agent_specs(self.config_home):
                # Thread purpose/tags through too — YAML is the source
                # of truth and the DB mirrors them for fast queries
                # (relaydeck agent find, peer lists, identity preambles).
                # Without this, hand-edited or restored YAML specs come
                # back with empty meta after a daemon restart.
                upsert_agent(
                    conn, spec.id, spec.type, spec.name,
                    workspace=spec.workspace, auto_start=spec.auto_start,
                    purpose=spec.purpose, tags=spec.tags,
                )
                if spec.auto_start:
                    self.start_agent(spec.id)
        finally:
            conn.close()

        # Boot the periodic zombie sweeper. Without this, `relaydeck agent
        # list` keeps reporting "running" for agents whose threads
        # died without successfully calling update_status — and the
        # only way to see the truth was to send a message (which
        # triggers the send-side reconcile).
        self._start_sweeper()

        # Boot the cross-harness semantic-status engine. It derives
        # working/idle/complete-unread/awaiting-input from each running
        # agent's rendered screen, so EVERY harness (not just the ones with
        # a vendor hook) reports what it's actually doing.
        self._start_semantic_engine()

    def _start_sweeper(self) -> None:
        if self._sweeper_thread is not None and self._sweeper_thread.is_alive():
            return
        self._sweeper_stop.clear()
        t = threading.Thread(
            target=self._sweeper_loop, name="relaydeck-zombie-sweeper", daemon=True,
        )
        self._sweeper_thread = t
        t.start()
        logger.info(
            "Started zombie sweeper (interval=%.1fs)", self.SWEEPER_INTERVAL,
        )

    def _start_semantic_engine(self) -> None:
        if self._semantic_engine is not None:
            return
        try:
            from relaydeck.semantic_engine import SemanticStatusEngine
            self._semantic_engine = SemanticStatusEngine(self)
            self._semantic_engine.start()
        except Exception:
            logger.exception("Failed to start semantic-status engine")
            self._semantic_engine = None

    def _sweeper_loop(self) -> None:
        while not self._sweeper_stop.wait(self.SWEEPER_INTERVAL):
            try:
                self.reconcile_zombies()
            except Exception:
                logger.exception("Zombie sweeper iteration failed")

    def reconcile_zombies(self) -> list[str]:
        """One pass of the zombie reconciler.

        For each DB row claiming `running` or `starting`, verify the
        orchestrator actually has a live thread + a non-exited child
        process for it. Mismatches get snapped to `stopped` (or
        `errored` if the child exited with a non-zero return code).

        Returns the list of agent_ids that were reconciled this pass
        (for tests + logs). No-op when this orchestrator is not the
        daemon — a CLI-side orchestrator has an empty `_instances` and
        would otherwise spuriously mark everything as stopped.
        """
        if not self._is_daemon:
            return []

        with self._lock:
            running_threads = {
                aid: th for aid, th in self._running.items() if th.is_alive()
            }
            instances_snapshot = dict(self._instances)

        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, status FROM agents "
                "WHERE status IN ('running','starting')"
            ).fetchall()

            reconciled: list[str] = []
            for row in rows:
                aid = row["id"]
                if aid in running_threads:
                    # Thread is alive — also verify the child process
                    # is still around. A dead child with a live thread
                    # is a transitional state; the runner is about to
                    # update DB on its own (in `_runner`'s except/
                    # finally), so don't race it.
                    inst = instances_snapshot.get(aid)
                    proc = getattr(inst, "_proc", None) if inst else None
                    if proc is not None and proc.poll() is not None:
                        # Child has exited but the thread is still
                        # winding down. Leave it — `_runner` will
                        # set status itself within milliseconds.
                        pass
                    continue

                # Live thread missing → genuine zombie. Snap to
                # stopped so the UI stops lying. If we have an
                # exited child with a non-zero rc on hand, prefer
                # 'errored' so the user knows it didn't shut down
                # cleanly.
                inst = instances_snapshot.get(aid)
                proc = getattr(inst, "_proc", None) if inst else None
                rc = proc.returncode if (proc is not None and proc.poll() is not None) else None
                new_status = "errored" if (rc is not None and rc != 0) else "stopped"
                last_error = f"child exited rc={rc}" if new_status == "errored" else None
                conn.execute(
                    "UPDATE agents SET status=?, last_error=? WHERE id=?",
                    (new_status, last_error, aid),
                )
                logger.warning(
                    "Zombie sweeper: %s status=%s but no live thread — "
                    "reconciling to %s (rc=%s).",
                    aid, row["status"], new_status, rc,
                )
                reconciled.append(aid)
            conn.commit()
        finally:
            conn.close()

        # Also drop stale `_instances` / `_running` entries whose
        # threads have died, so future reads don't confuse the picture.
        with self._lock:
            for aid in list(self._running.keys()):
                if not self._running[aid].is_alive():
                    self._running.pop(aid, None)
                    self._instances.pop(aid, None)

        return reconciled

    def stop(self) -> None:
        """SIGTERM every running agent and wait for threads."""
        # Halt the sweeper first so it doesn't race the teardown.
        self._sweeper_stop.set()
        if self._sweeper_thread is not None:
            self._sweeper_thread.join(timeout=2.0)
            self._sweeper_thread = None

        # Halt the semantic-status engine too.
        if self._semantic_engine is not None:
            try:
                self._semantic_engine.stop()
            except Exception:
                logger.debug("semantic engine stop failed", exc_info=True)
            self._semantic_engine = None

        with self._lock:
            for agent_id, agent in list(self._instances.items()):
                try:
                    agent.stop_flag.set()
                    agent.terminate()
                except Exception:
                    pass

        # Join all threads against ONE shared deadline. They're already
        # terminating in parallel (flags + terminate() set above), so a slow
        # agent doesn't push the others past the supervisor's SIGKILL grace.
        deadline = time.monotonic() + self.SHUTDOWN_JOIN_BUDGET_S
        for agent_id, thread in list(self._running.items()):
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive():
                logger.warning("Agent %s thread did not stop in time", agent_id)

        self._running.clear()
        self._instances.clear()

    # ── Agent CRUD (CLI + API) ───────────────────────────────────

    def create_agent(
        self,
        agent_id: str,
        agent_type: str,
        name: str,
        workspace: str | None = None,
        config: dict[str, Any] | None = None,
        auto_start: bool = False,
        purpose: str = "",
        tags: list[str] | None = None,
        system_prompt: str = "",
        inject_identity_preamble: bool = True,
    ) -> str:
        """Create a new agent spec on disk (YAML) and sync to DB.
        Returns the agent_id. Raises ValueError if `agent_id` violates the
        id rule (CLI prints it; API maps to 400)."""
        agent_id = relaydeck_config.validate_agent_id(agent_id)
        # Reject duplicates — create must never silently overwrite an existing
        # agent's spec/DB row (the YAML is the source of truth). Use
        # `relaydeck agent edit` / PATCH /api/agents/{id} to modify one.
        if (self.config_home / "agents" / f"{agent_id}.yaml").exists():
            ws_hint = ""
            try:
                existing = relaydeck_config.AgentSpec.from_yaml(
                    self.config_home / "agents" / f"{agent_id}.yaml"
                )
                if existing.workspace:
                    ws_hint = f" in workspace {existing.workspace!r}"
            except Exception:
                pass
            raise ValueError(
                f"agent {agent_id!r} already exists{ws_hint} — "
                "pick a different id or open that workspace"
            )
        spec = relaydeck_config.AgentSpec(
            id=agent_id, name=name, type=agent_type,
            workspace=workspace, config=config or {},
            auto_start=auto_start,
            purpose=purpose,
            tags=list(tags or []),
            system_prompt=system_prompt,
            inject_identity_preamble=inject_identity_preamble,
        )
        spec.save(self.config_home)

        conn = open_db(self.db_path)
        try:
            upsert_agent(conn, agent_id, agent_type, name,
                         workspace=workspace, auto_start=auto_start,
                         purpose=purpose, tags=list(tags or []))
        finally:
            conn.close()
        # The moment a workspace becomes multi-agent, ensure `messaging`
        # is enabled so the new (and future) agents get the relaydeck-cli
        # reply-contract skill. Without it an agent RECEIVES `[relay …]`
        # peer messages (the orchestrator injects them regardless) but
        # answers into its own terminal — invisible to the sender, so the
        # reply silently drops. Enabling here, before the agent starts,
        # means it spawns with the skill.
        self._ensure_peer_messaging(workspace)
        return agent_id

    def _ensure_peer_messaging(self, workspace: str | None) -> None:
        """Enable `messaging` on a workspace once it has 2+ agents, so
        co-located peers can actually reply to each other. No-op for a
        solo-agent workspace (nobody to message) or if already enabled."""
        if not workspace:
            return
        try:
            peers = [a for a in self.list_agents()
                     if (a.get("workspace") or None) == workspace]
            if len(peers) < 2:
                return
            ws = next((w for w in relaydeck_config.load_workspace_registry(self.config_home)
                       if w.name == workspace), None)
            current = list(ws.plugins or []) if ws else []
            if "messaging" in current:
                return
            plugins = [*current, "messaging"]
            relaydeck_config.set_workspace_plugins(self.config_home, workspace, plugins)
            # Fire workspace.updated so the skills plugin materializes the
            # relaydeck-cli skill into the workspace.
            self._emit_plugin_event("workspace.updated",
                                    {"name": workspace, "plugins": plugins})
            logger.info("enabled messaging on workspace %r (now multi-agent) "
                        "so peers can reply", workspace)
        except Exception:
            logger.debug("ensure-peer-messaging failed for %r", workspace, exc_info=True)

    def update_agent_meta(
        self,
        agent_id: str,
        *,
        purpose: str | None = None,
        tags: list[str] | None = None,
        system_prompt: str | None = None,
        inject_identity_preamble: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an agent's cross-orchestration meta + prompt config.

        Writes the YAML spec AND the DB row (for the columns the DB
        tracks). `system_prompt` and `inject_identity_preamble` live
        in YAML only — the harness reads them at spawn time, so the
        DB doesn't need to mirror them.

        `config` is shallow-merged into the spec's existing config
        dict so callers can update one key (e.g. `args`) without
        clobbering `model`, `preset`, `command`, or any other
        harness-specific settings.

        Returns the updated DB row (with purpose/tags reflected).
        """
        spec = self._load_spec(agent_id)
        if spec is None:
            raise ValueError(f"Agent {agent_id}: spec not found")
        if purpose is not None:
            spec.purpose = purpose
        if tags is not None:
            spec.tags = list(tags)
        if system_prompt is not None:
            spec.system_prompt = system_prompt
        if inject_identity_preamble is not None:
            spec.inject_identity_preamble = inject_identity_preamble
        if config is not None:
            merged = dict(spec.config or {})
            for k, v in config.items():
                # `null` means "remove this key" so the UI can clear
                # config.args by sending {args: null}.
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            spec.config = merged
        spec.save(self.config_home)

        conn = open_db(self.db_path)
        try:
            tags_json = json.dumps(spec.tags) if spec.tags else None
            conn.execute(
                "UPDATE agents SET purpose = ?, tags = ? WHERE id = ?",
                (spec.purpose or None, tags_json, agent_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ValueError(f"Agent {agent_id} not in DB after update")
        out = _agent_row_to_dict(row)
        # Surface the YAML-only fields too so API callers see one
        # complete payload (matches what /api/agents/<id> would return
        # if we ever choose to merge spec+DB read paths).
        out["system_prompt"] = spec.system_prompt
        out["inject_identity_preamble"] = spec.inject_identity_preamble
        return out

    def start_agent(self, agent_id: str, *, session: str | None = None) -> None:
        """Start an agent by id. Loads spec from YAML, instantiates the
        agent class, and spawns it in a thread.

        `session` is an optional one-shot session intent for this spawn,
        independent of the persisted spec: ``"fresh"`` strips any
        resume/continue flag (clean session); ``"resume"`` ensures the
        harness's "continue last session" flag is applied (keep history).
        ``None`` (default) spawns exactly as configured. The transform only
        touches the spawn `config` copy — no PTY/spawn code changes.

        Idempotent: a no-op if a live thread already exists. Also
        reconciles zombie DB rows (status=running with no live thread)
        by snapping them to 'stopped' BEFORE any other work — so even
        when the spec is missing or the type is unknown, calling
        `start_agent` still cleans up the lying status row instead of
        leaving it as "running" indefinitely."""
        # Reconcile FIRST. If the row says running/starting but no
        # live thread exists in `_running`, snap to stopped. We do
        # this before spec load + type resolution so a corrupt spec
        # or a removed harness plugin still triggers cleanup — the
        # user-visible "relaydeck agent list says running, nothing is"
        # bug is the symptom we're fixing, and it's most painful
        # exactly when something else is also broken.
        with self._lock:
            already_running_here = agent_id in self._running
        if not already_running_here:
            conn = open_db(self.db_path)
            try:
                row = conn.execute(
                    "SELECT status FROM agents WHERE id = ?", (agent_id,),
                ).fetchone()
                if row and (row["status"] or "") in ("running", "starting"):
                    logger.warning(
                        "Agent %s had stale status=%s before start — resetting.",
                        agent_id, row["status"],
                    )
                    conn.execute(
                        "UPDATE agents SET status='stopped' WHERE id = ?",
                        (agent_id,),
                    )
                    conn.commit()
            finally:
                conn.close()

        spec = self._load_spec(agent_id)
        if spec is None:
            raise ValueError(f"Agent {agent_id}: spec not found")

        agent_cls = get_agent_type(spec.type)
        if agent_cls is None:
            raise ValueError(f"Unknown agent type: {spec.type}")

        # Snapshot the spawn-time config ("restart to apply" detection). Done
        # before the lock — it reads skill files — and persisted after the
        # thread launches. Best-effort: a snapshot failure never blocks spawn.
        running_snapshot: dict | None = None
        try:
            from relaydeck import restart_state as _rs
            running_snapshot = _rs.compute_snapshot(
                self.config_home, self.db_path,
                agent_id=agent_id, agent_type=spec.type,
                workspace=spec.workspace,
                purpose=getattr(spec, "purpose", "") or "",
                system_prompt=getattr(spec, "system_prompt", "") or "",
                inject_preamble=bool(getattr(spec, "inject_identity_preamble", True)),
                config=spec.config or {},
                model_override=spec.model_override,
            )
        except Exception:
            logger.debug("running_config snapshot failed for %s", agent_id, exc_info=True)

        with self._lock:
            if agent_id in self._running:
                return  # Already running (raced with another start; safe).

            # The canonical model field is the top-level `model:`
            # (AgentSpec.model_override); the dashboard writes
            # `config.preset`; legacy specs used `config.model`. The
            # harness only reads `config`, so fold model_override in as a
            # preset fallback — otherwise an agent whose model lives only
            # in the top-level field spawns with no `--model`.
            spawn_config = dict(spec.config or {})
            if spec.model_override and not spawn_config.get("preset") and not spawn_config.get("model"):
                spawn_config["preset"] = spec.model_override
            # One-shot session intent (fresh vs. resume) — strips or injects
            # the harness's resume flag on the spawn config copy only. No-op
            # when session is None.
            if session:
                from relaydeck.harness_options import apply_session_mode
                apply_session_mode(spec.type, spawn_config, session)

            agent = agent_cls(
                agent_id=agent_id,
                name=spec.name,
                config=spawn_config,
                workspace=spec.workspace,
                db_path=self.db_path,
                stop_flag=threading.Event(),
            )

            def _runner():
                try:
                    agent.update_status("running")
                    self._emit_plugin_event("agent.start", {
                        "agent_id": agent_id, "type": spec.type, "name": spec.name,
                    })
                    agent.run()
                    # run() sets "errored" + a reason on a failed spawn (e.g. a
                    # missing harness CLI: "command not found: pi"). Don't clobber
                    # that diagnostic with "stopped" — preserve why it failed.
                    if getattr(agent, "_last_status", None) == "errored":
                        self._emit_plugin_event("agent.error", {
                            "agent_id": agent_id, "type": spec.type,
                        })
                    else:
                        agent.update_status("stopped")
                        self._emit_plugin_event("agent.stop", {
                            "agent_id": agent_id, "type": spec.type,
                        })
                except Exception as exc:
                    logger.exception("Agent %s crashed", agent_id)
                    agent.update_status("errored", str(exc))
                    self._emit_plugin_event("agent.error", {
                        "agent_id": agent_id, "type": spec.type, "error": str(exc),
                    })
                finally:
                    # Snapshot the last screen BEFORE tearing the instance
                    # down, so a crashed agent's output is recoverable (opt-in).
                    self._persist_transcript(agent_id, agent)
                    with self._lock:
                        self._running.pop(agent_id, None)
                        self._instances.pop(agent_id, None)

            thread = threading.Thread(
                target=_runner, name=f"relaydeck-agent-{agent_id}", daemon=True,
            )
            self._running[agent_id] = thread
            self._instances[agent_id] = agent
            thread.start()
            logger.info("Started agent: %s (%s)", agent_id, spec.type)

            # Persist the spawn-time config snapshot so a later config edit
            # surfaces as "restart to apply" against THIS running process.
            if running_snapshot is not None:
                try:
                    _c = open_db(self.db_path)
                    try:
                        _c.execute(
                            "UPDATE agents SET running_config = ? WHERE id = ?",
                            (json.dumps(running_snapshot), agent_id),
                        )
                        _c.commit()
                    finally:
                        _c.close()
                except Exception:
                    logger.debug("persist running_config failed for %s", agent_id, exc_info=True)

        # Verify the agent actually came up. Without this, `relaydeck agent
        # start` returned "✓ Agent started" the moment the thread was
        # launched — before the harness opened its PTY or Popen'd the
        # child. If pi/codex failed to start (binary missing, immediate
        # crash, denied perms) the user saw success and then wondered
        # why messages never landed. We poll briefly for either:
        #   - happy path: `_master_fd` is set AND `_proc.poll()` is None
        #     (PTY open, child alive) → return cleanly
        #   - sad path: thread died OR `_proc.poll()` returned non-None
        #     → raise so the CLI prints the real reason
        self._wait_for_start(agent_id, agent, thread)

    def _wait_for_start(
        self,
        agent_id: str,
        agent: BaseAgent,
        thread: threading.Thread,
    ) -> None:
        """Block briefly until the just-spawned agent has either
        confirmed a healthy PTY or hit an error. Raises RuntimeError
        if the agent failed to start. Bounded by `START_VERIFY_TIMEOUT`.
        """
        import time as _time
        deadline = _time.monotonic() + self.START_VERIFY_TIMEOUT
        while _time.monotonic() < deadline:
            if not thread.is_alive():
                # Runner already exited. `_runner`'s ordering is
                # update_status(...) → pop _instances → return. So
                # by the time the thread is dead, the DB has been
                # updated. But there's still a tiny window between
                # `thread.is_alive()` returning False and the
                # finally block having committed: join with a short
                # timeout to force the cleanup to be visible.
                thread.join(timeout=0.5)
                cur = self.get_agent(agent_id) or {}
                status = cur.get("status") or "unknown"
                err = cur.get("last_error")
                if status == "errored":
                    raise RuntimeError(
                        f"Agent {agent_id} failed to start: "
                        f"{err or 'errored before PTY was ready'}"
                    )
                if status == "stopped":
                    # Thread exited cleanly without errored: the
                    # child likely Popen'd, started, and exited
                    # before our verify window closed (e.g. an
                    # agent type whose `run()` returns immediately).
                    # Not a "start failure" per se; the caller
                    # asked to start and we did.
                    return
                # Status is still 'running' or 'starting' but the
                # thread has died and join confirmed it. That's
                # itself a regression — log and raise so it's
                # visible instead of papered over.
                raise RuntimeError(
                    f"Agent {agent_id} thread exited but DB still says "
                    f"status={status!r} — internal inconsistency."
                )

            proc = getattr(agent, "_proc", None)
            master_fd = getattr(agent, "_master_fd", None)
            if proc is not None:
                rc = proc.poll()
                if rc is not None and rc != 0:
                    # Child exited fast with non-zero — common case is
                    # missing binary / denied permission. Let the
                    # runner's except path mark the DB; just surface
                    # the error to the caller.
                    raise RuntimeError(
                        f"Agent {agent_id} child exited rc={rc} during start "
                        f"(check binary path and harness config)"
                    )
                if rc is None and master_fd is not None:
                    return  # healthy: child running, PTY open
            _time.sleep(0.05)
        # Timeout: thread is alive but neither healthy nor errored —
        # assume slow startup, let the caller proceed. The sweeper
        # will reconcile if things go bad later.

    def get_running_instance(self, agent_id: str) -> BaseAgent | None:
        """Return the live `BaseAgent` instance if the agent is running.

        Used by the terminal WebSocket to reach `subscribe_pty()` /
        `send_input()` / `resize()` on harness agents.
        """
        with self._lock:
            return self._instances.get(agent_id)

    def list_running_instances(self) -> dict[str, BaseAgent]:
        """Snapshot `{agent_id: instance}` of every live agent. Used by the
        semantic-status engine to sample each running agent's rendered screen.
        Returns a copy so the caller can iterate without holding the lock."""
        with self._lock:
            return dict(self._instances)

    def mark_agent_viewed(self, agent_id: str) -> bool:
        """Clear a `complete-unread` agent once the operator has looked at it —
        the read-transition for the semantic-status state machine.

        Idempotent and narrow: only `complete-unread` (the "result waiting to be
        read" state) is collapsed to `idle`, tagged `source="viewer"`. Working /
        awaiting-input / already-idle agents are left untouched, so viewing a
        busy agent never masks what it's doing. Returns True iff it changed."""
        from relaydeck.db import get_semantic_status, open_db
        conn = open_db(self.db_path)
        try:
            status, _at, _src = get_semantic_status(conn, agent_id)
        finally:
            conn.close()
        if status != "complete-unread":
            return False
        return self.set_semantic_status(agent_id, "idle", source="viewer")

    def reset_agent_session(self, agent_id: str) -> str:
        """Start a fresh session for an agent. If the running harness instance
        supports an IN-PLACE reset (`HarnessAgent.reset_session()` returns
        True), use it — no process churn. Otherwise restart the agent
        (stop + start), which gives every PTY harness a clean session.

        Returns the method used: ``"in-place"`` | ``"restart"``. Drives
        Telegram's `/new` / `/clear` so an operator can begin a fresh
        conversation from chat without touching the dashboard.
        """
        with self._lock:
            inst = self._instances.get(agent_id)
        reset = getattr(inst, "reset_session", None)
        if callable(reset):
            try:
                if reset():
                    return "in-place"
            except Exception as exc:
                logger.warning(
                    "reset_agent_session in-place failed for %s: %s — restarting",
                    agent_id, exc,
                )
        # Universal fallback: respawn with a FRESH session intent so the
        # harness comes up clean even if it was launched with --continue
        # (a plain restart would re-apply the resume flag and resume instead).
        try:
            self.stop_agent(agent_id)
        except Exception as exc:
            logger.warning("reset_agent_session stop failed for %s: %s", agent_id, exc)
        self.start_agent(agent_id, session="fresh")
        return "restart"

    def compact_agent(self, agent_id: str) -> dict[str, Any]:
        """Ask a running agent to compact its context IN PLACE via the
        harness's compaction command (KV-safer than a fresh session — the
        prefix is summarized, not discarded). Returns a status dict:
          {"ok": bool, "reason": "sent"|"not-running"|"unsupported"|"send-failed",
           "command": str}.
        `unsupported` means the harness has no known in-place compaction; the
        caller should fall back to `agent result` + `reset_agent_session`.
        Announces `agent.compacted` on success for the audit feed."""
        with self._lock:
            inst = self._instances.get(agent_id)
        if inst is None or not callable(getattr(inst, "compact", None)):
            return {"ok": False, "reason": "not-running", "command": ""}
        cmd = getattr(inst, "COMPACT_COMMAND", "") or ""
        if not cmd:
            return {"ok": False, "reason": "unsupported", "command": ""}
        ok = False
        try:
            ok = bool(inst.compact())
        except Exception as exc:
            logger.debug("compact_agent failed for %s: %s", agent_id, exc)
        if ok:
            self.emit_event(agent_id, "agent.compacted", {"command": cmd})
        return {"ok": ok, "reason": "sent" if ok else "send-failed", "command": cmd}

    def escalate_agent(self, agent_id: str, message: str = "") -> bool:
        """Raise a human escalation for an agent on demand — the one-command
        version of what hitl emits automatically. Publishes `hitl.escalation`
        (kind="manual") so every channel plugin (telegram/web/…) delivers it to
        a human, and it rides the hitl.* SSE bridge to the dashboard feed too.
        The move after an `autopilot.held` or a context-critical alert when you
        want a person, not a policy. Returns True if the event was emitted."""
        rec = self.get_agent(agent_id)
        workspace = (rec or {}).get("workspace") if isinstance(rec, dict) else None
        payload = {
            "agent_id": agent_id,
            "workspace": workspace,
            "kind": "manual",
            "message": message or "manual escalation requested",
            "stopped": False,
            "respond_hint": f'relaydeck workspace message "<reply>" --agent {agent_id}',
        }
        try:
            if getattr(self, "_plugin_event_bus", None) is not None:
                self._emit_plugin_event("hitl.escalation", payload)
            else:
                self.emit_event(agent_id, "hitl.escalation", payload)
            return True
        except Exception as exc:
            logger.debug("escalate_agent failed for %s: %s", agent_id, exc)
            return False

    def _transcript_path(self, agent_id: str) -> Path:
        return self.config_home / "runtime" / "transcripts" / f"{agent_id}.txt"

    def _persist_transcript(self, agent_id: str, agent: BaseAgent) -> None:
        """Snapshot the tail of an exiting agent's PTY buffer to disk so its
        last screen survives the process — the recovery companion to
        `agent result` for crashes. Opt-in + bounded via the
        `RELAYDECK_TRANSCRIPT_BYTES` env var (0 / unset = off). Best-effort:
        never let a snapshot failure affect teardown, and only a READ of the
        existing buffer (never touches the PTY loop)."""
        try:
            limit = int(os.environ.get("RELAYDECK_TRANSCRIPT_BYTES", "0") or 0)
        except ValueError:
            limit = 0
        if limit <= 0:
            return
        getter = getattr(agent, "get_pty_buffer", None)
        if not callable(getter):
            return
        try:
            buf = bytes(getter() or b"")
            if not buf:
                return
            text = buf[-limit:].decode("utf-8", "replace")
            path = self._transcript_path(agent_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except Exception:
            logger.debug("persist transcript failed for %s", agent_id, exc_info=True)

    def get_transcript(self, agent_id: str) -> str | None:
        """Return the persisted last-screen transcript for an exited agent, or
        None if none was captured (feature off, or agent never ran/exited)."""
        try:
            path = self._transcript_path(agent_id)
            return path.read_text() if path.is_file() else None
        except Exception:
            return None

    def restart_agent(self, agent_id: str, *, session: str | None = "resume") -> None:
        """Restart an agent's PTY (stop + start), keeping its conversation by
        default (`session="resume"` ensures the harness's continue flag is
        applied). Pass `session=None` to restart exactly as configured, or
        `"fresh"` for a clean session (prefer `reset_agent_session` for that).
        Backs Telegram's `/restart`."""
        try:
            self.stop_agent(agent_id)
        except Exception as exc:
            logger.warning("restart_agent stop failed for %s: %s", agent_id, exc)
        self.start_agent(agent_id, session=session)

    def stop_agent(self, agent_id: str) -> bool:
        """Stop a running agent. Returns True if a live instance was
        terminated, False if there was nothing running to stop (the
        DB row is still reconciled to `stopped` in either case so the
        UI stays honest).

        Idempotent + reconciling: if the agent isn't in `_instances`
        but the DB row still says `running`/`starting`, we force the
        row to `stopped` so the UI stops lying. The earlier version
        returned early on "no instance" and left zombie status rows
        in the DB, which then masqueraded as live agents and silently
        swallowed messages.

        Returning bool also lets `host.agents.stop()` in the SDK
        propagate a meaningful answer to plugins — previously it
        always returned False because we returned None.
        """
        with self._lock:
            agent = self._instances.get(agent_id)
            thread = self._running.get(agent_id)

        stopped_live = agent is not None
        if agent is not None:
            agent.stop_flag.set()
            agent.terminate()
            if thread is not None:
                thread.join(timeout=10.0)
                if thread.is_alive():
                    logger.warning(
                        "Agent %s thread did not stop within 10s — forcing "
                        "DB status='stopped' and dropping references.",
                        agent_id,
                    )

        with self._lock:
            self._running.pop(agent_id, None)
            self._instances.pop(agent_id, None)

        # Always end with the DB reflecting reality. Handles the case
        # where the thread crashed without running its `finally` block,
        # or where we called stop_agent on a row that was already
        # zombie (status=running but no live instance).
        conn = open_db(self.db_path)
        try:
            conn.execute(
                "UPDATE agents SET status='stopped' WHERE id = ? "
                "AND status IN ('running','starting','errored')",
                (agent_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return stopped_live

    def trigger_loop_tick(self, agent_id: str) -> bool:
        """Run-now for a loop automation: fire one immediate tick on the
        LIVE agent instance, off the request thread so a slow action
        (model call / subprocess) doesn't block the caller. Returns True
        if a tick was dispatched, False if the agent isn't running here.
        Raises ValueError if the agent is live but isn't a loop
        automation (no `trigger_now`)."""
        with self._lock:
            agent = self._instances.get(agent_id)
        if agent is None:
            return False
        fn = getattr(agent, "trigger_now", None)
        if fn is None:
            raise ValueError(f"agent {agent_id!r} is not a loop automation")
        threading.Thread(
            target=fn, daemon=True, name=f"manual-tick:{agent_id}"
        ).start()
        return True

    def invalidate_relaydeck_native_spawn_caches(self) -> list[str]:
        """Drop memoized pi argv/env bundles after vault provider-key changes.

        auth.json is updated immediately, but a long-lived PTY child only
        re-reads credentials on agent restart — clearing the cache ensures
        the next HTTP chat turn rebuilds argv with fresh env injection.
        The PTY pi process inherits env at spawn time though, so an
        already-running agent needs an explicit restart to pick up the
        rotated key. Returns the list of running relaydeck-native agent
        ids that have stale env so callers (vault.py) can emit a
        `relaydeck-native.credentials_rotated` event with a "Restart
        affected agents" affordance in the dashboard."""
        stale: list[str] = []
        with self._lock:
            for agent_id, agent in self._instances.items():
                if hasattr(agent, "_pi_spawn"):
                    agent._pi_spawn = None
                    # A running PTY captured the old env at spawn time.
                    status = getattr(agent, "status", None)
                    if status == "running":
                        stale.append(agent_id)
        return stale

    def list_agents(self) -> list[dict[str, Any]]:
        """Return all agents with their current status, including
        cross-orchestration meta (purpose, tags) and the semantic
        (observable) state set by vendor hooks or the semantic-status engine."""
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, type, name, status, workspace, auto_start, "
                "created_at, last_active_at, purpose, tags, "
                "semantic_status, semantic_status_at, semantic_status_source, "
                "running_config "
                "FROM agents ORDER BY created_at DESC"
            ).fetchall()
            return [_agent_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def set_semantic_status(
        self,
        agent_id: str,
        status: str | None,
        *,
        source: str = "hook",
    ) -> bool:
        """Set an agent's semantic state. Returns True iff the new
        value differs from the prior one — callers (the API
        endpoint, the semantic-status engine) use that to decide whether to
        emit `agent.status_changed` on the plugin bus.

        Internal `daemon`-source updates (lifecycle transitions
        from the orchestrator itself) ride the same code path so
        future "agent crashed → semantic_status=idle" wiring stays
        consistent.
        """
        from relaydeck.db import update_semantic_status
        conn = open_db(self.db_path)
        try:
            prior = update_semantic_status(conn, agent_id, status, source=source)
        finally:
            conn.close()
        changed = prior != status
        if changed and self._plugin_event_bus is not None:
            try:
                from relaydeck.plugin import Event
                self._plugin_event_bus.emit(Event(
                    type="agent.status_changed",
                    data={
                        "agent_id": agent_id,
                        "from": prior,
                        "to": status,
                        "source": source,
                    },
                    source_plugin="orchestrator",
                ))
            except Exception as exc:
                logger.debug("status_changed emit failed for %s: %s", agent_id, exc)
        return changed

    def find_agents(
        self,
        *,
        tag: str | None = None,
        purpose: str | None = None,
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Discover agents by metadata, matching the public API contract."""
        import re

        agents = self.list_agents()
        if workspace:
            agents = [a for a in agents if (a.get("workspace") or "") == workspace]
        if tag:
            agents = [a for a in agents if tag in (a.get("tags") or [])]
        if purpose:
            try:
                pat = re.compile(purpose, re.IGNORECASE)
                agents = [a for a in agents if pat.search(a.get("purpose") or "")]
            except re.error:
                needle = purpose.lower()
                agents = [a for a in agents if needle in (a.get("purpose") or "").lower()]
        return agents

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """Get a single agent's current state, merged with YAML-only
        fields (system_prompt, inject_identity_preamble, config) so the
        caller sees one complete picture without a second roundtrip."""
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        out = _agent_row_to_dict(row)
        spec = self._load_spec(agent_id)
        if spec is not None:
            out["system_prompt"] = spec.system_prompt
            out["inject_identity_preamble"] = spec.inject_identity_preamble
            out["config"] = spec.config
            # purpose/tags live in both places — YAML wins (source of truth)
            out["purpose"] = spec.purpose
            out["tags"] = list(spec.tags)
        else:
            out.setdefault("system_prompt", "")
            out.setdefault("inject_identity_preamble", True)
            out.setdefault("config", {})
        return out

    def delete_agent(self, agent_id: str, *, purge_history: bool = True) -> bool:
        """Delete an agent thoroughly.

        Always: stop the process, remove the YAML spec + the `agents` DB
        row, and clean up the agent's per-agent runtime files (pi/codex/
        opencode homes, fleet-context, prompt addendum) — those are pure
        leaks and a stale-state-on-id-reuse hazard.

        When `purge_history` (default True): also remove the agent's
        historical DB rows (events, usage_records, model_invocations,
        tasks, agent_messages, automation_runs). Pass False to keep that
        history for audit. Emits `agent.deleted` so the dashboard + plugins
        react in real time.
        """
        # Capture the spec BEFORE unlinking — we need its workspace to
        # locate the per-agent runtime files.
        spec = self._load_spec(agent_id)
        workspace = spec.workspace if spec else None

        self.stop_agent(agent_id)

        # Remove YAML spec
        spec_path = self.config_home / "agents" / f"{agent_id}.yaml"
        if spec_path.exists():
            spec_path.unlink()

        # Per-agent runtime files (always — pure leaks)
        self._purge_agent_files(agent_id, workspace)

        # DB rows
        conn = open_db(self.db_path)
        try:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            if purge_history:
                for sql in (
                    "DELETE FROM events WHERE agent_id = ?",
                    "DELETE FROM usage_records WHERE agent_id = ?",
                    "DELETE FROM model_invocations WHERE agent_id = ?",
                    "DELETE FROM tasks WHERE agent_id = ?",
                    "DELETE FROM automation_runs WHERE automation_id = ?",
                ):
                    try:
                        conn.execute(sql, (agent_id,))
                    except sqlite3.OperationalError:
                        pass  # table may not exist on older DBs
                try:
                    conn.execute(
                        "DELETE FROM agent_messages WHERE from_id = ? OR to_id = ?",
                        (agent_id, agent_id),
                    )
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()

        self._emit_plugin_event("agent.deleted", {
            "agent_id": agent_id, "workspace": workspace,
            "purged_history": bool(purge_history),
        })
        return True

    def _purge_agent_files(self, agent_id: str, workspace: str | None) -> None:
        """Remove the per-agent runtime artifacts created by the harnesses,
        plus the agent's drag-drop upload dir under config_home.
        Guarded so a malformed id can never escape its parent dir
        (`agent_id` must be a single safe path component)."""
        import shutil

        if (not agent_id or "/" in agent_id or "\\" in agent_id
                or agent_id in (".", "..")):
            return
        rt = ((self.config_home / "workspaces" / workspace / "runtime")
              if workspace else (self.config_home / "runtime"))
        dirs = [
            rt / "pi-sessions" / agent_id,
            rt / "codex-homes" / agent_id,
            rt / "opencode-homes" / agent_id,
            # Drag-drop image uploads live under config_home (not the
            # workspace runtime) — they're keyed by agent id and a pure
            # leak once the agent is gone.
            self.config_home / "uploads" / agent_id,
        ]
        files = [rt / "fleet-context" / f"{agent_id}.md"]
        if workspace:
            files.append(
                self.config_home / "workspaces" / workspace / "prompts" / f"{agent_id}.md"
            )
        for d in dirs:
            try:
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        for f in files:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass

    def get_events(self, agent_id: str, since_id: int = 0, limit: int = 100) -> list[dict]:
        """Return recent events for an agent from the DB."""
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, type, payload, ts FROM events "
                "WHERE agent_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
                (agent_id, since_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            conn.close()

    def subscribe_events(self, agent_id: str) -> queue.Queue:
        """Get a queue that will receive live events for agent_id.
        Use in SSE generators for real-time streaming."""
        return _bus.subscribe(agent_id)

    def unsubscribe_events(self, agent_id: str, q: queue.Queue) -> None:
        _bus.unsubscribe(agent_id, q)

    def subscribe_events_async(self, agent_id: str) -> "_AsyncSub":
        """Async-native event subscription for SSE generators. Unlike
        `subscribe_events` (a thread queue drained via a pool thread), this
        costs no thread while idle — the scalable path for many concurrent
        dashboard / `view` / `events tail -f` viewers."""
        return _bus.subscribe_async(agent_id)

    def unsubscribe_events_async(self, agent_id: str, sub: "_AsyncSub") -> None:
        _bus.unsubscribe_async(agent_id, sub)

    def emit_event(
        self, agent_id: str, event_type: str, payload: dict | None = None
    ) -> int:
        """Persist a custom event and publish it on the live bus.

        The operator-facing twin of `BaseAgent.emit`: writes one row to the
        `events` table (so `agent events` + history replay see it) and fans
        it out to SSE subscribers via `_bus` — both the per-`agent_id`
        stream and the `"*"` broadcast stream the dashboard and `view` TUI
        watch. Backs `POST /api/events/emit` so `relaydeck broadcast` /
        `relaydeck events emit` land on the exact stream agents already
        produce. `agent_id` is just the emitter label (the API defaults it
        to `"operator"`); it need not be a registered agent — a session row
        is ensured so the FK holds either way.
        """
        from relaydeck.db import ensure_session, log_event
        conn = open_db(self.db_path)
        try:
            ensure_session(conn, agent_id)
            ev_id = log_event(conn, agent_id, event_type, payload or {}, agent_id=agent_id)
        finally:
            conn.close()
        try:
            _bus.publish(agent_id, event_type, payload or {}, ev_id)
        except Exception:
            pass
        return ev_id

    # ── Structured results (durable hand-back) ──────────────────

    def put_result(
        self, agent_id: str, body: str, *, key: str = "", status: str = "ok",
        summary: str = "", workspace: str | None = None,
    ) -> int:
        """Persist a structured result an agent hands back (latest-wins per
        (agent_id, key)) and announce it as an `agent.result` event so the
        dashboard / `view` / `agent wait` see it land. Survives the agent's
        own crash — the durable answer to "collect results"."""
        from relaydeck.db import put_result as _put
        if workspace is None:
            rec = self.get_agent(agent_id)
            workspace = (rec or {}).get("workspace") if isinstance(rec, dict) else None
        conn = open_db(self.db_path)
        try:
            rid = _put(conn, agent_id, body, key=key, status=status,
                       summary=summary, workspace=workspace)
        finally:
            conn.close()
        self.emit_event(agent_id, "agent.result", {
            "result_id": rid, "key": key, "status": status,
            "summary": summary, "workspace": workspace,
        })
        return rid

    def get_results(
        self, agent_id: str, *, key: str | None = None,
        latest: bool = True, limit: int = 50,
    ) -> list[dict]:
        from relaydeck.db import get_results as _get
        conn = open_db(self.db_path)
        try:
            return _get(conn, agent_id, key=key, latest=latest, limit=limit)
        finally:
            conn.close()

    # ── Agent-to-agent messaging ────────────────────────────────

    def send_message_to(
        self,
        to_id: str,
        body: str,
        *,
        from_id: str = "user",
        in_reply_to: str | None = None,
        broadcast_id: str | None = None,
        format: str | None = None,
        plugin_default_format: str | None = None,
    ) -> tuple[str, bool]:
        """Persist a message to `to_id`'s inbox, emit `agent.message`
        on the plugin event bus, and best-effort push to the agent's
        live PTY if it's running.

        Returns `(msg_id, injected_now)`. `injected_now=False` when the
        target is stopped (message persists and drains on next start)
        or when the PTY write failed.

        Caller is responsible for permission checks (e.g. workspace
        scoping); this method delivers anywhere by `agent_id`.
        """
        from relaydeck.messages import (
            insert_message, list_one, mark_injected,
            record_delivery_failure, render_format,
        )

        agent = self.get_agent(to_id)
        if agent is None:
            raise ValueError(f"No such agent: {to_id}")

        # Zombie reconcile (mid-flight). If the DB row claims `running`
        # but the orchestrator has no live instance, the previous thread
        # exited without cleanly updating status (crash, daemon restart
        # gap, race). Snap the DB back to 'stopped' so `relaydeck agent list`
        # stops lying — and so the user knows they need to start it
        # again. Done HERE because send is the most common "I thought
        # it was running" trigger; the boot-time reconcile in start()
        # covers the daemon-restart case.
        instance = self.get_running_instance(to_id)
        if (
            instance is None
            and (agent.get("status") or "") in ("running", "starting")
        ):
            logger.warning(
                "Agent %s shows status=%s but has no live instance — "
                "reconciling to stopped.", to_id, agent.get("status"),
            )
            conn = open_db(self.db_path)
            try:
                conn.execute(
                    "UPDATE agents SET status='stopped' WHERE id = ?",
                    (to_id,),
                )
                conn.commit()
            finally:
                conn.close()
            agent["status"] = "stopped"

        msg_id = insert_message(
            from_id, to_id, body,
            in_reply_to=in_reply_to,
            workspace=agent.get("workspace"),
            broadcast_id=broadcast_id,
            db_path=self.db_path,
        )

        msg = list_one(msg_id, db_path=self.db_path)
        if msg is None:
            # Insert just succeeded; this is genuinely unreachable.
            raise RuntimeError(f"Message {msg_id} vanished after insert")

        # Surface to plugin subscribers (audit logger, loop agent, etc.)
        injected = False
        spec = self._load_spec(to_id)
        agent_config = spec.config if spec else None

        # Readiness gate for the live push, mirroring the harness drain:
        # an instance that's up but hasn't emitted any PTY output yet has
        # a TUI that isn't ready to accept input (startup race). Pushing
        # into a cold input widget is exactly the silent-loss case — so we
        # skip the live push and leave the message queued. The harness's
        # readiness-gated drain delivers it once the TUI comes up.
        # getattr default True keeps non-harness instances (and test
        # fakes) on the normal push path.
        if (
            instance is not None
            and hasattr(instance, "send_message")
            and getattr(instance, "_seen_output", True)
        ):
            text = render_format(
                msg,
                format=format,
                agent_config=agent_config,
                plugin_default=plugin_default_format,
            )
            send_err: str | None = None
            try:
                ok = bool(instance.send_message(text))
            except Exception as exc:
                logger.warning("send_message to %s failed: %s", to_id, exc)
                ok = False
                send_err = f"send raised: {exc}"
            # Echo-confirm (opt-in): a successful PTY write only means the
            # bytes reached the terminal. When confirmation is enabled,
            # require the harness to echo the message id before marking it
            # delivered; otherwise leave it queued so the harness's
            # live-drain retries instead of silently losing it.
            #
            # Sound here because this runs on the caller's thread (HTTP
            # handler / worker), NOT the target's PTY read loop — so the
            # read loop keeps filling the ring buffer while we poll it for
            # the echo. The same check inside the read-loop drain would
            # deadlock; that's why the drain stays optimistic.
            if ok:
                _confirm_enabled = getattr(instance, "_confirm_delivery_enabled", None)
                _verify_echo = getattr(instance, "_confirm_echo", None)
                if (callable(_confirm_enabled) and callable(_verify_echo)
                        and _confirm_enabled() and not _verify_echo(msg_id)):
                    ok = False
                    send_err = "echo not observed within confirm window"
            if ok:
                mark_injected(msg_id, rendered_body=text, db_path=self.db_path)
                injected = True
            else:
                # Record the failure so attempts tick up and the message
                # eventually transitions to terminal `failed` instead of
                # being retried silently on every harness restart.
                record_delivery_failure(
                    msg_id,
                    send_err or "send_message returned False",
                    db_path=self.db_path,
                )

        # Emit AFTER the inject attempt so subscribers see the final state.
        if self._plugin_event_bus is not None:
            try:
                payload = msg.to_dict()
                payload["injected"] = injected
                if injected:
                    payload["injected_at"] = time.time()
                from relaydeck.plugin import Event
                self._plugin_event_bus.emit(Event(
                    type="agent.message", data=payload, source_plugin="orchestrator",
                ))
            except Exception as exc:
                logger.debug("plugin-bus emit failed for agent.message: %s", exc)

        return msg_id, injected

    # ── Internal ─────────────────────────────────────────────────

    def _load_spec(self, agent_id: str) -> relaydeck_config.AgentSpec | None:
        spec_path = self.config_home / "agents" / f"{agent_id}.yaml"
        if not spec_path.exists():
            return None
        return relaydeck_config.AgentSpec.from_yaml(spec_path)


def _agent_row_to_dict(row) -> dict[str, Any]:
    """Materialize a sqlite Row into a JSON-ready dict, decoding the
    `tags` JSON-encoded list and normalizing `purpose`/`tags` defaults."""
    d = dict(row)
    raw_tags = d.get("tags")
    if isinstance(raw_tags, str) and raw_tags:
        try:
            parsed = json.loads(raw_tags)
            d["tags"] = parsed if isinstance(parsed, list) else []
        except Exception:
            d["tags"] = []
    elif raw_tags is None:
        d["tags"] = []
    if d.get("purpose") is None:
        d["purpose"] = ""
    raw_rc = d.get("running_config")
    if isinstance(raw_rc, str) and raw_rc:
        try:
            d["running_config"] = json.loads(raw_rc)
        except Exception:
            d["running_config"] = None
    # A dead process can't be actively "working" or "awaiting-input" —
    # those are live-only semantic states. If the process exited while
    # one was set (and the row wasn't reconciled), it's stale: drop it
    # so the UI doesn't show a green "working" dot on a stopped agent.
    # Quiescent states (complete-unread / idle) remain valid after stop
    # and are kept. `pending` is a transient startup state — left alone.
    if d.get("status") in ("stopped", "errored") \
            and d.get("semantic_status") in ("working", "awaiting-input"):
        d["semantic_status"] = None
    return d


# Module-level singleton (lazy)
_orchestrator: Orchestrator | None = None


def get_orchestrator(config_home: Path | None = None) -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator(config_home)
    return _orchestrator
