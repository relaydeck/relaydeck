"""
Git worktree → relaydeck task-workspace helper.

A "task workspace" is just a normal registered relaydeck workspace whose path
happens to be a git worktree — a separate working tree off a shared
repo, on its own branch. This is how a fleet works several PRs/issues in
parallel without trampling each other's checkout, and it's a *workspace
created by a plugin*, not a new "mode" (relaydeck has no modes — see AGENTS.md).

The git operations are plain `git worktree` subprocess calls (git is a
tool the operator already trusts, like `gh`). Registration goes through
`relaydeck.config.register_workspace`, the same path `relaydeck workspace add`
uses, so a worktree workspace is indistinguishable from a hand-added one
to the rest of the system. Plugin-owned metadata (which PR a worktree
belongs to, etc.) lives in the task store, not in a bespoke workspace
field.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class WorktreeError(RuntimeError):
    """A `git worktree` command failed."""


# A repo declares default worktree lifecycle hooks here so every worktree
# off it provisions itself the same way (the Twitter ask: "attach a custom
# setup+shutdown script so agents work in a fully set-up environment").
#   <repo>/.relaydeck/worktree.yaml
#     setup: ./scripts/dev-setup.sh      # runs after create (cwd = worktree)
#     teardown: ./scripts/dev-teardown.sh # runs before remove
#     env: { KEY: value }                # extra env for both hooks
# An explicit setup=/teardown= on the create/remove call overrides the file.
WORKTREE_CONFIG_REL = Path(".relaydeck") / "worktree.yaml"

# Per-path git metadata cache. Keyed on resolved path (case-folded on
# case-insensitive filesystems) with a TTL ceiling. We deliberately do
# NOT stamp on `.git` mtime: even read-only git subprocesses (status,
# rev-parse) touch direct entries of `.git/` (lockfiles, index refresh)
# which would self-bust the cache the moment we populated it. The TTL is
# set above the dashboard heartbeat (12s in data.js) so consecutive
# heartbeat polls hit the cache; relaydeck-driven worktree mutations
# explicitly call `clear_workspace_git_info_cache` so newly-created
# branches show up immediately; externally-driven changes (operator
# commits, branch switches outside relaydeck) bleed in via the TTL.
# Align with dashboard LiveStore heartbeat (12s in data.js) so /api/workspaces
# picks up git init / branch switches without a long stale "no git" chip.
_GIT_INFO_TTL_S = 12.0
_CASE_INSENSITIVE_FS = (
    os.name == "posix" and os.uname().sysname == "Darwin"
) or os.name == "nt"
_git_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_git_info_cache_lock = threading.Lock()


def _git_cache_key(resolved: Path) -> str:
    """Cache key for `resolved` — case-folded on case-insensitive filesystems
    so two registrations differing only in casing share one entry."""
    s = str(resolved)
    return s.casefold() if _CASE_INSENSITIVE_FS else s


def clear_workspace_git_info_cache() -> None:
    """Drop cached per-path git metadata (tests + worktree mutations)."""
    with _git_info_cache_lock:
        _git_info_cache.clear()


def _git(repo_path: Path, *args: str, timeout: float = 60.0) -> str:
    """Run a git command in `repo_path` and return stdout. Raises
    WorktreeError with the captured stderr on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {' '.join(args)} timed out") from exc
    stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return stdout


def _read_gitfile_gitdir(path: Path) -> str | None:
    """Return the gitdir target from a `.git` file, if present."""
    gitfile = path / ".git"
    if not gitfile.is_file():
        return None
    try:
        content = gitfile.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if content.startswith("gitdir:"):
        return content.split(":", 1)[1].strip()
    return None


def _is_submodule_gitdir(gitdir: str) -> bool:
    """True when `gitdir:` points at a superproject submodule store."""
    norm = gitdir.replace("\\", "/")
    return "/modules/" in norm or norm.endswith("/modules")


def _same_workspace_path(a: Path, b: Path) -> bool:
    """Path equality, case-insensitive on APFS/HFS+.

    Windows NTFS can also be case-insensitive — extend here if we ever
    support daemon-hosted workspaces on Windows."""
    ra, rb = a.resolve(), b.resolve()
    if ra == rb:
        return True
    if os.name == "posix" and os.uname().sysname == "Darwin":
        return str(ra).casefold() == str(rb).casefold()
    return False


