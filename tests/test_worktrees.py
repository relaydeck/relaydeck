"""
Git worktree → task-workspace helper. Exercised against a REAL temp git
repo (no mocking of git) so the actual `git worktree` contract is pinned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relaydeck.worktrees import (
    WorktreeError,
    create_worktree,
    create_worktree_workspace,
    is_git_repo,
    is_worktree,
    list_worktrees,
    list_repo_sibling_workspaces,
    load_worktree_hooks,
    remove_worktree,
    remove_worktree_workspace,
    resolve_git_root,
    run_hook,
    sanitize_workspace_name,
    worktree_status,
    workspace_git_info,
)


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "tester")
    (repo / "README.md").write_text("hi\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return repo


def test_sanitize_workspace_name():
    assert sanitize_workspace_name("feature/login-fix") == "feature-login-fix"
    assert sanitize_workspace_name("../../etc/passwd") == "etc-passwd"
    assert sanitize_workspace_name("") == "worktree"
    assert sanitize_workspace_name("  spaced name ") == "spaced-name"


def test_create_and_list_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt" / "pr1"
    p = create_worktree(repo, wt, "pr-1")
    assert p.exists()
    assert (p / "README.md").exists()  # the worktree has the repo content
    branches = {t.get("branch") for t in list_worktrees(repo)}
    assert "pr-1" in branches


def test_create_worktree_existing_branch_fails(tmp_path):
    repo = _init_repo(tmp_path)
    create_worktree(repo, tmp_path / "a", "dup")
    with pytest.raises(WorktreeError):
        create_worktree(repo, tmp_path / "b", "dup")  # branch already exists


def test_create_worktree_checkout_existing_branch(tmp_path):
    repo = _init_repo(tmp_path)
    # Make a branch, then check it out into a worktree (create_branch=False).
    subprocess.run(["git", "-C", str(repo), "branch", "existing"],
                   check=True, capture_output=True)
    wt = create_worktree(repo, tmp_path / "wt", "existing", create_branch=False)
    assert wt.exists()


def test_remove_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    wt = create_worktree(repo, tmp_path / "wt", "pr-x")
    remove_worktree(repo, wt)
    assert not wt.exists()


def test_list_worktrees_on_non_repo_raises(tmp_path):
    nonrepo = tmp_path / "nope"
    nonrepo.mkdir()
    with pytest.raises(WorktreeError):
        list_worktrees(nonrepo)


def test_create_worktree_workspace_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)

    info = create_worktree_workspace(cfg, repo, "feature/x", plugins=["messaging"])
    assert info["name"] == "feature-x"
    assert info["branch"] == "feature/x"
    assert Path(info["path"]).exists()

    from relaydeck.config import load_workspace_registry
    names = [w.name for w in load_workspace_registry()]
    assert "feature-x" in names
    assert (cfg / "workspaces" / "feature-x" / "agent.toml").exists()
    # The worktree default root is <config_home>/worktrees/<name>.
    assert (cfg / "worktrees" / "feature-x").exists()


def test_create_worktree_workspace_failure_registers_nothing(tmp_path, monkeypatch):
    """If git fails, no workspace is registered (worktree is created
    before the registry write)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    create_worktree_workspace(cfg, repo, "taken")
    with pytest.raises(WorktreeError):
        create_worktree_workspace(cfg, repo, "taken")  # branch exists → git fails

    from relaydeck.config import load_workspace_registry
    # Only the first one registered; the failed second didn't double-register.
    assert [w.name for w in load_workspace_registry()].count("taken") == 1


def test_create_worktree_rolls_back_on_duplicate_name(tmp_path, monkeypatch):
    """If registration fails (duplicate workspace name, NEW branch so git
    succeeds), the just-created worktree is rolled back — no orphan on disk
    or in `git worktree list`, and ValueError propagates."""
    from relaydeck.config import load_workspace_registry, register_workspace
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    cfg = tmp_path / ".relaydeck"; cfg.mkdir(parents=True)
    # A workspace named "dup" already exists (a plain one, not a worktree).
    plain = tmp_path / "plain"; plain.mkdir()
    register_workspace(cfg, "dup", plain, [])

    # New branch (git add succeeds) but name collides → register raises.
    with pytest.raises(ValueError):
        create_worktree_workspace(cfg, repo, "fresh-branch", name="dup")

    # Worktree rolled back: dir gone + not in git's list + registry unchanged.
    assert not (cfg / "worktrees" / "dup").exists()
    wt_paths = {t.get("path") for t in list_worktrees(repo)}
    assert not any(str(cfg / "worktrees" / "dup") == p for p in wt_paths)
    regs = [w for w in load_workspace_registry() if w.name == "dup"]
    assert len(regs) == 1 and str(plain.resolve()) == str(Path(regs[0].path))


