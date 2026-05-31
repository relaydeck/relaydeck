"""Regression tests for the plugin-ecosystem gaps.

- G2: `UIRegistrarHost.widget()` — programmatic home-widget registration,
  parity with tab/agent_tile/header_chip.
- G4: `MockHost(declared_capabilities=set())` honors an explicit empty set
  (refuses everything) instead of silently granting the broad default.
- G1 follow-up: `load_manifest` warns at load time on an unknown setting type
  instead of letting it silently vanish from the dashboard.
- G6: the package scaffold documents the local-relaydeck escape hatch so the
  "create a plugin for local use" path isn't a dead end pre-PyPI.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from relaydeck.plugin_manifest import load_manifest
from relaydeck.sdk import CapabilityNotDeclared
from relaydeck.testing import _DEFAULT_MOCK_CAPABILITIES, MockHost


# ── G2 — programmatic widget registration ────────────────────────────


def test_widget_registers_and_appears_in_manifest():
    host = MockHost()  # default caps include ui.register
    host.ui.widget("clock", "Clock", "clock.js", def_w=4, def_h=3, min_w=2, min_h=2)
    widgets = host.ui.manifest()["widgets"]
    assert widgets == [{
        "id": "clock", "title": "Clock", "module": "clock.js",
        "def_w": 4, "def_h": 3, "min_w": 2, "min_h": 2,
    }]


def test_widget_size_hints_default_to_zero():
    host = MockHost()
    host.ui.widget("mini", "Mini", "mini.js")
    w = host.ui.manifest()["widgets"][0]
    assert (w["def_w"], w["def_h"], w["min_w"], w["min_h"]) == (0, 0, 0, 0)


def test_manifest_always_includes_widgets_key():
    # The plugin.py register_ui merge does base+live for "widgets"; the live
    # side must always present the key even when empty.
    assert "widgets" in MockHost().ui.manifest()


def test_widget_requires_ui_register_capability():
    host = MockHost(declared_capabilities={"events.emit"})
    with pytest.raises(CapabilityNotDeclared):
        host.ui.widget("x", "X", "x.js")


# ── G4 — MockHost empty-capset sentinel ──────────────────────────────


def test_empty_capset_refuses_everything():
    host = MockHost(declared_capabilities=set())
    with pytest.raises(CapabilityNotDeclared):
        host.ui.tab("t", "T", "i", "m.js")


def test_default_capset_grants_the_broad_default():
    # None sentinel → broad default, so the common path still "just works".
    host = MockHost()
    host.ui.tab("t", "T", "i", "m.js")  # ui.register granted, no raise
    assert "ui.register" in _DEFAULT_MOCK_CAPABILITIES


def test_explicit_subset_is_honored_exactly():
    host = MockHost(declared_capabilities={"ui.register"})
    host.ui.tab("t", "T", "i", "m.js")  # granted
    with pytest.raises(CapabilityNotDeclared):
        host.kv.get("k")  # kv.read NOT in the subset


# ── G1 follow-up — load-time warning for unknown setting types ───────


def _manifest(tmp_path: Path, settings_body: str) -> Path:
    p = tmp_path / "plugin.toml"
    p.write_text(
        '[plugin]\nname = "demo"\nversion = "0.1.0"\n\n'
        f"[plugin.settings]\n{settings_body}\n"
    )
    return p


def test_unknown_setting_type_warns_at_load(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        load_manifest(_manifest(tmp_path, 'bad = { type = "bogus" }'))
    assert any("unknown type" in r.message and "bad" in r.message for r in caplog.records)


@pytest.mark.parametrize("good_type", ["text", "string", "boolean", "integer"])
def test_canonical_and_aliased_types_do_not_warn(tmp_path, caplog, good_type):
    with caplog.at_level(logging.WARNING):
        load_manifest(_manifest(tmp_path, f'ok = {{ type = "{good_type}" }}'))
    assert not [r for r in caplog.records if "unknown type" in r.message]


# ── G6 — scaffold documents the pre-PyPI local-relaydeck escape hatch ─


def test_scaffold_documents_local_relaydeck_path(tmp_path, monkeypatch):
    from relaydeck.transports import cli as C

    monkeypatch.chdir(tmp_path)
    C.plugin_new.callback(name="My Pkg", pattern="reactor", local=False, workspace=None)
    root = tmp_path / "relaydeck-plugin-my-pkg"
    pyproject = (root / "pyproject.toml").read_text()
    readme = (root / "README.md").read_text()
    # The commented uv.sources escape hatch + a working brace (f-string sanity).
    assert "[tool.uv.sources]" in pyproject
    assert "relaydeck = { path =" in pyproject
    # README steers authors at `plugin dev` and flags the editable-install need.
    assert "relaydeck plugin dev" in readme
    assert "installed (editable)" in readme
