"""
Workspace-level health roll-up.

Every workspace has N agents, each with a process-level status
(`running`/`stopped`/`errored`/`pending`) and an observable
semantic status (`working`/`awaiting-input`/`complete-unread`/
`idle`). For a glance-at-the-fleet view (sidebar dot, dashboard
tab badge, `relaydeck workspace list`), we collapse those into a
single workspace-level state.

## Precedence (highest urgency first)

  1. `errored`        — any agent in `errored` process state
  2. `awaiting-input` — any running agent is blocked on operator
  3. `working`        — any agent is actively making progress
  4. `complete-unread` — at least one agent finished, none read yet
  5. `idle`           — agents present, none of the above
  6. `stopped`        — no agent running
  7. `empty`          — no agents at all in this workspace

The rule is "surface the most-attention-demanding child". An
operator scanning a fleet of workspaces wants the alarm
(errored) and the blocker (awaiting-input) to pop before
"everyone is busy".
"""

from __future__ import annotations

from typing import Iterable


# Highest-priority first. The walk picks the first state that
# matches at least one agent.
HEALTH_LEVELS = (
    "errored",
    "awaiting-input",
    "working",
    "complete-unread",
    "idle",
    "stopped",
    "empty",
)


# UI colors for each state. Keyed on the workspace health value;
# CLI/dashboard share this palette so the visual language is
# uniform across surfaces.
HEALTH_STYLES = {
    "errored":         "red bold",
    "awaiting-input":  "yellow bold",
    "working":         "cyan",
    "complete-unread": "magenta",
    "idle":            "green",
    "stopped":         "dim",
    "empty":           "dim",
}


def roll_up(agents: Iterable[dict]) -> str:
    """Collapse a workspace's agent list into one health state.

    `agents` is a list of dicts (the same shape `list_agents`
    returns). Pure function, no DB hits — the caller is
    responsible for fetching the rows once.
    """
    rows = list(agents)
    if not rows:
        return "empty"

    # Build a per-state presence map in one pass so we can ask
    # the precedence table cleanly below. We don't short-circuit
    # on the first errored row because we still want a counted
    # answer for diagnostics later if we expose one.
    has_errored = False
    has_awaiting = False
    has_working = False
    has_unread = False
    has_idle = False
    has_running = False

    for a in rows:
        status = (a.get("status") or "").strip()
        semantic = a.get("semantic_status")
        if status == "errored":
            has_errored = True
        if status == "running":
            has_running = True
        if semantic == "awaiting-input":
            has_awaiting = True
        elif semantic == "working":
            has_working = True
        elif semantic == "complete-unread":
            has_unread = True
        elif semantic == "idle":
            has_idle = True

    if has_errored:
        return "errored"
    if has_awaiting:
        return "awaiting-input"
    if has_working:
        return "working"
    if has_unread:
        return "complete-unread"
    if has_idle:
        return "idle"
    if has_running:
        # Running but no semantic status reported yet — treat as
        # idle for roll-up purposes. Otherwise a freshly-started
        # agent with no hook events yet would appear as `stopped`
        # at the workspace level which is misleading.
        return "idle"
    return "stopped"
