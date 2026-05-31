"""
Issue/PR → working-agent spawn workflow.

The higher-level command the reviewer asked for (not "just another rule
action"): given a repo, a branch, and an upstream ref, stand up an
isolated working environment and an agent to drive it:

  1. `git worktree add` a fresh working tree on its own branch,
  2. register it as a normal relaydeck workspace (via the gated
     `host.workspaces.create`, which fires `workspace.added`),
  3. create + start an agent in that workspace (`host.agents.create/start`),
  4. record a task linking the agent ↔ the upstream ref
     (`host.tasks.create`).

Everything goes through the capability-gated `host.*` surface — no
private orchestrator calls, no `relaydeck` shell-outs. It runs in the daemon
process (where the host + orchestrator live); the CLI/API are thin
front-doors that call it there.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Capabilities the workflow needs. Checked up front so a missing grant
# fails BEFORE any side effect (no orphaned worktree from a permission
# error).
_REQUIRED_CAPS = ("workspaces.create", "agents.create", "agents.start", "tasks.write")


def spawn_worktree_agent(
    host: Any,
    *,
    repo_path: str | Path,
    branch: str,
    agent_id: str,
    agent_type: str = "claude-code",
    workspace_name: str | None = None,
    base: str | None = None,
    create_branch: bool = True,
    purpose: str = "",
    kind: str = "issue",
    external_ref: str | None = None,
    title: str = "",
    agent_config: dict[str, Any] | None = None,
    workspace_plugins: list[str] | None = None,
) -> dict[str, Any]:
    """Run the full spawn workflow and return a summary dict.

    Failure handling is transactional-ish: capabilities are checked
    before touching git, and any failure *after* the worktree exists
    triggers best-effort rollback (delete the agent, unregister the
    workspace, remove the worktree) so a failed spawn doesn't leave
    orphaned state on disk. `workspace_name` defaults to a sanitized
    `branch`; the worktree lands under `<config_home>/worktrees/<name>`.
    """
    from relaydeck.worktrees import WorktreeError, create_worktree, is_git_repo, sanitize_workspace_name

    # 0. Preflight capabilities — fail fast, before any side effect.
    gate = getattr(host, "_gate", None)
    if gate is not None:
        for cap in _REQUIRED_CAPS:
            gate.require(cap)

    repo = Path(repo_path).expanduser().resolve()
    if not is_git_repo(repo):
        raise WorktreeError(
            f"{repo} is not a git repository — spawn needs git. "
            f"Run `git init` (or clone) there first; relaydeck never auto-inits git."
        )
    ws_name = sanitize_workspace_name(workspace_name or branch)
    worktree_path = host.config_home / "worktrees" / ws_name

    # 1. git worktree (raises WorktreeError on failure — nothing registered yet)
    create_worktree(repo, worktree_path, branch, base=base, create_branch=create_branch)

    workspace_registered = False
    agent_created = False
    try:
        # 2. register as a workspace (gated; fires workspace.added)
        host.workspaces.create(
            ws_name, worktree_path, plugins=list(workspace_plugins or [])
        )
        workspace_registered = True

        # 3. create + start the agent in the new workspace
        host.agents.create(
            agent_id, agent_type, agent_id,
            workspace=ws_name, purpose=purpose, config=agent_config or {},
        )
        agent_created = True
        host.agents.start(agent_id)

        # 4. record the task linking agent ↔ upstream ref
        task = host.tasks.create(
            kind=kind, workspace=ws_name, agent_id=agent_id,
            external_ref=external_ref, title=title, status="in_progress",
        )
    except Exception:
        _rollback(
            host, repo, worktree_path, ws_name, agent_id,
            agent_created=agent_created,
            workspace_registered=workspace_registered,
        )
        raise

    return {
        "workspace": ws_name,
        "path": str(worktree_path.resolve()),
        "branch": branch,
        "agent_id": agent_id,
        "task_id": task.id,
        "external_ref": external_ref,
    }


def _rollback(
    host: Any,
    repo: Path,
    worktree_path: Path,
    ws_name: str,
    agent_id: str,
    *,
    agent_created: bool,
    workspace_registered: bool,
) -> None:
    """Best-effort undo of partial spawn state, in reverse order. Every
    step is independently guarded — one cleanup failure must not mask the
    original error (which is re-raised by the caller)."""
    from relaydeck.worktrees import remove_worktree

    if agent_created:
        orch = getattr(host, "_orchestrator", None)
        if orch is not None and hasattr(orch, "delete_agent"):
            with suppress(Exception):
                orch.delete_agent(agent_id)
    if workspace_registered:
        with suppress(Exception):
            from relaydeck.config import unregister_workspace
            unregister_workspace(host.config_home, ws_name)
    with suppress(Exception):
        remove_worktree(repo, worktree_path, force=True)
    logger.warning(
        "github spawn rolled back partial state for %s (agent=%s, ws=%s)",
        worktree_path, agent_id, ws_name,
    )