def test_remove_non_worktree_workspace_errors(tmp_path, monkeypatch):
    """remove_worktree_workspace on a normal (non-worktree) workspace
    returns an explicit error instead of crashing on git."""
    from relaydeck.config import register_workspace
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"; cfg.mkdir(parents=True)
    plain = tmp_path / "plain"; plain.mkdir()
    register_workspace(cfg, "plain", plain, [])
    res = remove_worktree_workspace(cfg, "plain")
    assert res["error"] == "not a worktree workspace"
    assert res["removed"] is False and res["unregistered"] is False
    # Still registered (we didn't touch it).
    from relaydeck.config import load_workspace_registry
    assert "plain" in [w.name for w in load_workspace_registry()]


# ── status + is_worktree ────────────────────────────────────────────


def test_worktree_status(tmp_path):
    repo = _init_repo(tmp_path)
    wtp = create_worktree(repo, tmp_path / "wt", "feat")
    st = worktree_status(wtp)
    assert st["branch"] == "feat"
    assert st["dirty"] is False
    (wtp / "new.txt").write_text("x")
    assert worktree_status(wtp)["dirty"] is True


def test_worktree_status_non_repo_is_safe(tmp_path):
    d = tmp_path / "plain"; d.mkdir()
    st = worktree_status(d)
    assert st["branch"] is None and st["dirty"] is False
    # diff-stat fields are always present (default 0), even off-repo.
    assert st["insertions"] == 0 and st["deletions"] == 0 and st["files_changed"] == 0


def test_worktree_status_diff_stats(tmp_path):
    repo = _init_repo(tmp_path)
    wtp = create_worktree(repo, tmp_path / "wt", "feat")
    # Clean checkout → no churn.
    st = worktree_status(wtp)
    assert (st["insertions"], st["deletions"], st["files_changed"]) == (0, 0, 0)
    # Modify a TRACKED file (README was a single line "hi") → +2 lines vs HEAD.
    (wtp / "README.md").write_text("hi\nthere\nmore\n")
    st = worktree_status(wtp)
    assert st["files_changed"] == 1
    assert st["insertions"] == 2 and st["deletions"] == 0
    # An untracked file flips `dirty` but isn't in the diff (no base) — line
    # totals stay put, file count unchanged.
    (wtp / "untracked.txt").write_text("z\n")
    st = worktree_status(wtp)
    assert st["dirty"] is True and st["files_changed"] == 1


def test_is_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    assert is_worktree(repo) is False           # main checkout
    wtp = create_worktree(repo, tmp_path / "wt", "feat")
    assert is_worktree(wtp) is True             # linked worktree
    assert is_worktree(tmp_path / "nope") is False


# ── lifecycle hooks (setup / teardown) ──────────────────────────────


def test_load_worktree_hooks(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".relaydeck").mkdir()
    (repo / ".relaydeck" / "worktree.yaml").write_text(
        "setup: echo hi\nteardown: echo bye\nenv: {FOO: bar}\n")
    hooks = load_worktree_hooks(repo)
    assert hooks["setup"] == "echo hi"
    assert hooks["teardown"] == "echo bye"
    assert hooks["env"] == {"FOO": "bar"}


def test_load_worktree_hooks_missing(tmp_path):
    assert load_worktree_hooks(_init_repo(tmp_path)) == {}


def test_run_hook_success_and_env(tmp_path):
    wtp = tmp_path / "wt"; wtp.mkdir()
    r = run_hook("setup", "echo $RELAYDECK_WORKTREE_BRANCH > out.txt && echo done",
                 wtp, branch="feat", workspace="ws")
    assert r["ok"] is True and r["code"] == 0
    assert (wtp / "out.txt").read_text().strip() == "feat"


def test_run_hook_failure_is_reported_not_raised(tmp_path):
    wtp = tmp_path / "wt"; wtp.mkdir()
    r = run_hook("setup", "exit 7", wtp)
    assert r["ok"] is False and r["code"] == 7


