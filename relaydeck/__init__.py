"""
relaydeck — local-first fleet OS for CLI coding agents.

Harness-native, plugin-extensible. One daemon per machine, agents wrapping
CLI harnesses (pi, claude-code, codex, cursor, opencode), web
dashboard at localhost:8765. Everything is a plugin.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path


def _resolve_version() -> str:
    """Single source of truth for the version: the installed `relaydeck`
    package metadata (which `pyproject.toml [project] version` feeds). Falls
    back to the pyproject literal for an uninstalled source checkout."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("relaydeck")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return "0.1.3"


__version__ = _resolve_version()


def main() -> None:
    """Entry point for the `relaydeck` CLI."""
    from relaydeck.plugin import PluginContext, get_registry
    from relaydeck.transports.cli import _register_plugin_cli
    from relaydeck.transports.cli import main as cli_main

    # Load plugins so commands like `relaydeck metering`, `relaydeck vault`, etc.
    # are wired into the click group BEFORE click parses argv. The
    # daemon (`relaydeck serve`) also calls registry.load_all() — that's a
    # no-op when already loaded.
    home = Path.home() / ".relaydeck"
    registry = get_registry(home)
    if not registry.all():
        with suppress(Exception):
            registry.load_all(PluginContext(config_home=home, workspace_path=None))
    _register_plugin_cli(registry, cli_main)

    cli_main()


if __name__ == "__main__":
    main()
