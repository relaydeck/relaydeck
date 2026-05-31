"""
Workspace CRUD + filesystem-browse HTTP endpoints. The dashboard
"Workspaces" tab is the primary caller; these tests pin the API
contract independently of the JS.

`fastapi.testclient.TestClient` mounts the FastAPI app in-process, so
the workspace endpoints run against a real Orchestrator pointed at a
tmp_path config home. No daemon, no sockets, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.transports.api import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """One TestClient per test, scoped to a fresh tmp config home so
    workspace state from one test can't leak into another."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    (cfg_home / "runtime").mkdir()
    # Reset orchestrator singleton between tests.
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    app = create_app(cfg_home)
    return TestClient(app)


# ── Workspace CRUD ──────────────────────────────────────────────────


def test_list_workspaces_empty(client):
    r = client.get("/api/workspaces")
    assert r.status_code == 200
    assert r.json() == []


def test_create_workspace_roundtrips(client, tmp_path):
    target = tmp_path / "my-proj"
    target.mkdir()
    r = client.post("/api/workspaces", json={
        "name": "demo", "path": str(target),
        "plugins": ["messaging", "skills"],
    })
    assert r.status_code == 200
    payload = r.json()
    assert payload["name"] == "demo"
    assert Path(payload["path"]) == target.resolve()
    assert payload["plugins"] == ["messaging", "skills"]

    # The on-disk side effects: config.toml entry + agent.toml
    cfg_home = tmp_path / ".relaydeck"
    assert (cfg_home / "workspaces" / "demo" / "agent.toml").exists()
    body = (cfg_home / "workspaces" / "demo" / "agent.toml").read_text()
    assert '"messaging"' in body
    assert '"skills"' in body

    # Visible in the list
    listed = client.get("/api/workspaces").json()
    assert listed and listed[0]["name"] == "demo"


def test_create_workspace_rejects_missing_path(client, tmp_path):
    r = client.post("/api/workspaces", json={
        "name": "ghost", "path": str(tmp_path / "does-not-exist"),
        "plugins": [],
    })
    assert r.status_code == 400


def test_create_workspace_makes_dir_when_requested(client, tmp_path):
    target = tmp_path / "new" / "nested-proj"
    assert not target.exists()
    r = client.post("/api/workspaces", json={
        "name": "fresh", "path": str(target),
        "plugins": ["messaging"], "create_dir": True,
    })
    assert r.status_code == 200
    assert target.is_dir()  # mkdir(parents=True) created the whole path
    listed = client.get("/api/workspaces").json()
    assert listed and listed[0]["name"] == "fresh"


def test_create_workspace_rejects_scalar_plugins(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    r = client.post("/api/workspaces", json={
        "name": "demo", "path": str(target), "plugins": "messaging",
    })
    assert r.status_code == 400
    assert "plugins must be a list of strings" in r.json()["detail"]


def test_create_workspace_rejects_duplicate_name(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={"name": "dup", "path": str(target), "plugins": []})
    r = client.post("/api/workspaces", json={"name": "dup", "path": str(target), "plugins": []})
    assert r.status_code == 409


def test_update_workspace_plugins(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={
        "name": "demo", "path": str(target), "plugins": [],
    })

    r = client.patch("/api/workspaces/demo", json={"plugins": ["messaging"]})
    assert r.status_code == 200
    assert r.json()["plugins"] == ["messaging"]

    # config.toml + agent.toml both got the update
    listed = client.get("/api/workspaces").json()
    assert listed[0]["plugins"] == ["messaging"]
    agent_toml = (tmp_path / ".relaydeck" / "workspaces" / "demo" / "agent.toml").read_text()
    assert '"messaging"' in agent_toml


def test_update_workspace_rejects_non_string_plugins(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={
        "name": "demo", "path": str(target), "plugins": [],
    })

    r = client.patch("/api/workspaces/demo", json={"plugins": ["messaging", 3]})
    assert r.status_code == 400
    assert "plugins must be a list of strings" in r.json()["detail"]


def test_update_workspace_404_for_unknown(client):
    r = client.patch("/api/workspaces/ghost", json={"plugins": []})
    assert r.status_code == 404


def test_delete_workspace(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={"name": "demo", "path": str(target), "plugins": []})

    r = client.delete("/api/workspaces/demo")
    assert r.status_code == 200

    assert client.get("/api/workspaces").json() == []


def test_delete_workspace_refuses_when_agents_exist(client, tmp_path):
    """Refuse the delete so the user explicitly removes agents first
    — prevents agent.workspace pointing at a phantom row."""
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={"name": "demo", "path": str(target), "plugins": []})

    # Insert an agent against this workspace directly into the DB.
    from relaydeck.db import open_db
    db_path = str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db")
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO agents (id, type, name, status, workspace, auto_start, created_at) "
            "VALUES ('a1', 'pi', 'a1', 'stopped', 'demo', 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    r = client.delete("/api/workspaces/demo")
    assert r.status_code == 409


