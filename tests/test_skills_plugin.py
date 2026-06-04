"""
Bundled skills plugin — the generic `[plugin.skills]` materializer
(manager.sync_all), the inventory rescan (cache + events), operator
link/unlink, and the plugin's on_load wiring (CLI + API + worker).

Real filesystem + real SQLite under tmp_path; the registry is stubbed
with lightweight entries so we exercise the manifest-driven sync without
booting the whole daemon.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from plugins.skills import commands, manager
from plugins.skills.plugin import SkillsPlugin
from relaydeck import skills as relaydeck_skills
from relaydeck import skills_cache
from relaydeck.db import open_db, record_usage
from relaydeck.plugin_manifest import load_manifest

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


def test_read_skill_metadata_reports_footprint(tmp_path):
    target = tmp_path / "ext" / "sample-skill"
    target.mkdir(parents=True)
    (target / "scripts").mkdir()
    (target / "scripts" / "run.sh").write_text("echo hello\n")
    (target / "SKILL.md").write_text(
        "---\n"
        "name: sample-skill\n"
        "description: sample\n"
        "---\n\n"
        "Run a shell helper."
    )

    meta = manager.read_skill_metadata(target)

    assert meta["valid"] is True
    assert meta["token_estimate"] > 0
    assert meta["name"] == "sample-skill"


def test_install_relaydeck_skill_copies_to_user_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    group = click.Group("skills")
    plugin = SimpleNamespace(
        host=SimpleNamespace(cli=group),
        config_home=tmp_path / ".relaydeck",
        db_path=str(tmp_path / ".relaydeck" / "runtime" / "relaydeck.db"),
        _include_codex=lambda: False,
        _include_claude=lambda: False,
    )
    commands.register(plugin)

    result = CliRunner().invoke(group, ["install", "--target", "both"])
    assert result.exit_code == 0, result.output

    for root in (tmp_path / "claude" / "skills", tmp_path / "codex" / "skills"):
        dest = root / "relaydeck"
        assert (dest / "SKILL.md").is_file()
        assert (dest / "scripts" / "relaydeck-bootstrap.sh").is_file()
        valid, errors, _warnings = relaydeck_skills.validate_skill_dir(dest)
        assert valid, errors

    again = CliRunner().invoke(group, ["install", "--target", "codex"])
    assert again.exit_code == 0, again.output
    assert "already exists" in again.output


def test_inventory_overview_counts_active_tokens_and_agents(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    ws = _register_ws(config_home, "proj", plugins=[])
    (ws / "skills" / "good").mkdir(parents=True)
    (ws / "skills" / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: d\n---\nbody"
    )
    conn = open_db(db)
    try:
        conn.execute(
            "INSERT INTO agents (id, type, name, status, workspace, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("a1", "codex", "A1", "running", "proj", 1.0),
        )
        record_usage(
            conn,
            "a1",
            "s1",
            "gpt-test",
            "openai",
            total_tokens=123,
            cost_usd=0.045,
            request_count=2,
        )
        conn.commit()
    finally:
        conn.close()

    manager.rescan(config_home, db, include_codex=False, include_claude=False)
    skills_cache.record_skill_usage(
        db,
        skill_name="good",
        workspace="proj",
        agent_id="a1",
        source="test",
        total_tokens=12,
        cost_usd=0.003,
    )
    overview = manager.inventory_overview(db)

    assert overview["summary"]["active"] == 1
    assert overview["summary"]["token_estimate"] > 0
    assert overview["summary"]["usage_tokens_by_skill_exposure"] == 123
    assert overview["summary"]["exact_skill_uses"] == 1
    assert overview["summary"]["exact_skill_usage_tokens"] == 12
    assert overview["usage_by_workspace"]["proj"]["total_tokens"] == 123
    assert overview["agents_by_workspace"]["proj"]["running"] == 1
    group = overview["groups"][0]
    assert group["agent_running"] == 1
    assert group["usage_tokens"] == 123
    assert group["usage_requests"] == 2
    assert group["exact_uses"] == 1
    assert group["exact_usage_tokens"] == 12
    assert group["workspaces"] == ["proj"]


def test_skill_usage_events_crud(tmp_path):
    db = _db(tmp_path)
    event = skills_cache.record_skill_usage(
        db,
        skill_name="review",
        workspace="proj",
        agent_id="agent-1",
        source="harness",
        total_tokens=42,
        metadata={"marker": "m1"},
    )

    rows = skills_cache.list_skill_usage(db, workspace="proj", skill_name="review")
    rollup = skills_cache.skill_usage_rollup(db)

    assert rows[0]["id"] == event["id"]
    assert rows[0]["metadata"] == {"marker": "m1"}
    assert rollup[("proj", "review")]["uses"] == 1
    assert rollup[("proj", "review")]["total_tokens"] == 42


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


def test_register_deploy_and_remove_catalog_skill(tmp_path):
    """A managed catalog skill injects into many workspaces from one place."""
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "a", plugins=[])
    _register_ws(config_home, "b", plugins=[])
    target = tmp_path / "ext" / "shared"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: shared\ndescription: d\n---\nbody")

    cat = manager.register_catalog_skill(config_home, db, str(target), "shared")
    assert cat["workspace"] == manager.CATALOG_WORKSPACE

    manager.deploy_catalog_skill(config_home, db, "shared", "a")
    manager.deploy_catalog_skill(config_home, db, "shared", "b")
    for ws in ("a", "b"):
        dest = config_home / "workspaces" / ws / "skills" / "shared"
        assert dest.is_symlink()
        names = [r.name for r in relaydeck_skills.discover_workspace_skills(config_home, ws)]
        assert "shared" in names

    with pytest.raises(ValueError):
        manager.deploy_catalog_skill(config_home, db, "does-not-exist", "a")

    assert manager.unlink_skill(config_home, db, "a", "shared") is True
    assert not (config_home / "workspaces" / "a" / "skills" / "shared").exists()
    assert (config_home / "workspaces" / "b" / "skills" / "shared").is_symlink()

    # Removing the catalog entry deletes its symlink too (no orphan), so the
    # alias can be re-centralized later.
    catalog_link = config_home / "plugin-data" / "skills" / "catalog" / "shared"
    assert catalog_link.is_symlink()
    assert manager.unlink_skill(config_home, db, manager.CATALOG_WORKSPACE, "shared") is True
    assert not catalog_link.exists() and not catalog_link.is_symlink()
    assert skills_cache.get_skill_link(db, manager.CATALOG_WORKSPACE, "shared") is None


def test_link_skill_rolls_back_filesystem_when_db_insert_fails(tmp_path, monkeypatch):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    target = tmp_path / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: bad\ndescription: d\n---\nbody")

    def boom(*_args, **_kwargs):
        raise RuntimeError("db failed")

    monkeypatch.setattr(skills_cache, "create_skill_link", boom)

    with pytest.raises(RuntimeError, match="db failed"):
        manager.link_skill(config_home, db, "proj", str(target), "bad", mode="copy")

    assert not (config_home / "workspaces" / "proj" / "skills" / "bad").exists()


def test_register_catalog_skill_rolls_back_symlink_when_db_insert_fails(
    tmp_path, monkeypatch,
):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: bad\ndescription: d\n---\nbody")

    def boom(*_args, **_kwargs):
        raise RuntimeError("db failed")

    monkeypatch.setattr(skills_cache, "create_skill_link", boom)

    with pytest.raises(RuntimeError, match="db failed"):
        manager.register_catalog_skill(config_home, db, str(target), "bad")

    catalog_link = config_home / "plugin-data" / "skills" / "catalog" / "bad"
    assert not catalog_link.exists() and not catalog_link.is_symlink()


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


def test_import_git_skill_records_provenance(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    repo = tmp_path / "repo"
    skill = repo / "skills" / "cool"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: Cool Skill\ndescription: d\n---\nbody")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        check=True,
    )

    res = manager.import_git_skill(
        config_home,
        db,
        workspace="proj",
        source_url=str(repo),
        source_subpath="skills/cool",
        mode="copy",
    )

    assert res["meta"]["valid"] is True
    link = skills_cache.get_skill_link(db, "proj", "cool-skill")
    assert link is not None
    assert link["target_id"] == res["meta"]["content_hash"]
    assert link["source_url"] == str(repo)
    assert link["source_subpath"] == "skills/cool"
    assert link["review_status"] == "imported"
    assert (
        config_home / "workspaces" / "proj" / "skills" / "cool-skill" / "SKILL.md"
    ).is_file()


def test_refresh_managed_imports_marks_upstream_update_available(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    repo = tmp_path / "repo"
    skill = repo / "skills" / "cool"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: cool\ndescription: d\n---\nold")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        check=True,
    )
    manager.import_git_skill(
        config_home,
        db,
        workspace="proj",
        source_url=str(repo),
        source_subpath="skills/cool",
        mode="symlink",
    )

    (skill / "SKILL.md").write_text("---\nname: cool\ndescription: d\n---\nnew")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "update"],
        check=True,
    )

    res = manager.refresh_managed_imports(config_home, db, force=True)

    assert res["checked"] == 1
    assert res["update_available"] == 1
    link = skills_cache.get_skill_link(db, "proj", "cool")
    assert link["review_status"] == "update-available"


def test_fetch_git_skill_resolves_slash_branch(tmp_path):
    # GitHub /tree/<ref>/<path> URLs are ambiguous when the branch name has a
    # slash: `feature/foo` + subpath `skills/cool` mis-parses to ref=`feature`,
    # subpath=`foo/skills/cool`. _disambiguate_ref must recover the real ref via
    # ls-remote and re-split the remainder so the right branch is cloned.
    from plugins.skills import imports

    config_home = tmp_path / ".relaydeck"
    config_home.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-C", str(repo)]
    subprocess.run([*g, "init", "-q", "-b", "main"], check=True)
    (repo / "README.md").write_text("base")
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "base"], check=True)
    subprocess.run([*g, "checkout", "-q", "-b", "feature/foo"], check=True)
    skill = repo / "skills" / "cool"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: cool\ndescription: d\n---\nbody")
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "feat"], check=True)

    # The mis-parsed inputs a GitHub /tree/feature/foo/skills/cool URL produces.
    res = imports.fetch_git_skill(
        config_home, str(repo),
        source_ref="feature", source_subpath="foo/skills/cool",
    )
    assert res["source_ref"] == "feature/foo"
    assert res["source_subpath"] == "skills/cool"
    assert (Path(res["path"]) / "SKILL.md").is_file()


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
        assert {
            "list", "show", "rescan", "link", "unlink",
            "hubs", "add", "import-git", "refresh-imports", "doctor",
        } <= cmd_names
        route_paths = {r["path"] for r in host.api.routes}
        assert "/skills" in route_paths
        assert "/hubs" in route_paths
        assert "/resolve" in route_paths
        assert "/import" in route_paths
        assert "/rescan" in route_paths
    finally:
        plugin.on_unload()
        assert plugin._worker is None
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

        hubs = client.get("/api/plugins/skills/hubs")
        assert hubs.status_code == 200
        assert any(h["id"] == "openai-skills" for h in hubs.json()["hubs"])

        wr = client.get("/api/plugins/skills/workspaces/proj")
        assert wr.status_code == 200
        assert [s["name"] for s in wr.json()["user_skills"]] == ["good"]

        ur = client.post(
            "/api/plugins/skills/usage",
            json={"skill_name": "good", "workspace": "proj", "total_tokens": 9},
        )
        assert ur.status_code == 200
        assert ur.json()["ok"] is True
        ul = client.get("/api/plugins/skills/usage?workspace=proj&skill=good")
        assert ul.status_code == 200
        assert ul.json()["usage"][0]["total_tokens"] == 9

        lr = client.post(
            "/api/plugins/skills/link",
            json={
                "workspace": "proj",
                "target_path": str(ws / "skills" / "good"),
                "alias": "linked-good",
                "mode": "reference",
            },
        )
        assert lr.status_code == 200
        assert lr.json()["ok"] is True
        link = skills_cache.get_skill_link(cfg_home / "runtime" / "relaydeck.db", "proj", "linked-good")
        assert link is not None
        assert link["review_status"] == "linked"
    finally:
        for entry in registry.all():
            try:
                entry.instance.on_unload()
            except Exception:
                pass


def test_catalog_and_deploy_routes(tmp_path, monkeypatch):
    """Centralize a workspace skill via /catalog, then inject it into another
    workspace via /deploy, and remove it via /unlink."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from fastapi.testclient import TestClient

    from relaydeck.plugin import PluginContext, get_registry
    from relaydeck.transports.api import create_app

    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    open_db(str(cfg_home / "runtime" / "relaydeck.db")).close()
    ws = _register_ws(cfg_home, "proj", plugins=[])
    _register_ws(cfg_home, "other", plugins=[])
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
    db = cfg_home / "runtime" / "relaydeck.db"
    try:
        cr = client.post(
            "/api/plugins/skills/catalog",
            json={"path": str(ws / "skills" / "good"), "alias": "good"},
        )
        assert cr.status_code == 200 and cr.json()["ok"] is True
        assert skills_cache.get_skill_link(db, manager.CATALOG_WORKSPACE, "good") is not None

        dr = client.post(
            "/api/plugins/skills/deploy",
            json={"alias": "good", "workspace": "other"},
        )
        assert dr.status_code == 200 and dr.json()["ok"] is True
        dest = cfg_home / "workspaces" / "other" / "skills" / "good"
        assert dest.is_symlink()

        # Per-skill detail: metadata, SKILL.md body, and where it is injected.
        cd = client.get("/api/plugins/skills/catalog/good")
        assert cd.status_code == 200
        body = cd.json()
        assert body["alias"] == "good"
        assert body["meta"]["valid"] is True
        assert "body" in body["body_preview"]
        assert [l["workspace"] for l in body["deployed"]] == ["other"]

        bad = client.post("/api/plugins/skills/deploy", json={"alias": "good"})
        assert bad.json()["ok"] is False

        un = client.post(
            "/api/plugins/skills/unlink",
            json={"workspace": "other", "alias": "good"},
        )
        assert un.status_code == 200 and un.json()["ok"] is True
        assert not dest.exists()
    finally:
        for entry in registry.all():
            try:
                entry.instance.on_unload()
            except Exception:
                pass


