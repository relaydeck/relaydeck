"""
Tests for the pluggable terminal viewer registry.

Coverage:
  - registry insert/lookup/listing
  - auto_detect honors AUTODETECT_ORDER and skips unavailable
    viewers
  - tmux recipe shape (existing pins still hold; the recipe now
    lives in relaydeck.transports.viewers.tmux)
  - ghostty viewer emits one window per agent + 1 inbox
  - print viewer is always available
  - SDK ViewerRegistrarHost: gating + de-registration on unload

We don't drive the actual launch subprocess in tests — that's
manual smoke (and CI runners typically don't have Ghostty or a
tmux daemon). The viewer's argv-building paths are pure
functions and unit-testable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.transports import viewers as viewers_mod
from relaydeck.transports.viewers import (
    TerminalViewer,
    ViewerContext,
    ViewerResult,
    auto_detect,
    register_builtin_viewers,
)
from relaydeck.transports.viewers.ghostty import GhosttyViewer
from relaydeck.transports.viewers.print_viewer import PrintViewer
from relaydeck.transports.viewers.tmux import TmuxViewer, build_recipe


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with a fresh registry so a viewer
    registered in one test doesn't leak into another."""
    saved = dict(viewers_mod._registry)
    viewers_mod._registry.clear()
    yield
    viewers_mod._registry.clear()
    viewers_mod._registry.update(saved)


# ── Registry semantics ─────────────────────────────────────────────


def test_register_builtin_viewers_registers_three():
    register_builtin_viewers()
    names = {v.name for v in viewers_mod.all_viewers()}
    assert names == {"tmux", "ghostty", "print"}


def test_register_is_idempotent():
    """Calling register_builtin_viewers twice doesn't duplicate —
    the registry is a dict keyed on viewer.name."""
    register_builtin_viewers()
    register_builtin_viewers()
    names = [v.name for v in viewers_mod.all_viewers()]
    assert sorted(names) == ["ghostty", "print", "tmux"]


def test_get_returns_none_for_unknown():
    register_builtin_viewers()
    assert viewers_mod.get("not-a-real-viewer") is None
    assert viewers_mod.get("tmux") is not None


# ── auto_detect ────────────────────────────────────────────────────


def test_auto_detect_respects_order_and_skips_unavailable(monkeypatch):
    """Auto-detect must walk AUTODETECT_ORDER and pick the first
    available viewer. Pin the order by registering stubs and
    flipping availability."""
    class _StubViewer:
        def __init__(self, name: str, available: bool):
            self.name = name
            self.description = ""
            self._available = available
        def is_available(self) -> bool:
            return self._available
        def launch(self, ctx):
            raise NotImplementedError

    # tmux not available → ghostty should win.
    viewers_mod.register(_StubViewer("tmux", available=False))
    viewers_mod.register(_StubViewer("ghostty", available=True))
    viewers_mod.register(_StubViewer("print", available=True))
    chosen = auto_detect()
    assert chosen is not None and chosen.name == "ghostty"

    # Now make ghostty unavailable too → print wins as the
    # always-on fallback.
    viewers_mod._registry["ghostty"]._available = False  # type: ignore[attr-defined]
    chosen = auto_detect()
    assert chosen is not None and chosen.name == "print"


def test_auto_detect_tolerates_misbehaving_is_available():
    """A viewer whose is_available() raises must not poison the
    walk — skip it and try the next."""
    class _BadViewer:
        name = "bad"
        description = "raises in is_available"
        def is_available(self) -> bool:
            raise RuntimeError("boom")
        def launch(self, ctx):
            raise NotImplementedError

    viewers_mod.register(_BadViewer())
    viewers_mod.register(PrintViewer())  # always-available fallback

    chosen = auto_detect()
    assert chosen is not None and chosen.name == "print"


# ── tmux viewer ────────────────────────────────────────────────────


def _ctx(agents: list[str], workspace: str = "demo") -> ViewerContext:
    return ViewerContext(
        session_name=f"relaydeck-{workspace}",
        workspace=workspace,
        agents=[{"id": a, "status": "running"} for a in agents],
        attach_command_for=lambda aid: f"relaydeck attach {aid}",
        inbox_command=f"relaydeck workspace inbox -f --full --workspace {workspace}",
    )


def test_tmux_build_recipe_has_attach_panes_and_inbox():
    """Pin the behavior operators care about without freezing every
    generated tmux command line."""
    cmds = build_recipe(_ctx(["alice", "bob"]))
    joined = [" ".join(c) for c in cmds]
    assert any("relaydeck attach alice" in c for c in joined)
    assert any("relaydeck attach bob" in c for c in joined)
    inbox_cmd = next(c for c in joined if "inbox" in c)
    assert "workspace inbox -f" in inbox_cmd


def test_tmux_print_only_returns_recipe_and_attach_command():
    """`--print-only` must build a ViewerResult with the recipe in
    `message` and the attach hint in `attach_command`, regardless
    of whether tmux is actually installed."""
    ctx = _ctx(["alice"], workspace="x")
    ctx.print_only = True
    result = TmuxViewer().launch(ctx)
    assert result.success
    assert "tmux new-session" in result.message
    assert result.attach_command == "tmux attach -t relaydeck-x"


