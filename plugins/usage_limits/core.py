"""
Window math for usage limits — kept pure / I/O-free so it can be
unit-tested without the daemon, orchestrator, or DB.

Two rolling windows mirror the Claude Pro/Max plan shape:

  * **Session window** — short rolling window (default 5 hours) meant
    to throttle bursts within a single working block.
  * **Weekly window** — long rolling window (default 7 days = 168
    hours) meant to enforce a longer-horizon spend ceiling.

Each window has:
  - duration in hours
  - token budget (0 = unlimited / disabled)
  - per-agent override (optional)

Given a list of `(ts, total_tokens)` usage records (cheap to fetch
from `usage_records` via SUM-with-WHERE), `compute_window_state`
returns the current consumption, percentage used, time until reset,
and threshold classifications. Plugin glue (CLI, API, event emits)
wraps this with no extra logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowConfig:
    """Static configuration for one rolling window.

    `budget_tokens=0` is interpreted as "unlimited / disabled" — the
    window is still tracked but never triggers warn/exceeded events.
    """

    name: str
    duration_hours: float
    budget_tokens: int


@dataclass(frozen=True)
class WindowState:
    """Computed state of one window at a point in time.

    `reset_at_ts` is the wall-clock time when the *oldest* usage event
    in the window falls off, freeing up that much budget. Operators and
    the dashboard surface use this for the "resets in 1h 23m" line.
    """

    name: str
    duration_hours: float
    budget_tokens: int
    used_tokens: int
    pct_used: float
    reset_at_ts: float
    seconds_until_reset: float
    state: str  # "ok" | "warn" | "exceeded" | "disabled"

    def is_disabled(self) -> bool:
        return self.budget_tokens <= 0


def compute_window_state(
    *,
    config: WindowConfig,
    records: list[tuple[float, int]],
    now_ts: float | None = None,
    warn_threshold_pct: float = 75.0,
) -> WindowState:
    """Roll `records` (ts, total_tokens) up against `config`.

    Records older than `duration_hours` are discarded. `reset_at_ts`
    is the timestamp at which the oldest in-window record falls out
    (its ts + duration). If the window is empty the reset is "now"
    (no waiting required).
    """
    now = now_ts if now_ts is not None else time.time()
    window_seconds = config.duration_hours * 3600.0
    cutoff = now - window_seconds

    in_window = [(ts, tok) for ts, tok in records if ts >= cutoff]
    used = sum(tok for _, tok in in_window)

    if config.budget_tokens <= 0:
        state = "disabled"
        pct = 0.0
    elif used >= config.budget_tokens:
        state = "exceeded"
        pct = 100.0 * used / max(1, config.budget_tokens)
    else:
        pct = 100.0 * used / config.budget_tokens
        state = "warn" if pct >= warn_threshold_pct else "ok"

    if in_window:
        oldest_ts = min(ts for ts, _ in in_window)
        reset_at = oldest_ts + window_seconds
    else:
        reset_at = now

    return WindowState(
        name=config.name,
        duration_hours=config.duration_hours,
        budget_tokens=config.budget_tokens,
        used_tokens=used,
        pct_used=pct,
        reset_at_ts=reset_at,
        seconds_until_reset=max(0.0, reset_at - now),
        state=state,
    )


def resolve_agent_budget(
    *,
    base_budget: int,
    agent_overrides: dict | None,
) -> int:
    """Apply a per-agent override on top of the plugin-wide budget.

    Two override shapes are supported (mirrors how users actually want
    to express this):

      - Absolute: `{"absolute": 50000}` → return 50000 verbatim.
      - Multiplier: `{"multiplier": 2.0}` → return `base * 2`.

    No override (None / unrecognized) returns `base_budget` unchanged.
    """
    if not agent_overrides:
        return base_budget
    absolute = agent_overrides.get("absolute")
    if isinstance(absolute, (int, float)) and absolute >= 0:
        return int(absolute)
    mult = agent_overrides.get("multiplier")
    if isinstance(mult, (int, float)) and mult >= 0:
        return int(base_budget * mult)
    return base_budget


def fmt_eta(seconds: float) -> str:
    """Human reset countdown — used by the CLI status table and the
    dashboard tile. Single short form: '0s', '45s', '12m', '3h 12m',
    '2d 4h'."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        return f"{h}h {rem // 60}m" if rem >= 60 else f"{h}h"
    d, rem = divmod(s, 86400)
    return f"{d}d {rem // 3600}h" if rem >= 3600 else f"{d}d"
