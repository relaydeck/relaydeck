"""Recommended-bundle manifest (plugins/bundle.toml + relaydeck/bundles.py).

The default bundle is the pinned recommended set of official plugins; doctor
and the dashboard flag a missing one. These tests pin the parse + resolution
and that the shipped default bundle matches what the package actually
discovers, so the manifest can't silently drift from reality.
"""

from __future__ import annotations

from relaydeck import bundles


def test_default_and_minimal_bundles_present():
    b = bundles.load_bundles()
    assert "default" in b
    assert "minimal" in b
    assert b["default"].plugins, "default bundle must list plugins"
    # minimal is a strict subset of default.
    assert set(b["minimal"].plugins) <= set(b["default"].plugins)


def test_get_bundle_and_missing():
    default = bundles.get_bundle("default")
    assert default is not None
    # Everything present → nothing missing.
    assert bundles.missing_from_bundle(set(default.plugins), "default") == []
    # Drop one → it shows as missing.
    present = set(default.plugins) - {default.plugins[0]}
    assert bundles.missing_from_bundle(present, "default") == [default.plugins[0]]


def test_unknown_bundle_is_not_an_error():
    assert bundles.get_bundle("nope") is None
    assert bundles.missing_from_bundle(set(), "nope") == []


def test_default_bundle_matches_discovered_official_plugins(tmp_path):
    """The shipped default bundle must equal the set of official plugins the
    registry actually discovers — otherwise doctor lies about coverage."""
    import relaydeck.plugin as plug
    from relaydeck.plugin import get_registry

    plug._registry = None
    reg = get_registry(tmp_path / ".relaydeck")
    discovered = {e.name for e in reg.discover()}
    official = {e.name for e in reg.discover() if e.source == "builtin"}

    default = bundles.get_bundle("default")
    assert default is not None
    bundle_set = set(default.plugins)
    # Every bundle entry is a real, discoverable official plugin.
    assert bundle_set <= discovered, f"bundle lists unknown: {bundle_set - discovered}"
    # And every official plugin is recommended in the default bundle.
    assert official <= bundle_set, f"official not in bundle: {official - bundle_set}"
