"""Shared validation for live dashboard commands.

Both the relaydeck-native harness `dashboard` tool and the `relaydeck
dashboard` CLI / `POST /api/dashboard/command` endpoint turn a request into a
validated ``dashboard.command`` event the browser applies live (theme/accent/
density/glow at the app level, widget ops on the Home grid — see
``app.js:_applyDashboardCommand`` + ``home.js:applyCommand``). This module is
the single source of truth for the op set, the enums, and the command-dict
builder so the two surfaces can't drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Write ops emit a command; `get` is a read handled by callers.
WRITE_OPS = (
    "theme", "accent", "density", "glow",
    "add_widget", "remove_widget", "move_widget", "resize_widget",
    "tidy", "reset",
)
OPS = ("get", *WRITE_OPS)

# Scalar appearance ops persist server-side (via set_appearance) so they survive
# with no browser connected and `get` reflects them; callers emit
# `appearance.changed` to repaint. Everything else is a live grid mutation a
# browser applies + persists, broadcast as `dashboard.command`. `accent` is a
# legacy alias for `theme` (the 5 accent names are builtin themes).
SCALAR_OPS = ("theme", "accent", "density", "glow")


def appearance_key(op: str) -> str:
    """The preferences `appearance` key a scalar op writes (`accent`→`theme`)."""
    return "theme" if op == "accent" else op

# Must match home.js widget keys + the values styles.css actually defines.
WIDGETS = ("clock", "notes", "focus", "fleet", "usage", "agents", "feed",
           "workspaces", "workers", "spawn", "worktrees")
ACCENTS = ("cyan", "green", "amber", "violet", "mono")
DENSITY = ("compact", "comfy", "regular")
GLOW = ("on", "off")

# Must match home.js DEFAULT_LAYOUT — the package default when preferences
# have no saved `appearance.dashboard` blob yet.
DEFAULT_HOME_LAYOUT: tuple[dict[str, Any], ...] = (
    {"id": "w-fleet", "key": "fleet", "x": 0, "y": 0, "w": 8, "h": 3},
    {"id": "w-usage", "key": "usage", "x": 8, "y": 0, "w": 4, "h": 3},
    {"id": "w-agents", "key": "agents", "x": 0, "y": 3, "w": 6, "h": 4},
    {"id": "w-feed", "key": "feed", "x": 6, "y": 3, "w": 6, "h": 4},
    {"id": "w-workspaces", "key": "workspaces", "x": 0, "y": 7, "w": 6, "h": 2},
    {"id": "w-spawn", "key": "spawn", "x": 6, "y": 7, "w": 6, "h": 2},
)

_NEEDS_VALUE = ("theme", "accent", "density", "glow",
                "add_widget", "remove_widget", "move_widget", "resize_widget")


class DashboardCommandError(ValueError):
    """Raised when a dashboard command fails validation."""


def theme_catalog_hint(*, config_home: Path | None = None) -> str:
    """Agent-facing summary of valid theme ids + light/dark guidance."""
    from relaydeck import themes

    names = sorted(t.name for t in themes.list_themes(config_home=config_home))
    light = [n for n in ("base", "daylight") if n in names]
    dark = [n for n in ("ink", "gruvbox-dark") if n in names]
    parts = [f"available themes: {', '.join(names)}"]
    if light:
        parts.append(f"light UI → {', '.join(light)} (not 'light' or 'paper')")
    if dark:
        parts.append(f"dark UI → {', '.join(dark)}")
    return "; ".join(parts)


def effective_widget_layout(raw: Any) -> list[dict[str, Any]]:
    """Resolved widget list: saved layout or the package default."""
    if isinstance(raw, list) and raw:
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("key"):
                out.append(dict(item))
        if out:
            return out
    return [dict(w) for w in DEFAULT_HOME_LAYOUT]


def format_widget_layout(
    raw: Any,
    *,
    scope: str = "global",
    using_default: bool | None = None,
) -> str:
    """Human-readable Home grid for agents (`op=get`, prompts, CLI)."""
    saved = isinstance(raw, list) and bool(raw)
    layout = effective_widget_layout(raw)
    if using_default is None:
        using_default = not saved
    head = (
        f"Home widgets ({scope}{', package default' if using_default else ', saved'}) "
        "— 12 columns; each line is `widget @ (col x, row y) WxH cells`:"
    )
    lines = [
        f"- {w['key']} @ ({w.get('x', '?')},{w.get('y', '?')}) "
        f"{w.get('w', '?')}x{w.get('h', '?')}"
        for w in layout[:40]
    ]
    return head + "\n" + "\n".join(lines)


def _as_int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def build_dashboard_command(
    op: str,
    value: Any = None,
    *,
    x: Any = None,
    y: Any = None,
    w: Any = None,
    h: Any = None,
    known_themes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate a write op and return the ``dashboard.command`` data dict.

    Raises ``DashboardCommandError`` on anything invalid. ``op="get"`` is a
    read, not a command — callers handle it separately. When ``known_themes``
    is given, a ``theme`` op is validated against it (so we never set a
    dangling theme reference).
    """
    if op not in WRITE_OPS:
        raise DashboardCommandError(
            f"unknown dashboard op {op!r}; use one of {', '.join(WRITE_OPS)} (or 'get' to read)"
        )
    if op in _NEEDS_VALUE and not value:
        raise DashboardCommandError(f"dashboard op {op} needs a value")
    if op == "add_widget" and value not in WIDGETS:
        raise DashboardCommandError(f"unknown widget {value!r}; choose from {', '.join(WIDGETS)}")
    if op == "accent" and value not in ACCENTS:
        raise DashboardCommandError(f"unknown accent {value!r}; choose from {', '.join(ACCENTS)}")
    if op == "density" and value not in DENSITY:
        raise DashboardCommandError(f"unknown density {value!r}; choose from {', '.join(DENSITY)}")
    if op == "glow" and value not in GLOW:
        raise DashboardCommandError(f"unknown glow {value!r}; choose from {', '.join(GLOW)}")
    if op == "theme" and known_themes is not None:
        known = set(known_themes)
        if value not in known:
            raise DashboardCommandError(
                f"unknown theme {value!r}; known: {', '.join(sorted(known))}"
            )

    cmd: dict[str, Any] = {"op": op, "value": value}
    if op == "move_widget":
        xi, yi = _as_int(x), _as_int(y)
        if xi is None or yi is None:
            raise DashboardCommandError("move_widget needs x and y (0-based grid cells)")
        cmd["x"], cmd["y"] = xi, yi
    if op == "resize_widget":
        wi, hi = _as_int(w), _as_int(h)
        if wi is None or hi is None:
            raise DashboardCommandError("resize_widget needs w and h (grid cells)")
        cmd["w"], cmd["h"] = wi, hi
    return cmd
