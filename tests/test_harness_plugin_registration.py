"""
Pin: every shipped harness plugin registers its agent types through the SDK
`host.harnesses` path (not direct `register_agent_type` calls), and the loaded
plugin name appears in the registry. New harness plugins MUST follow this
pattern (subclass `Plugin` + `host.harnesses.register`) — capability gating
and clean unbind-on-disable only work for SDK-style plugins.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_registry(tmp_path):
    import relaydeck.plugin as plug
    from relaydeck.orchestrator import known_agent_types
    from relaydeck.plugin import PluginContext, get_registry

    plug._registry = None
    reg = get_registry(tmp_path / ".relaydeck")
    reg.load_all(PluginContext(config_home=tmp_path / ".relaydeck"))
    return reg, known_agent_types()


def test_claude_code_plugin_loads_and_registers_agent_types(tmp_path):
    reg, types = _load_registry(tmp_path)
    assert reg.get("claude-code-harness") is not None
    assert "claude" in types
    assert "claude-code" in types


def test_cursor_plugin_loads_and_registers_agent_types(tmp_path):
    reg, types = _load_registry(tmp_path)
    assert reg.get("cursor-harness") is not None
    assert "cursor" in types
    assert "cursor-cli" in types
    assert "cursor-agent" in types


def test_antigravity_plugin_loads_and_registers_agent_types(tmp_path):
    reg, types = _load_registry(tmp_path)
    assert reg.get("antigravity-harness") is not None
    assert "antigravity" in types
    assert "agy" in types


def test_every_harness_plugin_is_sdk_style(tmp_path):
    """Drift-pin: any future harness that
    introduces a bare `RelaydeckPlugin()` instead of subclassing
    `relaydeck.sdk.Plugin` should fail this test. Capability gating only
    works for SDK-style plugins, so this is load-bearing.

    SDK-style plugins are wrapped in `HostPluginAdapter` by the
    registry; the underlying plugin instance lives at
    `entry.instance.plugin` and must be a `relaydeck.sdk.Plugin`."""
    from relaydeck.plugin import HostPluginAdapter
    from relaydeck.sdk import Plugin

    reg, _ = _load_registry(tmp_path)
    harnesses = [
        e for e in reg.all()
        if e.manifest and e.manifest.category == "harness"
    ]
    assert harnesses, "expected at least one harness plugin loaded"
    for entry in harnesses:
        assert isinstance(entry.instance, HostPluginAdapter), (
            f"harness plugin {entry.name!r} is not SDK-style "
            f"(got {type(entry.instance).__name__}); migrate to "
            f"`class ...(Plugin)` + `host.harnesses.register(...)`"
        )
        assert isinstance(entry.instance.plugin, Plugin), (
            f"harness plugin {entry.name!r} wrapped adapter doesn't "
            f"hold a Plugin instance"
        )
