"""
Plugin trust_level field + load-time gate.

Implements the small-impl piece (option C: refuse to load
untrusted plugins by default). Sandboxing (A/B) is still out of scope;
this is the honest "we know which plugins we trust" layer.

Contract pinned here:
  - Manifest declares `trust_level` in plugin.toml. Valid values:
    bundled | local | signed | untrusted.
  - Empty manifest field falls back to source-derived default:
    builtin -> bundled, anything else -> local.
  - Daemon refuses to load `untrusted` plugins on startup unless
    RELAYDECK_ALLOW_UNTRUSTED_PLUGINS=1 is set.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Manifest parsing ──────────────────────────────────────────────


def test_manifest_parses_trust_level_when_present(tmp_path):
    from relaydeck.plugin_manifest import load_manifest

    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        '[plugin]\n'
        'name = "x"\n'
        'version = "0.1"\n'
        'trust_level = "signed"\n'
    )
    m = load_manifest(manifest_path)
    assert m.trust_level == "signed"


def test_manifest_trust_level_defaults_to_empty_string(tmp_path):
    """Empty == not declared. Registry derives effective level."""
    from relaydeck.plugin_manifest import load_manifest

    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text('[plugin]\nname = "x"\nversion = "0.1"\n')
    m = load_manifest(manifest_path)
    assert m.trust_level == ""


def test_manifest_rejects_unknown_trust_level(tmp_path):
    """Typo in plugin.toml shouldn't silently produce an ambiguous
    state -- ManifestError fails fast at load time."""
    from relaydeck.plugin_manifest import ManifestError, load_manifest

    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        '[plugin]\n'
        'name = "x"\n'
        'version = "0.1"\n'
        'trust_level = "kinda-ok"\n'
    )
    with pytest.raises(ManifestError, match="trust_level"):
        load_manifest(manifest_path)


# ── effective_trust_level resolution ──────────────────────────────


def _make_entry(source: str, manifest_trust: str = ""):
    from relaydeck.plugin import PluginEntry
    from relaydeck.plugin_manifest import PluginManifest

    manifest = PluginManifest(
        name="x", version="0.1", trust_level=manifest_trust,
    )
    return PluginEntry(
        name="x", category="tool", version="0.1",
        instance=object(),  # type: ignore[arg-type]
        source=source, path=Path("/tmp/x"), manifest=manifest,
    )


def test_effective_trust_level_builtin_defaults_to_bundled():
    from relaydeck.plugin import effective_trust_level
    assert effective_trust_level(_make_entry("builtin")) == "bundled"


def test_effective_trust_level_user_defaults_to_local():
    from relaydeck.plugin import effective_trust_level
    assert effective_trust_level(_make_entry("user")) == "local"


def test_effective_trust_level_workspace_defaults_to_local():
    from relaydeck.plugin import effective_trust_level
    assert effective_trust_level(_make_entry("workspace:demo")) == "local"


def test_explicit_manifest_trust_level_wins_over_source_default():
    from relaydeck.plugin import effective_trust_level
    # builtin source + explicit untrusted -> untrusted (still refused).
    e = _make_entry("builtin", manifest_trust="untrusted")
    assert effective_trust_level(e) == "untrusted"


# ── Load gate ─────────────────────────────────────────────────────


def _make_untrusted_plugin(plugins_dir: Path, name: str) -> None:
    pkg = plugins_dir / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.py").write_text(
        "from relaydeck.plugin import RelaydeckPlugin\n"
        f'PLUGIN = RelaydeckPlugin()\n'
        f'PLUGIN.name = "{name}"\n'
        f'PLUGIN.category = "tool"\n'
        f'PLUGIN.version = "0.1"\n'
    )
    (pkg / "plugin.toml").write_text(
        '[plugin]\n'
        f'name = "{name}"\n'
        'version = "0.1"\n'
        'category = "tool"\n'
        'trust_level = "untrusted"\n'
    )


def test_load_all_refuses_untrusted_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", raising=False)
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    _make_untrusted_plugin(cfg / "plugins", "scary-plugin")

    import relaydeck.plugin as plug
    plug._registry = None
    reg = plug.get_registry(cfg)
    reg.load_all(plug.PluginContext(config_home=cfg))

    loaded_names = {e.name for e in reg.all()}
    assert "scary-plugin" not in loaded_names, (
        "untrusted plugin must not load with the default env"
    )
    # Discovery still records it so the dashboard can show "skipped".
    assert "scary-plugin" in reg._discovered


def test_load_all_allows_untrusted_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", "1")
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    _make_untrusted_plugin(cfg / "plugins", "scary-plugin")

    import relaydeck.plugin as plug
    plug._registry = None
    reg = plug.get_registry(cfg)
    reg.load_all(plug.PluginContext(config_home=cfg))

    loaded_names = {e.name for e in reg.all()}
    assert "scary-plugin" in loaded_names, (
        "RELAYDECK_ALLOW_UNTRUSTED_PLUGINS=1 must let untrusted plugins load"
    )
