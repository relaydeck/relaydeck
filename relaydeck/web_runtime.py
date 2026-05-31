"""Runtime lookup for dashboard assets.

The dashboard ships in-tree today under ``relaydeck/web/static``. A split
``relaydeck-web`` distribution can provide the same static tree by setting
``RELAYDECK_WEB_PACKAGE`` to an importable package, or local frontend dev can
point ``RELAYDECK_WEB_STATIC_DIR`` at a checkout/build directory.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path


def web_static_dir() -> Path:
    override = os.environ.get("RELAYDECK_WEB_STATIC_DIR")
    if override:
        return Path(override).expanduser()

    package = os.environ.get("RELAYDECK_WEB_PACKAGE")
    if package:
        resolved = _package_static_dir(package)
        if resolved is not None:
            return resolved

    # Optional override: a separately-packaged `relaydeck_web` dashboard. Not
    # present in the bundled install, so fall through to the source tree.
    resolved = _package_static_dir("relaydeck_web")
    if resolved is not None:
        return resolved

    return Path(__file__).resolve().parent / "web" / "static"


def _package_static_dir(package: str) -> Path | None:
    try:
        root = resources.files(package)
    except (ImportError, ModuleNotFoundError, TypeError):
        return None

    static = root / "static"
    if static.is_dir():
        return Path(str(static))
    if root.is_dir():
        return Path(str(root))
    return None
