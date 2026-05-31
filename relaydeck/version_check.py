"""Update check — compare the installed version against the latest GitHub
release tag.

relaydeck publishes to PyPI, and every PyPI release is cut from a GitHub Release
of the same tag (.github/workflows/release.yml + docs/RELEASE.md), so the two are
always in lockstep — the repo's latest GitHub Release tag is a reliable "is there
a newer version?" signal that matches what `uv tool upgrade relaydeck` would
install. This module fetches the tag, compares, and CACHES the result (6h TTL) so
the dashboard banner never blocks a page load or hammers the GitHub API.
Everything is fail-open: offline, rate-limited, or no-releases-yet all resolve to
"no update" rather than an error. The network call goes through an injectable
`_fetch` seam for tests.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

GITHUB_REPO = "relaydeck/relaydeck"
CACHE_TTL_S = 6 * 3600
_RELEASES_URL = "https://api.github.com/repos/{repo}/releases/latest"


def _default_fetch(url: str, timeout: float = 4.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "relaydeck"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _norm(tag: str | None) -> str:
    return (tag or "").lstrip("vV").strip()


def _parse(v: str) -> tuple[int, ...]:
    """A lenient numeric version tuple: `1.2.3` → (1,2,3). Non-numeric or
    pre-release suffixes (`1.2.0rc1`) truncate to their leading digits so the
    comparison never throws."""
    out: list[int] = []
    for part in _norm(v).split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(latest: str | None, current: str) -> bool:
    if not latest:
        return False
    return _parse(latest) > _parse(current)


def fetch_latest_tag(
    repo: str = GITHUB_REPO, *, _fetch: Callable[..., Any] = _default_fetch
) -> str | None:
    """The latest GitHub *Release* tag (normalized, no leading `v`), or None
    when there are no releases / the call fails / we're offline."""
    try:
        data = _fetch(_RELEASES_URL.format(repo=repo))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name") or data.get("name")
    return _norm(tag) if tag else None


def _read_cache(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(path: Path | None, data: dict[str, Any]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass


def check_for_update(
    current: str,
    *,
    repo: str = GITHUB_REPO,
    cache_path: Path | None = None,
    ttl: float = CACHE_TTL_S,
    force: bool = False,
    _fetch: Callable[..., Any] = _default_fetch,
) -> dict[str, Any]:
    """Return `{current, latest, update_available, repo, checked_at}`.

    Uses the cache within `ttl`; otherwise re-fetches and persists. On a failed
    fetch it falls back to any cached `latest` (so a brief outage doesn't drop
    the banner). `latest=None` → `update_available=False`."""
    now = time.time()
    cache = _read_cache(cache_path)
    latest: str | None
    checked_at = float(cache.get("checked_at", 0) or 0)

    if not force and cache.get("latest") and (now - checked_at < ttl):
        latest = cache.get("latest")
    else:
        latest = fetch_latest_tag(repo, _fetch=_fetch)
        if latest is not None:
            checked_at = now
            _write_cache(cache_path, {"latest": latest, "checked_at": now})
        else:
            latest = cache.get("latest")  # offline → keep last-known

    return {
        "current": current,
        "latest": latest,
        "update_available": is_newer(latest, current),
        "repo": repo,
        "checked_at": checked_at,
    }
