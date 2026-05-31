"""
Cross-harness semantic-status engine.

`semantic_status` (working | awaiting-input | complete-unread | idle) tells an
operator what an agent is *actually doing*, orthogonal to the process-level
`status` (running/stopped). Historically only Claude Code produced it — via a
vendor hook that POSTs to `/api/agents/{id}/state`. Every other harness relied
on a "mood classifier" that lived in the `emote` plugin, which was removed — so
pi / codex / cursor / opencode / antigravity produced **no** semantic status at
all, and the `complete-unread` state had no producer or read-transition anywhere.

This engine fixes that with one signal **every** harness has: its rendered
screen. The daemon already renders any agent's PTY to plain text on demand
(`relaydeck agent screen`, via `screen.render(get_pty_buffer())`). We poll that
read-only — never touching the PTY read loop, `_broadcast`, or `send_input` (the
"terminal is untouchable" contract) — and derive status from how the screen
behaves:

  - the rendered screen is **changing** (tokens streaming, a work spinner
    animating)  → `working`. Identical redraws of a static prompt hash the same,
    so a constantly-refreshing TUI that isn't doing anything reads as quiet.
  - it **settles** (no change for `SETTLE_S`) after a real work burst →
    `complete-unread` (a result is waiting); the first settle after spawn is the
    `idle` baseline, not a phantom completion.
  - the harness declares its screen shows a blocked-on-operator prompt
    (`HarnessAgent.detect_awaiting_input`) → `awaiting-input`.
  - the operator views the agent → `idle` (the read-transition, wired from the
    web/CLI; see `Orchestrator.mark_agent_viewed`).

Arbitration keeps producers from fighting: when an agent's status was last set by
an **authoritative** external source (a vendor `hook`, `hitl`, or a `manual`
override) and that signal is **fresh**, the engine stays out of the way — so
Claude Code remains hook-driven exactly as before. The engine reclaims a harness
only when it has no authoritative producer (every hookless harness) or when the
authoritative one has gone **silent for `STALE_S`** — which also turns a Claude
hook that died mid-session from "stuck forever" into "self-heals in under a
minute".

The state-machine core (`derive_status`), the prompt matcher
(`screen_matches_any`), and the arbitration rule (`may_write`) are pure
functions so they can be unit-tested without a PTY or a daemon.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relaydeck.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# How often to sample each running agent's rendered screen. A render runs the
# full pyte emulation, so this is deliberately ~1Hz, not tighter.
POLL_INTERVAL_S: float = 1.5
# Quiet duration (no screen change) after which a working agent is considered
# settled. Above the typical inter-token gap so streaming output doesn't
# flap working<->settled.
SETTLE_S: float = 4.0
# A working burst shorter than this doesn't count as a "completion" worth a
# complete-unread badge — it's spinner noise, not a finished turn.
MIN_WORK_S: float = 2.0
# After this long with no fresh update from an authoritative producer (a vendor
# hook / hitl / manual), the engine reclaims the agent. Turns a silently-dead
# hook from "stuck forever" into "self-heals".
STALE_S: float = 45.0
# The provenance tag the engine stamps on the statuses it writes.
ENGINE_SOURCE: str = "engine"
# External producers the engine defers to while their signal is fresh.
AUTHORITATIVE_SOURCES: frozenset[str] = frozenset({"hook", "hitl", "manual"})


def screen_matches_any(screen: str, patterns: tuple[str, ...]) -> bool:
    """True if any regex in `patterns` matches the tail of `screen`
    (case-insensitive). Matching the last few non-empty lines — where a prompt
    lives — instead of the whole scrollback keeps an 'approve?' echoed earlier
    in the transcript from tripping a false awaiting-input."""
    if not screen or not patterns:
        return False
    lines = [ln for ln in screen.splitlines() if ln.strip()]
    tail = "\n".join(lines[-12:])
    for pat in patterns:
        try:
            if re.search(pat, tail, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def may_write(
    source: str | None, at: float | None, now: float, *, stale_s: float = STALE_S
) -> bool:
    """Arbitration: may the engine overwrite the current status?

    No when an authoritative external producer set it and the signal is still
    fresh — that keeps the engine from fighting Claude Code's vendor hook (or
    hitl / a manual override). Yes otherwise: no producer, the engine's own
    prior write, a `viewer` read-transition, or an authoritative source that has
    gone stale (the self-heal path)."""
    if source in AUTHORITATIVE_SOURCES and (now - (at or 0.0)) < stale_s:
        return False
    return True


@dataclass
class _Track:
    """Per-agent engine state between polls."""

    last_hash: str | None = None
    last_change: float = 0.0
    phase: str = "init"  # init | working | settled | awaiting
    work_started: float = 0.0
    baseline_done: bool = False


def derive_status(
    track: _Track,
    *,
    screen_hash: str,
    awaiting: bool,
    now: float,
    settle_s: float = SETTLE_S,
    min_work_s: float = MIN_WORK_S,
) -> str | None:
    """Pure state machine. Mutates `track` and returns the status to WRITE on a
    transition edge, or None when nothing changed this tick.

    Edges only — steady state returns None — so the engine emits
    `agent.status_changed` once per real transition, never as a heartbeat."""
    first = track.last_hash is None
    changed = (not first) and (screen_hash != track.last_hash)
    track.last_hash = screen_hash
    if first or changed:
        track.last_change = now

    # awaiting-input is level-triggered (the prompt sits on screen), but we
    # emit only when entering it.
    if awaiting:
        if track.phase != "awaiting":
            track.phase = "awaiting"
            return "awaiting-input"
        return None

    if changed:
        # The screen is actively repainting with new content → working.
        if track.phase != "working":
            track.phase = "working"
            track.work_started = now
            return "working"
        return None

    # No change this tick. Has it been quiet long enough to be settled?
    if now - track.last_change < settle_s:
        return None

    if track.phase in ("working", "awaiting"):
        worked = (
            track.phase == "working"
            and (track.last_change - track.work_started) >= min_work_s
        )
        track.phase = "settled"
        if not track.baseline_done:
            # First settle after spawn is the idle baseline, not a completion
            # the operator needs to read.
            track.baseline_done = True
            return "idle"
        return "complete-unread" if worked else "idle"

    if track.phase == "init" and not track.baseline_done:
        # Agent that just sits at a prompt, never churning → idle baseline.
        track.baseline_done = True
        track.phase = "settled"
        return "idle"
    return None


class SemanticStatusEngine:
    """Daemon-side poller that derives cross-harness semantic status from each
    running agent's rendered screen and funnels it through
    `Orchestrator.set_semantic_status` (so the existing `agent.status_changed`
    fan-out, workspace roll-up, and `agent wait` all light up for free)."""

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self._orch = orchestrator
        self._tracks: dict[str, _Track] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._loop, name="relaydeck-semantic-engine", daemon=True
        )
        self._thread = t
        t.start()
        logger.info("Started semantic-status engine (interval=%.1fs)", POLL_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL_S):
            try:
                self.tick()
            except Exception:
                logger.exception("Semantic-status engine tick failed")

    def tick(self, *, now: float | None = None) -> None:
        """One sampling pass over every running harness agent. Public so tests
        can drive it deterministically."""
        from relaydeck.db import get_semantic_status, open_db
        from relaydeck.screen import render

        instances = self._orch.list_running_instances()
        # Drop trackers for agents that are no longer running so a restarted
        # agent re-baselines instead of inheriting a stale phase.
        for aid in list(self._tracks):
            if aid not in instances:
                del self._tracks[aid]
        if not instances:
            return

        now = time.time() if now is None else now
        conn = open_db(self._orch.db_path)
        try:
            for aid, inst in instances.items():
                getter = getattr(inst, "get_pty_buffer", None)
                detect = getattr(inst, "detect_awaiting_input", None)
                if getter is None or detect is None:
                    continue  # not a PTY harness (no screen to read)
                try:
                    buf = getter() or b""
                    screen = render(bytes(buf)) if buf else ""
                    screen_hash = hashlib.sha1(screen.encode("utf-8", "replace")).hexdigest()
                    awaiting = bool(detect(screen))
                    track = self._tracks.setdefault(aid, _Track())
                    desired = derive_status(
                        track, screen_hash=screen_hash, awaiting=awaiting, now=now
                    )
                    if desired is None:
                        continue
                    status, at, source = get_semantic_status(conn, aid)
                    if status == desired:
                        continue
                    if not may_write(source, at, now):
                        continue
                    self._orch.set_semantic_status(aid, desired, source=ENGINE_SOURCE)
                except Exception as exc:
                    logger.debug("semantic engine: agent %s tick failed: %s", aid, exc)
        finally:
            conn.close()
