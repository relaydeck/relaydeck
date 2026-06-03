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
ids are numeric strings, so on each poll we compare them numerically
where possible and walk oldest-first so rules fire in chronological
order.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relaydeck.automation import ActionContext, ActionError, dispatch

from .rules import RulesConfig, evaluate, load_config

logger = logging.getLogger(__name__)

# How many recently-seen event ids to remember. GitHub caps a repo's events
# feed at ~300 events / 90 days, so this comfortably covers the whole window
# with headroom — every id currently in the feed stays remembered, so a poll
# never re-fires an event that's still listed.
_SEEN_CAP = 600


# ── Cursor on disk ──────────────────────────────────────────────────


@dataclass
class Cursor:
    last_event_id: str | None = None
    last_poll_ts: str | None = None
    last_error: str | None = None
    # The set of recently-seen event ids (most-recent first), bounded to
    # _SEEN_CAP. This is the real dedup key. We do NOT use a numeric
    # high-water-mark: GitHub event ids are NOT monotonic with time (a
    # CreateEvent can carry a higher id than a later IssuesEvent), so a
    # max-id watermark silently swallows every newer-but-lower-id event.
    seen_ids: list[str] = field(default_factory=list)


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
    seen = data.get("seen_ids")
    return Cursor(
        last_event_id=data.get("last_event_id"),
        last_poll_ts=data.get("last_poll_ts"),
        last_error=data.get("last_error"),
        seen_ids=[str(x) for x in seen] if isinstance(seen, list) else [],
    )


def save_cursor(path: Path, cursor: Cursor) -> None:
    from relaydeck.atomicio import atomic_write_text
    payload = {
        "last_event_id": cursor.last_event_id,
        "last_poll_ts": cursor.last_poll_ts,
        "last_error": cursor.last_error,
        "seen_ids": cursor.seen_ids[:_SEEN_CAP],
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
    per_page: int = 100,
    timeout: float = 30.0,
) -> PollResult:
    """Call `gh api repos/<repo>/events` and return a PollResult.

    Errors are captured on `.error` rather than raised — a flaky
    network shouldn't take the worker into ERRORED, but the worker
    DOES need to distinguish "successful poll, no events" from
    "poll failed, status unknown". The previous version collapsed
    both into [], which made broken auth look healthy.

    Pagination intentionally caps at one max-sized page. GitHub's
    Events API exposes a bounded recent timeline, so this poller is for
    near-real-time automation, not backfilling long gaps.
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
            # Record the error only; last_event_id + seen_ids keep their
            # persisted values (the _KEEP sentinel default).
            self._save_cursor(error=result.error)
            return

        cursor = load_cursor(self._cursor_path)
        events = result.events
        current_ids = [str(e.get("id") or "") for e in events if e.get("id")]
        newest_id = current_ids[0] if current_ids else cursor.last_event_id

        # Bootstrap (fresh workspace) OR migrate (an old cursor written before
        # seen-id tracking — it only has a numeric high-water-mark, which is
        # unreliable because GitHub ids aren't monotonic). Either way: seed the
        # seen-set with the CURRENT feed and fire no rules, so we neither
        # replay ~90 days of history nor trust the stale watermark.
        if not cursor.seen_ids:
            reason = "bootstrap" if cursor.last_event_id is None else "migrate"
            with contextlib.suppress(Exception):
                worker.log(
                    f"{reason}: bookmarking {len(current_ids)} events "
                    "(historical events skipped)"
                )
            self._save_cursor(error=None, seen_ids=current_ids, last_event_id=newest_id)
            return

        seen = set(cursor.seen_ids)
        new_events = [e for e in events if str(e.get("id") or "") not in seen]
        if not new_events:
            # Successful poll with no new events. Refresh the seen-set against
            # the current feed (ids rotate as the window slides) and log a
            # quiet heartbeat so the Workers lens shows the poller IS alive.
            with contextlib.suppress(Exception):
                worker.log(f"polled {config.repo} — no new events", level="debug")
            self._save_cursor(
                error=None,
                seen_ids=_merge_seen(current_ids, cursor.seen_ids),
                last_event_id=newest_id,
            )
            return

        # Fire oldest-first by actual event time (ids are not a reliable order),
        # so a labeled-then-unlabeled pair never fires backwards.
        new_events.sort(key=lambda e: (str(e.get("created_at") or ""), _id_key(e)))

        with contextlib.suppress(Exception):
            worker.log(f"got {len(new_events)} new events from {config.repo}")

        for event in new_events:
            ev_type = str(event.get("type") or "")
            self._emit_internal(ev_type, event)
            self._run_rules(config, event, worker)

        # Remember everything currently in the feed (covers the just-fired
        # events too) plus prior history, bounded.
        self._save_cursor(
            error=None,
            seen_ids=_merge_seen(current_ids, cursor.seen_ids),
            last_event_id=newest_id,
        )

    # ── helpers ─────────────────────────────────────────────────────

    def _safe_load_config(self) -> RulesConfig | None:
        cfg_path = self.config_home / "workspaces" / self.workspace / "github.yaml"
        try:
            return load_config(cfg_path)
        except ValueError as exc:
            logger.warning("github: %s: %s", cfg_path, exc)
            self._save_cursor(error=f"config: {exc}")
            return None

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

    _KEEP = object()  # sentinel: leave a field at its current persisted value

    def _save_cursor(
        self,
        *,
        error: str | None = None,
        last_event_id: Any = _KEEP,
        seen_ids: Any = _KEEP,
    ) -> None:
        current = load_cursor(self._cursor_path)
        new = Cursor(
            last_event_id=(
                current.last_event_id if last_event_id is self._KEEP else last_event_id
            ),
            last_poll_ts=_now_iso(),
            last_error=error,
            seen_ids=(current.seen_ids if seen_ids is self._KEEP else list(seen_ids)),
        )
        try:
            save_cursor(self._cursor_path, new)
        except OSError as exc:
            logger.warning("github: failed to save cursor: %s", exc)


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id_key(event: dict[str, Any]) -> int:
    """Numeric id for stable tie-breaking when two events share a
    created_at second. Non-numeric ids sort as 0 (rare/defensive)."""
    try:
        return int(str(event.get("id") or ""))
    except (TypeError, ValueError):
        return 0


def _merge_seen(current_ids: list[str], prior: list[str]) -> list[str]:
    """Most-recent-first union of the current feed and prior history,
    de-duped and bounded to _SEEN_CAP. Current feed ids go first so they
    are never trimmed out (which would let a still-listed event re-fire);
    older history that has scrolled off the feed ages out past the cap."""
    return list(dict.fromkeys([*current_ids, *prior]))[:_SEEN_CAP]