def _plain_git_info() -> dict[str, Any]:
    return {
        "is_git": False,
        "is_worktree": False,
        "kind": "plain",
        "branch": None,
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "insertions": 0,
        "deletions": 0,
        "files_changed": 0,
        "untracked_files": 0,
        "modified_files": 0,
        "added_files": 0,
        "deleted_files": 0,
        "repo_root": None,
        "sibling_workspaces": [],
    }


def _summarize_porcelain(porcelain: str) -> dict[str, int]:
    """File-level counts from `git status --porcelain` (not line ±)."""
    counts = {"untracked": 0, "modified": 0, "added": 0, "deleted": 0, "renamed": 0}
    for line in porcelain.splitlines():
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            counts["untracked"] += 1
        elif x == "D" or y == "D":
            counts["deleted"] += 1
        elif x == "A" or y == "A":
            counts["added"] += 1
        elif x == "R" or y == "R" or x == "C" or y == "C":
            counts["renamed"] += 1
        elif x != " " or y != " ":
            counts["modified"] += 1
    return counts


def sanitize_workspace_name(raw: str) -> str:
    """Make a registry-safe, filesystem-safe name from a branch/ref.
    `feature/login-fix` → `feature-login-fix`. Mirrors the spirit of the
    layout-name sanitizer: no path separators, no traversal."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-.")
    return name or "worktree"


def create_worktree(
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    *,
    base: str | None = None,
    create_branch: bool = True,
) -> Path:
    """Create a git worktree at `worktree_path` on `branch`.

    `create_branch=True` (default) makes a new branch (`git worktree add
    -b <branch> <path> [<base>]`); set False to check out an existing
    branch. Returns the resolved worktree path. Raises WorktreeError on
    any git failure (e.g. the branch already exists, the path is taken).
    """
    worktree_path = worktree_path.expanduser()
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch, str(worktree_path)]
        if base:
            args.append(base)
    else:
        args += [str(worktree_path), branch]
    _git(repo_path, *args)
    return worktree_path.resolve()


def remove_worktree(
    repo_path: Path, worktree_path: Path, *, force: bool = False
) -> None:
    """Remove a worktree. `force=True` removes even with uncommitted
    changes. This only detaches the working tree from the repo — it does
    NOT delete the branch or unregister any relaydeck workspace."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    _git(repo_path, *args)


