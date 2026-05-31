"""
GitHub event poller.

One per opted-in workspace. Calls `gh api repos/<repo>/events` on a
configurable interval, dedups by event id against an on-disk cursor,
and routes each fresh event through the rules engine + action
dispatcher.

Why subprocess + gh CLI instead of the GitHub API directly:
  - operators already manage gh auth (`gh auth login`); we'd otherwise
    have to invent a token-management story
  - rate-limit handling, retries, and pagination are gh's job
  - works on enterprise GitHub instances the operator has configured
    via `gh auth login --hostname`

The cursor lives at:

    ~/.relaydeck/workspaces/<ws>/runtime/github/cursor.json

Cursor structure:
    {
      "last_event_id": "12345678901",
      "last_poll_ts": "2026-05-18T12:34:56Z",
      "last_error": null
    }

`last_event_id` is the highest GitHub event id we've processed. Event
ids are monotonically increasing strings (GitHub stops paginating
before they wrap), so on each poll we filter to ids > last and walk
oldest-first so rules fire in chronological order.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.automation import ActionContext, ActionError, dispatch

from .rules import RulesConfig, evaluate, load_config

logger = logging.getLogger(__name__)


# ── Cursor on disk ──────────────────────────────────────────────────


@dataclass
class Cursor:
    last_event_id: str | None = None
    last_poll_ts: str | None = None
    last_error: str | None = None


def cursor_path(config_home: Path, workspace: str) -> Path:
    return (
        config_home / "workspaces" / workspace
        / "runtime" / "github" / "cursor.json"
    )


def load_cursor(path: Path) -> Cursor:
    if not path.exists():
        return Cursor()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return Cursor()
    return Cursor(
        last_event_id=data.get("last_event_id"),
        last_poll_ts=data.get("last_poll_ts"),
        last_error=data.get("last_error"),
    )


def save_cursor(path: Path, cursor: Cursor) -> None:
    from relaydeck.atomicio import atomic_write_text
    payload = {
        "last_event_id": cursor.last_event_id,
        "last_poll_ts": cursor.last_poll_ts,
        "last_error": cursor.last_error,
    }
    atomic_write_text(path, json.dumps(payload, indent=2))


# ── gh wrapper ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PollResult:
    """Outcome of one `fetch_events` call.

    `error` is None on success — even a successful poll with zero
    events. A non-None `error` means the poll itself failed (auth
    missing, gh missing, network error, non-JSON response). The
    worker uses this distinction to set `cursor.last_error`, which
    surfaces in `relaydeck github status`. Without it, broken auth would
    look identical to a quiet repo."""

    events: list[dict[str, Any]]
    error: str | None = None


def fetch_events(
    repo: str,
    *,
    gh_binary: str = "gh",
    per_page: int = 30,
    timeout: float = 30.0,
) -> PollResult:
    """Call `gh api repos/<repo>/events` and return a PollResult.

    Errors are captured on `.error` rather than raised — a flaky
    network shouldn't take the worker into ERRORED, but the worker
    DOES need to distinguish "successful poll, no events" from
    "poll failed, status unknown". The previous version collapsed
    both into [], which made broken auth look healthy.

    Pagination intentionally caps at one page. The Events API only
    returns the last 90 days / 300 events / one page reliably, so
    chasing pagination doesn't recover history we'd otherwise miss —
    if the cursor falls behind by more than 300 events, the cursor
    is broken regardless of pagination.
    """
    try:
        proc = subprocess.run(
            [
                gh_binary, "api",
                "-X", "GET",
                f"repos/{repo}/events",
                "-F", f"per_page={per_page}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        msg = f"gh binary not found at {gh_binary!r}"
        logger.warning("github: %s", msg)
        return PollResult(events=[], error=msg)
    except subprocess.TimeoutExpired:
        msg = f"gh api timed out after {timeout}s"
        logger.warning("github: %s", msg)
        return PollResult(events=[], error=msg)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        msg = f"gh api failed (rc={proc.returncode}): {stderr[:200]}"
        logger.warning("github: %s", msg)
        return PollResult(events=[], error=msg)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        msg = f"gh api returned non-JSON: {exc}"
        logger.warning("github: %s", msg)
        return PollResult(events=[], error=msg)
    if not isinstance(out, list):
        return PollResult(events=[], error="gh api returned non-list response")
    return PollResult(events=out, error=None)


# ── Worker ──────────────────────────────────────────────────────────


class GithubPoller:
    """One poller per workspace. Wired by GithubPlugin in plugin.py.

    The worker tick is `_tick(worker)`. Each tick:
      1. Reload rules from disk (cheap; lets edits to github.yaml take
         effect without a daemon restart)
      2. Call `gh api` for the configured repo
      3. Diff against cursor.last_event_id, walk new events
         oldest-first, emit + dispatch
      4. Save cursor

    Errors in step 2/3 are logged + recorded on the cursor; the
    worker stays RUNNING with restart_policy=RESTART (set by the
    spawner) so transient failures self-heal.
    """

    def __init__(
        self,
        *,
        workspace: str,
        config_home: Path,
        workspace_path: Path | None,
        bus: Any,
        send_message: Any,
        emit_event: Any,
        gh_binary: str = "gh",
        default_interval_s: float = 30.0,
    ) -> None:
        self.workspace = workspace
        self.config_home = config_home
        self.workspace_path = workspace_path
        self._bus = bus
        self._send_message = send_message
        self._emit_event = emit_event
        self.gh_binary = gh_binary
        self.default_interval_s = default_interval_s
        self._worker: Any | None = None
        self._config: RulesConfig | None = None
        self._cursor_path = cursor_path(config_home, workspace)

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self, worker_spawner: Any) -> None:
        """Load config once for the interval, then spawn the worker.

        Reload-on-tick handles in-flight rule edits, but the interval
        is fixed at worker registration time — the worker primitive
        doesn't currently support dynamic interval changes. Operators
        editing `poll_interval_s` should restart the daemon (or
        disable + re-enable the github plugin) to pick it up.
        """
        config = self._safe_load_config()
        interval = (
            config.poll_interval_s if config is not None
            else self.default_interval_s
        )
        self._config = config
        self._worker = worker_spawner.spawn(
            name=f"github:{self.workspace}",
            fn=self._tick,
            interval=interval,
            config={
                "workspace": self.workspace,
                "repo": config.repo if config else None,
                "poll_interval_s": interval,
            },
            description=(
                "Polls gh api repos/<repo>/events on the configured interval "
                "and routes each new event through this workspace's github.yaml "
                "rules (agent.message / script / gh / model / code / bus.emit). "
                "A tick is one poll — it logs 'no new events' at debug when quiet."
            ),
        )

    def stop(self) -> None:
        if self._worker is not None:
            with contextlib.suppress(Exception):
                self._worker.stop()
        self._worker = None

    # ── worker tick ────────────────────────────────────────────────

    def _tick(self, worker: Any) -> None:
        config = self._safe_load_config()
        self._config = config
        if config is None:
            return  # no github.yaml yet — poll loop stays alive as a no-op

        result = fetch_events(config.repo, gh_binary=self.gh_binary)
        if result.error is not None:
            with contextlib.suppress(Exception):
                worker.log(f"poll failed: {result.error}", level="warn")
            # Preserve the existing cursor.last_event_id; only the error
            # field changes. advance_to="__no_change__" is the sentinel
            # that means "leave last_event_id alone".
            self._save_cursor(error=result.error)
            return

        cursor = load_cursor(self._cursor_path)
        events = result.events

        # Bootstrap: a fresh workspace with no cursor records the
        # latest fetched event id WITHOUT firing rules. Otherwise the
        # first poll would replay ~90 days of history through the
        # action loop. We DO advance the cursor here — without that,
        # every subsequent tick would stay in bootstrap mode and
        # rules would never fire.
        if cursor.last_event_id is None:
            latest_id = self._latest_event_id(events)
            if latest_id is not None:
                with contextlib.suppress(Exception):
                    worker.log(
                        f"bootstrap: bookmarking {latest_id} ({len(events)} historical "
                        "events skipped)"
                    )
            self._save_cursor(error=None, advance_to=latest_id)
            return

        new_events = self._select_new(events, cursor.last_event_id)
        if not new_events:
            # Successful poll with no new events: clear last_error and
            # keep last_event_id where it was. Log a quiet heartbeat so
            # the Workers lens shows the poller IS alive and working —
            # "logs nothing" otherwise reads as "broken".
            with contextlib.suppress(Exception):
                worker.log(f"polled {config.repo} — no new events", level="debug")
            self._save_cursor(error=None)
            return

        with contextlib.suppress(Exception):
            worker.log(f"got {len(new_events)} new events from {config.repo}")

        last_seen = cursor.last_event_id
        for event in new_events:
            ev_id = str(event.get("id") or "")
            ev_type = str(event.get("type") or "")
            self._emit_internal(ev_type, event)
            self._run_rules(config, event, worker)
            if ev_id and (last_seen is None or ev_id > last_seen):
                last_seen = ev_id

        self._save_cursor(error=None, advance_to=last_seen)

    def _latest_event_id(self, events: list[dict[str, Any]]) -> str | None:
        """Highest id across the fetched events. GitHub returns
        newest-first, so events[0].id is usually it — but we walk the
        list defensively in case the order ever changes."""
        latest: str | None = None
        for e in events:
            eid = str(e.get("id") or "")
            if eid and (latest is None or eid > latest):
                latest = eid
        return latest

    # ── helpers ─────────────────────────────────────────────────────

    def _safe_load_config(self) -> RulesConfig | None:
        cfg_path = self.config_home / "workspaces" / self.workspace / "github.yaml"
        try:
            return load_config(cfg_path)
        except ValueError as exc:
            logger.warning("github: %s: %s", cfg_path, exc)
            self._save_cursor(error=f"config: {exc}")
            return None

    def _select_new(
        self, events: list[dict[str, Any]], last_id: str
    ) -> list[dict[str, Any]]:
        """Filter events newer than `last_id`, oldest first.

        Bootstrap (`last_id is None`) is handled in `_tick` before
        this is reached; by the time we get here we have a real
        cursor to compare against.

        GitHub returns events newest-first. We reverse so rule
        actions fire in the order events actually happened — a
        labeled-then-unlabeled pair shouldn't fire backwards."""
        new = []
        for e in events:
            eid = str(e.get("id") or "")
            if eid and eid > last_id:
                new.append(e)
        new.reverse()
        return new

    def _emit_internal(self, ev_type: str, event: dict[str, Any]) -> None:
        if self._emit_event is None or not ev_type:
            return
        with contextlib.suppress(Exception):
            self._emit_event(
                f"github.{ev_type}",
                {
                    "workspace": self.workspace,
                    "id": event.get("id"),
                    "type": ev_type,
                    "actor": (event.get("actor") or {}).get("login"),
                    "repo": (event.get("repo") or {}).get("name"),
                    "created_at": event.get("created_at"),
                    "payload": event.get("payload") or {},
                },
            )

    def _model_complete(self, prompt: str, *, model: str = "role:fast",
                        max_tokens: int = 256, **kwargs: Any) -> str:
        """Bridge the `model` action to the core model gateway so a
        github rule can classify/summarize an event with a (local)
        model. Records each call to `model_invocations` (keyed by the
        github worker id) for Workers-lens visibility. Ungated like the
        loop agent's bridge — the poller runs inside the daemon, not
        across the plugin capability boundary."""
        from relaydeck.model_invocations import timed_complete
        db_path = str(self.config_home / "runtime" / "relaydeck.db")
        return timed_complete(
            f"github:{self.workspace}", prompt, model=model,
            max_tokens=max_tokens, source="github", db_path=db_path,
            config_home=self.config_home, **kwargs,
        )

    def _run_rules(
        self,
        config: RulesConfig,
        event: dict[str, Any],
        worker: Any,
    ) -> None:
        matches = evaluate(config, event)
        if not matches:
            return
        ctx = ActionContext(
            send_message=self._send_message,
            emit_event=self._emit_event,
            gh_binary=self.gh_binary,
            workspace_path=self.workspace_path,
            event=event,
            model_complete=self._model_complete,
        )
        for rule, rendered_actions in matches:
            for action in rendered_actions:
                try:
                    summary = dispatch(action, ctx)
                    with contextlib.suppress(Exception):
                        worker.log(
                            f"rule={rule.name} ok action={summary.get('kind')}"
                        )
                except ActionError as exc:
                    with contextlib.suppress(Exception):
                        worker.log(
                            f"rule={rule.name} action invalid: {exc}",
                            level="warn",
                        )
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        worker.log(
                            f"rule={rule.name} action raised: {exc}",
                            level="warn",
                        )

    def _save_cursor(
        self,
        *,
        error: str | None = None,
        advance_to: str | None = "__no_change__",
    ) -> None:
        current = load_cursor(self._cursor_path)
        new = Cursor(
            last_event_id=(
                current.last_event_id if advance_to == "__no_change__" else advance_to
            ),
            last_poll_ts=_now_iso(),
            last_error=error,
        )
        try:
            save_cursor(self._cursor_path, new)
        except OSError as exc:
            logger.warning("github: failed to save cursor: %s", exc)


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