# ── Active workspace state ──────────────────────────────────────────


def test_active_workspace_get_default_is_null(client):
    r = client.get("/api/state/active-workspace")
    assert r.status_code == 200
    # Empty registry → no active workspace
    assert r.json()["name"] is None


def test_active_workspace_set_persists(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={"name": "demo", "path": str(target), "plugins": []})

    r = client.post("/api/state/active-workspace", json={"name": "demo"})
    assert r.status_code == 200
    assert r.json()["name"] == "demo"

    assert client.get("/api/state/active-workspace").json()["name"] == "demo"


def test_delete_active_workspace_clears_active_pointer(client, tmp_path):
    target = tmp_path / "p"
    target.mkdir()
    client.post("/api/workspaces", json={"name": "demo", "path": str(target), "plugins": []})
    client.post("/api/state/active-workspace", json={"name": "demo"})
    client.delete("/api/workspaces/demo")

    # No registered workspaces → resolver falls all the way through.
    # state.yaml's current_workspace should be cleared (not pointing
    # at the deleted workspace).
    from relaydeck.state import _load
    assert _load().get("current_workspace") in (None, "")


# ── Filesystem browser ──────────────────────────────────────────────


def test_fs_browse_returns_subdirs(client, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "file.txt").write_text("x")  # files filtered out
    (tmp_path / ".hidden").mkdir()           # dotfiles filtered out

    r = client.get(f"/api/fs/browse?path={tmp_path}")
    assert r.status_code == 200
    data = r.json()
    assert Path(data["path"]) == tmp_path.resolve()
    names = [e["name"] for e in data["entries"]]
    assert names == ["alpha", "beta"]
    assert data["parent"] is not None
    assert data["home"]


def test_fs_browse_includes_validation_chips(client, tmp_path):
    """The add-workspace modal renders inline chips (✓ git repo, ⚠ already
    a workspace, ⚠ read-only) so the operator doesn't need to commit + read
    a 4xx to learn the path's status. The endpoint must surface those
    flags per-call."""
    plain = tmp_path / "plain"
    plain.mkdir()
    r = client.get(f"/api/fs/browse?path={plain}")
    assert r.status_code == 200
    data = r.json()
    # No .git, no workspace registration → all the validation fields are
    # present-and-false (not missing — frontend reads them with `data.x`,
    # so they must exist on every response).
    assert data["is_git_repo"] is False
    assert data["existing_workspace"] is None
    assert data["writable"] is True  # tmp_path is owned by the test user


def test_fs_browse_flags_git_repo(client, tmp_path):
    """Both regular repos (.git dir) and worktrees/submodules (.git file)
    must trip the is_git_repo flag — git itself uses the same probe."""
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    r = client.get(f"/api/fs/browse?path={repo}")
    assert r.json()["is_git_repo"] is True

    wt = tmp_path / "wt"; wt.mkdir()
    (wt / ".git").write_text("gitdir: /tmp/something\n")
    r2 = client.get(f"/api/fs/browse?path={wt}")
    assert r2.json()["is_git_repo"] is True


def test_fs_browse_flags_existing_workspace(client, tmp_path):
    """A path that's already registered as a workspace must surface its
    name so the modal can warn the operator (currently as a yellow chip)
    rather than failing at the POST step with a generic 409."""
    proj = tmp_path / "proj"; proj.mkdir()
    # Register it first via the same workspace API the modal uses.
    add = client.post(
        "/api/workspaces",
        json={"name": "alreadyhere", "path": str(proj), "plugins": []},
    )
    assert add.status_code == 200, add.text

    r = client.get(f"/api/fs/browse?path={proj}")
    assert r.status_code == 200
    assert r.json()["existing_workspace"] == "alreadyhere"


def test_fs_browse_404_for_missing(client):
    r = client.get("/api/fs/browse?path=/this/does/not/exist/anywhere")
    assert r.status_code == 404


