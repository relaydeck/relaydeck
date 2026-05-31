"""
GitHub issue→PR enrichment + spawn workflow.

  - enrichment (`enrich.py`): gh api wrappers + PR-health summary, with
    `gh` mocked at the subprocess / gh_api boundary (no network).
  - spawn workflow (`spawn.py`): worktree → workspace → agent → task,
    end-to-end against a REAL temp git repo + MockHost (hermetic).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from plugins.github import enrich
from plugins.github.spawn import spawn_worktree_agent
from relaydeck.sdk import CapabilityNotDeclared
from relaydeck.testing import MockHost


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# ── enrichment ──────────────────────────────────────────────────────


def test_gh_api_parses_json(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(out='{"x": 1}'))
    assert enrich.gh_api("gh", "repos/o/r") == {"x": 1}


def test_gh_api_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(rc=1, err="boom"))
    with pytest.raises(enrich.GhError):
        enrich.gh_api("gh", "x")


def test_gh_api_raises_on_nonjson(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(out="not json"))
    with pytest.raises(enrich.GhError):
        enrich.gh_api("gh", "x")


def test_fetch_check_runs_unwraps_envelope(monkeypatch):
    monkeypatch.setattr(enrich, "gh_api", lambda *a, **k: {"check_runs": [{"name": "ci"}]})
    assert enrich.fetch_check_runs("gh", "o/r", "sha") == [{"name": "ci"}]


def test_summarize_pr_health_flags_failures_and_changes(monkeypatch):
    pr = {
        "title": "T", "state": "open", "user": {"login": "alice"},
        "head": {"sha": "abc", "ref": "feature"}, "mergeable": True,
    }
    checks = {"check_runs": [
        {"name": "ci", "conclusion": "failure", "html_url": "u"},
        {"name": "lint", "conclusion": "success"},
    ]}
    reviews = [
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "carol"}, "state": "APPROVED"},
    ]

    def fake(_gh, endpoint, **_k):
        if endpoint.endswith("/pulls/5"):
            return pr
        if "check-runs" in endpoint:
            return checks
        if endpoint.endswith("/reviews"):
            return reviews
        return None

    monkeypatch.setattr(enrich, "gh_api", fake)
    h = enrich.summarize_pr_health("gh", "o/r", 5)
    assert h["healthy"] is False
    assert h["author"] == "alice"
    assert h["head_sha"] == "abc"
    assert [c["name"] for c in h["failing_checks"]] == ["ci"]
    assert h["changes_requested_by"] == ["bob"]  # carol approved → not listed


def test_summarize_pr_health_healthy(monkeypatch):
    pr = {"title": "T", "state": "open", "user": {"login": "a"}, "head": {"sha": "abc"}}

    def fake(_gh, endpoint, **_k):
        if "check-runs" in endpoint:
            return {"check_runs": [{"name": "ci", "conclusion": "success"}]}
        if endpoint.endswith("/reviews"):
            return [{"user": {"login": "b"}, "state": "APPROVED"}]
        return pr

    monkeypatch.setattr(enrich, "gh_api", fake)
    assert enrich.summarize_pr_health("gh", "o/r", 1)["healthy"] is True


# ── spawn workflow ──────────────────────────────────────────────────


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "tester")
    (repo / "f.txt").write_text("x\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return repo


def test_spawn_worktree_agent_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    host = MockHost(name="github", config_home=tmp_path / ".relaydeck")

    res = spawn_worktree_agent(
        host,
        repo_path=repo,
        branch="relaydeck/issue-42",
        agent_id="fixer-42",
        kind="issue",
        external_ref="o/r#42",
        title="Fix the bug",
        purpose="fix issue 42",
        workspace_plugins=["messaging"],
    )

    assert res["workspace"] == "relaydeck-issue-42"
    assert res["agent_id"] == "fixer-42"
    assert Path(res["path"]).exists()

    # Agent created + started in the worktree workspace.
    info = host.agents.get("fixer-42")
    assert info is not None
    assert info.workspace == "relaydeck-issue-42"
    assert info.status == "running"

    # Task recorded, linking agent ↔ upstream ref.
    task = host.tasks.get(res["task_id"])
    assert task is not None
    assert task.external_ref == "o/r#42"
    assert task.agent_id == "fixer-42"
    assert task.kind == "issue"
    assert task.status == "in_progress"

    # Workspace registered with the requested plugins.
    from relaydeck.config import load_workspace_registry
    ws = next(w for w in load_workspace_registry() if w.name == "relaydeck-issue-42")
    assert "messaging" in ws.plugins


def test_spawn_aborts_cleanly_on_git_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    host = MockHost(name="github", config_home=tmp_path / ".relaydeck")

    spawn_worktree_agent(host, repo_path=repo, branch="dup", agent_id="a1")
    # Re-spawning the same branch fails at `git worktree add`; nothing new
    # should be registered or created.
    from relaydeck.worktrees import WorktreeError
    with pytest.raises(WorktreeError):
        spawn_worktree_agent(host, repo_path=repo, branch="dup", agent_id="a2")

    assert host.agents.get("a2") is None
    from relaydeck.config import load_workspace_registry
    assert [w.name for w in load_workspace_registry()].count("dup") == 1


class _CtxResp:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return b'{"ok": true}'


def test_post_daemon_uses_ca_pinned_tls_for_https(monkeypatch):
    """`relaydeck github spawn` must carry the pinned daemon CA so it works
    against an HTTPS daemon (`relaydeck serve --tls-self-signed`), like the
    core CLI — not bypass TLS verification."""
    import ssl
    import urllib.request

    from plugins.github import plugin as ghplugin

    monkeypatch.setattr("relaydeck.state.get_daemon_url", lambda: "https://localhost:8765")
    monkeypatch.setattr("relaydeck.state.get_daemon_ca", lambda: None)
    monkeypatch.setattr("relaydeck.auth.read_token", lambda: None)
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _CtxResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, _resp = ghplugin._post_daemon("/api/plugins/github/spawn", {"x": 1})
    assert ok is True
    assert isinstance(captured["context"], ssl.SSLContext)


def test_post_daemon_no_tls_context_for_http(monkeypatch):
    import urllib.request

    from plugins.github import plugin as ghplugin

    monkeypatch.setattr("relaydeck.state.get_daemon_url", lambda: "http://localhost:8765")
    monkeypatch.setattr("relaydeck.state.get_daemon_ca", lambda: None)
    monkeypatch.setattr("relaydeck.auth.read_token", lambda: None)
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _CtxResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ghplugin._post_daemon("/x", {})
    assert captured["context"] is None  # plain HTTP → no ssl context


def test_spawn_requires_capabilities_before_any_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    # Host lacks workspaces.create / tasks.write → preflight refuses the
    # workflow BEFORE the worktree is created (no orphaned state).
    host = MockHost(
        name="github", config_home=tmp_path / ".relaydeck",
        declared_capabilities={"agents.list", "agents.create", "agents.start"},
    )
    with pytest.raises(CapabilityNotDeclared):
        spawn_worktree_agent(host, repo_path=repo, branch="cap-test", agent_id="a1")
    # Preflight ran first — no worktree was created.
    assert not (tmp_path / ".relaydeck" / "worktrees" / "cap-test").exists()


def test_spawn_rolls_back_when_workspace_step_fails(tmp_path, monkeypatch):
    """A failure after the worktree exists removes the worktree and
    unregisters anything partial — no orphaned state."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    host = MockHost(name="github", config_home=tmp_path / ".relaydeck")

    def boom(*_a, **_k):
        raise RuntimeError("agent create failed")

    monkeypatch.setattr(host.agents, "create", boom)
    with pytest.raises(RuntimeError, match="agent create failed"):
        spawn_worktree_agent(host, repo_path=repo, branch="rb", agent_id="a1")

    assert not (tmp_path / ".relaydeck" / "worktrees" / "rb").exists(), \
        "worktree should be removed on rollback"
    from relaydeck.config import load_workspace_registry
    assert "rb" not in [w.name for w in load_workspace_registry()], \
        "workspace should be unregistered on rollback"


def test_spawn_rolls_back_agent_on_start_failure(tmp_path, monkeypatch):
    """If the agent is created but start fails, rollback deletes the agent
    too (exercises the orchestrator delete path)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = _init_repo(tmp_path)
    host = MockHost(name="github", config_home=tmp_path / ".relaydeck")

    def boom(*_a, **_k):
        raise RuntimeError("start failed")

    monkeypatch.setattr(host.agents, "start", boom)
    with pytest.raises(RuntimeError, match="start failed"):
        spawn_worktree_agent(host, repo_path=repo, branch="rb2", agent_id="a2")

    assert host.agents.get("a2") is None, "agent should be deleted on rollback"
    assert not (tmp_path / ".relaydeck" / "worktrees" / "rb2").exists()
    from relaydeck.config import load_workspace_registry
    assert "rb2" not in [w.name for w in load_workspace_registry()]
