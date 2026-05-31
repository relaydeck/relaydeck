"""
PR / check-run / review enrichment via `gh api`.

The events poller (`poller.py`) is intentionally lossy — it reads one
page of `repos/{repo}/events` and bookmarks a cursor, fine for
lightweight routing but not enough to answer "which checks are failing
on this PR" or "who requested changes." This module fills that gap with
targeted `gh api` calls, so a rule/workflow can route a CI failure or an
unresolved review to the responsible agent.

Auth is whatever `gh auth status` provides — we never read tokens. Every
call shells the operator's `gh`; failures raise `GhError` rather than
returning bogus data, so a caller can decide whether to retry or skip.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


class GhError(RuntimeError):
    """A `gh api` invocation failed (non-zero exit, timeout, or non-JSON)."""


def gh_api(
    gh_binary: str,
    endpoint: str,
    *,
    args: list[str] | None = None,
    timeout: float = 30.0,
) -> Any:
    """Run `gh api <endpoint> [args...]` and return the parsed JSON.

    `args` are extra `gh api` flags (e.g. `["-X", "POST"]` or
    `["--paginate"]`). Raises GhError on any failure.
    """
    cmd = [gh_binary, "api", endpoint, *(args or [])]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise GhError(f"{gh_binary} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh api {endpoint} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise GhError(
            f"gh api {endpoint} failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:200]}"
        )
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GhError(f"gh api {endpoint} returned non-JSON: {exc}") from exc


def fetch_issue(gh_binary: str, repo: str, number: int | str) -> dict[str, Any]:
    data = gh_api(gh_binary, f"repos/{repo}/issues/{number}")
    return data if isinstance(data, dict) else {}


def fetch_pull(gh_binary: str, repo: str, number: int | str) -> dict[str, Any]:
    data = gh_api(gh_binary, f"repos/{repo}/pulls/{number}")
    return data if isinstance(data, dict) else {}


def fetch_check_runs(gh_binary: str, repo: str, ref: str) -> list[dict[str, Any]]:
    """Check runs for a commit `ref` (a SHA, branch, or tag)."""
    data = gh_api(gh_binary, f"repos/{repo}/commits/{ref}/check-runs")
    if isinstance(data, dict):
        runs = data.get("check_runs")
        return runs if isinstance(runs, list) else []
    return []


def fetch_reviews(gh_binary: str, repo: str, number: int | str) -> list[dict[str, Any]]:
    data = gh_api(gh_binary, f"repos/{repo}/pulls/{number}/reviews")
    return data if isinstance(data, list) else []


# Check-run conclusions that mean "something a human/agent must act on".
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)


def summarize_pr_health(
    gh_binary: str, repo: str, number: int | str
) -> dict[str, Any]:
    """Combine PR + check-runs + reviews into a routing-ready summary.

    This is the "route CI failures and unresolved review comments to the
    responsible agent" signal: who owns the PR, which checks are failing,
    and who has requested changes. `healthy` is True only when nothing
    needs attention. Each underlying fetch can raise GhError; the caller
    decides whether a partial picture is acceptable.
    """
    pr = fetch_pull(gh_binary, repo, number)
    head_sha = ((pr.get("head") or {}).get("sha")) or ""
    checks = fetch_check_runs(gh_binary, repo, head_sha) if head_sha else []
    failing = [c for c in checks if c.get("conclusion") in FAILING_CONCLUSIONS]

    reviews = fetch_reviews(gh_binary, repo, number)
    # Collapse to the latest review state per reviewer (a reviewer can
    # leave several reviews; only the most recent state counts).
    latest_state: dict[str, str] = {}
    for review in reviews:
        login = ((review.get("user") or {}).get("login")) or "?"
        state = review.get("state")
        if state:
            latest_state[login] = state
    changes_requested_by = sorted(
        login for login, state in latest_state.items()
        if state == "CHANGES_REQUESTED"
    )

    return {
        "number": number,
        "title": pr.get("title", ""),
        "state": pr.get("state", ""),
        "author": ((pr.get("user") or {}).get("login")),
        "head_sha": head_sha,
        "head_ref": ((pr.get("head") or {}).get("ref")),
        "mergeable": pr.get("mergeable"),
        "failing_checks": [
            {
                "name": c.get("name"),
                "conclusion": c.get("conclusion"),
                "url": c.get("html_url"),
            }
            for c in failing
        ],
        "changes_requested_by": changes_requested_by,
        "healthy": not failing and not changes_requested_by,
    }
