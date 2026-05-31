"""
Base agent class — the single primitive.

Every long-lived thing the daemon does is a subclass of `BaseAgent`.
The orchestrator spawns each in its own thread and manages lifecycle.
Agents communicate via emit() → events table → SSE subscribers.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any


class BaseAgent:
    """Lifecycle contract:
        - __init__(...) — light setup; do NOT block.
        - run()          — invoked in its own thread. Loop until stop_flag is set.
        - terminate()    — optional. Called for external kill signals (SIGTERM to subprocess).

    The `emit()` helper writes a structured event tagged with agent_id,
    so the dashboard can filter per agent without call-site bookkeeping.
    """

    # Subclasses may override for sleep_unless_stopped default intervals.
    DEFAULT_INTERVAL_S: float = 60.0

    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        config: dict[str, Any],
        workspace: str | None,
        db_path: str,
        stop_flag: threading.Event,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.config = config
        self.workspace = workspace
        self.db_path = db_path
        self.stop_flag = stop_flag
        self._session_id = f"agent:{agent_id}"

    # ── Public API ───────────────────────────────────────────────

    def run(self) -> None:
        raise NotImplementedError("Subclasses must implement run()")

    def terminate(self) -> None:
        """Override if the agent needs to actively cancel an in-progress
        operation (e.g. a subprocess). Default: no-op."""
        return

    # ── Helpers for subclasses ───────────────────────────────────

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        """Write an event tagged with this agent's id and publish to the
        in-memory bus so SSE subscribers get it within milliseconds."""
        from relaydeck.db import ensure_session, log_event, open_db

        conn = open_db(self.db_path)
        try:
            ensure_session(conn, self._session_id)
            ev_id = log_event(
                conn, self._session_id, event_type, payload, agent_id=self.agent_id,
            )
        finally:
            conn.close()

        # Publish to in-memory bus for SSE subscribers
        try:
            from relaydeck.orchestrator import _bus
            _bus.publish(self.agent_id, event_type, payload, ev_id)
        except Exception:
            pass

        return ev_id

    def update_status(self, status: str, error: str | None = None) -> None:
        """Update this agent's status in the DB."""
        from relaydeck.db import open_db, update_agent_status
        # Remember the last status we set so the run-thread wrapper can tell a
        # clean exit (→ "stopped") from a failed spawn (→ keep "errored").
        self._last_status = status
        conn = open_db(self.db_path)
        try:
            update_agent_status(conn, self.agent_id, status, error)
        finally:
            conn.close()

    def sleep_unless_stopped(self, seconds: float) -> bool:
        """Sleep in small increments, returning True if stop was requested."""
        step = min(0.5, seconds)
        elapsed = 0.0
        while elapsed < seconds:
            if self.stop_flag.is_set():
                return True
            time.sleep(step)
            elapsed += step
        return False

    def _conn(self) -> sqlite3.Connection:
        from relaydeck.db import open_db
        return open_db(self.db_path)
