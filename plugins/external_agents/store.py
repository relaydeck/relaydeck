"""On-disk registry for external agents.

One JSON file per agent under `<config_home>/plugin-data/external/agents/`.
Plain functions (no `host` needed) so the daemon's plugin AND a daemon-less
CLI fallback read/write the exact same files. Writes are atomic (temp +
rename). Ids are sanitized so a crafted id can't escape the agents dir.

This is the persistence for the external-agent primitive — deliberately a
flat JSON store, not a core DB table, so the plugin stays additive (no
migration) and the spec can evolve freely.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import ExternalAgent, slug_id


def agents_dir(config_home: Path | str) -> Path:
    return Path(config_home) / "plugin-data" / "external" / "agents"


def _safe_id(agent_id: str) -> str:
    """Path-safe id. Reuses the same slug rule as id creation so a lookup
    can never traverse out of the agents dir."""
    sid = slug_id(agent_id)
    if not sid or sid in (".", ".."):
        raise ValueError(f"invalid external-agent id: {agent_id!r}")
    return sid


def _path(config_home: Path | str, agent_id: str) -> Path:
    return agents_dir(config_home) / f"{_safe_id(agent_id)}.json"


def exists(config_home: Path | str, agent_id: str) -> bool:
    return _path(config_home, agent_id).exists()


def get_agent(config_home: Path | str, agent_id: str) -> ExternalAgent | None:
    p = _path(config_home, agent_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ExternalAgent.from_dict(data)
    except (KeyError, ValueError, TypeError):
        return None


def list_agents(config_home: Path | str) -> list[ExternalAgent]:
    d = agents_dir(config_home)
    if not d.is_dir():
        return []
    out: list[ExternalAgent] = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            out.append(ExternalAgent.from_dict(data))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda a: a.id)
    return out


def save_agent(config_home: Path | str, agent: ExternalAgent) -> None:
    d = agents_dir(config_home)
    d.mkdir(parents=True, exist_ok=True)
    target = _path(config_home, agent.id)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(agent.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def delete_agent(config_home: Path | str, agent_id: str) -> bool:
    p = _path(config_home, agent_id)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False
