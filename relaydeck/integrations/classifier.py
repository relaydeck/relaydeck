"""
Engine-source integrations (catalog entries for hookless harnesses).

Harnesses without a native vendor hook (codex, pi, opencode, cursor,
antigravity) get their semantic status from the built-in **semantic-status
engine** (`relaydeck/semantic_engine.py`), which derives
working / idle / complete-unread / awaiting-input from each running agent's
rendered screen. There is nothing to install per-harness — the engine runs
inside the daemon and covers every harness automatically.

These objects exist only so `relaydeck integration list` is honest about every
harness's signal source (engine vs. vendor hook), not just the ones that ship
an installable script. `kind` stays `"classifier"` for catalog back-compat (the
operator-facing label is "engine"); `install()` is a no-op that returns a
human explanation; `is_installed()` reflects that the engine is always present.

(Historically this fell back to a "mood classifier" in the `emote` plugin; that
plugin was removed and the screen-driven engine replaced it as the universal,
always-on source — so hookless harnesses now report status out of the box.)
"""

from __future__ import annotations


class _ClassifierIntegration:
    """Base shape for the engine-source harnesses. Only the harness id +
    description vary."""

    name: str = ""
    description: str = ""
    kind = "classifier"

    def state(self) -> str:
        # The semantic-status engine is built into the daemon and always
        # running, so the signal source for these harnesses is always present.
        return "installed"

    def is_installed(self) -> bool:
        return self.state() == "installed"

    def install(self) -> str:
        return (
            f"{self.name}: signal source is the built-in semantic-status "
            "classifier engine (no vendor hooks for this harness). It runs in "
            "the daemon and derives status from the agent's screen — nothing "
            "to install."
        )

    def uninstall(self) -> bool:
        # Nothing on disk to remove — the engine is part of the daemon.
        return False


class CodexClassifierIntegration(_ClassifierIntegration):
    name = "codex"
    description = "Codex CLI: engine-source (no native hook API; screen-driven)"


class PiClassifierIntegration(_ClassifierIntegration):
    name = "pi"
    description = "Pi SDK CLI: engine-source (screen-driven semantic status)"


class OpencodeClassifierIntegration(_ClassifierIntegration):
    name = "opencode"
    description = "Opencode CLI: engine-source (screen-driven semantic status)"


class CursorClassifierIntegration(_ClassifierIntegration):
    # Cursor has a native hook API (Cursor Hooks, beta), so a deterministic
    # kind="hook" integration is a future upgrade. Today it uses the built-in
    # engine source like the other hookless harnesses.
    name = "cursor"
    description = "Cursor CLI: engine-source (native hooks are a future upgrade)"


class AntigravityClassifierIntegration(_ClassifierIntegration):
    name = "antigravity"
    description = "Antigravity CLI: engine-source (screen-driven semantic status)"