def test_source_parser_handles_github_git_and_npx():
    from plugins.skills import source_parser

    git = source_parser.parse("https://github.com/mattpocock/skills.git")
    assert git.kind == "git"
    assert git.repo_url == "https://github.com/mattpocock/skills.git"

    tree = source_parser.parse(
        "https://github.com/mattpocock/skills/tree/main/skills/engineering/grill-with-docs"
    )
    assert tree.source_ref == "main"
    assert tree.source_subpath == "skills/engineering/grill-with-docs"

    blob = source_parser.parse_github_url(
        "https://github.com/openclaw/openclaw/blob/main/skills/apple-reminders/SKILL.md"
    )
    assert blob["source_ref"] == "main"
    assert blob["source_subpath"] == "skills/apple-reminders"

    npx = source_parser.parse(
        "npx skills add https://github.com/mattpocock/skills --skill grill-me"
    )
    assert npx.kind == "git"
    assert npx.skill_filter == "grill-me"

    npm = source_parser.parse("npm install -g @juliusbrussee/caveman-code")
    assert npm.kind == "npm"
    assert npm.npm_package == "@juliusbrussee/caveman-code"


def test_discover_skills_in_tree_finds_multiple(tmp_path):
    root = tmp_path / "repo" / "skills" / "engineering"
    for name in ("alpha", "beta"):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: skill {name}\n---\n\nbody"
        )
    from plugins.skills.imports import discover_skills_in_tree

    skills = discover_skills_in_tree(root)
    assert {s["name"] for s in skills} == {"alpha", "beta"}


