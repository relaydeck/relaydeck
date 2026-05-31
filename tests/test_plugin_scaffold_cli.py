"""
CLI helpers for the plugin developer tiers (`relaydeck plugin new --local`
/ `--workspace`, `plugin list --mine`).

The default `plugin new` scaffolds a publishable package; `--local` /
`--workspace` scaffold a *private* plain-directory plugin in place that the
loader discovers as `source=user` / `trust=local` and that is never pushed
upstream. These tests pin both shapes + discovery so the tier behavior can't
silently regress.
"""

from __future__ import annotations

import pytest

from relaydeck.transports import cli as C


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """A temp config home wired into the CLI's `_get_config_home`."""
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    monkeypatch.setattr(C, "_get_config_home", lambda: home)
    # Discovery mutates a process-global registry; reset around each test.
    import relaydeck.plugin as plug

    monkeypatch.setattr(plug, "_registry", None)
    return home


def test_new_local_scaffolds_plain_private_dir(cfg):
    C.plugin_new.callback(name="My Local Tool", pattern="reactor", local=True, workspace=None)
    dest = cfg / "plugins" / "my-local-tool"
    assert (dest / "plugin.py").exists()
    assert (dest / "plugin.toml").exists()
    # Private = a plain dir, NOT a package.
    assert not (dest / "pyproject.toml").exists()
    assert 'name = "my-local-tool"' in (dest / "plugin.toml").read_text()
    assert "PLUGIN = " in (dest / "plugin.py").read_text()


def test_new_local_skill_writes_skill_md(cfg):
    C.plugin_new.callback(name="my skill", pattern="skill", local=True, workspace=None)
    dest = cfg / "plugins" / "my-skill"
    assert (dest / "SKILL.md").exists()
    assert (dest / "SKILL.md").read_text().startswith("---")


def test_new_local_discovered_as_user_local(cfg):
    C.plugin_new.callback(name="mine", pattern="reactor", local=True, workspace=None)
    from relaydeck.plugin import effective_trust_level, get_registry

    reg = get_registry(cfg)
    entry = next((e for e in reg.discover() if e.name == "mine"), None)
    assert entry is not None
    assert entry.source == "user"
    assert effective_trust_level(entry) == "local"
    # the `--mine` filter keeps it (source != builtin)
    assert entry.source != "builtin"


def test_new_into_workspace(cfg, tmp_path):
    from relaydeck.config import register_workspace

    ws_path = tmp_path / "proj"
    ws_path.mkdir()
    register_workspace(cfg, "proj", ws_path, [])
    C.plugin_new.callback(name="ws tool", pattern="reactor", local=False, workspace="proj")
    dest = ws_path / "plugins" / "ws-tool"
    assert (dest / "plugin.py").exists()
    assert (dest / "plugin.toml").exists()
    assert not (dest / "pyproject.toml").exists()


def test_new_local_and_workspace_mutually_exclusive(cfg):
    with pytest.raises(SystemExit):
        C.plugin_new.callback(name="x", pattern="reactor", local=True, workspace="proj")


def test_new_unknown_workspace_errors(cfg):
    with pytest.raises(SystemExit):
        C.plugin_new.callback(name="x", pattern="reactor", local=False, workspace="nope")


def test_new_default_scaffolds_package(cfg, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    C.plugin_new.callback(name="My Pkg", pattern="reactor", local=False, workspace=None)
    root = work / "relaydeck-plugin-my-pkg"
    assert (root / "pyproject.toml").exists()
    assert (root / "my_pkg" / "plugin.py").exists()
    assert (root / "my_pkg" / "plugin.toml").exists()
    assert "relaydeck.plugins" in (root / "pyproject.toml").read_text()


def test_list_mine_filters_to_custom(cfg, capsys):
    # No custom plugins yet → helpful hint, no crash.
    C.plugin_list.callback(mine=True)
    out = capsys.readouterr().out
    assert "No custom plugins" in out or "Your plugins" in out
