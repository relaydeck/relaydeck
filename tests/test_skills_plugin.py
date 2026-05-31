"""
Bundled skills plugin — the generic `[plugin.skills]` materializer
(manager.sync_all), the inventory rescan (cache + events), operator
link/unlink, and the plugin's on_load wiring (CLI + API + worker).

Real filesystem + real SQLite under tmp_path; the registry is stubbed
with lightweight entries so we exercise the manifest-driven sync without
booting the whole daemon.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from relaydeck import skills as relaydeck_skills
from relaydeck import skills_cache
from relaydeck.db import open_db
from relaydeck.plugin_manifest import load_manifest
from plugins.skills import manager
from plugins.skills.plugin import SkillsPlugin

ROOT = Path(__file__).resolve().parent.parent


# ── fixtures / stubs ─────────────────────────────────────────────────


def _register_ws(config_home: Path, name: str, plugins=None) -> Path:
    from relaydeck.config import register_workspace
    src = config_home / "src" / name
    src.mkdir(parents=True, exist_ok=True)
    register_workspace(config_home, name, src, plugins or [])
    return config_home / "workspaces" / name


def _plugin_src(tmp_path: Path, name: str, body: str = "BODY") -> Path:
    """A fake plugin directory shipping a SKILL.md source file."""
    d = tmp_path / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: from {name}\n---\n\n{body}"
    )
    return d


def _entry(name, instance, skills_map, path):
    return SimpleNamespace(
        name=name, instance=instance,
        manifest=SimpleNamespace(skills=skills_map), path=path,
    )


class _Registry:
    def __init__(self, entries):
        self._entries = entries

    def all(self):
        return self._entries


def _db(tmp_path) -> str:
    p = tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    open_db(str(p)).close()
    return str(p)


# ── manager.sync_all (generic [plugin.skills] consumer) ──────────────


def test_sync_default_targets_workspace_scoped(tmp_path):
    """A workspace-scoped plugin's skill ships to the workspaces that
    list it in agent.toml — no custom hook needed."""
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "yes", plugins=["messaging"])
    _register_ws(config_home, "no", plugins=[])
    src = _plugin_src(tmp_path, "messaging")
    inst = SimpleNamespace(workspace_scoped=True)
    reg = _Registry([_entry("messaging", inst, {"relaydeck-cli": "SKILL.md"}, src)])

    rep = manager.sync_all(config_home, registry=reg)
    assert rep["written"] == 1
    yes = config_home / "workspaces" / "yes" / "runtime" / "skills" / "relaydeck-cli"
    no = config_home / "workspaces" / "no" / "runtime" / "skills" / "relaydeck-cli"
    assert yes.exists()
    assert not no.exists()
    # Sidecar attributes the skill to the plugin.
    sc = relaydeck_skills.read_sidecar(yes)
    assert sc["owner_plugin"] == "messaging"


def test_sync_honors_target_hook(tmp_path):
    """A daemon-wide plugin (telegram) targets a dynamic set via
    skill_target_workspaces()."""
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "routed", plugins=[])
    _register_ws(config_home, "other", plugins=[])
    src = _plugin_src(tmp_path, "telegram")

    class _Tg:
        workspace_scoped = False
        def skill_target_workspaces(self, all_ws):
            return ["routed"]

    reg = _Registry([_entry("telegram", _Tg(), {"relaydeck-telegram": "SKILL.md"}, src)])
    manager.sync_all(config_home, registry=reg)
    assert (config_home / "workspaces" / "routed" / "runtime" / "skills" / "relaydeck-telegram").exists()
    assert not (config_home / "workspaces" / "other" / "runtime" / "skills" / "relaydeck-telegram").exists()


def test_daemon_wide_skill_via_host_adapter_materializes(tmp_path):
    """Regression: an SDK plugin is wrapped in HostPluginAdapter, so the
    manager sees the ADAPTER as `instance`. The adapter must forward
    skill_target_workspaces, or a daemon-wide plugin (theme) resolves to
    NO workspaces and never materializes."""
    from types import SimpleNamespace

    from relaydeck.plugin import HostPluginAdapter

    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "a", plugins=[])
    _register_ws(config_home, "b", plugins=[])
    src = _plugin_src(tmp_path, "theme")

    class _ThemeSDK:  # a new-style sdk.Plugin: daemon-wide, ships to all
        def skill_target_workspaces(self, all_ws):
            return list(all_ws)

    manifest = SimpleNamespace(name="theme", version="0.1.0", category="tool",
                               description="", dependencies=[], workspace_scoped=False)
    adapter = HostPluginAdapter(_ThemeSDK(), manifest, src)
    reg = _Registry([_entry("theme", adapter, {"theme-skill": "SKILL.md"}, src)])
    manager.sync_all(config_home, registry=reg)
    for ws in ("a", "b"):
        assert (config_home / "workspaces" / ws / "runtime" / "skills" / "theme-skill").exists()


def test_host_adapter_skill_hook_passthrough_when_absent(tmp_path):
    """An adapter wrapping a plugin WITHOUT the hooks returns None (so the
    manager applies its workspace-scoped default) and passes content
    through unchanged."""
    from types import SimpleNamespace

    from relaydeck.plugin import HostPluginAdapter

    manifest = SimpleNamespace(name="x", version="0", category="tool",
                               description="", dependencies=[], workspace_scoped=True)
    adapter = HostPluginAdapter(object(), manifest, tmp_path)
    assert adapter.skill_target_workspaces(["a", "b"]) is None
    assert adapter.skill_content("s", "body") == "body"


def test_sync_honors_content_hook(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "w", plugins=["messaging"])
    src = _plugin_src(tmp_path, "messaging", body="FILE BODY")

    class _Msg:
        workspace_scoped = True
        def skill_content(self, skill_name, source_text):
            return "OVERRIDE BODY"

    reg = _Registry([_entry("messaging", _Msg(), {"relaydeck-cli": "SKILL.md"}, src)])
    manager.sync_all(config_home, registry=reg)
    md = config_home / "workspaces" / "w" / "runtime" / "skills" / "relaydeck-cli" / "SKILL.md"
    assert md.read_text() == "OVERRIDE BODY"


def test_skills_plugin_ships_plugin_authoring_skill_to_all_workspaces(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "a", plugins=[])
    _register_ws(config_home, "b", plugins=[])
    plugin_dir = ROOT / "plugins" / "skills"
    manifest = load_manifest(plugin_dir / "plugin.toml")

    class _Settings:
        def get(self, key):
            assert key == "inject_plugin_authoring_skill"
            return True

    inst = SkillsPlugin()
    inst.host = SimpleNamespace(settings=_Settings())
    reg = _Registry([_entry("skills", inst, manifest.skills, plugin_dir)])

    manager.sync_all(config_home, registry=reg)

    for ws in ("a", "b"):
        skill = (
            config_home
            / "workspaces"
            / ws
            / "runtime"
            / "skills"
            / "relaydeck-plugin-dev"
            / "SKILL.md"
        )
        assert skill.exists()
        assert "relaydeck plugin development" in skill.read_text().lower()


def test_skills_plugin_authoring_skill_can_be_disabled(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "a", plugins=[])
    inst = SkillsPlugin()
    inst.host = SimpleNamespace(settings=SimpleNamespace(get=lambda key: False))

    assert inst.skill_target_workspaces(["a"]) == []


def test_sync_removes_orphans_when_target_drops(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "w", plugins=["messaging"])
    src = _plugin_src(tmp_path, "messaging")
    inst = SimpleNamespace(workspace_scoped=True)
    reg = _Registry([_entry("messaging", inst, {"relaydeck-cli": "SKILL.md"}, src)])
    manager.sync_all(config_home, registry=reg)
    md_dir = config_home / "workspaces" / "w" / "runtime" / "skills" / "relaydeck-cli"
    assert md_dir.exists()

    # Opt the workspace out → next sync removes the materialized skill.
    from relaydeck.config import set_workspace_plugins
    set_workspace_plugins(config_home, "w", [])
    manager.sync_all(config_home, registry=reg)
    assert not md_dir.exists()


def test_remove_plugin_skills(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "w", plugins=["messaging"])
    src = _plugin_src(tmp_path, "messaging")
    inst = SimpleNamespace(workspace_scoped=True)
    reg = _Registry([_entry("messaging", inst, {"relaydeck-cli": "SKILL.md"}, src)])
    manager.sync_all(config_home, registry=reg)
    assert manager.remove_plugin_skills(config_home, "messaging") == 1


# ── manager.rescan (cache mirror + events) ───────────────────────────


def test_rescan_populates_cache_and_emits(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    ws = _register_ws(config_home, "proj", plugins=[])
    (ws / "skills" / "good").mkdir(parents=True)
    (ws / "skills" / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: d\n---\nbody"
    )
    emitted: list[tuple] = []
    summary = manager.rescan(config_home, db, emit=lambda t, d: emitted.append((t, d)),
                             include_codex=False, include_claude=False)
    assert summary["total"] == 1
    assert summary["changed"] == 1
    rows = skills_cache.list_skills_cache(db)
    assert [r["name"] for r in rows] == ["good"]
    assert any(t == "skills.changed" for t, _ in emitted)


def test_rescan_emits_removed_on_disappearance(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    ws = _register_ws(config_home, "proj", plugins=[])
    skill = ws / "skills" / "temp"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: temp\ndescription: d\n---\nx")
    manager.rescan(config_home, db, include_codex=False, include_claude=False)
    import shutil
    shutil.rmtree(skill)
    emitted: list[tuple] = []
    summary = manager.rescan(config_home, db, emit=lambda t, d: emitted.append((t, d)),
                             include_codex=False, include_claude=False)
    assert summary["removed"] == 1
    assert any(t == "skills.removed" for t, _ in emitted)


# ── link / unlink ────────────────────────────────────────────────────


def test_link_symlink_and_unlink(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    target = tmp_path / "ext" / "cool-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: cool\ndescription: d\n---\nbody")

    link = manager.link_skill(config_home, db, "proj", str(target), "cool", "symlink")
    assert link["alias"] == "cool"
    dest = config_home / "workspaces" / "proj" / "skills" / "cool"
    assert dest.is_symlink()
    # Discovered as an injectable workspace skill.
    refs = relaydeck_skills.discover_workspace_skills(config_home, "proj")
    assert any(r.name == "cool" for r in refs)

    assert manager.unlink_skill(config_home, db, "proj", "cool") is True
    assert not dest.exists()


def test_link_copy_mode(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    target = tmp_path / "ext" / "c"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: c\ndescription: d\n---\nbody")
    manager.link_skill(config_home, db, "proj", str(target), "copied", "copy")
    dest = config_home / "workspaces" / "proj" / "skills" / "copied"
    assert dest.is_dir() and not dest.is_symlink()
    assert (dest / "SKILL.md").is_file()


def test_link_rejects_dir_without_skill_md(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    target = tmp_path / "ext" / "empty"
    target.mkdir(parents=True)
    with pytest.raises(ValueError, match="no SKILL.md"):
        manager.link_skill(config_home, db, "proj", str(target), "x", "symlink")


# ── path-traversal guards (the /api/plugins/skills/{link,unlink} routes
#    pass workspace+alias straight to these manager fns with no extra
#    sanitization, so guarding the manager guards the HTTP entry) ───────


@pytest.mark.parametrize("bad_alias", ["../escape", "../../etc", "a/b", "..", ".", "/abs"])
def test_link_rejects_traversal_alias(tmp_path, bad_alias):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    target = tmp_path / "ext" / "ok"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: ok\ndescription: d\n---\nx")
    with pytest.raises(ValueError, match="invalid alias|outside workspace"):
        manager.link_skill(config_home, db, "proj", str(target), bad_alias, "symlink")
    # Nothing escaped the config home.
    assert not (config_home / "workspaces" / "proj" / "escape").exists()


@pytest.mark.parametrize("bad_ws", ["../other", "..", "a/b", "/abs"])
def test_link_rejects_traversal_workspace(tmp_path, bad_ws):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    target = tmp_path / "ext" / "ok"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: ok\ndescription: d\n---\nx")
    with pytest.raises(ValueError, match="invalid workspace|outside workspace"):
        manager.link_skill(config_home, db, bad_ws, str(target), "ok", "symlink")


def test_unlink_rejects_traversal_alias(tmp_path):
    # A malicious unlink must not rmtree outside the workspace skills dir.
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("important")
    # `../../../victim` would resolve outside skills/ — must be refused.
    with pytest.raises(ValueError, match="invalid alias|outside workspace"):
        manager.unlink_skill(config_home, db, "proj", "../../../victim")
    assert (victim / "keep.txt").exists(), "unlink must not delete outside the skills dir"


@pytest.mark.parametrize("bad_name", ["../evil", "a/b", "..", "."])
def test_materialize_rejects_traversal_skill_name(tmp_path, bad_name):
    # A malformed/malicious `[plugin.skills]` name must not escape runtime/skills.
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj", plugins=[])
    res = relaydeck_skills.materialize_skill(
        config_home, "proj", "evil-plugin", bad_name, "body",
    )
    assert res == "invalid"
    assert not (config_home / "workspaces" / "proj" / "evil").exists()
    assert not (config_home / "evil").exists()


def test_sync_plugin_skills_skips_malicious_name(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj", plugins=[])
    report = relaydeck_skills.sync_plugin_skills(
        config_home, "evil-plugin",
        {"../escape": "body", "good": "body"},
        target_workspaces=["proj"], all_workspaces=["proj"],
    )
    # The good one materializes; the traversal one is silently skipped.
    assert report["written"] == 1
    assert (config_home / "workspaces" / "proj" / "runtime" / "skills" / "good").is_dir()
    assert not (config_home / "workspaces" / "proj" / "runtime" / "escape").exists()


# ── plugin on_load wiring ────────────────────────────────────────────


def test_on_load_registers_cli_api_and_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.skills.plugin import SkillsPlugin
    from relaydeck.testing import MockHost

    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj", plugins=[])
    open_db(str(config_home / "runtime" / "relaydeck.db")).close()

    host = MockHost(name="skills", config_home=config_home)
    plugin = SkillsPlugin()
    try:
        plugin.on_load(host)
        cmd_names = {name for name, _fn, _attrs in host.cli.commands}
        assert {"list", "show", "validate", "rescan", "link", "unlink", "doctor"} <= cmd_names
        route_paths = {r["path"] for r in host.api.routes}
        assert "/skills" in route_paths
        assert "/rescan" in route_paths
    finally:
        plugin.on_unload()
        host.workers.teardown()


def test_api_routes_end_to_end(tmp_path, monkeypatch):
    """Load the registry the way `relaydeck serve` does, register the skills
    plugin's routes onto the real app, and exercise the HTTP surface."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from fastapi.testclient import TestClient

    from relaydeck.plugin import PluginContext, get_registry
    from relaydeck.transports.api import create_app

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    open_db(str(cfg_home / "runtime" / "relaydeck.db")).close()
    ws = _register_ws(cfg_home, "proj", plugins=[])
    (ws / "skills" / "good").mkdir(parents=True)
    (ws / "skills" / "good" / "SKILL.md").write_text("---\nname: good\ndescription: d\n---\nbody")

    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    import relaydeck.plugin as plugin_mod
    plugin_mod._registry = None
    registry = get_registry(cfg_home)
    registry.load_all(PluginContext(config_home=cfg_home))

    app = create_app(cfg_home)
    for entry in registry.all():
        try:
            entry.instance.register_api_routes(app)
        except Exception:
            pass
    client = TestClient(app)
    try:
        # Rescan first so the cache mirror is populated.
        rs = client.post("/api/plugins/skills/rescan")
        assert rs.status_code == 200
        assert rs.json()["ok"] is True

        r = client.get("/api/plugins/skills/skills")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["skills"]]
        assert "good" in names

        wr = client.get("/api/plugins/skills/workspaces/proj")
        assert wr.status_code == 200
        assert [s["name"] for s in wr.json()["user_skills"]] == ["good"]
    finally:
        for entry in registry.all():
            try:
                entry.instance.on_unload()
            except Exception:
                pass


def test_cli_list_and_validate(tmp_path, monkeypatch):
    """Drive the actual CLI command callbacks through Click."""
    import click
    from click.testing import CliRunner

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from plugins.skills.plugin import SkillsPlugin
    from relaydeck.testing import MockHost

    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj", plugins=[])
    (ws / "skills" / "good").mkdir(parents=True)
    (ws / "skills" / "good" / "SKILL.md").write_text("---\nname: good\ndescription: d\n---\nx")
    bad = ws / "skills" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter")
    open_db(str(config_home / "runtime" / "relaydeck.db")).close()

    host = MockHost(name="skills", config_home=config_home)
    plugin = SkillsPlugin()
    try:
        plugin.on_load(host)
        group = click.Group(name="skills")
        for name, fn, attrs in host.cli.commands:
            group.add_command(click.command(name=name)(fn))
        runner = CliRunner()
        out = runner.invoke(group, ["list"])
        assert out.exit_code == 0
        assert "good" in out.output
        # validate exits 1 because the bad skill is invalid
        res = runner.invoke(group, ["validate"])
        assert res.exit_code == 1
        assert "bad" in res.output
    finally:
        plugin.on_unload()
        host.workers.teardown()
