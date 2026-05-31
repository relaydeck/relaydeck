"""
Harness SDK registrar (`host.harnesses`).

Pins the `host.harnesses` registrar:

  - declarative register → SDK adapter binds at load time
  - disable / unload → SDK adapter unbinds; `known_agent_types()` loses
    the names immediately
  - missing capability declaration → CapabilityNotDeclared
  - empty / blank type_name → ValueError
  - aliases for the same class register cleanly
  - the existing built-in pi-harness plugin still registers `pi` +
    `pi-harness` (regression guard)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.orchestrator import known_agent_types, unregister_agent_type
from relaydeck.plugin import PluginContext, PluginRegistry
from relaydeck.sdk import CapabilityNotDeclared


def test_builtin_pi_harness_registers_via_sdk(tmp_path, monkeypatch):
    """The migrated pi-harness must still surface `pi` + `pi-harness`
    in known_agent_types() after going through the SDK path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_home = tmp_path / ".relaydeck"

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    types = known_agent_types()
    assert "pi" in types, types
    assert "pi-harness" in types, types


def test_third_party_plugin_can_register_harness(tmp_path, monkeypatch):
    """User-installed plugin that subclasses relaydeck.sdk.Plugin and calls
    host.harnesses.register binds the type via the adapter."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Clean slate — strip prior test residue.
    unregister_agent_type("zonk")

    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "zonk"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "zonk"
version = "0.1.0"
category = "harness"
host_api_version = 1
declared_capabilities = ["harnesses.register"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.agents_base import BaseAgent
from relaydeck.sdk import Plugin, PluginHost


class ZonkAgent(BaseAgent):
    def run(self):
        pass


class Zonk(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("zonk", ZonkAgent)


PLUGIN = Zonk()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    assert "zonk" in known_agent_types()


def test_unloading_plugin_unregisters_harness(tmp_path, monkeypatch):
    """The big payoff: disable plugin → its agent types vanish from
    known_agent_types() immediately. The legacy on-load-only path
    leaked stale registrations forever."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    unregister_agent_type("blarg")

    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "blarg"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "blarg"
version = "0.1.0"
category = "harness"
host_api_version = 1
declared_capabilities = ["harnesses.register"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.agents_base import BaseAgent
from relaydeck.sdk import Plugin, PluginHost


class BlargAgent(BaseAgent):
    def run(self):
        pass


class Blarg(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.harnesses.register("blarg", BlargAgent)


PLUGIN = Blarg()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))
    assert "blarg" in known_agent_types()

    # Disable the plugin → adapter must unregister the type.
    registry.disable("blarg")
    assert "blarg" not in known_agent_types(), (
        "Disabling a harness plugin must remove its agent types from "
        "known_agent_types() — the SDK adapter's on_unload owns this."
    )


def test_harness_register_requires_capability(tmp_path, monkeypatch):
    """A plugin that doesn't declare `harnesses.register` cannot call
    host.harnesses.register — the gate raises CapabilityNotDeclared."""
    from relaydeck.sdk import PluginHost
    from relaydeck.plugin import PluginEventBus

    host = PluginHost(
        name="nope",
        config_home=tmp_path,
        declared_capabilities=[],  # no caps declared
        event_bus=PluginEventBus(),
        orchestrator=None,
    )
    with pytest.raises(CapabilityNotDeclared):
        host.harnesses.register("nope", object)


def test_harness_register_rejects_empty_type_name(tmp_path):
    from relaydeck.sdk import PluginHost
    from relaydeck.plugin import PluginEventBus

    host = PluginHost(
        name="ok",
        config_home=tmp_path,
        declared_capabilities=["harnesses.register"],
        event_bus=PluginEventBus(),
        orchestrator=None,
    )
    with pytest.raises(ValueError):
        host.harnesses.register("   ", object)


def test_harness_register_supports_aliases(tmp_path):
    """Plugin can register the same class under multiple names —
    that's how pi/pi-harness, codex/codex-cli, etc. work."""
    from relaydeck.sdk import PluginHost
    from relaydeck.plugin import PluginEventBus

    host = PluginHost(
        name="pi",
        config_home=tmp_path,
        declared_capabilities=["harnesses.register"],
        event_bus=PluginEventBus(),
        orchestrator=None,
    )

    class FakeAgent: ...

    host.harnesses.register("foo", FakeAgent)
    host.harnesses.register("foo-canonical", FakeAgent)
    names = [n for n, _ in host.harnesses.registrations]
    assert names == ["foo", "foo-canonical"]