def list_worktrees(repo_path: Path) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` into dicts with keys
    `path`, `head`, `branch` (branch may be absent for detached HEADs)."""
    out = _git(repo_path, "worktree", "list", "--porcelain")
    trees: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                trees.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = line[len("worktree "):].strip()
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref.removeprefix("refs/heads/")
    if cur:
        trees.append(cur)
    return trees


# ── Lifecycle hooks (setup / teardown) ──────────────────────────────


def load_worktree_hooks(repo_path: Path) -> dict[str, Any]:
    """Read `<repo>/.relaydeck/worktree.yaml` → {setup, teardown, env}.
    Missing file / malformed → empty dict (hooks are opt-in)."""
    import yaml

    p = repo_path.expanduser() / WORKTREE_CONFIG_REL
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for k in ("setup", "teardown"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    env = data.get("env")
    if isinstance(env, dict):
        out["env"] = {str(k): str(v) for k, v in env.items()}
    return out


def run_hook(
    name: str,
    command: str,
    cwd: Path,
    *,
    branch: str = "",
    workspace: str = "",
    repo: str = "",
    env: dict[str, str] | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run a worktree lifecycle hook (`setup`/`teardown`) as a subprocess
    via the shell, cwd = the worktree, with RELAYDECK_WORKTREE_* env so the
    script knows where it's running. Same operator-authored trust model as
    automation `script`/`code` actions — never `exec()` in-process.

    Returns `{ok, code, output, error}` — never raises; a failed hook is
    reported, not fatal (the worktree still exists for the operator to
    inspect or re-provision)."""
    full_env = dict(os.environ)
    full_env.update({
        "RELAYDECK_WORKTREE_PATH": str(cwd),
        "RELAYDECK_WORKTREE_BRANCH": branch,
        "RELAYDECK_WORKSPACE": workspace,
        "RELAYDECK_REPO": repo,
        "RELAYDECK_WORKTREE_HOOK": name,
    })
    if env:
        full_env.update({str(k): str(v) for k, v in env.items()})
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(cwd), env=full_env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": "", "error": f"{name} hook timed out"}
    except OSError as exc:
        return {"ok": False, "code": -1, "output": "", "error": str(exc)}
    out = (proc.stdout or "")[-4000:]
    err = (proc.stderr or "")[-4000:]
    return {"ok": proc.returncode == 0, "code": proc.returncode, "output": out, "error": err}


# ── Status (branch / dirty / ahead-behind) ──────────────────────────


def worktree_status(path: Path) -> dict[str, Any]:
    """Lightweight git status for a worktree path: current branch, whether
    it has uncommitted changes, and ahead/behind vs its upstream. Best-
    effort — any git failure yields the partial dict gathered so far so a
    non-git or detached path never crashes the caller."""
    import contextlib
    path = path.expanduser()
    status: dict[str, Any] = {
        "branch": None, "dirty": False, "ahead": 0, "behind": 0,
        "insertions": 0, "deletions": 0, "files_changed": 0,
        "untracked_files": 0, "modified_files": 0, "added_files": 0, "deleted_files": 0,
    }
    with contextlib.suppress(WorktreeError):
        status["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    with contextlib.suppress(WorktreeError):
        porcelain = _git(path, "status", "--porcelain")
        status["dirty"] = bool(porcelain.strip())
        summary = _summarize_porcelain(porcelain)
        status["untracked_files"] = summary["untracked"]
        status["modified_files"] = summary["modified"]
        status["added_files"] = summary["added"]
        status["deleted_files"] = summary["deleted"]
    with contextlib.suppress(WorktreeError, ValueError):
        # behind<TAB>ahead vs the configured upstream (errors if no upstream)
        counts = _git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").split()
        if len(counts) == 2:
            status["behind"], status["ahead"] = int(counts[0]), int(counts[1])
    with contextlib.suppress(WorktreeError, ValueError):
        # Line/file churn vs HEAD (staged + unstaged tracked changes). Untracked
        # files have no diff base so they don't count toward ±lines, but they
        # already flip `dirty` above. Binary files report "-\t-" → skipped for
        # line totals but still counted as a changed file.
        ins = dele = files = 0
        for line in _git(path, "diff", "--numstat", "HEAD").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                files += 1
                if parts[0].isdigit():
                    ins += int(parts[0])
                if parts[1].isdigit():
                    dele += int(parts[1])
        status["insertions"], status["deletions"], status["files_changed"] = ins, dele, files
    return status


def repo_workspace_rows(
    config_home: Path,
    repo_root: str,
    *,
    current: str | None = None,
) -> list[dict[str, Any]]:
    """All registered relaydeck workspaces on `repo_root`, with git + agents."""
    from relaydeck.config import load_workspace_registry

    registry = load_workspace_registry(config_home)
    batch = batch_workspace_git_info(
        [(w.name, w.path) for w in registry],
        config_home=config_home,
    )
    agents_by_ws: dict[str, list[dict[str, Any]]] = {}
    try:
        from relaydeck.orchestrator import get_orchestrator
        orch = get_orchestrator()
        if orch is not None:
            for a in orch.list_agents():
                ws = a.get("workspace")
                if ws:
                    agents_by_ws.setdefault(ws, []).append({
                        "id": a.get("id"),
                        "status": a.get("status"),
                        "semantic_status": a.get("semantic_status"),
                    })
    except Exception:
        pass
    rows: list[dict[str, Any]] = []
    for w in registry:
        info = batch.get(w.name, _plain_git_info())
        if info.get("repo_root") != repo_root:
            continue
        rows.append({
            "workspace": w.name,
            "path": str(Path(w.path).expanduser()),
            "current": w.name == current,
            "git": info,
            "agents": agents_by_ws.get(w.name, []),
        })
    return sorted(rows, key=lambda r: (not r.get("current"), r.get("workspace") or ""))


def git_worktree_rows(repo_root: Path, *, config_home: Path | None = None) -> list[dict[str, Any]]:
    """`git worktree list` enriched with relaydeck workspace name when registered."""
    import contextlib

    path_by_ws: dict[str, str] = {}
    if config_home:
        from relaydeck.config import load_workspace_registry
        for w in load_workspace_registry(config_home):
            path_by_ws[str(Path(w.path).expanduser().resolve())] = w.name
    rows: list[dict[str, Any]] = []
    with contextlib.suppress(WorktreeError):
        for wt in list_worktrees(repo_root):
            p = wt.get("path") or ""
            resolved = str(Path(p).expanduser().resolve()) if p else ""
            rows.append({
                **wt,
                "workspace": path_by_ws.get(resolved),
            })
    return rows


def git_status_lines(path: Path, *, limit: int = 120) -> list[dict[str, str]]:
    """Porcelain `git status` lines for a vim-style change log in the UI."""
    import contextlib

    path = path.expanduser()
    lines: list[dict[str, str]] = []
    with contextlib.suppress(WorktreeError):
        raw = _git(path, "status", "--porcelain").splitlines()
        for line in raw[: max(0, limit)]:
            if len(line) < 3:
                continue
            code = line[:2].rstrip() or line[0]
            p = line[3:].strip()
            lines.append({"code": code, "path": p})
    return lines


def main_worktree(path: Path) -> Path:
    """The main working tree of the repo `path` belongs to (the first
    entry of `git worktree list`). Lets us run `git worktree remove` from
    the canonical repo even when given a linked worktree path."""
    trees = list_worktrees(path)
    if trees and trees[0].get("path"):
        return Path(trees[0]["path"])
    return path


def resolve_git_root(path: Path) -> Path | None:
    """Filesystem path of the repo's primary checkout.

    Works when `path` is the main tree, a linked worktree, or any path
    inside either. Checked live on every call — a folder that becomes a
    repo later (`git init`) is recognized on the next lookup with no
    re-registration. Returns None when not in a git repo."""
    path = path.expanduser()
    if not is_git_repo(path):
        return None
    try:
        return main_worktree(path).resolve()
    except WorktreeError:
        pass
    import contextlib
    with contextlib.suppress(WorktreeError):
        top = _git(path, "rev-parse", "--show-toplevel").strip()
        if top:
            return Path(top).resolve()
    return None


def _workspace_git_info_core(path: Path) -> dict[str, Any]:
    """Git metadata for one workspace path, excluding sibling lookups."""
    path = path.expanduser()
    if not path.exists() or not is_git_repo(path):
        return _plain_git_info()
    resolved = path.resolve()
    is_wt = is_worktree(resolved)
    status = worktree_status(resolved)
    root = resolve_git_root(resolved)
    return {
        **_plain_git_info(),
        "is_git": True,
        "is_worktree": is_wt,
        "kind": "worktree" if is_wt else "main",
        **status,
        "repo_root": str(root) if root else None,
    }


def _cached_workspace_git_info_core(path: Path) -> dict[str, Any]:
    """Cached wrapper around `_workspace_git_info_core`. TTL-only —
    see the module-level cache comment for why we don't stamp on .git."""
    resolved = path.expanduser().resolve()
    key = _git_cache_key(resolved)
    now = time.monotonic()
    with _git_info_cache_lock:
        hit = _git_info_cache.get(key)
        if hit is not None:
            cached_at, info = hit
            if (now - cached_at) < _GIT_INFO_TTL_S:
                return copy.deepcopy(info)
    info = _workspace_git_info_core(resolved)
    with _git_info_cache_lock:
        _git_info_cache[key] = (now, copy.deepcopy(info))
    return info


def batch_workspace_git_info(
    entries: list[tuple[str, Path | str]],
    *,
    config_home: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute git metadata for many workspaces in one pass.

    Groups by repo root so sibling enrichment is O(N) instead of
    re-running `resolve_git_root` for every pair."""
    from collections import defaultdict

    resolved: list[tuple[str, Path]] = []
    out: dict[str, dict[str, Any]] = {}
    for name, raw in entries:
        p = Path(raw).expanduser()
        if not p.exists():
            out[name] = _plain_git_info()
            continue
        resolved.append((name, p.resolve()))

    by_root: dict[str, list[tuple[str, Path, dict[str, Any]]]] = defaultdict(list)
    for name, path in resolved:
        info = _cached_workspace_git_info_core(path)
        out[name] = info
        root = info.get("repo_root")
        if root:
            by_root[root].append((name, path, info))

    if config_home:
        for name, path in resolved:
            root = out[name].get("repo_root")
            if not root:
                continue
            siblings: list[dict[str, Any]] = []
            for other_name, other_path, other_info in by_root[root]:
                if _same_workspace_path(other_path, path):
                    continue
                siblings.append({
                    "workspace": other_name,
                    "path": str(other_path),
                    "is_worktree": other_info.get("is_worktree", False),
                    "branch": other_info.get("branch"),
                    "dirty": other_info.get("dirty", False),
                })
            out[name]["sibling_workspaces"] = sorted(
                siblings, key=lambda r: str(r.get("workspace") or ""),
            )
    return out


def list_repo_sibling_workspaces(
    config_home: Path,
    path: Path,
    *,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Other registered relaydeck workspaces that share the same git repo.

    Used in agent identity preambles so parallel harnesses on different
    branches know about each other's workspace names without scanning disk."""
    from relaydeck.config import load_workspace_registry

    path = path.expanduser().resolve()
    registry = load_workspace_registry(config_home)
    batch = batch_workspace_git_info(
        [(w.name, w.path) for w in registry],
        config_home=config_home,
    )
    for w in registry:
        if _same_workspace_path(Path(w.path).expanduser(), path):
            return list(batch.get(w.name, {}).get("sibling_workspaces") or [])
    return []


def workspace_git_info(
    path: Path,
    *,
    config_home: Path | None = None,
) -> dict[str, Any]:
    """Unified git metadata for any workspace path.

    `kind` is one of:
      - `plain` — not a git repo (relaydeck never runs `git init` for you)
      - `main`  — the primary checkout
      - `worktree` — a linked parallel checkout

    Best-effort throughout — a missing upstream or transient git error
    never crashes the caller."""
    path = path.expanduser()
    if not path.exists():
        return _plain_git_info()
    if config_home:
        from relaydeck.config import load_workspace_registry

        registry = load_workspace_registry(config_home)
        resolved = path.resolve()
        batch = batch_workspace_git_info(
            [(w.name, w.path) for w in registry],
            config_home=config_home,
        )
        for w in registry:
            if _same_workspace_path(Path(w.path).expanduser(), resolved):
                return dict(batch.get(w.name, _plain_git_info()))
    return _cached_workspace_git_info_core(path)


def create_worktree_workspace(
    config_home: Path,
    repo_path: Path,
    branch: str,
    *,
    name: str | None = None,
    worktrees_root: Path | None = None,
    base: str | None = None,
    create_branch: bool = True,
    plugins: list[str] | None = None,
    setup: str | None = None,
    run_setup: bool = True,
) -> dict[str, Any]:
    """Create a worktree AND register it as a relaydeck workspace in one call.

    The high-level entry point a plugin (or `relaydeck worktree create`) uses:
      1. `git worktree add` a new working tree on `branch`,
      2. register it as a workspace via `relaydeck.config.register_workspace`,
      3. run the `setup` hook (explicit arg, else the repo's
         `.relaydeck/worktree.yaml`) so an agent lands in a ready env.

    `name` defaults to a sanitized `branch`; `worktrees_root` defaults to
    `<config_home>/worktrees`. Returns `{"name", "path", "branch",
    "plugins", "repo", "setup"}` where `setup` is the hook result (or None
    if no hook ran). On a git failure nothing is registered (the registry
    write only runs after the worktree exists). A failed setup hook is
    reported in the result, not raised — the worktree is already real.
    """
    from relaydeck.config import register_workspace

    repo_path = repo_path.expanduser().resolve()
    repo_resolved = resolve_git_root(repo_path) or repo_path
    # Friendly, actionable guards before touching git — otherwise the
    # operator sees a raw "fatal: not a git repository" from git's stderr.
    if not repo_path.exists():
        raise WorktreeError(f"{repo_path} does not exist on the daemon host")
    if not is_git_repo(repo_path):
        raise WorktreeError(
            f"{repo_path} is not a git repository — worktrees need git. "
            f"Run `git init` (or clone) there first, then create the worktree. "
            f"A non-git folder still works fine as a plain workspace; you only "
            f"need git when you want parallel branch checkouts."
        )
    ws_name = sanitize_workspace_name(name or branch)
    root = (worktrees_root or (config_home / "worktrees")).expanduser()
    worktree_path = root / ws_name

    created = create_worktree(
        repo_resolved, worktree_path, branch, base=base, create_branch=create_branch,
    )
    # Atomic from here: if registration fails (e.g. ValueError on a
    # duplicate workspace name), roll the worktree back so we never leave
    # an orphan on disk + in `git worktree list`. Re-raise so the caller
    # (API/CLI) maps it to a clean error, not a 500.
    try:
        info = register_workspace(config_home, ws_name, created, list(plugins or []))
    except Exception:
        import contextlib
        with contextlib.suppress(WorktreeError):
            remove_worktree(repo_resolved, created, force=True)
        raise

    hooks = load_worktree_hooks(repo_resolved)
    setup_cmd = setup if setup is not None else hooks.get("setup")
    setup_result = None
    if run_setup and setup_cmd:
        setup_result = run_hook(
            "setup", setup_cmd, created, branch=branch, workspace=ws_name,
            repo=str(repo_resolved), env=hooks.get("env"),
        )
    clear_workspace_git_info_cache()
    return {
        "name": ws_name,
        "path": info["path"],
        "branch": branch,
        "plugins": info["plugins"],
        "repo": str(repo_resolved),
        "setup": setup_result,
    }


def remove_worktree_workspace(
    config_home: Path,
    name: str,
    *,
    force: bool = False,
    teardown: str | None = None,
    run_teardown: bool = True,
    delete_dir: bool = True,
) -> dict[str, Any]:
    """Tear down a worktree workspace: run the `teardown` hook, `git
    worktree remove` the working tree, then unregister the relaydeck
    workspace. Looked up by workspace `name` (its registered path is the
    worktree). Returns `{"name", "removed", "unregistered", "teardown"}`.

    `delete_dir=True` (default) actually removes the working tree; set
    False to only unregister (leave the dir). Teardown failure is reported,
    not fatal — we still remove + unregister so a half-broken hook can't
    strand a workspace.
    """
    from relaydeck.config import load_workspace_registry, unregister_workspace

    ws = next((w for w in load_workspace_registry(config_home) if w.name == name), None)
    if ws is None:
        return {"name": name, "removed": False, "unregistered": False,
                "teardown": None, "error": "no such workspace"}
    worktree_path = Path(ws.path).expanduser()

    # Guard: this endpoint is worktree-only. A normal registered workspace
    # isn't a linked worktree, so `git worktree remove` (and main_worktree)
    # would raise — return an explicit error the caller maps to 400 instead
    # of crashing. Unregister such a workspace via `relaydeck workspace rm`.
    if not is_worktree(worktree_path):
        return {"name": name, "removed": False, "unregistered": False,
                "teardown": None, "error": "not a worktree workspace"}

    branch = worktree_status(worktree_path).get("branch") or ""
    hooks = load_worktree_hooks(main_worktree(worktree_path))
    teardown_cmd = teardown if teardown is not None else hooks.get("teardown")
    teardown_result = None
    if run_teardown and teardown_cmd and worktree_path.exists():
        teardown_result = run_hook(
            "teardown", teardown_cmd, worktree_path, branch=branch,
            workspace=name, repo=str(main_worktree(worktree_path)), env=hooks.get("env"),
        )

    removed = False
    if delete_dir and worktree_path.exists():
        try:
            remove_worktree(main_worktree(worktree_path), worktree_path, force=force)
            removed = True
        except WorktreeError as exc:
            return {"name": name, "removed": False, "unregistered": False,
                    "teardown": teardown_result, "error": str(exc)}

    unregistered = unregister_workspace(config_home, name)
    clear_workspace_git_info_cache()
    return {"name": name, "removed": removed, "unregistered": unregistered,
            "teardown": teardown_result}


def is_git_repo(path: Path) -> bool:
    """True if `path` is inside a git working tree (a main checkout or a
    linked worktree). Checked live (a `git rev-parse`), so a folder that
    becomes a repo later — `git init` after the fact — is recognized on
    the next call with no caching or re-registration. A non-git folder
    returns False and stays a perfectly good plain workspace; it just
    can't host worktrees until it has git."""
    path = path.expanduser()
    if not path.exists():
        return False
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except WorktreeError:
        return False


def is_worktree(path: Path) -> bool:
    """True if `path` is a *linked* git worktree (not the main checkout).

    Submodule checkouts also use a `.git` file — those are excluded."""
    path = path.expanduser()
    gitdir_from_file = _read_gitfile_gitdir(path)
    if gitdir_from_file:
        if _is_submodule_gitdir(gitdir_from_file):
            return False
        norm = gitdir_from_file.replace("\\", "/")
        if "/worktrees/" in norm:
            return True
    try:
        common = _git(path, "rev-parse", "--git-common-dir").strip()
        gitdir = _git(path, "rev-parse", "--git-dir").strip()
        if _is_submodule_gitdir(gitdir):
            return False
        return common != gitdir
    except WorktreeError:
        return False
