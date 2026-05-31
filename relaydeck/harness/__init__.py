"""Public harness-authoring SDK for relaydeck.

This package is the stable seam every harness plugin builds on. The real
implementation — the PTY runtime base class plus the spawn-time prompt
composition helpers — lives in :mod:`relaydeck.harness.base` here in core, so
the harness plugins (which ship under ``plugins/harnesses/``) and core's own
prompt-composition / identity routes both import from this one place. Core
never reaches into a plugin to get the harness base.

A harness plugin subclasses :class:`HarnessAgent`, sets ``CLI``, and registers
its agent type via ``host.harnesses.register(...)``. See the bundled harnesses
under ``plugins/harnesses/`` for reference.
"""

from __future__ import annotations

from relaydeck.harness.base import (
    HarnessAgent,
    compose_git_context_block,
    compose_identity_preamble,
)

__all__ = [
    "HarnessAgent",
    "compose_git_context_block",
    "compose_identity_preamble",
]