def test_create_runs_setup_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    (repo / ".relaydeck").mkdir()
    (repo / ".relaydeck" / "worktree.yaml").write_text("setup: echo ok > .ready\n")
    cfg = tmp_path / ".relaydeck"; cfg.mkdir(parents=True)
    # Create worktree from the linked tree path — hooks still resolve via main root.
    wtp = create_worktree(repo, tmp_path / "linked", "linked-base")
    info = create_worktree_workspace(cfg, wtp, "feature/x")
    assert info["setup"]["ok"] is True
    assert (Path(info["path"]) / ".ready").exists()
    assert info["repo"] == str(repo.resolve())


def test_create_setup_override_and_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    cfg = tmp_path / ".relaydeck"; cfg.mkdir(parents=True)
    # explicit setup overrides (no repo config present)
    info = create_worktree_workspace(cfg, repo, "a", setup="echo x > .prov")
    assert (Path(info["path"]) / ".prov").exists()
    # run_setup=False skips
    info2 = create_worktree_workspace(cfg, repo, "b", setup="echo x > .prov", run_setup=False)
    assert info2["setup"] is None
    assert not (Path(info2["path"]) / ".prov").exists()


def test_remove_worktree_workspace_full(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    (repo / ".relaydeck").mkdir()
    (repo / ".relaydeck" / "worktree.yaml").write_text(
        "teardown: echo bye > $RELAYDECK_REPO/.tore_down\n")
    cfg = tmp_path / ".relaydeck"; cfg.mkdir(parents=True)
    info = create_worktree_workspace(cfg, repo, "feature/x")
    wpath = Path(info["path"])

    res = remove_worktree_workspace(cfg, "feature-x", force=True)
    assert res["removed"] is True and res["unregistered"] is True
    assert res["teardown"]["ok"] is True
    assert not wpath.exists()
    from relaydeck.config import load_workspace_registry
    assert "feature-x" not in [w.name for w in load_workspace_registry()]


def test_remove_worktree_workspace_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"; cfg.mkdir(parents=True)
    res = remove_worktree_workspace(cfg, "ghost")
    assert res["removed"] is False and res.get("error") == "no such workspace"


# ── HTTP API ────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as om
    om._orchestrator = None
    from relaydeck.transports.api import create_app
    return TestClient(create_app(cfg)), tmp_path


def test_api_worktree_crud(client, tmp_path):
    c, _ = client
    repo = _init_repo(tmp_path)
    (repo / ".relaydeck").mkdir()
    (repo / ".relaydeck" / "worktree.yaml").write_text("setup: echo ok > .ready\n")
    r = c.post("/api/worktrees", json={"repo": str(repo), "branch": "feature/y",
                                       "plugins": ["messaging"]})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "feature-y" and body["setup"]["ok"] is True
    # listed with status
    rows = c.get("/api/worktrees").json()
    assert any(w["name"] == "feature-y" and w["status"]["branch"] == "feature/y" for w in rows)
    # removed
    rd = c.delete("/api/worktrees/feature-y?force=true")
    assert rd.status_code == 200 and rd.json()["removed"] is True
    assert all(w["name"] != "feature-y" for w in c.get("/api/worktrees").json())


def test_api_worktree_create_validation(client):
    c, _ = client
    assert c.post("/api/worktrees", json={"branch": "x"}).status_code == 400
    assert c.delete("/api/worktrees/ghost").status_code == 404


def test_api_worktree_duplicate_name_409(client, tmp_path):
    """A name already in the registry (here a plain workspace) returns a
    clean 409, not a 500 — and the just-created worktree is rolled back so
    nothing is orphaned. (Same name → same path would fail at
    git first; the registration conflict needs a free path + taken name.)"""
    c, _ = client
    from relaydeck.config import register_workspace
    repo = _init_repo(tmp_path)
    plain = tmp_path / "plain"; plain.mkdir()
    register_workspace(tmp_path / ".relaydeck", "dup", plain, [])
    r = c.post("/api/worktrees", json={"repo": str(repo), "branch": "b", "name": "dup"})
    assert r.status_code == 409
    # Rolled back: no worktrees/dup left behind in git's list.
    rows = c.get(f"/api/worktrees?repo={repo}").json()["worktrees"]
    assert not any(str(w.get("path", "")).endswith("/dup") for w in rows)


def test_api_worktree_delete_non_worktree_400(client, tmp_path):
    """DELETE on a normal registered workspace returns 400, not 500."""
    c, _ = client
    from relaydeck.config import register_workspace
    plain = tmp_path / "plain"; plain.mkdir()
    register_workspace(tmp_path / ".relaydeck", "plain", plain, [])
    r = c.delete("/api/worktrees/plain")
    assert r.status_code == 400
    assert "not a worktree" in r.json()["detail"]


def test_api_worktree_repo_listing(client, tmp_path):
    c, _ = client
    repo = _init_repo(tmp_path)
    create_worktree(repo, tmp_path / "wt", "feat")
    out = c.get(f"/api/worktrees?repo={repo}").json()
    branches = {w.get("branch") for w in out["worktrees"]}
    assert "feat" in branches


# ── CLI (daemon-down local fallback path) ───────────────────────────


def test_cli_worktree_create_list_remove(tmp_path, monkeypatch):
    """With the daemon unreachable, the CLI falls back to local
    create/list/remove via worktrees.py."""
    from click.testing import CliRunner

    from relaydeck.transports.cli import main

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RELAYDECK_DAEMON_URL", "http://127.0.0.1:1")  # unreachable
    repo = _init_repo(tmp_path)
    (repo / ".relaydeck").mkdir()
    (repo / ".relaydeck" / "worktree.yaml").write_text("setup: echo ok > .ready\n")
    runner = CliRunner()

    r = runner.invoke(main, ["worktree", "create", "feature/login", "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    assert "feature-login" in r.output
    assert (tmp_path / ".relaydeck" / "worktrees" / "feature-login" / ".ready").exists()

    r = runner.invoke(main, ["worktree", "list"])
    assert r.exit_code == 0 and "feature-login" in r.output

    r = runner.invoke(main, ["worktree", "remove", "feature-login", "--force"])
    assert r.exit_code == 0
    assert not (tmp_path / ".relaydeck" / "worktrees" / "feature-login").exists()


# ── git-repo detection + friendly non-git handling ──────────────────


def test_resolve_git_root_from_linked_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    wtp = create_worktree(repo, tmp_path / "wt", "feat")
    assert resolve_git_root(wtp).resolve() == repo.resolve()
    assert resolve_git_root(repo).resolve() == repo.resolve()


def test_workspace_git_info_plain_main_and_worktree(tmp_path, monkeypatch):
    from relaydeck.config import register_workspace
    from relaydeck.worktrees import (
        list_repo_sibling_workspaces,
        resolve_git_root,
        workspace_git_info,
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspace_git_info(plain)["kind"] == "plain"

    repo = _init_repo(tmp_path)
    main_info = workspace_git_info(repo)
    assert main_info["is_git"] is True
    assert main_info["kind"] == "main"
    assert main_info["branch"] == "main" or main_info["branch"]  # default branch name varies
    assert resolve_git_root(repo) == repo.resolve()

    wtp = create_worktree(repo, tmp_path / "wt", "feat")
    wt_info = workspace_git_info(wtp)
    assert wt_info["kind"] == "worktree"
    assert wt_info["branch"] == "feat"


def test_list_repo_sibling_workspaces(tmp_path, monkeypatch):
    from relaydeck.config import register_workspace
    from relaydeck.worktrees import create_worktree_workspace, list_repo_sibling_workspaces

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    register_workspace(cfg, "main-ws", repo, [])
    create_worktree_workspace(cfg, repo, "feature/a", name="wt-a")
    create_worktree_workspace(cfg, repo, "feature/b", name="wt-b")

    sibs = list_repo_sibling_workspaces(cfg, repo)
    names = {s["workspace"] for s in sibs}
    assert names == {"wt-a", "wt-b"}


def test_compose_identity_includes_git_context(tmp_path, monkeypatch):
    from relaydeck.config import register_workspace
    from relaydeck.harness import compose_identity_preamble
    from relaydeck.worktrees import create_worktree_workspace

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    register_workspace(cfg, "main-ws", repo, [])
    info = create_worktree_workspace(cfg, repo, "feature/x", name="wt-x")

    text = compose_identity_preamble(
        "agent-a", "wt-x", "review", [],
        workspace_path=info["path"], config_home=cfg,
    )
    assert "Git checkout context" in text
    assert "linked git worktree" in text
    assert "feature/x" in text
    assert "main-ws" in text  # sibling workspace
    # The guardrails that keep an agent from trampling peers — load-bearing,
    # so pin them explicitly: don't switch branches in a shared tree, and
    # don't edit sibling workspace paths.
    assert "SHARED with any peer agents" in text
    assert "edit sibling workspace paths" in text


def test_harness_injects_git_env(tmp_path, monkeypatch):
    from plugins.harnesses.pi.agent import PiAgent
    from relaydeck.worktrees import create_worktree

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    wtp = create_worktree(repo, tmp_path / "wt", "feat")
    agent = PiAgent(
        agent_id="a1", name="a1", config={}, workspace="demo",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        stop_flag=__import__("threading").Event(),
    )
    agent.workspace = str(wtp)  # direct path workspace for test
    env = agent._build_env()
    assert env["RELAYDECK_WORKTREE"] == "1"
    assert env["RELAYDECK_GIT_BRANCH"] == "feat"
    assert env["RELAYDECK_WORKTREE_BRANCH"] == "feat"


def test_api_workspaces_include_git(client, tmp_path):
    c, _ = client
    from relaydeck.config import register_workspace
    repo = _init_repo(tmp_path)
    register_workspace(tmp_path / ".relaydeck", "main-ws", repo, [])
    rows = c.get("/api/workspaces").json()
    row = next(r for r in rows if r["name"] == "main-ws")
    assert row["git"]["is_git"] is True
    assert row["git"]["kind"] == "main"


def test_api_workspace_git_detail(client, tmp_path):
    c, _ = client
    from relaydeck.config import register_workspace
    repo = _init_repo(tmp_path)
    register_workspace(tmp_path / ".relaydeck", "detail-ws", repo, [])
    (repo / "tracked.txt").write_text("hi\n")
    res = c.get("/api/workspaces/detail-ws/git-detail")
    assert res.status_code == 200
    body = res.json()
    assert body["workspace"] == "detail-ws"
    assert body["git"]["is_git"] is True
    assert body["git"]["branch"] is not None
    assert isinstance(body["changes"], list)
    assert body["github"]["configured"] is False


def test_git_status_lines_reports_porcelain(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "newfile.txt"
    f.write_text("x\n")
    from relaydeck.worktrees import git_status_lines
    lines = git_status_lines(repo)
    assert any(ln["path"].endswith("newfile.txt") for ln in lines)


def test_is_git_repo(tmp_path):
    repo = _init_repo(tmp_path)
    assert is_git_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_git_repo(plain) is False
    assert is_git_repo(tmp_path / "nope") is False  # missing path


def test_is_git_repo_detects_later_git_init(tmp_path):
    """A folder that wasn't a repo becomes worktree-capable the moment it's
    `git init`-ed — detection is live (no caching)."""
    d = tmp_path / "later"
    d.mkdir()
    assert is_git_repo(d) is False
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True, capture_output=True)
    assert is_git_repo(d) is True


def test_create_worktree_workspace_rejects_non_git(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        create_worktree_workspace(home, plain, "feature/x")
    # error is actionable: it names the fix
    try:
        create_worktree_workspace(home, plain, "feature/x")
    except WorktreeError as exc:
        assert "git init" in str(exc)


def test_create_worktree_workspace_rejects_missing_repo(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(WorktreeError, match="does not exist"):
        create_worktree_workspace(home, tmp_path / "ghost", "feature/x")


def test_is_worktree_false_for_submodule_gitfile(tmp_path):
    """Submodule checkouts use a `.git` file — must not look like worktrees."""
    superrepo = _init_repo(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../super/.git/modules/sub\n")
    assert is_worktree(sub) is False


def test_git_handles_non_utf8_output(tmp_path, monkeypatch):
    """Non-UTF-8 git output must not crash callers."""
    import subprocess as sp
    from relaydeck.worktrees import _git

    repo = _init_repo(tmp_path)

    def fake_run(*args, **kwargs):
        class Proc:
            returncode = 0
            stdout = b"\xff\xfe"
            stderr = b""
        return Proc()

    monkeypatch.setattr(sp, "run", fake_run)
    out = _git(repo, "rev-parse", "--is-inside-work-tree")
    assert "\ufffd" in out


def test_batch_workspace_git_info_avoids_quadratic_resolve(tmp_path, monkeypatch):
    """Sibling enrichment must not re-resolve every workspace pair."""
    from relaydeck.config import register_workspace
    from relaydeck.worktrees import batch_workspace_git_info, resolve_git_root

    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path)

    entries: list[tuple[str, Path]] = [( "main", repo )]
    register_workspace(home, "main", repo, [])
    for i in range(3):
        branch = f"feat-{i}"
        wt = create_worktree(repo, tmp_path / "wts" / branch, branch)
        name = f"ws-{i}"
        register_workspace(home, name, wt, [])
        entries.append((name, wt))

    calls = {"n": 0}
    real = resolve_git_root

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr("relaydeck.worktrees.resolve_git_root", counting)
    batch = batch_workspace_git_info(entries, config_home=home)
    assert len(batch) == len(entries)
    assert calls["n"] <= len(entries) + 1
    assert len(batch["ws-0"]["sibling_workspaces"]) == len(entries) - 1


def test_workspace_git_info_cache_hits_on_repeat(tmp_path, monkeypatch):
    from relaydeck.worktrees import (
        _cached_workspace_git_info_core,
        _workspace_git_info_core,
        clear_workspace_git_info_cache,
    )

    repo = _init_repo(tmp_path)
    clear_workspace_git_info_cache()
    calls = {"n": 0}
    real = _workspace_git_info_core

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr("relaydeck.worktrees._workspace_git_info_core", counting)
    _cached_workspace_git_info_core(repo)
    _cached_workspace_git_info_core(repo)
    assert calls["n"] == 1


def test_workspace_git_info_no_duplicate_core_for_single_path(tmp_path, monkeypatch):
    from relaydeck.config import register_workspace
    from relaydeck.worktrees import (
        clear_workspace_git_info_cache,
        workspace_git_info,
        _workspace_git_info_core,
    )

    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path)
    register_workspace(home, "main", repo, [])
    clear_workspace_git_info_cache()
    calls = {"n": 0}
    real = _workspace_git_info_core

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr("relaydeck.worktrees._workspace_git_info_core", counting)
    info = workspace_git_info(repo, config_home=home)
    assert info["is_git"] is True
    assert calls["n"] == 1


def test_workspace_git_info_cache_busts_on_ttl_expiry(monkeypatch):
    """Once the TTL elapses, the next call must recompute."""
    from relaydeck.worktrees import (
        _cached_workspace_git_info_core,
        _git_info_cache,
        _git_info_cache_lock,
        _workspace_git_info_core,
        clear_workspace_git_info_cache,
    )

    # We don't need a real repo — `_workspace_git_info_core` is patched out.
    target = Path("/tmp/__relaydeck_cache_ttl_test__")
    clear_workspace_git_info_cache()
    calls = {"n": 0}

    def fake(path):
        calls["n"] += 1
        return {"is_git": False, "branch": None, "dirty": False}

    monkeypatch.setattr("relaydeck.worktrees._workspace_git_info_core", fake)
    monkeypatch.setattr("relaydeck.worktrees._GIT_INFO_TTL_S", 0.01)
    _cached_workspace_git_info_core(target)
    assert calls["n"] == 1
    # Backdate the entry past the TTL so the next call must recompute.
    import time as _t
    with _git_info_cache_lock:
        for k, (_, info) in list(_git_info_cache.items()):
            _git_info_cache[k] = (_t.monotonic() - 1.0, info)
    _cached_workspace_git_info_core(target)
    assert calls["n"] == 2


def test_git_cache_key_case_folds_on_case_insensitive_fs(monkeypatch):
    """Two casings of the same resolved path must collapse to one key
    when the host FS is case-insensitive (APFS, NTFS), and stay distinct
    otherwise (case-sensitive Linux)."""
    from relaydeck.worktrees import _git_cache_key

    p1 = Path("/Users/x/Repo")
    p2 = Path("/Users/x/repo")

    monkeypatch.setattr("relaydeck.worktrees._CASE_INSENSITIVE_FS", True)
    assert _git_cache_key(p1) == _git_cache_key(p2)

    monkeypatch.setattr("relaydeck.worktrees._CASE_INSENSITIVE_FS", False)
    assert _git_cache_key(p1) != _git_cache_key(p2)
