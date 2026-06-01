#!/usr/bin/env python3
"""Bump relaydeck's version in lockstep across the places that carry it.

Single source of truth at runtime is the installed package metadata
(`relaydeck/__init__.py` reads it via importlib), but three files hold the
literal and must agree for a clean release:

  1. pyproject.toml          [project] version  (feeds the wheel + metadata)
  2. relaydeck/__init__.py   the _resolve_version() fallback literal
  3. CHANGELOG.md            rolls [Unreleased] -> [X.Y.Z] - <today>
  4. uv.lock                 workspace package version (via `uv lock`)

Usage:
  uv run python scripts/bump_version.py patch        # 0.1.0 -> 0.1.1
  uv run python scripts/bump_version.py minor        # 0.1.0 -> 0.2.0
  uv run python scripts/bump_version.py major        # 0.1.0 -> 1.0.0
  uv run python scripts/bump_version.py 0.3.0        # explicit

Prints the new version on stdout (so CI can capture it). CHANGELOG rolling is
best-effort: if the file's shape is unexpected, the version bump still succeeds
and a warning is printed to stderr.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "relaydeck" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
UV_LOCK = ROOT / "uv.lock"


def _read_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    if not m:
        sys.exit("could not find [project] version in pyproject.toml")
    return m.group(1)


def _next(current: str, level: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", level):
        return level  # explicit version
    major, minor, patch = (int(x) for x in current.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    sys.exit(f"unknown level {level!r} (use major|minor|patch or an explicit X.Y.Z)")


def _sub_once(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if n != 1:
        sys.exit(f"expected exactly one match for {pattern!r} in {path}")
    path.write_text(new)


def _refresh_lockfile() -> None:
    """Re-sync uv.lock after a version bump (editable package entry)."""
    if not UV_LOCK.is_file():
        return
    try:
        subprocess.run(
            ["uv", "lock"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("warn: uv not on PATH; run `uv lock` before merging", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"warn: uv lock failed ({err}); run `uv lock` before merging", file=sys.stderr)


def _roll_changelog(new: str) -> None:
    if not CHANGELOG.is_file():
        return
    text = CHANGELOG.read_text()
    today = _dt.date.today().isoformat()
    # Move the accumulated [Unreleased] notes under a new [X.Y.Z] heading and
    # leave a fresh, empty [Unreleased] on top.
    m = re.search(r"^## \[Unreleased\]\s*\n", text, re.MULTILINE)
    if not m:
        print("warn: no '## [Unreleased]' heading; skipping CHANGELOG roll", file=sys.stderr)
        return
    insert_at = m.end()
    rolled = text[:insert_at] + f"\n## [{new}] - {today}\n" + text[insert_at:]
    CHANGELOG.write_text(rolled)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    current = _read_version()
    new = _next(current, sys.argv[1])
    if new == current:
        sys.exit(f"version is already {new}")

    _sub_once(PYPROJECT, r'^version\s*=\s*"[^"]+"', f'version = "{new}"')
    _sub_once(INIT, r'^    return "[^"]+"', f'    return "{new}"')
    _roll_changelog(new)
    _refresh_lockfile()

    print(new)


if __name__ == "__main__":
    main()
