"""
Atomic text writes.

`atomic_write_text` writes to a UNIQUE temp file in the destination
directory (via `tempfile.mkstemp`) and then `os.replace`s it into place.
The uniqueness is the point: a fixed `<name>.tmp` collides when two
writers touch the same target concurrently (a CLI `relaydeck github sync`
racing the daemon's poller, a duplicate worker after a plugin reload, or
overlapping ticks) — one renames the shared tmp away and the other's
`replace` fails with `FileNotFoundError: ....tmp -> ....json`. A
per-call temp name removes that race entirely. Mirrors the existing
mkstemp idiom in `auth.py` / `layouts.py` / `preferences.py` / `tls.py`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str) -> None:
    """Atomically write `text` to `path` (creating parent dirs). Safe
    under concurrent writers to the same path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