def test_fs_browse_400_for_file(client, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    r = client.get(f"/api/fs/browse?path={f}")
    assert r.status_code == 400


def test_fs_browse_no_path_defaults_to_home(client, tmp_path, monkeypatch):
    # We already monkeypatched Path.home to tmp_path in the fixture.
    r = client.get("/api/fs/browse")
    assert r.status_code == 200
    assert Path(r.json()["path"]) == tmp_path.resolve()


# ── Workspace-plugin catalog ────────────────────────────────────────


def test_workspace_plugin_catalog_lists_only_scoped(client, tmp_path):
    """The Workspaces tab calls this — it must NOT include
    harnesses, providers, or always-on infrastructure (file-watcher,
    gateway, metering, etc.). It MUST include the harness-gate names
    (skills, fleet-context, forbidden-tools) and any plugin that opted
    in via workspace_scoped=True (messaging in a standard install)."""
    # Force the registry to discover real plugins so the catalog has
    # the workspace_scoped flag populated. Tests run with a tmp_path
    # config home but the builtin plugin tree is on the source repo,
    # so discovery still finds messaging/skills/etc.
    import relaydeck.plugin as plug
    plug._registry = None
    from relaydeck.plugin import PluginContext, get_registry
    reg = get_registry(tmp_path / ".relaydeck")
    reg.load_all(PluginContext(config_home=tmp_path / ".relaydeck"))

    r = client.get("/api/workspace-plugins")
    assert r.status_code == 200
    names = {entry["name"] for entry in r.json()}

    # Plugin-backed workspace-scoped entries
    assert "messaging" in names
    # Harness-gate entries (no Python plugin behind them)
    assert "skills" in names
    assert "fleet-context" in names
    assert "forbidden-tools" in names

    # Things that MUST NOT appear here — harnesses, providers, always-on infra
    for absent in ("pi-harness", "claude-code-harness", "codex-harness",
                   "openai", "anthropic", "ollama", "openrouter",
                   "file-watcher", "gateway", "metering", "vault"):
        assert absent not in names, f"{absent!r} should not be in workspace catalog"


def test_plugin_ui_manifest_drops_disabled_plugins(client, tmp_path):
    """When a plugin is disabled, its tab/chip/tile must drop out of
    /api/plugins/ui so the dashboard removes the corresponding DOM
    nodes. Uses `metering` as the test plugin since it ships an agent
    tile — any tab/chip/tile-contributing plugin would do."""
    import relaydeck.plugin as plug
    plug._registry = None
    from relaydeck.plugin import PluginContext, get_registry
    reg = get_registry(tmp_path / ".relaydeck")
    reg.load_all(PluginContext(config_home=tmp_path / ".relaydeck"))

    # Seed app.state.ui_manifest as cli.py would after collecting
    # register_ui() results from each plugin.
    client.app.state.ui_manifest = {
        "tabs": [
            {"id": "extra-tab", "title": "Extra",
             "icon": "★", "module": "/static/plugins/metering/panel.js",
             "plugin": "metering", "order": 100},
        ],
        "header_chips": [],
        "agent_tiles": [
            {"id": "mood", "title": "Mood",
             "module": "/static/plugins/metering/tile_metering.js",
             "plugin": "metering", "order": 50},
            {"id": "msg-count", "title": "Messages",
             "module": "/static/plugins/messaging/tile.js",
             "plugin": "messaging", "order": 60},
        ],
    }

    # Before disabling — metering's contributions are in the manifest.
    r = client.get("/api/plugins/ui")
    assert r.status_code == 200
    data = r.json()
    assert any(t["plugin"] == "metering" for t in data["tabs"])
    assert any(t["plugin"] == "metering" for t in data["agent_tiles"])
    assert any(t["plugin"] == "messaging" for t in data["agent_tiles"])

    # Disable metering globally.
    reg.disable("metering")

    # After disable — metering's tab + tile gone, but messaging's tile
    # (different plugin) still passes through.
    r = client.get("/api/plugins/ui")
    data = r.json()
    assert not any(t["plugin"] == "metering" for t in data["tabs"])
    assert not any(t["plugin"] == "metering" for t in data["agent_tiles"])
    assert any(t["plugin"] == "messaging" for t in data["agent_tiles"])

    # Re-enable — metering's contributions reappear.
    reg.enable("metering")
    r = client.get("/api/plugins/ui")
    data = r.json()
    assert any(t["plugin"] == "metering" for t in data["tabs"])


def test_workspace_plugin_catalog_shape(client, tmp_path):
    """Each entry has the keys the dashboard needs to render."""
    import relaydeck.plugin as plug
    plug._registry = None
    from relaydeck.plugin import PluginContext, get_registry
    reg = get_registry(tmp_path / ".relaydeck")
    reg.load_all(PluginContext(config_home=tmp_path / ".relaydeck"))

    r = client.get("/api/workspace-plugins")
    entries = r.json()
    assert entries
    for e in entries:
        assert "name" in e
        assert "description" in e
        assert "kind" in e and e["kind"] in ("plugin", "harness-gate")
        assert "globally_enabled" in e

    # Harness-gate entries are always globally_enabled (no daemon-load gate)
    gates = [e for e in entries if e["kind"] == "harness-gate"]
    assert gates and all(g["globally_enabled"] is True for g in gates)
