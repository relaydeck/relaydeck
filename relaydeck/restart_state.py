"""Restart-to-apply: detect when a running agent is behind its edited config.

A harness bakes its config at spawn — the composed system prompt, the enabled
plugins, the injected skills, the model, and launch flags. Editing any of those
leaves the live process stale until it restarts. We capture a structured
snapshot of that config each time an agent (re)starts (`agents.running_config`)
and diff it against the freshly-computed *desired* config to answer two things:
is a restart pending, and **what changed** (so the UI can say "skill grill-me
added", not just "something changed").

The desired snapshot reuses `compose_prompt_components` — the same source of
truth (and the same gating) the harness uses at spawn — so detection can't
drift from reality. Pure + read-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_SKILL_LABEL = "skill · "

# Config keys that change the spawn command (and so need a restart to apply).
# Session intent (resume / fresh / continue) is one-shot per spawn and is
# deliberately excluded — it isn't a persisted config change. `command` is a
# full argv override (pi/base honor config["command"]); editing it must restart.
_LAUNCH_KEYS = (
    "autonomy", "plan", "plan_mode", "dangerous", "danger",
    "args", "extra_args", "command", "endpoint", "base_url", "reasoning", "thinking",
)


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _peers(db_path: str | Path, workspace: str | None, agent_id: str) -> list[dict]:
    if not workspace:
        return []
    from relaydeck.db import open_db
    try:
        conn = open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT id, type, purpose FROM agents "
                "WHERE workspace = ? AND id != ? ORDER BY id ASC",
                (workspace, agent_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def compute_snapshot(
    config_home: str | Path,
    db_path: str | Path,
    *,
    agent_id: str,
    agent_type: str = "",
    workspace: str | None = None,
    purpose: str = "",
    system_prompt: str = "",
    inject_preamble: bool = True,
    config: dict | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """The structured spawn-time config snapshot for one agent.

    Reuses `compose_prompt_components` (same gating as the harness) so the
    prompt/skill parts can't diverge from what the model actually receives.
    """
    from relaydeck.prompt_composition import (
        _workspace_plugins,
        compose_prompt_components,
    )

    config = config or {}
    home = Path(config_home)
    peers = _peers(db_path, workspace, agent_id)
    comp = compose_prompt_components(
        home,
        agent_id=agent_id,
        workspace=workspace,
        agent_type=agent_type,
        purpose=purpose,
        system_prompt=system_prompt,
        peers=peers,
        inject_preamble=inject_preamble,
    )
    prompt_parts: list[str] = []
    skills: dict[str, str] = {}
    for c in comp.get("components", []):
        label = str(c.get("label", ""))
        if label.startswith(_SKILL_LABEL):
            skills[label[len(_SKILL_LABEL):]] = _sha(c.get("text", ""))
        else:
            prompt_parts.append(c.get("text", ""))

    plugins = sorted(_workspace_plugins(home, workspace)) if workspace else []
    # The model can arrive three ways: a relaydeck preset/model ref, the
    # relaydeck-native model_override, OR a harness-specific override key
    # (codex_model / claude_model / cursor_model / opencode_model). Resolve to
    # the actual `--model` value the harness launches with so swapping a native
    # harness's model is detected — otherwise the running agent stays stale with
    # no restart chip.
    model = config.get("preset") or config.get("model") or model_override or ""
    try:
        from relaydeck.harness_model import cli_model_for_agent

        cli = cli_model_for_agent(agent_type, config, config_home=home)
        if cli:
            model = cli
    except Exception:
        pass
    launch = {k: config.get(k) for k in _LAUNCH_KEYS if k in config}
    return {
        "prompt": _sha("\n\x00\n".join(prompt_parts)),
        "plugins": plugins,
        "skills": skills,
        "model": str(model),
        "launch": _sha(json.dumps(launch, sort_keys=True, default=str)),
    }


def snapshot_for_agent(
    config_home: str | Path, db_path: str | Path, agent_id: str,
) -> dict[str, Any] | None:
    """Desired snapshot for a persisted agent — loads its YAML spec the same
    way the spawn path does. None if the spec is missing/unreadable."""
    from relaydeck.config import AgentSpec

    spec_path = Path(config_home) / "agents" / f"{agent_id}.yaml"
    if not spec_path.exists():
        return None
    try:
        spec = AgentSpec.from_yaml(spec_path)
    except Exception:
        return None
    return compute_snapshot(
        config_home, db_path,
        agent_id=agent_id,
        agent_type=getattr(spec, "type", "") or "",
        workspace=getattr(spec, "workspace", None),
        purpose=getattr(spec, "purpose", "") or "",
        system_prompt=getattr(spec, "system_prompt", "") or "",
        inject_preamble=bool(getattr(spec, "inject_identity_preamble", True)),
        config=getattr(spec, "config", {}) or {},
        model_override=getattr(spec, "model_override", None),
    )


def diff_snapshots(running: dict | None, desired: dict | None) -> list[dict[str, str]]:
    """Human-readable list of what changed between the captured (running) and
    desired snapshots. Empty when nothing spawn-affecting changed."""
    changes: list[dict[str, str]] = []
    if not running or not desired:
        return changes
    if running.get("prompt") != desired.get("prompt"):
        changes.append({"component": "prompt", "summary": "system prompt changed"})

    rp, dp = set(running.get("plugins") or []), set(desired.get("plugins") or [])
    for p in sorted(dp - rp):
        changes.append({"component": "plugins", "summary": f"plugin {p} enabled"})
    for p in sorted(rp - dp):
        changes.append({"component": "plugins", "summary": f"plugin {p} disabled"})

    rs = dict(running.get("skills") or {})
    ds = dict(desired.get("skills") or {})
    for s in sorted(set(ds) - set(rs)):
        changes.append({"component": "skills", "summary": f"skill {s} added"})
    for s in sorted(set(rs) - set(ds)):
        changes.append({"component": "skills", "summary": f"skill {s} removed"})
    for s in sorted(set(rs) & set(ds)):
        if rs[s] != ds[s]:
            changes.append({"component": "skills", "summary": f"skill {s} edited"})

    if running.get("model") != desired.get("model"):
        changes.append({
            "component": "model",
            "summary": f"model {running.get('model') or '—'} → {desired.get('model') or '—'}",
        })
    if running.get("launch") != desired.get("launch"):
        changes.append({"component": "launch", "summary": "launch settings changed"})
    return changes


def restart_state(running_config: dict | None, desired: dict | None) -> dict[str, Any]:
    """`{restart_pending: bool, pending_changes: [{component, summary}]}`.

    No running snapshot (legacy agent that started before this shipped, or one
    that never started) → not pending; there's nothing to compare against, and
    we don't want to nag about a baseline we can't establish."""
    changes = diff_snapshots(running_config or {}, desired or {})
    return {"restart_pending": bool(changes), "pending_changes": changes}
