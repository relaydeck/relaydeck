"""
Package-boundary drift guard (AGENTS.md hard rule #1's corollary).

Core (`relaydeck/`) is the engine + host contract. Every relaydeck-managed
plugin lives in the root `plugins/` package. The boundary is one-directional:

  **core must NEVER statically import the `plugins` package (or the legacy
  `relaydeck_plugins`).**

The loader reaches the plugins dynamically (`importlib` + entry points /
`_scan_package("plugins")`), never via a static `import plugins` — that keeps
`relaydeck` installable + bootable without the plugin set, and stops core from
secretly depending on a plugin. Plugins import core only through the public
facades (`relaydeck.sdk`, `relaydeck.harness`, `relaydeck.provider`,
`relaydeck.vault`, `relaydeck.automation`, `relaydeck.testing`).

This walks every core source file's AST (so comments / docstrings / dynamic
`importlib.import_module("plugins...")` strings don't false-positive) and fails
on any real `import plugins` / `from plugins ...` (or the legacy
`relaydeck_plugins` / `relaydeck.plugins.*`).
"""

from __future__ import annotations

import ast
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parent.parent / "relaydeck"

# Module roots core must never statically import.
_FORBIDDEN_EXACT = {"plugins", "relaydeck_plugins", "relaydeck.plugins"}
_FORBIDDEN_PREFIX = ("plugins.", "relaydeck_plugins.", "relaydeck.plugins.")


def _is_forbidden(name: str) -> bool:
    return name in _FORBIDDEN_EXACT or name.startswith(_FORBIDDEN_PREFIX)


def _core_py_files():
    return [p for p in _CORE_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_core_never_statically_imports_the_plugins_package():
    offenders: list[str] = []
    for path in _core_py_files():
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - core must parse
            offenders.append(f"{path}: unparseable")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        offenders.append(f"{path}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                # Ignore relative imports (node.level > 0) — those stay within
                # the same package, never reaching across to plugins.
                if node.level == 0 and _is_forbidden(node.module or ""):
                    offenders.append(f"{path}:{node.lineno} from {node.module}")

    assert not offenders, (
        "core (relaydeck/) must not statically import the plugins package — "
        "use the loader's dynamic discovery and the public facades instead:\n  "
        + "\n  ".join(offenders)
    )
