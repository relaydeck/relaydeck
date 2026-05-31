"""
Schedule parsing for relaydeck automations.

A schedule string names a trigger cadence: `interval:30s`, `cron:0 9 * * 1-5`,
or `on_event:agent.error`. The loop agent's `__init__`, the HTTP automation
routes, and the Workers lens all parse schedules through `parse_schedule` so
the contract stays identical wherever a trigger is configured.

Lives in core (not a plugin) for the same reason as the action dispatcher:
it is part of the host's automation contract, not an implementation detail
of the loop plugin.
"""

from __future__ import annotations

import re
from typing import Any

# Supported interval units. Anything else raises at config parse.
_INTERVAL_UNITS: dict[str, float] = {"s": 1.0, "m": 60.0, "h": 3600.0}
_INTERVAL_RE = re.compile(r"^(\d+)\s*([smh])$")


def parse_schedule(schedule: str) -> tuple[str, Any]:
    """Parse `interval:30s` / `on_event:agent.*` / `cron:…` into `(kind, value)`.

    Returns `("interval", float_seconds)`, `("on_event", pattern_str)`, or
    `("cron", expr_str)`. Raises ValueError on shape or unit errors.
    Centralized so the loop agent's __init__, the API routes, and tests use
    the same parser.
    """
    if not isinstance(schedule, str) or ":" not in schedule:
        raise ValueError(
            f"schedule must be 'interval:<N><s|m|h>' or 'on_event:<pattern>', "
            f"got {schedule!r}"
        )
    kind, _, value = schedule.partition(":")
    kind = kind.strip()
    value = value.strip()
    if kind == "interval":
        m = _INTERVAL_RE.match(value)
        if not m:
            raise ValueError(
                f"interval must be '<N>s' / '<N>m' / '<N>h', got {value!r}"
            )
        n, unit = m.groups()
        return ("interval", float(n) * _INTERVAL_UNITS[unit])
    if kind == "on_event":
        if not value:
            raise ValueError("on_event requires a non-empty pattern")
        return ("on_event", value)
    if kind == "cron":
        try:
            from croniter import croniter
        except ImportError as exc:
            # croniter is a core relaydeck dependency, so this only fires on a
            # broken/partial install — surface that rather than a bare traceback.
            raise ValueError(
                "cron schedules need croniter, which ships with relaydeck — this "
                "import failure points to a broken install; reinstall relaydeck."
            ) from exc
        if not value or not croniter.is_valid(value):
            raise ValueError(f"invalid cron expression: {value!r}")
        return ("cron", value)
    raise ValueError(f"unknown schedule kind {kind!r}")
