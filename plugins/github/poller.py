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
import re
import subprocess
import time
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
    # ── issues-since source only ──
    # The `since` watermark for the next /issues query, and the last-known
    # state of each issue/PR (`{number: {"state": ..., "labels": [...]}}`).
    # Synthesized events are deduped by diffing this state — once stored, an
    # unchanged issue produces nothing, so dedup is idempotent (no seen-set).
    since: str | None = None
    issues: dict[str, Any] = field(default_factory=dict)


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
    issues = data.get("issues")
    return Cursor(
        last_event_id=data.get("last_event_id"),
        last_poll_ts=data.get("last_poll_ts"),
        last_error=data.get("last_error"),
        seen_ids=[str(x) for x in seen] if isinstance(seen, list) else [],
        since=data.get("since"),
        issues=issues if isinstance(issues, dict) else {},
    )


def save_cursor(path: Path, cursor: Cursor) -> None:
    from relaydeck.atomicio import atomic_write_text
    payload = {
        "last_event_id": cursor.last_event_id,
        "last_poll_ts": cursor.last_poll_ts,
        "last_error": cursor.last_error,
        "seen_ids": cursor.seen_ids[:_SEEN_CAP],
        "since": cursor.since,
        "issues": cursor.issues,
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
    look identical to a quiet repo.

    `status` is the parsed HTTP code (when the failure was an HTTP error) and
    `rate_limited` flags a GitHub rate-limit response — the poller uses both
    to decide how hard to back off (see `_record_failure`)."""

    events: list[dict[str, Any]]
    error: str | None = None
    status: int | None = None
    rate_limited: bool = False


_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def _gh_failure(rc: int, stderr: str) -> PollResult:
    """Build a classified PollResult from a non-zero `gh` invocation."""
    s = (stderr or "").strip()
    m = _HTTP_STATUS_RE.search(s)
    status = int(m.group(1)) if m else None
    low = s.lower()
    rate_limited = "rate limit" in low or status == 429
    return PollResult(
        events=[], error=f"gh api failed (rc={rc}): {s[:200]}",
        status=status, rate_limited=rate_limited,
    )


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
        # Quiet here — the worker logs a throttled, classified warning + backs
        # off (a dead/forbidden repo otherwise spams the daemon log per tick).
        logger.debug("github: gh api failed (rc=%s): %s", proc.returncode, stderr[:200])
        return _gh_failure(proc.returncode, stderr)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        msg = f"gh api returned non-JSON: {exc}"
        logger.warning("github: %s", msg)
        return PollResult(events=[], error=msg)
    if not isinstance(out, list):
        return PollResult(events=[], error="gh api returned non-list response")
    return PollResult(events=out, error=None)


def fetch_issues_since(
    repo: str,
    since: str | None,
    *,
    gh_binary: str = "gh",
    per_page: int = 100,
    timeout: float = 30.0,
) -> PollResult:
    """Fetch issues + PRs updated since `since` (ISO8601), oldest-first.

    Hits `GET /repos/<repo>/issues` — a core, uncached endpoint (the events
    feed, by contrast, is delayed and eventually-consistent). `/issues`
    includes PRs (they carry a `pull_request` key). On bootstrap (`since`
    None) it returns the current open+recent issues to seed state without
    firing. `PollResult.events` carries raw issue objects here, not events;
    the caller diffs them into synthesized events."""
    args = [
        gh_binary, "api", "-X", "GET", f"repos/{repo}/issues",
        "-F", "state=all", "-F", "sort=updated", "-F", "direction=asc",
        "-F", f"per_page={per_page}",
    ]
    if since:
        args += ["-F", f"since={since}"]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
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
        logger.debug("github: gh api failed (rc=%s): %s", proc.returncode, stderr[:200])
        return _gh_failure(proc.returncode, stderr)
    try:
        # strict=False: issue/PR bodies legitimately contain raw control chars.
        out = json.loads(proc.stdout, strict=False)
    except json.JSONDecodeError as exc:
        msg = f"gh api returned non-JSON: {exc}"
        logger.warning("github: %s", msg)
        return PollResult(events=[], error=msg)
    if not isinstance(out, list):
        return PollResult(events=[], error="gh api returned non-list response")
    return PollResult(events=out, error=None)


def _issue_snapshot(issue: dict[str, Any]) -> dict[str, Any]:
    """The diff-relevant state of an issue/PR: open/closed + its label set."""
    labels = sorted(
        str(lbl.get("name"))
        for lbl in (issue.get("labels") or [])
        if isinstance(lbl, dict) and lbl.get("name")
    )
    return {"state": str(issue.get("state") or ""), "labels": labels}


def _synth_event(repo: str, issue: dict[str, Any], action: str,
                 *, label: str | None = None) -> dict[str, Any]:
    """Build an event dict shaped like the activity feed's IssuesEvent /
    PullRequestEvent, so the rules engine + templates work unchanged.

    Actor is best-effort: /issues gives the issue author, not who performed
    a later label/state change. Rules matching on `actor` for label changes
    should use `source: events`."""
    is_pr = "pull_request" in issue
    etype = "PullRequestEvent" if is_pr else "IssuesEvent"
    payload: dict[str, Any] = {"action": action, "issue": issue}
    if is_pr:
        payload["pull_request"] = issue
    if label is not None:
        payload["label"] = {"name": label}
    num = issue.get("number")
    return {
        "id": f"issue:{num}:{action}:{label or ''}:{issue.get('updated_at') or ''}",
        "type": etype,
        "actor": {"login": (issue.get("user") or {}).get("login")},
        "repo": {"name": repo},
        "created_at": issue.get("updated_at"),
        "payload": payload,
    }


def diff_issue_events(
    repo: str, issues: list[dict[str, Any]], prior: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Diff fetched issues/PRs against their last-known state and synthesize
    the events that happened. Returns (events oldest-first, updated state).

    Per issue: never seen → `opened`; state flip → `closed`/`reopened`; label
    set delta → `labeled`/`unlabeled` (one event per label). Idempotent: the
    returned state is persisted, so an unchanged issue yields nothing next
    time (the diff IS the dedup)."""
    events: list[dict[str, Any]] = []
    state = dict(prior)
    for issue in issues:
        num = issue.get("number")
        if num is None:
            continue
        key = str(num)
        snap = _issue_snapshot(issue)
        was = prior.get(key)
        if was is None:
            events.append(_synth_event(repo, issue, "opened"))
        else:
            if snap["state"] != was.get("state"):
                action = "closed" if snap["state"] == "closed" else "reopened"
                events.append(_synth_event(repo, issue, action))
            old_labels = set(was.get("labels") or [])
            new_labels = set(snap["labels"])
            for lbl in sorted(new_labels - old_labels):
                events.append(_synth_event(repo, issue, "labeled", label=lbl))
            for lbl in sorted(old_labels - new_labels):
                events.append(_synth_event(repo, issue, "unlabeled", label=lbl))
        state[key] = snap
    events.sort(key=lambda e: str(e.get("created_at") or ""))
    return events, state


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
        # Backoff state (in-memory; resets on daemon restart). A repo that
        # 404s / is forbidden / rate-limits us shouldn't be hammered every
        # interval — we skip ticks for a growing cooldown until it recovers.
        self._fail_streak = 0
        self._skip_until = 0.0

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
        # Backoff: after repeated failures we skip ticks for a growing cooldown
        # instead of hammering a dead/forbidden/rate-limited repo every interval.
        if self._skip_until and time.monotonic() < self._skip_until:
            return

        config = self._safe_load_config()
        self._config = config
        if config is None:
            return  # no github.yaml yet — poll loop stays alive as a no-op

        if config.effective_source() == "issues":
            self._tick_issues(config, worker)
        else:
            self._tick_events(config, worker)

    def _record_success(self) -> None:
        """Clear backoff after any successful poll."""
        self._fail_streak = 0
        self._skip_until = 0.0

    def _record_failure(self, result: PollResult, worker: Any) -> None:
        """Classify a failed poll, set a backoff cooldown, and log it (throttled
        so a persistently-broken repo doesn't spam the worker log every tick)."""
        self._fail_streak += 1
        interval = (
            self._config.poll_interval_s if self._config else self.default_interval_s
        )
        n = self._fail_streak
        if result.rate_limited:
            # Respect rate limits hard — 5 min, growing to 30 min.
            delay = min(300.0 * n, 1800.0)
            kind = "rate-limited"
        elif result.status in (401, 403, 404, 410):
            # Won't self-heal without a config/access fix — back off aggressively.
            delay = min(interval * (2 ** min(n, 6)), 1800.0)
            kind = f"http {result.status}"
        else:
            # Transient (network blip, timeout, 5xx) — quick exponential, 5 min cap.
            delay = min(interval * (2 ** min(n, 5)), 300.0)
            kind = "transient"
        self._skip_until = time.monotonic() + delay
        # Log on the first failure of a streak (+ every 20th) only.
        if n == 1 or n % 20 == 0:
            with contextlib.suppress(Exception):
                worker.log(
                    f"poll failed ({kind}, attempt {n}): {result.error} "
                    f"— backing off ~{int(delay)}s",
                    level="warn",
                )

    def _tick_issues(self, config: RulesConfig, worker: Any) -> None:
        """Reliable source: poll /issues?since= and diff state into events.

        The issue-state map IS the dedup (an unchanged issue diffs to nothing),
        so this path doesn't use the seen-id set."""
        # Capture the watermark BEFORE fetching, so anything updated during the
        # request is caught next poll. State-diffing makes the overlap a no-op.
        poll_started = _now_iso()
        cursor = load_cursor(self._cursor_path)
        result = fetch_issues_since(config.repo, cursor.since, gh_binary=self.gh_binary)
        if result.error is not None:
            self._record_failure(result, worker)
            self._save_cursor(error=result.error)
            return
        self._record_success()

        issues = result.events  # raw issue/PR objects on this path

        # Bootstrap: no watermark yet → record current state, fire nothing
        # (don't replay history on first run).
        if cursor.since is None and not cursor.issues:
            _, state = diff_issue_events(config.repo, issues, {})
            with contextlib.suppress(Exception):
                worker.log(
                    f"bootstrap: bookmarking {len(state)} issues/PRs "
                    "(history skipped)"
                )
            self._save_cursor(error=None, since=poll_started, issues=state)
            return

        events, state = diff_issue_events(config.repo, issues, cursor.issues)
        if not events:
            with contextlib.suppress(Exception):
                worker.log(f"polled {config.repo} — no new events", level="debug")
            self._save_cursor(error=None, since=poll_started, issues=state)
            return

        with contextlib.suppress(Exception):
            worker.log(f"got {len(events)} new events from {config.repo}")
        for event in events:
            self._emit_internal(str(event.get("type") or ""), event)
            self._run_rules(config, event, worker)
        self._save_cursor(error=None, since=poll_started, issues=state)

    def _tick_events(self, config: RulesConfig, worker: Any) -> None:
        result = fetch_events(config.repo, gh_binary=self.gh_binary)
        if result.error is not None:
            self._record_failure(result, worker)
            # Record the error only; last_event_id + seen_ids keep their
            # persisted values (the _KEEP sentinel default).
            self._save_cursor(error=result.error)
            return
        self._record_success()

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
        since: Any = _KEEP,
        issues: Any = _KEEP,
    ) -> None:
        current = load_cursor(self._cursor_path)
        new = Cursor(
            last_event_id=(
                current.last_event_id if last_event_id is self._KEEP else last_event_id
            ),
            last_poll_ts=_now_iso(),
            last_error=error,
            seen_ids=(current.seen_ids if seen_ids is self._KEEP else list(seen_ids)),
            since=(current.since if since is self._KEEP else since),
            issues=(current.issues if issues is self._KEEP else dict(issues)),
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
