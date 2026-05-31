"""
Task metadata model — data layer + SDK host surfaces.

`tasks` is the plugin-owned lifecycle store (PR/issue/CI work records),
deliberately separate from agent specs. Tests cover the `relaydeck.tasks`
helpers, the `host.tasks` SDK surface (owner + workspace scoping,
capability gates, idempotent upsert), and the `host.workspaces`
lifecycle surface (register/update task workspaces).

Real SQLite, real registry writes under tmp_path — no I/O mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.db import open_db
from relaydeck.sdk import CapabilityNotDeclared
from relaydeck.tasks import (
    create_task,
    delete_task,
    find_by_external_ref,
    get_task,
    list_tasks,
    update_task,
    upsert_by_external_ref,
)
from relaydeck.testing import MockHost


def _db(tmp_path) -> str:
    p = tmp_path / "relaydeck.db"
    open_db(str(p)).close()
    return str(p)


# ── Data layer ──────────────────────────────────────────────────────


def test_create_and_get(tmp_path):
    db = _db(tmp_path)
    t = create_task(
        "github", kind="pr", workspace="ws", external_ref="repo#1",
        title="Fix bug", metadata={"pr": 1}, db_path=db,
    )
    got = get_task(t.id, db_path=db)
    assert got is not None
    assert got.plugin == "github"
    assert got.kind == "pr"
    assert got.external_ref == "repo#1"
    assert got.workspace == "ws"
    assert got.status == "open"
    assert got.metadata == {"pr": 1}
    assert got.created_at > 0 and got.updated_at > 0


def test_find_by_external_ref_is_plugin_scoped(tmp_path):
    db = _db(tmp_path)
    create_task("github", external_ref="repo#1", db_path=db)
    create_task("other", external_ref="repo#1", db_path=db)
    a = find_by_external_ref("github", "repo#1", db_path=db)
    assert a is not None and a.plugin == "github"
    assert find_by_external_ref("nobody", "repo#1", db_path=db) is None


def test_list_filters(tmp_path):
    db = _db(tmp_path)
    create_task("github", workspace="w1", agent_id="a1", status="open", db_path=db)
    create_task("github", workspace="w2", agent_id="a2", status="done", db_path=db)
    create_task("other", workspace="w1", db_path=db)
    assert len(list_tasks(plugin="github", db_path=db)) == 2
    assert len(list_tasks(plugin="github", workspace="w1", db_path=db)) == 1
    assert len(list_tasks(plugin="github", status="done", db_path=db)) == 1
    assert len(list_tasks(plugin="github", agent_id="a2", db_path=db)) == 1
    assert len(list_tasks(workspace="w1", db_path=db)) == 2  # across plugins


def test_update_partial_merges_metadata(tmp_path):
    db = _db(tmp_path)
    t = create_task("github", title="orig", metadata={"pr": 1, "ci": "red"}, db_path=db)
    u = update_task(t.id, status="in_progress", metadata={"ci": "green"}, db_path=db)
    assert u is not None
    assert u.status == "in_progress"
    assert u.metadata == {"pr": 1, "ci": "green"}  # merged, pr preserved
    assert u.title == "orig"  # untouched
    assert u.updated_at >= t.updated_at


def test_update_can_replace_metadata(tmp_path):
    db = _db(tmp_path)
    t = create_task("github", metadata={"a": 1}, db_path=db)
    u = update_task(t.id, metadata={"b": 2}, merge_metadata=False, db_path=db)
    assert u.metadata == {"b": 2}


def test_update_clears_agent_with_explicit_none(tmp_path):
    db = _db(tmp_path)
    t = create_task("github", agent_id="a1", db_path=db)
    u = update_task(t.id, agent_id=None, db_path=db)
    assert u.agent_id is None


def test_update_unknown_returns_none(tmp_path):
    db = _db(tmp_path)
    assert update_task("task_missing", status="done", db_path=db) is None


def test_upsert_creates_then_updates_in_place(tmp_path):
    db = _db(tmp_path)
    t1 = upsert_by_external_ref("github", "repo#9", title="t", status="open", db_path=db)
    t2 = upsert_by_external_ref("github", "repo#9", status="done", db_path=db)
    assert t1.id == t2.id  # same row, not a duplicate
    assert t2.status == "done"
    assert len(list_tasks(plugin="github", db_path=db)) == 1


def test_delete(tmp_path):
    db = _db(tmp_path)
    t = create_task("github", db_path=db)
    assert delete_task(t.id, db_path=db) is True
    assert get_task(t.id, db_path=db) is None
    assert delete_task("task_missing", db_path=db) is False


# ── host.tasks ──────────────────────────────────────────────────────


def _host(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return MockHost(config_home=tmp_path / ".relaydeck", **kw)


def test_host_tasks_create_and_owner_scoping(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github")
    t = host.tasks.create(kind="pr", external_ref="repo#1", title="x")
    assert t.plugin == "github"
    assert host.tasks.get(t.id).id == t.id

    # A different plugin's host can't see github's task.
    other = _host(tmp_path, monkeypatch, name="other")
    assert other.tasks.get(t.id) is None
    assert other.tasks.update(t.id, status="done") is None


def test_host_tasks_scopes_to_host_workspace(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github", workspace="proj")
    t = host.tasks.create(external_ref="r#1")
    assert t.workspace == "proj"
    assert [x.id for x in host.tasks.list()] == [t.id]


def test_host_tasks_upsert_is_idempotent(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github")
    a = host.tasks.upsert_by_ref("r#1", title="t", status="open")
    b = host.tasks.upsert_by_ref("r#1", status="done")
    assert a.id == b.id and b.status == "done"
    assert len(host.tasks.list()) == 1


def test_host_tasks_requires_capabilities(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github",
                 declared_capabilities={"agents.list"})
    with pytest.raises(CapabilityNotDeclared):
        host.tasks.create(title="x")
    with pytest.raises(CapabilityNotDeclared):
        host.tasks.list()


# ── host.workspaces ─────────────────────────────────────────────────


def test_host_workspaces_create_registers_and_lists(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github")
    ws_dir = tmp_path / "worktrees" / "pr-7"
    ws_dir.mkdir(parents=True)
    info = host.workspaces.create("pr-7", ws_dir, plugins=["messaging"])
    assert info["name"] == "pr-7"

    names = [w["name"] for w in host.workspaces.list()]
    assert "pr-7" in names
    cfg = tmp_path / ".relaydeck"
    assert (cfg / "config.toml").exists()
    assert (cfg / "workspaces" / "pr-7" / "agent.toml").exists()


def test_host_workspaces_list_uses_host_config_home(tmp_path, monkeypatch):
    """Regression: list() must read the host's config_home, not the
    process Path.home — otherwise a plugin creates into one registry and
    lists a different one for embedded/custom roots."""
    # Point Path.home somewhere unrelated; a list() that ignored
    # config_home would read this empty registry instead.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "elsewhere")
    cfg = tmp_path / "custom" / "relaydeck"
    cfg.mkdir(parents=True)
    host = MockHost(name="github", config_home=cfg)
    ws_dir = tmp_path / "wt"
    ws_dir.mkdir()
    host.workspaces.create("api", ws_dir)
    assert [w["name"] for w in host.workspaces.list()] == ["api"]


def test_host_workspaces_update_plugins(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github")
    ws_dir = tmp_path / "wt"
    ws_dir.mkdir()
    host.workspaces.create("pr-7", ws_dir, plugins=[])
    host.workspaces.update("pr-7", plugins=["recipes"])
    ws = next(w for w in host.workspaces.list() if w["name"] == "pr-7")
    assert ws["plugins"] == ["recipes"]


def test_host_workspaces_duplicate_name_raises(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github")
    ws_dir = tmp_path / "wt"
    ws_dir.mkdir()
    host.workspaces.create("pr-7", ws_dir)
    with pytest.raises(ValueError, match="already registered"):
        host.workspaces.create("pr-7", ws_dir)


def test_host_workspaces_requires_capabilities(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, name="github",
                 declared_capabilities={"agents.list"})
    with pytest.raises(CapabilityNotDeclared):
        host.workspaces.create("x", tmp_path)
    with pytest.raises(CapabilityNotDeclared):
        host.workspaces.list()
