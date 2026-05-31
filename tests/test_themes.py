"""
Theme engine tests — registry, resolver, appearance, API, CLI, skill.

Real SQLite + real FastAPI TestClient + real Click runner, isolated tmp
config home (no mocks at I/O boundaries).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from relaydeck import preferences as prefs
from relaydeck import themes

# ── registry ────────────────────────────────────────────────────────


def test_builtins_present_and_resolve(tmp_path):
    names = {t.name for t in themes.list_themes(config_home=tmp_path)}
    assert {"base", "cyan", "amber", "violet", "green", "mono"} <= names
    # base is the empty theme (== :root).
    assert themes.resolve_theme("base", config_home=tmp_path) == {}
    # amber extends base and overrides the accent set.
    amber = themes.resolve_theme("amber", config_home=tmp_path)
    assert amber["acc"] == "#fbbf24"
    assert "acc-text" in amber


def test_full_palette_builtins(tmp_path):
    # Gruvbox + Daylight are full-palette (recolor surfaces + text + accent),
    # not just accent swaps.
    names = {t.name for t in themes.list_themes(config_home=tmp_path)}
    assert {"gruvbox-dark", "daylight"} <= names
    gb = themes.resolve_theme("gruvbox-dark", config_home=tmp_path)
    assert gb["bg-0"] == "#1d2021" and gb["t-1"] == "#ebdbb2" and gb["acc"] == "#fe8019"
    day = themes.resolve_theme("daylight", config_home=tmp_path)
    # Light theme: light canvas, dark text, white on-accent text.
    assert day["bg-0"] == "#eceef1" and day["t-1"] == "#1a1d23"
    assert day["acc-text"] == "#ffffff"


def test_all_builtins_use_valid_tokens():
    # Guard against a typo'd token name in BUILTIN_THEMES (builtins bypass
    # save_theme's validation).
    for t in themes.BUILTIN_THEMES.values():
        themes.validate_tokens(t.tokens)


def test_save_load_resolve_extends(tmp_path):
    t = themes.Theme(name="prod", display_name="Prod", extends="amber",
                     tokens={"bg-0": "#0a0500", "acc": "#ff8800"})
    path = themes.save_theme(t, config_home=tmp_path)
    assert path.exists()
    # mode 0600
    assert (path.stat().st_mode & 0o777) == 0o600
    r = themes.resolve_theme("prod", config_home=tmp_path)
    assert r["acc"] == "#ff8800"        # own override wins
    assert r["bg-0"] == "#0a0500"
    assert r["acc-d"] == "#f59e0b"      # inherited from amber


def test_user_file_shadows_builtin(tmp_path):
    # A user theme of a builtin name shadows it; deleting reverts.
    themes.save_theme(themes.Theme(name="amber", extends="base",
                                   tokens={"acc": "#000001"}), config_home=tmp_path)
    assert themes.resolve_theme("amber", config_home=tmp_path)["acc"] == "#000001"
    assert themes.delete_theme("amber", config_home=tmp_path) is True
    # builtin amber restored
    assert themes.resolve_theme("amber", config_home=tmp_path)["acc"] == "#fbbf24"


def test_delete_pure_builtin_returns_false(tmp_path):
    assert themes.delete_theme("base", config_home=tmp_path) is False
    assert themes.is_builtin("base") is True


def test_unknown_token_rejected(tmp_path):
    with pytest.raises(ValueError):
        themes.save_theme(themes.Theme(name="bad", tokens={"nope": "x"}),
                          config_home=tmp_path)


def test_token_value_caps(tmp_path):
    with pytest.raises(ValueError):
        themes.validate_tokens({"acc": "x" * 999})
    with pytest.raises(ValueError):
        themes.validate_tokens({"acc": "a\nb"})


def test_extends_cycle_rejected(tmp_path):
    with pytest.raises(ValueError):
        themes.save_theme(themes.Theme(name="a", extends="a"), config_home=tmp_path)
    # two-step cycle
    themes.save_theme(themes.Theme(name="x", extends="base"), config_home=tmp_path)
    themes.save_theme(themes.Theme(name="y", extends="x"), config_home=tmp_path)
    with pytest.raises(ValueError):
        themes.save_theme(themes.Theme(name="x", extends="y"), config_home=tmp_path)


def test_resolve_missing_is_empty(tmp_path):
    assert themes.resolve_theme("ghost", config_home=tmp_path) == {}


def test_sanitize_name_path_traversal(tmp_path):
    t = themes.Theme(name="../../etc/passwd", tokens={"acc": "#111111"})
    path = themes.save_theme(t, config_home=tmp_path)
    # File lands under themes dir, name flattened.
    assert path.parent == tmp_path / "themes"
    assert "/" not in path.name.replace(".yaml", "")


def test_config_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(tmp_path))
    themes.save_theme(themes.Theme(name="envy", tokens={"acc": "#222222"}))
    assert (tmp_path / "themes" / "envy.yaml").exists()


# ── appearance resolution ───────────────────────────────────────────


def test_appearance_global_default(tmp_path):
    ap = prefs.resolve_appearance(tmp_path)
    assert ap["theme"] == "base"
    assert ap["density"] == "regular"
    assert ap["glow"] == "on"
    assert ap["dashboard"] is None
    assert ap["scope"] == "global"


def test_appearance_per_workspace_override(tmp_path):
    prefs.set_appearance(tmp_path, {"theme": "mono"})
    prefs.set_appearance(tmp_path, {"theme": "amber", "density": "compact"}, workspace="prod")
    assert prefs.resolve_appearance(tmp_path)["theme"] == "mono"
    prod = prefs.resolve_appearance(tmp_path, "prod")
    assert prod["theme"] == "amber" and prod["density"] == "compact"
    assert prod["scope"] == "workspace"
    # An unconfigured workspace inherits the global default.
    dev = prefs.resolve_appearance(tmp_path, "dev")
    assert dev["theme"] == "mono" and dev["scope"] == "global"


def test_appearance_clear_falls_back(tmp_path):
    prefs.set_appearance(tmp_path, {"theme": "mono"})
    prefs.set_appearance(tmp_path, {"theme": "amber", "density": "compact"}, workspace="prod")
    # Clearing the workspace theme falls back to global; density stays.
    prefs.set_appearance(tmp_path, {"theme": None}, workspace="prod")
    prod = prefs.resolve_appearance(tmp_path, "prod")
    assert prod["theme"] == "mono"
    assert prod["density"] == "compact"


def test_clear_appearance_theme(tmp_path):
    # Global + two workspaces reference 'neon'; one references something else.
    prefs.set_appearance(tmp_path, {"theme": "neon"})
    prefs.set_appearance(tmp_path, {"theme": "neon", "density": "compact"}, workspace="prod")
    prefs.set_appearance(tmp_path, {"theme": "amber"}, workspace="dev")
    cleared = prefs.clear_appearance_theme(tmp_path, "neon")
    assert set(cleared) == {"global", "prod"}
    # Global fell back to package default; prod fell back to global, density kept.
    assert prefs.resolve_appearance(tmp_path)["theme"] == "base"
    prod = prefs.resolve_appearance(tmp_path, "prod")
    assert prod["theme"] == "base" and prod["density"] == "compact"
    # dev untouched.
    assert prefs.resolve_appearance(tmp_path, "dev")["theme"] == "amber"
    # No-op when nothing references the name.
    assert prefs.clear_appearance_theme(tmp_path, "ghost") == []


def test_appearance_dashboard_layout_roundtrip(tmp_path):
    layout = [{"id": "w-fleet", "key": "fleet", "x": 0, "y": 0, "w": 8, "h": 3}]
    prefs.set_appearance(tmp_path, {"dashboard": layout}, workspace="prod")
    assert prefs.resolve_appearance(tmp_path, "prod")["dashboard"] == layout
    # Removing all overrides drops the workspace entry entirely.
    prefs.set_appearance(tmp_path, {"dashboard": None}, workspace="prod")
    assert prefs.resolve_appearance(tmp_path, "prod")["dashboard"] is None


def test_appearance_ignores_unknown_keys(tmp_path):
    prefs.set_appearance(tmp_path, {"theme": "amber", "evil": "x"})
    raw = prefs.read_appearance(tmp_path)
    assert "evil" not in raw


# ── HTTP API ────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    from relaydeck.transports.api import create_app
    return TestClient(create_app(cfg_home)), cfg_home


def test_api_contract(client):
    c, _ = client
    r = c.get("/api/themes/contract")
    assert r.status_code == 200
    cats = {x["name"] for x in r.json()["categories"]}
    assert {"Surfaces", "Accent", "Status", "Type"} <= cats


def test_api_list_and_get(client):
    c, _ = client
    names = {t["name"] for t in c.get("/api/themes").json()}
    assert "base" in names and "amber" in names
    amber = c.get("/api/themes/amber").json()
    assert amber["builtin"] is True
    assert amber["resolved"]["acc"] == "#fbbf24"


def test_api_create_update_delete(client):
    c, cfg_home = client
    r = c.put("/api/themes/neon", json={"extends": "cyan", "tokens": {"acc": "#39ff14"},
                                        "display_name": "Neon"})
    assert r.status_code == 200
    assert r.json()["resolved"]["acc"] == "#39ff14"
    assert (cfg_home / "themes" / "neon.yaml").exists()
    r = c.delete("/api/themes/neon")
    assert r.status_code == 200 and r.json()["deleted"] is True


def test_api_delete_active_theme_clears_appearance(client):
    """Deleting a theme that's the active global/workspace theme clears
    the dangling ref so the scope falls back."""
    c, cfg_home = client
    c.put("/api/themes/neon", json={"extends": "cyan", "tokens": {"acc": "#39ff14"}})
    c.put("/api/appearance", json={"theme": "neon"})
    c.put("/api/appearance?workspace=prod", json={"theme": "neon"})
    r = c.delete("/api/themes/neon")
    assert r.status_code == 200
    assert set(r.json()["cleared"]) == {"global", "prod"}
    # Both scopes fell back; no dangling 'neon'.
    assert c.get("/api/appearance").json()["resolved"]["theme"] == "base"
    assert c.get("/api/appearance?workspace=prod").json()["resolved"]["theme"] == "base"


def test_api_delete_shadow_keeps_appearance_ref(client):
    """Deleting a user file that *shadows* a builtin reverts to the
    builtin — the name still resolves, so the appearance ref is kept."""
    c, _ = client
    # Shadow the builtin 'amber', point appearance at it, then delete.
    c.put("/api/themes/amber", json={"extends": "base", "tokens": {"acc": "#000001"}})
    c.put("/api/appearance", json={"theme": "amber"})
    r = c.delete("/api/themes/amber")
    assert r.status_code == 200 and r.json()["cleared"] == []
    # Still 'amber' (now the builtin), not fallen back.
    ap = c.get("/api/appearance").json()["resolved"]
    assert ap["theme"] == "amber"
    assert c.get("/api/themes/amber").json()["resolved"]["acc"] == "#fbbf24"


def test_api_create_cycle_400(client):
    c, _ = client
    assert c.put("/api/themes/loopy", json={"extends": "loopy"}).status_code == 400


def test_api_create_unknown_token_400(client):
    c, _ = client
    assert c.put("/api/themes/bad", json={"tokens": {"zzz": "1"}}).status_code == 400


def test_api_delete_builtin_409(client):
    c, _ = client
    assert c.delete("/api/themes/base").status_code == 409


def test_api_appearance_roundtrip(client):
    c, _ = client
    c.put("/api/appearance", json={"theme": "amber"})
    c.put("/api/appearance?workspace=prod", json={"theme": "violet", "density": "compact"})
    assert c.get("/api/appearance").json()["resolved"]["theme"] == "amber"
    prod = c.get("/api/appearance?workspace=prod").json()["resolved"]
    assert prod["theme"] == "violet" and prod["density"] == "compact"


def test_api_appearance_notify(client):
    c, _ = client
    assert c.post("/api/appearance/notify?workspace=prod").status_code == 200


# ── CLI ─────────────────────────────────────────────────────────────


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _run(args):
    from relaydeck.transports.cli import main
    return CliRunner().invoke(main, args)


def test_cli_list_create_show(cli_home):
    assert _run(["theme", "list"]).exit_code == 0
    r = _run(["theme", "create", "ocean", "--extends", "base",
              "--set", "acc=#38bdf8", "--display-name", "Ocean"])
    assert r.exit_code == 0
    assert (cli_home / "themes" / "ocean.yaml").exists()
    show = _run(["theme", "show", "ocean", "--resolved"])
    assert show.exit_code == 0 and "#38bdf8" in show.output


def test_cli_create_bad_token_exits_1(cli_home):
    r = _run(["theme", "create", "bad", "--set", "nonsense=x"])
    assert r.exit_code == 1
    assert "unknown token" in r.output


def test_cli_edit_and_rm(cli_home):
    _run(["theme", "create", "ocean", "--set", "acc=#38bdf8"])
    assert _run(["theme", "edit", "ocean", "--set", "bg-0=#001018"]).exit_code == 0
    assert themes.resolve_theme("ocean", config_home=cli_home)["bg-0"] == "#001018"
    assert _run(["theme", "rm", "ocean"]).exit_code == 0
    assert not (cli_home / "themes" / "ocean.yaml").exists()


def test_cli_rm_builtin_refuses(cli_home):
    r = _run(["theme", "rm", "base"])
    assert r.exit_code == 1


def test_cli_set_and_appearance(cli_home):
    _run(["theme", "create", "ocean", "--set", "acc=#38bdf8"])
    assert _run(["theme", "set", "ocean", "-w", "prod"]).exit_code == 0
    ap = prefs.resolve_appearance(cli_home, "prod")
    assert ap["theme"] == "ocean" and ap["scope"] == "workspace"
    r = _run(["theme", "appearance", "-w", "prod"])
    assert r.exit_code == 0 and "ocean" in r.output


def test_cli_export_import(cli_home, tmp_path):
    _run(["theme", "create", "ocean", "--set", "acc=#38bdf8"])
    out = tmp_path / "ocean.yaml"
    assert _run(["theme", "export", "ocean", "--out", str(out)]).exit_code == 0
    assert out.exists()
    assert _run(["theme", "import", str(out), "--name", "ocean2"]).exit_code == 0
    assert themes.resolve_theme("ocean2", config_home=cli_home)["acc"] == "#38bdf8"


def test_cli_import_rejects_unknown_token(cli_home, tmp_path):
    """A shared theme with a typo'd token name fails loudly on import
    instead of silently dropping it."""
    import yaml
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(
        {"name": "shared", "tokens": {"not-a-token": "x", "acc": "#fff"}}))
    r = _run(["theme", "import", str(bad)])
    assert r.exit_code == 1
    assert "unknown token" in r.output
    # Nothing was saved.
    assert themes.get_theme("shared", config_home=cli_home) is None


def test_cli_rm_active_theme_clears_appearance(cli_home):
    """`relaydeck theme rm` of an active theme clears the appearance ref."""
    _run(["theme", "create", "ocean", "--set", "acc=#38bdf8"])
    _run(["theme", "set", "ocean"])
    assert prefs.resolve_appearance(cli_home)["theme"] == "ocean"
    assert _run(["theme", "rm", "ocean"]).exit_code == 0
    assert prefs.resolve_appearance(cli_home)["theme"] == "base"


# ── bundled skill ───────────────────────────────────────────────────


def test_theme_skill_validates():
    from relaydeck import skills
    plug_dir = Path(__file__).resolve().parent.parent / "plugins" / "theme"
    ok, errors, _ = skills.validate_skill_dir(plug_dir)
    assert ok, errors
    fm, _body = skills.parse_skill_md((plug_dir / "SKILL.md").read_text())
    assert fm["name"] == "relaydeck-theme"
    assert fm.get("description")


def test_theme_plugin_targets_all_workspaces():
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "plugins" / "theme" / "plugin.py"
    spec = importlib.util.spec_from_file_location("theme_plugin_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.PLUGIN.skill_target_workspaces(["a", "b"]) == ["a", "b"]


def test_theme_skill_materializes_into_workspaces(tmp_path):
    """The theme plugin ships its skill to every workspace via the
    generic [plugin.skills] manager (mirrors test_skills_plugin harness)."""
    from types import SimpleNamespace

    from relaydeck.config import register_workspace
    from relaydeck.db import open_db
    from plugins.skills import manager

    config_home = tmp_path / ".relaydeck"
    db = config_home / "runtime" / "relaydeck.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    open_db(str(db)).close()
    for ws in ("alpha", "beta"):
        src = config_home / "src" / ws
        src.mkdir(parents=True, exist_ok=True)
        register_workspace(config_home, ws, src, [])

    plugin_dir = Path(__file__).resolve().parent.parent / "plugins" / "theme"

    class _Theme:
        workspace_scoped = False
        def skill_target_workspaces(self, all_ws):
            return list(all_ws)

    entry = SimpleNamespace(
        name="theme", instance=_Theme(),
        manifest=SimpleNamespace(skills={"relaydeck-theme": "SKILL.md"}), path=plugin_dir,
    )

    class _Reg:
        def all(self):
            return [entry]

    manager.sync_all(config_home, registry=_Reg())
    for ws in ("alpha", "beta"):
        skill = config_home / "workspaces" / ws / "runtime" / "skills" / "relaydeck-theme" / "SKILL.md"
        assert skill.exists()