# ── ghostty viewer ─────────────────────────────────────────────────


def test_ghostty_windows_mode_builds_one_per_agent_plus_inbox(monkeypatch):
    """Each agent gets a Ghostty window in the legacy windows mode.
    Pinned by setting RELAYDECK_GHOSTTY_LAYOUT=windows so the test is
    deterministic across CI runners (macOS would default to splits)."""
    monkeypatch.setenv("RELAYDECK_GHOSTTY_LAYOUT", "windows")
    ctx = _ctx(["alice", "bob", "carol"])
    ctx.print_only = True
    result = GhosttyViewer().launch(ctx)
    assert result.success
    lines = result.message.splitlines()
    # 3 agents + 1 inbox = 4 window-spawn commands.
    assert len(lines) == 4
    for ln in lines:
        # Each is either `ghostty -e` or `open -na Ghostty.app`.
        assert "ghostty" in ln.lower()
    assert any("workspace inbox -f" in ln for ln in lines)


def test_ghostty_splits_mode_emits_applescript(monkeypatch):
    """In splits mode (default on macOS) the viewer's print-only
    output is an osascript program that opens one Ghostty window
    and uses Cmd+D / Cmd+Shift+D to spawn splits."""
    monkeypatch.setenv("RELAYDECK_GHOSTTY_LAYOUT", "splits")
    # Force the Darwin-only branch even when tests run on Linux CI.
    monkeypatch.setattr(
        "relaydeck.transports.viewers.ghostty.platform.system",
        lambda: "Darwin",
    )

    ctx = _ctx(["alice", "bob", "carol"])
    ctx.print_only = True
    result = GhosttyViewer().launch(ctx)
    assert result.success
    script = result.message
    # Hallmarks of the AppleScript path.
    assert 'tell application "Ghostty" to activate' in script
    assert 'tell application "System Events"' in script
    assert 'keystroke "n" using {command down}' in script  # new window
    # Each agent's command gets typed via keystroke.
    assert 'keystroke "relaydeck attach alice"' in script
    assert 'keystroke "relaydeck attach bob"' in script
    assert 'keystroke "relaydeck attach carol"' in script
    # The inbox shows up too.
    assert "workspace inbox -f" in script


# ── print viewer ───────────────────────────────────────────────────


def test_print_viewer_is_always_available():
    """The fallback viewer must report available regardless of
    environment so auto-detect always has something to fall back to."""
    pv = PrintViewer()
    assert pv.is_available() is True


def test_print_viewer_emits_one_attach_per_agent():
    ctx = _ctx(["alice", "bob"])
    result = PrintViewer().launch(ctx)
    assert result.success
    msg = result.message
    assert "relaydeck attach alice" in msg
    assert "relaydeck attach bob" in msg
    assert "workspace inbox -f" in msg


# ── SDK plugin registrar ───────────────────────────────────────────


def test_sdk_viewers_register_requires_capability(tmp_path):
    """A plugin that doesn't declare `viewers.register` cannot
    call host.viewers.register — the gate raises
    CapabilityNotDeclared."""
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import CapabilityNotDeclared, PluginHost

    host = PluginHost(
        name="nope",
        config_home=tmp_path,
        declared_capabilities=[],
        event_bus=PluginEventBus(),
        orchestrator=None,
    )
    with pytest.raises(CapabilityNotDeclared):
        host.viewers.register(PrintViewer())


def test_sdk_viewers_register_rejects_unnamed():
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import PluginHost

    host = PluginHost(
        name="ok",
        config_home=Path("/tmp"),
        declared_capabilities=["viewers.register"],
        event_bus=PluginEventBus(),
        orchestrator=None,
    )

    class _NoName:
        name = ""
    with pytest.raises(ValueError):
        host.viewers.register(_NoName())


def test_third_party_viewer_plugin_registers_via_adapter(tmp_path, monkeypatch):
    """End-to-end: a plugin that subclasses relaydeck.sdk.Plugin and
    calls host.viewers.register makes its viewer available through
    the canonical registry."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from relaydeck.plugin import PluginContext, PluginRegistry

    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "kittyish"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "kittyish"
version = "0.1.0"
category = "tool"
host_api_version = 1
declared_capabilities = ["viewers.register"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost


class KittyishViewer:
    name = "kittyish"
    description = "fake kitty viewer for tests"
    def is_available(self): return True
    def launch(self, ctx):
        from relaydeck.transports.viewers import ViewerResult
        return ViewerResult(success=True, message="(launched)")


class KittyPlugin(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.viewers.register(KittyishViewer())


PLUGIN = KittyPlugin()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    # The new viewer should be in the canonical registry now,
    # alongside whatever the built-in loader has registered.
    assert viewers_mod.get("kittyish") is not None
    chosen = viewers_mod.get("kittyish")
    assert chosen is not None
    assert chosen.is_available() is True

    # And disabling the plugin removes the viewer entry — symmetric
    # to harness registration.
    registry.disable("kittyish")
    assert viewers_mod.get("kittyish") is None