def test_resolve_import_source_local_multi(tmp_path):
    config_home = tmp_path / ".relaydeck"
    root = tmp_path / "bundle"
    for name in ("one", "two"):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n\nx"
        )
    from plugins.skills.imports import resolve_import_source

    resolved = resolve_import_source(config_home, str(root))
    assert len(resolved["skills"]) == 2
    assert resolved["source"]["kind"] == "local"


def test_import_resolved_skills_batch(tmp_path):
    config_home = tmp_path / ".relaydeck"
    db = _db(tmp_path)
    _register_ws(config_home, "proj", plugins=[])
    root = tmp_path / "bundle"
    for name in ("alpha", "beta"):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n\nx"
        )
    from plugins.skills.imports import import_resolved_skills, resolve_import_source

    resolved = resolve_import_source(config_home, str(root))
    result = import_resolved_skills(
        config_home,
        db,
        workspace="proj",
        resolved=resolved,
        selections=[
            {"subpath": "alpha"},
            {"subpath": "beta"},
        ],
        mode="copy",
    )
    assert len(result["imports"]) == 2
    assert (config_home / "workspaces" / "proj" / "skills" / "alpha" / "SKILL.md").is_file()
    assert (config_home / "workspaces" / "proj" / "skills" / "beta" / "SKILL.md").is_file()


def test_cli_list(tmp_path, monkeypatch):
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
        bad_out = runner.invoke(group, ["list", "--invalid"])
        assert bad_out.exit_code == 0
        assert "bad" in bad_out.output
    finally:
        plugin.on_unload()
        host.workers.teardown()
