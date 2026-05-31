"""
relaydeck/skills.py — discovery, frontmatter parsing, validation, content
hashing, and plugin-contributed materialization (ownership sidecars,
idempotency, collision safety).

Real filesystem under tmp_path; no mocks. The materialization tests are
the contract the messaging/telegram migration relies on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relaydeck import skills


# ── helpers ──────────────────────────────────────────────────────────


def _write_skill(root: Path, name: str, *, fm_name=None, desc="does a thing",
                 body="Body text.") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm_name = name if fm_name is None else fm_name
    front = ["---", f"name: {fm_name}"]
    if desc is not None:
        front.append(f"description: {desc}")
    front.append("---")
    (d / "SKILL.md").write_text("\n".join(front) + f"\n\n{body}\n")
    return d


def _register_ws(config_home: Path, name: str, plugins=None) -> Path:
    from relaydeck.config import register_workspace
    ws_dir = config_home / "src" / name
    ws_dir.mkdir(parents=True, exist_ok=True)
    register_workspace(config_home, name, ws_dir, plugins or [])
    return config_home / "workspaces" / name


# ── parsing ──────────────────────────────────────────────────────────


def test_parse_frontmatter_and_body():
    fm, body = skills.parse_skill_md(
        "---\nname: foo\ndescription: bar\nallowed-tools: [read, write]\n---\n\nhello\n"
    )
    assert fm["name"] == "foo"
    assert fm["description"] == "bar"
    assert fm["allowed-tools"] == ["read", "write"]
    assert body.strip() == "hello"


def test_parse_no_frontmatter():
    fm, body = skills.parse_skill_md("# just markdown\n")
    assert fm == {}
    assert body == "# just markdown\n"


def test_parse_malformed_frontmatter_is_safe():
    fm, body = skills.parse_skill_md("---\nname: : : bad\n  - nope\n---\nx")
    # Either it parses to a dict or falls back to {}, never raises.
    assert isinstance(fm, dict)


def test_read_metadata_merges_openai_yaml(tmp_path):
    d = _write_skill(tmp_path, "demo")
    (d / "agents").mkdir()
    (d / "agents" / "openai.yaml").write_text("name: Demo Skill\nicon: x\n")
    meta = skills.read_skill_metadata(d)
    assert meta["name"] == "demo"            # frontmatter wins for name
    assert meta["display_name"] == "Demo Skill"
    assert meta["openai"]["icon"] == "x"


# ── validation ───────────────────────────────────────────────────────


def test_validate_ok(tmp_path):
    d = _write_skill(tmp_path, "good")
    valid, errors, warnings = skills.validate_skill_dir(d)
    assert valid is True
    assert errors == []


def test_validate_missing_skill_md(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    valid, errors, _ = skills.validate_skill_dir(d)
    assert valid is False
    assert "missing SKILL.md" in errors


def test_validate_missing_required_fields(tmp_path):
    d = tmp_path / "nameless"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nfoo: bar\n---\nbody")
    valid, errors, _ = skills.validate_skill_dir(d)
    assert valid is False
    assert any("name" in e for e in errors)
    assert any("description" in e for e in errors)


def test_validate_name_mismatch_is_warning_not_error(tmp_path):
    d = _write_skill(tmp_path, "dirname", fm_name="other-name")
    valid, errors, warnings = skills.validate_skill_dir(d)
    assert valid is True
    assert any("differs from directory" in w for w in warnings)


def test_validate_large_skill_warns(tmp_path):
    d = _write_skill(tmp_path, "big", body="x" * (skills._LARGE_SKILL_BYTES + 10))
    valid, _errors, warnings = skills.validate_skill_dir(d)
    assert valid is True
    assert any("large" in w for w in warnings)


def test_validate_allowed_tools_warns(tmp_path):
    d = tmp_path / "tooly"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: tooly\ndescription: d\nallowed-tools: [bash]\n---\nbody"
    )
    _valid, _errors, warnings = skills.validate_skill_dir(d)
    assert any("allowed-tools" in w for w in warnings)


# ── hashing + ids ────────────────────────────────────────────────────


def test_content_hash_changes_with_content(tmp_path):
    d = _write_skill(tmp_path, "h1", body="one")
    h1 = skills.content_hash(d)
    (d / "SKILL.md").write_text("---\nname: h1\ndescription: d\n---\ntwo")
    h2 = skills.content_hash(d)
    assert h1 != h2


def test_content_hash_ignores_sidecar(tmp_path):
    d = _write_skill(tmp_path, "h2")
    before = skills.content_hash(d)
    (d / skills.SIDECAR_NAME).write_text(json.dumps({"owner_plugin": "x"}))
    assert skills.content_hash(d) == before


def test_skill_id_stable_and_scope_sensitive():
    a = skills._skill_id("/a/b", skills.SOURCE_WORKSPACE, "ws")
    assert a == skills._skill_id("/a/b", skills.SOURCE_WORKSPACE, "ws")
    assert a != skills._skill_id("/a/b", skills.SOURCE_RUNTIME_PLUGIN, "ws")
    assert a != skills._skill_id("/a/b", skills.SOURCE_WORKSPACE, "other")


# ── discovery ────────────────────────────────────────────────────────


def test_discover_workspace_user_and_runtime(tmp_path):
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    _write_skill(ws / "skills", "user-skill")
    _write_skill(ws / "runtime" / "skills", "plugin-skill")

    refs = skills.discover_workspace_skills(config_home, "proj")
    by_name = {r.name: r for r in refs}
    assert by_name["user-skill"].source_type == skills.SOURCE_WORKSPACE
    assert by_name["plugin-skill"].source_type == skills.SOURCE_RUNTIME_PLUGIN
    assert all(r.injectable for r in refs)


def test_discover_skips_hidden_and_non_skill_dirs(tmp_path):
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    (ws / "skills" / ".hidden").mkdir(parents=True)
    (ws / "skills" / "no-md").mkdir(parents=True)  # dir without SKILL.md
    _write_skill(ws / "skills", "real")
    names = [r.name for r in skills.discover_workspace_skills(config_home, "proj")]
    assert names == ["real"]


def test_discover_codex_read_only(tmp_path, monkeypatch):
    codex = tmp_path / ".codex" / "skills"
    _write_skill(codex, "cx-user")
    _write_skill(codex / ".system", "cx-sys")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    refs = skills.discover_codex_skills()
    by_name = {r.name: r for r in refs}
    assert by_name["cx-user"].source_type == skills.SOURCE_CODEX_USER
    assert by_name["cx-sys"].source_type == skills.SOURCE_CODEX_SYSTEM
    assert not by_name["cx-user"].injectable  # codex skills are read-only


def test_discover_claude_read_only(tmp_path, monkeypatch):
    claude = tmp_path / ".claude" / "skills"
    _write_skill(claude, "frontend-slides")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    refs = skills.discover_claude_skills()
    by_name = {r.name: r for r in refs}
    assert by_name["frontend-slides"].source_type == skills.SOURCE_CLAUDE_USER
    assert not by_name["frontend-slides"].injectable  # claude-code loads it itself


def test_discover_claude_project_level(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".empty"))
    repo = tmp_path / "repo"
    _write_skill(repo / ".claude" / "skills", "proj-skill")
    refs = skills.discover_claude_skills([("myws", repo)])
    ref = next(r for r in refs if r.name == "proj-skill")
    assert ref.source_type == skills.SOURCE_CLAUDE_PROJECT
    assert ref.workspace == "myws"


def test_discover_all_includes_claude(tmp_path, monkeypatch):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj")
    claude = tmp_path / ".claude" / "skills"
    _write_skill(claude, "cc-skill")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".nocodex"))
    refs = skills.discover_all_skills(config_home)
    assert any(r.name == "cc-skill" and r.source_type == skills.SOURCE_CLAUDE_USER for r in refs)
    # include_claude=False omits them
    refs2 = skills.discover_all_skills(config_home, include_claude=False)
    assert not any(r.source_type == skills.SOURCE_CLAUDE_USER for r in refs2)


def test_discover_all_spans_workspaces_and_codex(tmp_path, monkeypatch):
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    _write_skill(ws / "skills", "ws-skill")
    codex = tmp_path / ".codex" / "skills"
    _write_skill(codex, "cx")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    refs = skills.discover_all_skills(config_home, include_codex=True)
    names = {r.name for r in refs}
    assert {"ws-skill", "cx"} <= names


def test_symlink_target_recorded(tmp_path):
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    target = _write_skill(tmp_path / "external", "ext")
    link = ws / "skills" / "linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    refs = skills.discover_workspace_skills(config_home, "proj")
    ref = next(r for r in refs if r.name in ("linked", "ext"))
    assert ref.symlink_target is not None
    assert ref.valid is True


# ── injection helpers ────────────────────────────────────────────────


def test_injection_dirs_skips_invalid(tmp_path):
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    _write_skill(ws / "skills", "valid-one")
    bad = ws / "skills" / "bad-one"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("---\nfoo: bar\n---\nno name/desc")
    user, runtime = skills.injection_dirs(config_home, "proj")
    assert [p.name for p in user] == ["valid-one"]
    assert runtime == []


# ── materialization ──────────────────────────────────────────────────


def test_materialize_writes_skill_and_sidecar(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj")
    res = skills.materialize_skill(
        config_home, "proj", "messaging", "relaydeck-cli", "SKILL BODY",
        source_path="/src/SKILL.md",
    )
    assert res == "written"
    d = config_home / "workspaces" / "proj" / "runtime" / "skills" / "relaydeck-cli"
    assert (d / "SKILL.md").read_text() == "SKILL BODY"
    sc = json.loads((d / skills.SIDECAR_NAME).read_text())
    assert sc["owner_plugin"] == "messaging"
    assert sc["managed_by"] == skills.MANAGED_BY
    assert sc["source_path"] == "/src/SKILL.md"


def test_materialize_idempotent(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj")
    skills.materialize_skill(config_home, "proj", "messaging", "relaydeck-cli", "B")
    res = skills.materialize_skill(config_home, "proj", "messaging", "relaydeck-cli", "B")
    assert res == "unchanged"


def test_materialize_rewrites_on_content_change(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj")
    skills.materialize_skill(config_home, "proj", "messaging", "relaydeck-cli", "B1")
    res = skills.materialize_skill(config_home, "proj", "messaging", "relaydeck-cli", "B2")
    assert res == "written"
    d = config_home / "workspaces" / "proj" / "runtime" / "skills" / "relaydeck-cli"
    assert (d / "SKILL.md").read_text() == "B2"


def test_materialize_refuses_cross_owner(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj")
    skills.materialize_skill(config_home, "proj", "messaging", "relaydeck-cli", "A")
    res = skills.materialize_skill(config_home, "proj", "telegram", "relaydeck-cli", "B")
    assert res == "conflict"
    d = config_home / "workspaces" / "proj" / "runtime" / "skills" / "relaydeck-cli"
    assert (d / "SKILL.md").read_text() == "A"  # original owner preserved


def test_remove_skill_only_when_owner_matches(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "proj")
    skills.materialize_skill(config_home, "proj", "messaging", "relaydeck-cli", "A")
    assert skills.remove_skill(config_home, "proj", "telegram", "relaydeck-cli") is False
    assert skills.remove_skill(config_home, "proj", "messaging", "relaydeck-cli") is True
    d = config_home / "workspaces" / "proj" / "runtime" / "skills" / "relaydeck-cli"
    assert not d.exists()


def test_remove_skill_leaves_unmanaged_alone(tmp_path):
    """A hand-written runtime skill (no sidecar) is never removed."""
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    _write_skill(ws / "runtime" / "skills", "hand-rolled")
    assert skills.remove_skill(config_home, "proj", "messaging", "hand-rolled") is False
    assert (ws / "runtime" / "skills" / "hand-rolled" / "SKILL.md").exists()


def test_sync_materializes_targets_and_removes_orphans(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "a")
    _register_ws(config_home, "b")
    all_ws = ["a", "b"]
    # Materialize into 'a' only.
    rep = skills.sync_plugin_skills(
        config_home, "messaging", {"relaydeck-cli": "BODY"}, ["a"], all_ws
    )
    assert rep["written"] == 1
    a_dir = config_home / "workspaces" / "a" / "runtime" / "skills" / "relaydeck-cli"
    assert a_dir.exists()

    # Now target only 'b' → 'a' becomes an orphan and is removed.
    rep2 = skills.sync_plugin_skills(
        config_home, "messaging", {"relaydeck-cli": "BODY"}, ["b"], all_ws
    )
    assert rep2["written"] == 1
    assert rep2["removed"] == 1
    assert not a_dir.exists()
    b_dir = config_home / "workspaces" / "b" / "runtime" / "skills" / "relaydeck-cli"
    assert b_dir.exists()


def test_sync_retires_removed_skill_name(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "a")
    skills.sync_plugin_skills(config_home, "p", {"old": "x", "new": "y"}, ["a"], ["a"])
    # Drop 'old' from the manifest → it should be removed on next sync.
    rep = skills.sync_plugin_skills(config_home, "p", {"new": "y"}, ["a"], ["a"])
    assert rep["removed"] == 1
    base = config_home / "workspaces" / "a" / "runtime" / "skills"
    assert not (base / "old").exists()
    assert (base / "new").exists()


def test_remove_all_plugin_skills(tmp_path):
    config_home = tmp_path / ".relaydeck"
    _register_ws(config_home, "a")
    _register_ws(config_home, "b")
    skills.sync_plugin_skills(config_home, "p", {"s": "x"}, ["a", "b"], ["a", "b"])
    count = skills.remove_all_plugin_skills(config_home, "p", ["a", "b"])
    assert count == 2


def test_materialized_skills_lists_only_managed(tmp_path):
    config_home = tmp_path / ".relaydeck"
    ws = _register_ws(config_home, "proj")
    skills.materialize_skill(config_home, "proj", "messaging", "managed", "x")
    _write_skill(ws / "runtime" / "skills", "unmanaged")  # no sidecar
    listed = skills.materialized_skills(config_home, "proj")
    assert [e["skill_name"] for e in listed] == ["managed"]


# ── skills_cache + skill_links (SQLite mirror) ───────────────────────


def _ref(name="s", source=skills.SOURCE_WORKSPACE, ws="proj", valid=True):
    return skills.SkillRef(
        id=skills._skill_id(f"/x/{name}", source, ws),
        name=name, description="d", source_type=source,
        path=f"/x/{name}", realpath=f"/x/{name}", valid=valid,
        workspace=ws, content_hash="h", errors=[] if valid else ["bad"],
    )


def _cache_db(tmp_path):
    from relaydeck.db import open_db
    p = tmp_path / "relaydeck.db"
    open_db(str(p)).close()
    return str(p)


def test_cache_upsert_and_get(tmp_path):
    from relaydeck import skills_cache
    db = _cache_db(tmp_path)
    ref = _ref()
    skills_cache.upsert_skill_cache(db, ref)
    got = skills_cache.get_skill_cache(db, ref.id)
    assert got is not None
    assert got["name"] == "s"
    assert got["valid"] is True
    assert got["injectable"] is True


def test_cache_replace_prunes_missing(tmp_path):
    from relaydeck import skills_cache
    db = _cache_db(tmp_path)
    a, b = _ref("a"), _ref("b")
    skills_cache.replace_skill_cache(db, [a, b])
    assert len(skills_cache.list_skills_cache(db)) == 2
    pruned = skills_cache.replace_skill_cache(db, [a])  # b disappears
    assert pruned == 1
    names = [r["name"] for r in skills_cache.list_skills_cache(db)]
    assert names == ["a"]


def test_cache_filters(tmp_path):
    from relaydeck import skills_cache
    db = _cache_db(tmp_path)
    skills_cache.replace_skill_cache(db, [
        _ref("u", skills.SOURCE_WORKSPACE, "w1"),
        _ref("p", skills.SOURCE_RUNTIME_PLUGIN, "w1"),
        _ref("c", skills.SOURCE_CODEX_USER, None, valid=False),
    ])
    assert len(skills_cache.list_skills_cache(db, workspace="w1")) == 2
    assert len(skills_cache.list_skills_cache(db, source_type=skills.SOURCE_CODEX_USER)) == 1
    assert len(skills_cache.list_skills_cache(db, valid=False)) == 1


def test_skill_links_crud(tmp_path):
    from relaydeck import skills_cache
    db = _cache_db(tmp_path)
    link = skills_cache.create_skill_link(db, "proj", "myalias", "/ext/skill", mode="symlink")
    assert link["alias"] == "myalias"
    assert skills_cache.get_skill_link(db, "proj", "myalias")["target_path"] == "/ext/skill"
    assert len(skills_cache.list_skill_links(db, "proj")) == 1
    assert skills_cache.delete_skill_link(db, "proj", "myalias") is True
    assert skills_cache.get_skill_link(db, "proj", "myalias") is None


def test_skill_links_duplicate_raises(tmp_path):
    from relaydeck import skills_cache
    db = _cache_db(tmp_path)
    skills_cache.create_skill_link(db, "proj", "a", "/t")
    with pytest.raises(ValueError, match="already exists"):
        skills_cache.create_skill_link(db, "proj", "a", "/t2")
