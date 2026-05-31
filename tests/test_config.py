"""
Tests for the config layer: YAML specs, workspace registry, model presets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.config import (
    AgentSpec,
    ModelPreset,
    WorkspaceConfig,
    load_agent_specs,
    load_model_presets,
    load_workspace_registry,
)


class TestAgentSpec:
    """Tests for AgentSpec — YAML is source of truth."""

    def test_from_yaml(self, tmp_path):
        spec_path = tmp_path / "agents" / "planner.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(yaml.dump({
            "id": "planner",
            "name": "Planner",
            "type": "pi",
            "workspace": "my-proj",
            "auto_start": True,
            "config": {"model": "claude-sonnet-4"},
        }))

        spec = AgentSpec.from_yaml(spec_path)
        assert spec.id == "planner"
        assert spec.name == "Planner"
        assert spec.type == "pi"
        assert spec.workspace == "my-proj"
        assert spec.auto_start is True
        assert spec.config["model"] == "claude-sonnet-4"

    def test_from_yaml_defaults(self, tmp_path):
        spec_path = tmp_path / "minimal.yaml"
        spec_path.write_text(yaml.dump({"id": "minimal"}))

        spec = AgentSpec.from_yaml(spec_path)
        assert spec.id == "minimal"
        assert spec.name == "minimal"  # defaults to id
        assert spec.type == "harness"
        assert spec.config == {}
        assert spec.auto_start is False

    def test_from_yaml_scalar_tag_is_single_tag(self, tmp_path):
        spec_path = tmp_path / "scalar-tag.yaml"
        spec_path.write_text(yaml.dump({"id": "scalar", "tags": "reviewer"}))

        spec = AgentSpec.from_yaml(spec_path)

        assert spec.tags == ["reviewer"]

    def test_to_yaml_roundtrip(self, tmp_path):
        spec = AgentSpec(id="test", name="Test", type="pi", workspace="ws")
        yaml_str = spec.to_yaml()
        assert "id: test" in yaml_str
        assert "type: pi" in yaml_str

        # Parse back
        data = yaml.safe_load(yaml_str)
        assert data["id"] == "test"

    def test_save_and_load(self, tmp_path):
        config_home = tmp_path / "config"
        config_home.mkdir()
        spec = AgentSpec(id="saved-agent", name="Saved", type="pi")
        spec.save(config_home)

        saved_path = config_home / "agents" / "saved-agent.yaml"
        assert saved_path.exists()

        loaded = AgentSpec.from_yaml(saved_path)
        assert loaded.id == "saved-agent"
        assert loaded.name == "Saved"


class TestModelPreset:
    """Tests for ModelPreset — model/provider logical pairings."""

    def test_from_yaml(self, tmp_path):
        preset_path = tmp_path / "presets" / "my-preset.yaml"
        preset_path.parent.mkdir(parents=True, exist_ok=True)
        preset_path.write_text(yaml.dump({
            "name": "my-preset",
            "provider": "openrouter",
            "model": "claude-sonnet-4-20250514",
        }))

        preset = ModelPreset.from_yaml(preset_path)
        assert preset.name == "my-preset"
        assert preset.provider == "openrouter"
        assert preset.model == "claude-sonnet-4-20250514"

    def test_from_yaml_tolerates_legacy_fields(self, tmp_path):
        # Legacy presets carried base_url/api_key_env/max_tokens/temperature.
        # Those moved to the provider (or were dropped) — loading must ignore
        # them, not error, so old preset files keep working.
        preset_path = tmp_path / "minimal.yaml"
        preset_path.write_text(yaml.dump({
            "provider": "anthropic",
            "model": "claude-haiku-3.5",
            "base_url": "https://legacy/v1",
            "api_key_env": "OLD_KEY",
            "max_tokens": 8192,
            "temperature": 0.3,
        }))

        preset = ModelPreset.from_yaml(preset_path)
        assert preset.provider == "anthropic"
        assert preset.model == "claude-haiku-3.5"
        assert preset.name == "minimal"


class TestWorkspaceConfig:
    """Tests for WorkspaceConfig."""

    def test_defaults(self):
        ws = WorkspaceConfig(name="test", path=Path("/tmp/test"))
        assert ws.name == "test"
        assert ws.plugins == []
        assert ws.has_plugin("cognitive") is False

    def test_with_plugins(self):
        ws = WorkspaceConfig(name="test", path=Path("/tmp/test"),
                            plugins=["cognitive", "recipes"])
        assert ws.plugins == ["cognitive", "recipes"]
        assert ws.has_plugin("cognitive") is True
        assert ws.has_plugin("recipes") is True
        assert ws.has_plugin("skills") is False


class TestLoadFunctions:
    """Tests for load helpers."""

    def test_load_workspace_registry_empty(self, tmp_path):
        # Should return empty list when no config exists
        # This test is isolated — we don't write a config.toml
        pass

    def test_load_workspace_registry_prefers_agent_toml_plugins(
        self, tmp_path, monkeypatch,
    ):
        """`agent.toml` is the source of truth for plugins because
        that's what the harness reads at spawn. config.toml's plugins
        field can drift (UI writes both, but the user can also hand-edit
        either file) — when they disagree, the registry must reflect
        what the harness will actually see.

        Regression for a workspace showing "no plugins" in the dashboard
        despite agent.toml having `plugins = ["messaging"]`.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_home = tmp_path / ".relaydeck"
        cfg_home.mkdir(parents=True)
        (tmp_path / "demo").mkdir()

        # config.toml says no plugins.
        (cfg_home / "config.toml").write_text(
            '[[workspace]]\n'
            'name = "demo"\n'
            f'path = "{tmp_path / "demo"}"\n'
            'plugins = []\n'
        )
        # agent.toml says messaging is enabled — user edited it
        # directly (or it was updated by another path that didn't
        # sync config.toml).
        ws_dir = cfg_home / "workspaces" / "demo"
        ws_dir.mkdir(parents=True)
        (ws_dir / "agent.toml").write_text(
            '[workspace]\nplugins = ["messaging"]\n'
        )

        registry = load_workspace_registry()
        assert len(registry) == 1
        assert registry[0].plugins == ["messaging"], (
            "registry must report what the harness will actually see"
        )

    def test_load_workspace_registry_falls_back_to_config_toml(
        self, tmp_path, monkeypatch,
    ):
        """Legacy workspaces without agent.toml should still report
        their plugins from config.toml. Don't break old installs."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_home = tmp_path / ".relaydeck"
        cfg_home.mkdir(parents=True)
        (tmp_path / "legacy").mkdir()
        (cfg_home / "config.toml").write_text(
            '[[workspace]]\n'
            'name = "legacy"\n'
            f'path = "{tmp_path / "legacy"}"\n'
            'plugins = ["skills", "recipes"]\n'
        )
        # NOTE: no agent.toml created.

        registry = load_workspace_registry()
        assert len(registry) == 1
        assert registry[0].plugins == ["skills", "recipes"]

    def test_load_workspace_registry_scalar_agent_toml_plugin(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_home = tmp_path / ".relaydeck"
        cfg_home.mkdir(parents=True)
        (tmp_path / "demo").mkdir()
        (cfg_home / "config.toml").write_text(
            '[[workspace]]\n'
            'name = "demo"\n'
            f'path = "{tmp_path / "demo"}"\n'
            'plugins = []\n'
        )
        ws_dir = cfg_home / "workspaces" / "demo"
        ws_dir.mkdir(parents=True)
        (ws_dir / "agent.toml").write_text(
            '[workspace]\nplugins = "messaging"\n'
        )

        registry = load_workspace_registry()

        assert registry[0].plugins == ["messaging"]

    def test_load_agent_specs_empty(self, tmp_path):
        specs = load_agent_specs(tmp_path)
        assert specs == []

    def test_load_agent_specs_from_dir(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "a.yaml").write_text(yaml.dump({"id": "a", "type": "pi"}))
        (agents_dir / "b.yaml").write_text(yaml.dump({"id": "b", "type": "claude-code"}))

        specs = load_agent_specs(tmp_path)
        assert len(specs) == 2
        assert {s.id for s in specs} == {"a", "b"}

    def test_load_model_presets_empty(self, tmp_path):
        # The package ships no presets — empty until the operator creates one.
        assert load_model_presets(tmp_path) == []

    def test_load_model_presets_from_dir(self, tmp_path):
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "p1.yaml").write_text(yaml.dump({
            "name": "p1", "provider": "openrouter", "model": "gpt-4o",
        }))

        presets = load_model_presets(tmp_path)
        assert len(presets) == 1
        assert presets[0].name == "p1"
        assert presets[0].model == "gpt-4o"
