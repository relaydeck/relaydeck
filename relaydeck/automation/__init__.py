"""
relaydeck automations — the shared trigger + action contract.

This package owns the two pieces every relaydeck trigger depends on:

  * `actions` — the action dispatcher (`dispatch`, `ActionContext`,
    `ActionError`, `_HANDLERS`) shared by the github poller's rules and
    loop-agent automations.
  * `schedule` — `parse_schedule`, the one parser for `interval:` / `cron:` /
    `on_event:` trigger strings.

It lives in core so neither the github plugin nor the loop plugin has to
import the other, and so the HTTP automation routes never reach into a
plugin. One action schema across the codebase; one schedule parser.
"""

from __future__ import annotations

from relaydeck.automation.actions import (
    _HANDLERS,  # noqa: F401 — importable for core (api validation), not public API
    ActionContext,
    ActionError,
    dispatch,
)
from relaydeck.automation.schedule import parse_schedule


def action_kinds() -> list[str]:
    """The valid action kinds (e.g. for validating an automation config)."""
    return list(_HANDLERS.keys())


# `_HANDLERS` is intentionally NOT exported: it's a private dispatch table that
# core can still import for validation, but plugin authors should use the
# public `dispatch` / `action_kinds`.
__all__ = [
    "ActionContext",
    "ActionError",
    "action_kinds",
    "dispatch",
    "parse_schedule",
]
