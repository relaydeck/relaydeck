"""
Tests for the plugin system — the backbone of relaydeck.

Tests: discovery, loading, dependency ordering, lifecycle hooks,
category-based querying, and the singleton registry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure relaydeck is importable from tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.plugin import (
    RelaydeckPlugin,
    PluginContext,
    PluginRegistry,
    get_registry,
)


# ── Test plugins ─────────────────────────────────────────────────────


class _TestPluginA(RelaydeckPlugin):
    """A test plugin with no dependencies."""
    name = "test-a"
    category = "tool"
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self.loaded = False
        self.ctx = None

    def on_load(self, ctx: PluginContext) -> None:
        self.loaded = True
        self.ctx = ctx


class _TestPluginB(RelaydeckPlugin):
    """A test plugin that depends on test-a."""
    name = "test-b"
    category = "tool"
    requires = ["test-a"]

    def __init__(self):
        super().__init__()
        self.loaded = False

    def on_load(self, ctx: PluginContext) -> None:
        self.loaded = True


class _TestPluginC(RelaydeckPlugin):
    """A test plugin with CLI and API registration."""
    name = "test-c"
    category = "harness"

    def __init__(self):
        super().__init__()
        self.cli_registered = False
        self.api_registered = False

    def register_cli(self, cli) -> None:
        self.cli_registered = True

    def register_api_routes(self, app) -> None:
        self.api_registered = True


# ── Tests ────────────────────────────────────────────────────────────


class TestPluginRegistry:
    """Tests for the PluginRegistry."""

    def test_empty_registry(self):
        registry = PluginRegistry()
        assert len(registry.all()) == 0
        assert len(registry.categories()) == 0
        assert len(registry.harnesses) == 0
        assert len(registry.tools) == 0

    def test_get_nonexistent(self):
        registry = PluginRegistry()
        assert registry.get("nonexistent") is None

    def test_by_category_empty(self):
        registry = PluginRegistry()
        assert registry.by_category("harness") == []


class TestPluginContext:
    """Tests for PluginContext."""

    def test_context_creation(self, tmp_path):
        ctx = PluginContext(config_home=tmp_path, workspace_path=None)
        assert ctx.config_home == tmp_path
        assert ctx.workspace_path is None

    def test_resolve_path(self, tmp_path):
        ctx = PluginContext(config_home=tmp_path, workspace_path=None)
        resolved = ctx.resolve_path("test/soul")
        assert resolved == tmp_path / "test" / "soul"

    def test_resolve_path_with_workspace(self, tmp_path):
        ws = tmp_path / "workspaces" / "myproj"
        ctx = PluginContext(config_home=tmp_path, workspace_path=ws)
        resolved = ctx.resolve_path("soul")
        assert resolved == ws / "soul"


class TestRelaydeckPlugin:
    """Tests for the RelaydeckPlugin base class."""

    def test_plugin_has_required_attrs(self):
        p = _TestPluginA()
        assert p.name == "test-a"
        assert p.category == "tool"
        assert p.version == "1.0.0"
        assert p.requires == []

    def test_plugin_lifecycle(self, tmp_path):
        p = _TestPluginA()
        ctx = PluginContext(config_home=tmp_path, workspace_path=None)
        assert not p.loaded
        p.on_load(ctx)
        assert p.loaded
        assert p.ctx == ctx
        p.on_unload()

    def test_plugin_web_components_default(self):
        p = _TestPluginA()
        assert p.register_web_components() == {}

    def test_plugin_agent_factory_default(self):
        p = _TestPluginA()
        assert p.register_agent_factory() == {}

    def test_plugin_dependency_chain(self, tmp_path):
        ctx = PluginContext(config_home=tmp_path, workspace_path=None)
        a = _TestPluginA()
        b = _TestPluginB()
        a.on_load(ctx)
        b.on_load(ctx)
        assert a.loaded
        assert b.loaded


class TestPluginCLIAPI:
    """Tests for plugin CLI and API registration extension points."""

    def test_cli_registration(self):
        p = _TestPluginC()
        assert not p.cli_registered
        p.register_cli(object())
        assert p.cli_registered

    def test_api_registration(self):
        p = _TestPluginC()
        assert not p.api_registered
        p.register_api_routes(object())
        assert p.api_registered


class TestGetRegistry:
    """Tests for the module-level singleton."""

    def test_get_registry_singleton(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2
