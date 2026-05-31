"""
Saved viewer layouts.

A layout is a small named bundle: which workspace, which viewer
(tmux / ghostty / print / relaydeck-view), which session name, whether
to include stopped agents. `relaydeck workspace view --save NAME`
writes one; `--restore NAME` reads one and re-runs `relaydeck workspace
view` with the captured flags.

Saved layouts live in `~/.relaydeck/layouts/<name>.yaml`, one
file per layout. Plain YAML, no schema migration story — these
are operator conveniences, not load-bearing state. If the user
deletes the file, the next `--restore` returns "no such layout"
and the worst outcome is they re-create one with a single command.

Mirrors the same patterns as state.yaml: mode 0600, atomic write
via temp + rename, defensive load that returns `None` on parse
failure rather than crashing the CLI.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Layout:
    """The captured shape of a `relaydeck workspace view` invocation.

    Fields mirror the CLI flags one-for-one so restore is just
    "play the same command back" — no behavioral drift between
    saving and restoring."""

    name: str
    workspace: str
    viewer: str | None = None         # --viewer
    session: str | None = None        # --session
    include_stopped: bool = False     # --include-stopped
    force: bool = False               # --force
    agents: list[str] = field(default_factory=list)  # informational
    # `agents` is captured for human readability — at restore time
    # we re-query the running set from the daemon, since agents
    # come and go between snapshots. The list lets `relaydeck layout
    # show` print what *was* attached without round-tripping to
    # the daemon.

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Layout:
        return cls(
            name=str(data.get("name") or ""),
            workspace=str(data.get("workspace") or ""),
            viewer=data.get("viewer"),
            session=data.get("session"),
            include_stopped=bool(data.get("include_stopped") or False),
            force=bool(data.get("force") or False),
            agents=list(data.get("agents") or []),
        )


def _layouts_dir(config_home: Path | None = None) -> Path:
    home = config_home or (Path.home() / ".relaydeck")
    return home / "layouts"


def _layout_path(name: str, config_home: Path | None = None) -> Path:
    safe = _sanitize_name(name)
    return _layouts_dir(config_home) / f"{safe}.yaml"


def _sanitize_name(name: str) -> str:
    """Restrict layout names to filename-safe characters. Layouts
    map 1:1 to files; an operator who types
    `--save ../../etc/passwd` should land on a name that can only
    write under the layouts directory."""
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    return "".join(c if c in keep else "_" for c in name).strip("._") or "unnamed"


# ── Public API ─────────────────────────────────────────────────────


def save(layout: Layout, *, config_home: Path | None = None) -> Path:
    """Persist a layout to `<config>/layouts/<name>.yaml`. Returns
    the path written. Atomic — writes to a temp file in the same
    directory and renames, so a crash mid-write can't corrupt an
    existing layout file."""
    import yaml

    path = _layout_path(layout.name, config_home)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = layout.to_dict()
    body = yaml.safe_dump(data, sort_keys=True, default_flow_style=False)

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(name: str, *, config_home: Path | None = None) -> Layout | None:
    """Read a saved layout. Returns `None` if the file doesn't
    exist or can't be parsed — callers print "no such layout" and
    move on rather than crash."""
    import yaml

    path = _layout_path(name, config_home)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            return None
        # The on-disk `name` might mismatch the requested one
        # if the operator hand-renamed a file. Prefer the
        # filename-derived name so list/restore stay consistent.
        data.setdefault("name", _sanitize_name(name))
        return Layout.from_dict(data)
    except Exception as exc:
        logger.warning("layout %s load failed: %s", name, exc)
        return None


def delete(name: str, *, config_home: Path | None = None) -> bool:
    """Remove a saved layout. Returns True iff a file was actually
    deleted (so the CLI can distinguish "removed" from "wasn't
    there")."""
    path = _layout_path(name, config_home)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("layout %s delete failed: %s", name, exc)
        return False


def list_layouts(*, config_home: Path | None = None) -> list[Layout]:
    """Return every saved layout, alphabetical by name. Missing
    or malformed files are skipped (defensive — `relaydeck layout list`
    must never crash on a half-edited yaml)."""
    d = _layouts_dir(config_home)
    if not d.exists():
        return []
    out: list[Layout] = []
    for child in sorted(d.iterdir()):
        if child.is_file() and child.suffix in (".yaml", ".yml"):
            name = child.stem
            lay = load(name, config_home=config_home)
            if lay is not None:
                out.append(lay)
    return out
