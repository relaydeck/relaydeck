"""
Vendor-side hook integrations.

Every PTY harness is covered by the built-in semantic-status engine. Where a
harness exposes a native lifecycle API, relaydeck may additionally install a
small vendor-side script that POSTs `/api/agents/{id}/state` events. Today
Claude Code has that deterministic hook; hookless harnesses appear in this
catalog as `kind="classifier"` and use the always-on screen engine.

Why split this out from the harness plugins themselves: the
hook lives in the harness's own filesystem (`~/.claude/hooks/`,
`~/.pi/agent/extensions/`, ...) and is installed once per
machine. The harness plugin in relaydeck is concerned with spawning
the PTY child and parsing its output stream; the integration
hook is concerned with semantic state telemetry from a
side-channel. Two different lifetimes, two different concerns.

## Adding a new integration

Each integration is a class implementing the `Integration`
protocol:

  - `name: str`           — harness id (`claude`, `codex`, ...)
  - `description: str`    — one-line operator-facing summary
  - `state()`             — installed / orphaned-* / not-installed
  - `is_installed()`      — True only when fully installed
  - `install()`           — write the hook(s), idempotent
  - `uninstall()`         — remove cleanly

Hook scripts are kept as string templates in this package so
the install command can render them with the operator's
daemon URL + auth token.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "Integration",
    "all_integrations",
    "fix_orphaned_hook_integrations",
    "get",
    "installed_hook_integrations",
    "integration_state",
    "register",
    "register_builtin_integrations",
    "uninstall_hook_integrations_before_config_removal",
]


class Integration(Protocol):
    """A vendor-side telemetry source.

    Two `kind`s exist today:

    - `"hook"` — installs a script into the vendor's native hook
      system (claude code's `~/.claude/hooks/`). Deterministic
      ground truth; `install()` writes files, `uninstall()` removes
      them.
    - `"classifier"` — no vendor hooks available; the harness's status
      comes from the built-in **semantic-status engine**
      (`relaydeck/semantic_engine.py`), which derives it from the
      agent's rendered screen. `install()` is a no-op that returns a
      documentary string; `is_installed()` is always True because the
      engine runs inside the daemon for every harness.

    The split keeps `relaydeck integration list` honest: an operator
    inspecting their setup sees every harness with the actual
    signal source it's using, not just the ones with installable
    files.
    """

    name: str
    description: str
    kind: str  # "hook" | "classifier"

    def state(self) -> str: ...
    def is_installed(self) -> bool: ...
    def install(self) -> str: ...
    def uninstall(self) -> bool: ...


# States where the right fix is to UNINSTALL — one side of the install
# pair is missing (script or registration), so there's nothing coherent
# left to keep. `outdated` is deliberately NOT in this set: both halves
# exist and are wired, only the script body diverges from what we'd render
# now — the right fix is to RE-INSTALL (which is idempotent and rewrites
# the body), not remove the hook entirely. The agent-spawn path's
# `_ensure_state_hook` re-renders the same way on next spawn, so an
# outdated hook self-heals; `doctor --fix` just brings it forward.
_ORPHAN_STATES = frozenset({
    "orphaned-registration",
    "orphaned-script",
})

_REGENERATE_STATES = frozenset({"outdated"})


_registry: dict[str, Integration] = {}


def register(integration: Integration) -> None:
    _registry[integration.name] = integration


def get(name: str) -> Integration | None:
    return _registry.get(name)


def all_integrations() -> list[Integration]:
    return list(_registry.values())


def integration_state(integration: Integration) -> str:
    """Return the integration's lifecycle state (installed, orphaned-*, …)."""
    st = integration.state()
    return st if st else "not-installed"


def fix_orphaned_hook_integrations() -> list[tuple[str, str]]:
    """Heal drifted hook integrations. Returns [(name, action), …] where
    action is "uninstalled" (orphan halves cleaned) or "regenerated"
    (outdated script body rewritten). Callers print human output.

    The split matters: orphan-state hooks have ONE half missing and
    nothing coherent to keep — uninstall both sides. `outdated` has both
    halves wired correctly but the script body diverges from what we'd
    render now — rewriting the script (idempotent `install()`) preserves
    telemetry; an `uninstall()` would silently disable the hook until
    the operator's next claude-code spawn re-installs it via
    `_ensure_state_hook`.
    """
    register_builtin_integrations()
    fixed: list[tuple[str, str]] = []
    for it in all_integrations():
        if getattr(it, "kind", "hook") != "hook":
            continue
        st = integration_state(it)
        if st in _REGENERATE_STATES:
            try:
                it.install()  # idempotent re-render
                fixed.append((it.name, "regenerated"))
            except Exception as exc:
                # Don't silently drop — record so the doctor's --fix
                # output shows an explicit failure line (with the
                # exception in the log) instead of "no action" followed
                # by the same warn the operator just tried to fix.
                logger.warning(
                    "integration %s regenerate failed: %s", it.name, exc,
                )
                fixed.append((it.name, "regenerate-failed"))
        elif st in _ORPHAN_STATES:
            try:
                if it.uninstall():
                    fixed.append((it.name, "uninstalled"))
            except Exception as exc:
                logger.warning(
                    "integration %s uninstall failed: %s", it.name, exc,
                )
                fixed.append((it.name, "uninstall-failed"))
    return fixed


# States where the integration STILL has a registration in vendor config
# (e.g. ~/.claude/settings.json) that a `rm -rf ~/.relaydeck` would orphan.
# Excludes `orphaned-script`: that state is "local script exists, but no
# vendor entry" — `rm -rf ~/.relaydeck` deletes the script and there's
# nothing on the vendor side to clean up afterwards, so the daemon-stop
# hint would just be noise.
_VENDOR_TRACE_STATES = frozenset({
    "installed",
    "orphaned-registration",
    "outdated",
})


def installed_hook_integrations() -> list[str]:
    """Names of hook integrations leaving vendor-side traces today — the
    set whose registrations would be orphaned by a `rm -rf ~/.relaydeck`.
    Used by the `daemon stop` hint to decide whether to prod the operator
    toward `relaydeck integration cleanup-all`."""
    register_builtin_integrations()
    names: list[str] = []
    for it in all_integrations():
        if getattr(it, "kind", "hook") != "hook":
            continue
        try:
            if integration_state(it) in _VENDOR_TRACE_STATES:
                names.append(it.name)
        except Exception:
            continue
    return names


def uninstall_hook_integrations_before_config_removal() -> list[str]:
    """Remove vendor-side hook registrations before deleting ~/.relaydeck.

    Call this (or `relaydeck integration cleanup-all`) before `rm -rf
    ~/.relaydeck` so hook entries in vendor config (e.g.
    ~/.claude/settings.json) are stripped while the integration paths
    are still known.
    """
    register_builtin_integrations()
    removed: list[str] = []
    for it in all_integrations():
        if getattr(it, "kind", "hook") != "hook":
            continue
        if integration_state(it) == "not-installed":
            continue
        try:
            if it.uninstall():
                removed.append(it.name)
        except Exception:
            continue
    return removed


def register_builtin_integrations() -> None:
    """Register every in-tree integration. Idempotent."""
    from relaydeck.integrations.classifier import (
        AntigravityClassifierIntegration,
        CodexClassifierIntegration,
        CursorClassifierIntegration,
        OpencodeClassifierIntegration,
        PiClassifierIntegration,
    )
    from relaydeck.integrations.claude import ClaudeIntegration

    register(ClaudeIntegration())
    register(CodexClassifierIntegration())
    register(PiClassifierIntegration())
    register(OpencodeClassifierIntegration())
    register(CursorClassifierIntegration())
    register(AntigravityClassifierIntegration())
