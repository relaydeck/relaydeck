from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from relaydeck.plugin import Event, PluginContext, PluginRegistry
from relaydeck.plugin_lock import entry_from_manifest, load_lock, save_lock, verify_lock
from relaydeck.plugin_manifest import load_manifest
from relaydeck.sdk import CapabilityNotDeclared, RemoteHost
from relaydeck.testing import MockHost
from relaydeck.workers import WorkerStatus, get_worker_registry


def _write_entry_point_plugin(
    site: Path,
    *,
    dist_name: str,
    package: str,
    manifest_name: str,
    trust_level: str = "",
    plugin_body: str | None = None,
) -> Path:
    pkg = site / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    trust = f'trust_level = "{trust_level}"\n' if trust_level else ""
    (pkg / "plugin.toml").write_text(
        f"""
[plugin]
name = "{manifest_name}"
version = "0.1.0"
description = "entry point plugin"
category = "tool"
host_api_version = 1
{trust}declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text(
        plugin_body or "from relaydeck.sdk import Plugin\n\nPLUGIN = Plugin()\n"
    )
    dist = site / f"{dist_name}-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Name: {dist_name.replace('_', '-')}\nVersion: 0.1\n")
    (dist / "entry_points.txt").write_text(
        f"""
[relaydeck.plugins]
{manifest_name} = {package}.plugin:PLUGIN
""".strip()
    )
    return pkg / "plugin.toml"


def test_plugin_manifest_parses_declared_capabilities_and_ui(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "digest"
version = "0.1.0"
category = "cognitive"
host_api_version = 1
workspace_scoped = true
declared_capabilities = ["events.subscribe", "kv.read", "ui.register"]
needs_vault = ["DIGEST_API_KEY"]

[plugin.settings]
model = { type = "string", default = "local-fast", description = "Model preset" }

[plugin.ui]
tabs = [{ id = "digest", title = "Digest", module = "static/digest.js" }]
""".strip()
    )

    manifest = load_manifest(manifest_path)

    assert manifest.name == "digest"
    assert manifest.workspace_scoped is True
    assert "kv.read" in manifest.capabilities
    assert manifest.needs_vault == ("DIGEST_API_KEY",)
    assert manifest.settings_schema()[0]["key"] == "model"
    assert manifest.ui_manifest()["tabs"][0]["id"] == "digest"
    assert manifest.manifest_hash.startswith("sha256:")


def test_mockhost_enforces_declared_capabilities(tmp_path):
    host = MockHost(
        name="limited",
        config_home=tmp_path,
        declared_capabilities={"kv.read"},
    )

    assert host.kv.get("missing", "fallback") == "fallback"
    with pytest.raises(CapabilityNotDeclared):
        host.kv.set("x", 1)


def test_plugin_registry_loads_new_style_plugin_with_manifest(tmp_path):
    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "demo"
version = "0.1.0"
category = "tool"
host_api_version = 1
declared_capabilities = ["events.subscribe", "events.emit", "kv.read", "kv.write", "ui.register"]

[plugin.ui]
tabs = [{ id = "demo", title = "Demo", module = "static/demo.js" }]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost

class Demo(Plugin):
    def on_load(self, host: PluginHost) -> None:
        self.host = host
        host.kv.set("loaded", True)
        host.ui.tab("live", "Live", "L", "static/live.js")

PLUGIN = Demo()
""".strip()
    )

    registry = PluginRegistry(config_home)
    entries = registry._scan_directory(config_home / "plugins", "user")
    demo = next(e for e in entries if e.name == "demo")
    assert demo.manifest is not None

    registry.load_all(PluginContext(config_home=config_home))
    loaded = registry.get("demo")

    assert loaded is not None
    assert loaded.instance.get_settings_schema() == []
    ui = loaded.instance.register_ui()
    assert [tab["id"] for tab in ui["tabs"]] == ["demo:demo", "demo:live"]


def test_plugin_registry_discovers_installed_entry_point_plugin(tmp_path, monkeypatch):
    pkg = tmp_path / "site" / "relaydeck_demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "demo-entry"
version = "0.1.0"
description = "entry point plugin"
category = "tool"
host_api_version = 1
trust_level = "local"
declared_capabilities = ["kv.write"]
""".strip()
    )
    (pkg / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost

class Demo(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.kv.set("loaded", True)

PLUGIN = Demo()
""".strip()
    )
    dist = tmp_path / "site" / "relaydeck_demo-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: relaydeck-demo\nVersion: 0.1\n")
    (dist / "entry_points.txt").write_text(
        """
[relaydeck.plugins]
demo-entry = relaydeck_demo.plugin:PLUGIN
""".strip()
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site"))

    registry = PluginRegistry(tmp_path / ".relaydeck")
    entries = registry._scan_entry_points("relaydeck.plugins", "installed")

    demo = next(e for e in entries if e.name == "demo-entry")
    assert demo.source == "installed"
    assert demo.manifest is not None
    assert demo.path == pkg


def test_plugin_registry_dedupes_same_plugin_discovered_from_same_path(tmp_path):
    from relaydeck.plugin import PluginEntry, RelaydeckPlugin

    plugin_dir = tmp_path / "pkg"
    plugin_dir.mkdir()
    first = PluginEntry(
        name="demo",
        category="tool",
        version="0.1.0",
        instance=RelaydeckPlugin(),
        source="builtin",
        path=plugin_dir,
    )
    duplicate = PluginEntry(
        name="demo",
        category="tool",
        version="0.1.0",
        instance=RelaydeckPlugin(),
        source="installed",
        path=plugin_dir,
    )

    deduped = PluginRegistry(tmp_path)._dedupe_discovered([first, duplicate])

    assert deduped == [first]


def test_entry_point_scan_skips_bundled_plugin_metadata(tmp_path, monkeypatch):
    dist = tmp_path / "site" / "relaydeck_plugins-0.1.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Name: relaydeck-plugins\nVersion: 0.1\n")
    (dist / "entry_points.txt").write_text(
        """
[relaydeck.plugins]
messaging = plugins.messaging.plugin:PLUGIN
""".strip()
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site"))

    entries = PluginRegistry(tmp_path / ".relaydeck")._scan_entry_points(
        "relaydeck.plugins", "installed"
    )

    assert not any(entry.name == "messaging" for entry in entries)


def test_plugin_registry_does_not_import_untrusted_entry_point_by_default(
    tmp_path, monkeypatch
):
    pkg = tmp_path / "site" / "relaydeck_untrusted"
    pkg.mkdir(parents=True)
    marker = tmp_path / "imported"
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "untrusted-entry"
version = "0.1.0"
description = "untrusted entry point plugin"
category = "tool"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text(
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("imported")
PLUGIN = object()
""".strip()
    )
    dist = tmp_path / "site" / "relaydeck_untrusted-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: relaydeck-untrusted\nVersion: 0.1\n")
    (dist / "entry_points.txt").write_text(
        """
[relaydeck.plugins]
untrusted-entry = relaydeck_untrusted.plugin:PLUGIN
""".strip()
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.delenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", raising=False)

    registry = PluginRegistry(tmp_path / ".relaydeck")
    entries = registry._scan_entry_points("relaydeck.plugins", "installed")

    assert all(entry.name != "untrusted-entry" for entry in entries)
    assert not marker.exists()


def test_plugin_registry_imports_package_entry_point_after_lock_approval(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    marker = tmp_path / "imported"
    manifest_path = _write_entry_point_plugin(
        site,
        dist_name="relaydeck_plugin_locked",
        package="relaydeck_plugin_locked",
        manifest_name="locked-entry",
        plugin_body=f"""
from pathlib import Path
from relaydeck.sdk import Plugin
Path({str(marker)!r}).write_text("imported")
PLUGIN = Plugin()
""".strip(),
    )
    manifest = load_manifest(manifest_path)
    config_home = tmp_path / ".relaydeck"
    save_lock(config_home, {
        "locked-entry": entry_from_manifest(
            manifest,
            source="relaydeck-plugin-locked",
            scope="user",
            installed_via="package",
        )
    })
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", raising=False)
    sys.modules.pop("relaydeck_plugin_locked.plugin", None)

    registry = PluginRegistry(config_home)
    entries = registry._scan_entry_points("relaydeck.plugins", "installed")

    assert any(entry.name == "locked-entry" for entry in entries)
    assert marker.exists()


def test_plugin_registry_loads_package_entry_point_after_lock_approval(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    loaded_marker = tmp_path / "loaded"
    manifest_path = _write_entry_point_plugin(
        site,
        dist_name="relaydeck_plugin_locked_load",
        package="relaydeck_plugin_locked_load",
        manifest_name="locked-load",
        plugin_body=f"""
from pathlib import Path
from relaydeck.sdk import Plugin, PluginHost


class Demo(Plugin):
    def on_load(self, host: PluginHost) -> None:
        Path({str(loaded_marker)!r}).write_text(host.name)


PLUGIN = Demo()
""".strip(),
    )
    manifest = load_manifest(manifest_path)
    config_home = tmp_path / ".relaydeck"
    save_lock(config_home, {
        "locked-load": entry_from_manifest(
            manifest,
            source="relaydeck-plugin-locked-load",
            scope="user",
            installed_via="package",
        )
    })
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", raising=False)
    sys.modules.pop("relaydeck_plugin_locked_load.plugin", None)

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    assert loaded_marker.read_text() == "locked-load"


def test_plugin_registry_loads_editable_package_entry_point_after_lock_approval(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    loaded_marker = tmp_path / "loaded"
    manifest_path = _write_entry_point_plugin(
        site,
        dist_name="relaydeck_plugin_editable_load",
        package="relaydeck_plugin_editable_load",
        manifest_name="editable-load",
        plugin_body=f"""
from pathlib import Path
from relaydeck.sdk import Plugin, PluginHost


class Demo(Plugin):
    def on_load(self, host: PluginHost) -> None:
        Path({str(loaded_marker)!r}).write_text(host.name)


PLUGIN = Demo()
""".strip(),
    )
    manifest = load_manifest(manifest_path)
    config_home = tmp_path / ".relaydeck"
    save_lock(config_home, {
        "editable-load": entry_from_manifest(
            manifest,
            source=str(site),
            scope="user",
            installed_via="editable-package",
        )
    })
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", raising=False)
    sys.modules.pop("relaydeck_plugin_editable_load.plugin", None)

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    assert loaded_marker.read_text() == "editable-load"


def test_plugin_registry_allows_editable_package_manifest_drift_before_import(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    imported_marker = tmp_path / "imported"
    manifest_path = _write_entry_point_plugin(
        site,
        dist_name="relaydeck_plugin_editable_drift",
        package="relaydeck_plugin_editable_drift",
        manifest_name="editable-drift",
        plugin_body=f"""
from pathlib import Path
from relaydeck.sdk import Plugin
Path({str(imported_marker)!r}).write_text("imported")
PLUGIN = Plugin()
""".strip(),
    )
    manifest = load_manifest(manifest_path)
    config_home = tmp_path / ".relaydeck"
    save_lock(config_home, {
        "editable-drift": entry_from_manifest(
            manifest,
            source=str(site),
            scope="user",
            installed_via="editable-package",
        )
    })
    manifest_path.write_text(
        manifest_path.read_text().replace(
            'declared_capabilities = []',
            'declared_capabilities = ["kv.read"]',
        )
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", raising=False)
    sys.modules.pop("relaydeck_plugin_editable_drift.plugin", None)

    registry = PluginRegistry(config_home)
    entries = registry._scan_entry_points("relaydeck.plugins", "installed")

    assert any(entry.name == "editable-drift" for entry in entries)
    assert imported_marker.exists()


def test_plugin_registry_can_import_untrusted_entry_point_when_opted_in(
    tmp_path, monkeypatch
):
    pkg = tmp_path / "site" / "relaydeck_untrusted_allowed"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "untrusted-allowed"
version = "0.1.0"
description = "untrusted entry point plugin"
category = "tool"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin

class Demo(Plugin):
    pass

PLUGIN = Demo()
""".strip()
    )
    dist = tmp_path / "site" / "relaydeck_untrusted_allowed-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: relaydeck-untrusted-allowed\nVersion: 0.1\n")
    (dist / "entry_points.txt").write_text(
        """
[relaydeck.plugins]
untrusted-allowed = relaydeck_untrusted_allowed.plugin:PLUGIN
""".strip()
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.setenv("RELAYDECK_ALLOW_UNTRUSTED_PLUGINS", "1")

    registry = PluginRegistry(tmp_path / ".relaydeck")
    entries = registry._scan_entry_points("relaydeck.plugins", "installed")

    assert any(entry.name == "untrusted-allowed" for entry in entries)


def test_plugin_lock_verify_records_manifest_provenance(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "1.2.3"
host_api_version = 1
declared_capabilities = ["events.subscribe"]
""".strip()
    )
    manifest = load_manifest(manifest_path)

    rebuilt = verify_lock(tmp_path, [manifest])
    loaded = load_lock(tmp_path)

    assert rebuilt["demo"].manifest_hash == manifest.manifest_hash
    assert loaded["demo"].version == "1.2.3"
    assert loaded["demo"].declared_capabilities == ["events.subscribe"]


def test_plugin_lock_blocks_installed_manifest_drift(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "1.2.3"
host_api_version = 1
declared_capabilities = ["events.subscribe"]
""".strip()
    )
    manifest = load_manifest(manifest_path)
    old_entry = entry_from_manifest(
        manifest,
        source=str(tmp_path),
        scope="user",
        installed_via="local",
    )
    save_lock(tmp_path, {"demo": old_entry})
    manifest_path.write_text(
        manifest_path.read_text().replace('["events.subscribe"]', '["events.emit"]')
    )
    changed = load_manifest(manifest_path)

    rebuilt = verify_lock(tmp_path, [changed])

    assert rebuilt["demo"].state == "blocked"
    assert "manifest hash changed" in rebuilt["demo"].block_reason


def test_plugin_lock_preserves_git_provenance(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "1.2.3"
host_api_version = 1
declared_capabilities = ["events.subscribe"]
""".strip()
    )
    manifest = load_manifest(manifest_path)

    save_lock(tmp_path, {
        "demo": entry_from_manifest(
            manifest,
            source="git+https://example.test/demo.git@v1.2.3",
            scope="user",
            installed_via="git",
            git_url="https://example.test/demo.git",
            git_ref="v1.2.3",
            git_commit="abc123",
        )
    })
    loaded = load_lock(tmp_path)["demo"]

    assert loaded.git_url == "https://example.test/demo.git"
    assert loaded.git_ref == "v1.2.3"
    assert loaded.git_commit == "abc123"


def test_plugin_lock_allows_editable_manifest_drift(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "1.2.3"
host_api_version = 1
declared_capabilities = ["events.subscribe"]
""".strip()
    )
    manifest = load_manifest(manifest_path)
    save_lock(tmp_path, {
        "demo": entry_from_manifest(
            manifest,
            source=str(tmp_path),
            scope="user",
            installed_via="editable",
        )
    })
    manifest_path.write_text(
        manifest_path.read_text().replace('["events.subscribe"]', '["events.emit"]')
    )
    changed = load_manifest(manifest_path)

    rebuilt = verify_lock(tmp_path, [changed])

    assert rebuilt["demo"].state == "enabled"
    assert rebuilt["demo"].manifest_hash == changed.manifest_hash


def test_plugin_lock_allows_editable_package_manifest_drift(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "1.2.3"
host_api_version = 1
declared_capabilities = ["events.subscribe"]
""".strip()
    )
    manifest = load_manifest(manifest_path)
    save_lock(tmp_path, {
        "demo": entry_from_manifest(
            manifest,
            source=str(tmp_path),
            scope="user",
            installed_via="editable-package",
        )
    })
    manifest_path.write_text(
        manifest_path.read_text().replace('["events.subscribe"]', '["events.emit"]')
    )
    changed = load_manifest(manifest_path)

    rebuilt = verify_lock(tmp_path, [changed])

    assert rebuilt["demo"].state == "enabled"
    assert rebuilt["demo"].manifest_hash == changed.manifest_hash


def test_git_plugin_source_parser_accepts_tagged_https_sources():
    from relaydeck.transports.cli import _parse_git_plugin_source

    assert _parse_git_plugin_source(
        "git+https://github.com/acme/plugin.git@v0.1.0"
    ) == ("https://github.com/acme/plugin.git", "v0.1.0")
    assert _parse_git_plugin_source(
        "git+https://github.com/acme/plugin@feature/dev"
    ) == ("https://github.com/acme/plugin", "feature/dev")


def test_git_plugin_install_requires_pinned_ref(tmp_path):
    from relaydeck.plugin_install import PluginInstallError, install_plugin_source

    with pytest.raises(PluginInstallError, match="must pin"):
        install_plugin_source(
            "git+https://github.com/acme/relaydeck-plugin-floating.git",
            tmp_path / ".relaydeck",
        )


def test_git_plugin_install_records_pinned_ref_and_commit(tmp_path, monkeypatch):
    import subprocess

    from relaydeck.plugin_install import install_plugin_source

    config_home = tmp_path / ".relaydeck"
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append([str(part) for part in argv])
        if argv[:2] == ["git", "clone"]:
            clone_dest = Path(argv[-1])
            clone_dest.mkdir(parents=True, exist_ok=True)
            (clone_dest / "plugin.toml").write_text(
                """
[plugin]
name = "git-demo"
version = "0.1.0"
description = "demo"
license = "MIT"
host_api_version = 1
declared_capabilities = []
""".strip()
            )
            (clone_dest / "plugin.py").write_text("PLUGIN = object()\n")
        if argv[:4] == ["git", "-C", str(Path(argv[2])), "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, stdout="abc123\n")
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = install_plugin_source(
        "git+https://github.com/acme/relaydeck-plugin-demo.git@v0.1.0",
        config_home,
    )

    lock_entry = load_lock(config_home)["git-demo"]
    assert result.names == ["git-demo"]
    assert lock_entry.installed_via == "git"
    assert lock_entry.git_url == "https://github.com/acme/relaydeck-plugin-demo.git"
    assert lock_entry.git_ref == "v0.1.0"
    assert lock_entry.git_commit == "abc123"
    assert calls[0] == [
        "git",
        "clone",
        "https://github.com/acme/relaydeck-plugin-demo.git",
        calls[0][-1],
    ]
    assert ["git", "-C", calls[0][-1], "checkout", "v0.1.0"] in calls


def test_plugin_lock_blocks_host_api_version_mismatch(tmp_path):
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "future"
version = "1.0.0"
host_api_version = 99
declared_capabilities = []
""".strip()
    )
    manifest = load_manifest(manifest_path)

    rebuilt = verify_lock(tmp_path, [manifest])

    assert rebuilt["future"].state == "blocked"
    assert "host_api_version 99" in rebuilt["future"].block_reason


def test_plugin_registry_skips_blocked_manifest_drift(tmp_path):
    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    manifest_path = plugin_dir / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "1.0.0"
host_api_version = 1
declared_capabilities = ["kv.write"]
""".strip()
    )
    manifest = load_manifest(manifest_path)
    save_lock(config_home, {
        "demo": entry_from_manifest(
            manifest,
            source=str(plugin_dir),
            scope="user",
            installed_via="local",
        )
    })
    manifest_path.write_text(
        manifest_path.read_text().replace('["kv.write"]', '["kv.read", "kv.write"]')
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost


class Demo(Plugin):
    def on_load(self, host: PluginHost) -> None:
        host.kv.set("loaded", True)


PLUGIN = Demo()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    assert registry.get("demo") is None
    assert load_lock(config_home)["demo"].state == "blocked"


def test_plugin_install_editable_links_source_and_records_lock(tmp_path, monkeypatch):
    from relaydeck.transports import cli as cli_mod

    config_home = tmp_path / ".relaydeck"
    plugin_dir = tmp_path / "relaydeck-plugin-demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "demo"
version = "0.1.0"
description = "demo plugin"
license = "MIT"
host_api_version = 1
declared_capabilities = ["kv.read"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text("PLUGIN = object()\n")
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)

    result = CliRunner().invoke(
        cli_mod.main,
        ["plugin", "install", "--editable", str(plugin_dir)],
    )

    assert result.exit_code == 0, result.output
    dest = config_home / "plugins" / "demo"
    assert dest.is_symlink()
    assert dest.resolve() == plugin_dir
    lock_entry = load_lock(config_home)["demo"]
    assert lock_entry.installed_via == "editable"
    assert lock_entry.source == str(plugin_dir.resolve())


def test_plugin_install_package_name_installs_and_approves_entry_point(
    tmp_path, monkeypatch
):
    from relaydeck.transports import cli as cli_mod

    site = tmp_path / "site"
    _write_entry_point_plugin(
        site,
        dist_name="relaydeck_plugin_demo_pkg",
        package="relaydeck_plugin_demo_pkg",
        manifest_name="demo-pkg",
    )
    config_home = tmp_path / ".relaydeck"
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)
    monkeypatch.setattr(cli_mod, "_install_python_package", lambda src: None)
    sys.modules.pop("relaydeck_plugin_demo_pkg.plugin", None)

    result = CliRunner().invoke(
        cli_mod.main,
        ["plugin", "install", "relaydeck-plugin-demo-pkg"],
    )

    assert result.exit_code == 0, result.output
    lock_entry = load_lock(config_home)["demo-pkg"]
    assert lock_entry.installed_via == "package"
    assert lock_entry.source == "relaydeck-plugin-demo-pkg"


def test_plugin_install_editable_package_installs_project_and_approves_entry_point(
    tmp_path, monkeypatch
):
    from relaydeck.transports import cli as cli_mod

    config_home = tmp_path / ".relaydeck"
    plugin_root = tmp_path / "relaydeck-plugin-demo"
    pkg = plugin_root / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "demo"
version = "0.1.0"
description = "demo plugin"
license = "MIT"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text("PLUGIN = object()\n")
    (plugin_root / "pyproject.toml").write_text(
        """
[project]
name = "relaydeck-plugin-demo"
version = "0.1.0"

[project.entry-points."relaydeck.plugins"]
demo = "demo.plugin:PLUGIN"
""".strip()
    )
    manifest = load_manifest(pkg / "plugin.toml")
    installed: list[str] = []
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)
    monkeypatch.setattr(
        cli_mod,
        "_install_editable_python_package",
        lambda src: installed.append(src),
    )
    monkeypatch.setattr(cli_mod, "_package_plugin_manifests", lambda src: [manifest])

    result = CliRunner().invoke(
        cli_mod.main,
        ["plugin", "install", "--editable", str(plugin_root)],
    )

    assert result.exit_code == 0, result.output
    assert installed == [str(plugin_root.resolve())]
    assert not (config_home / "plugins" / "demo").exists()
    lock_entry = load_lock(config_home)["demo"]
    assert lock_entry.installed_via == "editable-package"
    assert lock_entry.source == str(plugin_root.resolve())


def test_plugin_install_editable_package_approves_manifest_from_source_tree(tmp_path):
    from relaydeck.plugin_install import install_plugin_source

    config_home = tmp_path / ".relaydeck"
    plugin_root = tmp_path / "relaydeck-plugin-local-source"
    pkg = plugin_root / "local_source"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "local-source"
version = "0.1.0"
description = "demo plugin"
license = "MIT"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text("PLUGIN = object()\n")
    (plugin_root / "pyproject.toml").write_text(
        """
[project]
name = "relaydeck-plugin-local-source"
version = "0.1.0"

[project.entry-points."relaydeck.plugins"]
local-source = "local_source.plugin:PLUGIN"
""".strip()
    )
    installed: list[str] = []

    result = install_plugin_source(
        str(plugin_root),
        config_home,
        editable=True,
        install_editable_python_package=lambda src: installed.append(src),
    )

    assert result.names == ["local-source"]
    assert installed == [str(plugin_root.resolve())]
    lock_entry = load_lock(config_home)["local-source"]
    assert lock_entry.installed_via == "editable-package"
    assert lock_entry.source == str(plugin_root.resolve())


def test_plugin_install_editable_package_supports_src_layout(tmp_path):
    from relaydeck.plugin_install import install_plugin_source

    config_home = tmp_path / ".relaydeck"
    plugin_root = tmp_path / "relaydeck-plugin-src-layout"
    pkg = plugin_root / "src" / "src_layout"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "src-layout"
version = "0.1.0"
description = "demo plugin"
license = "MIT"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text("PLUGIN = object()\n")
    (plugin_root / "pyproject.toml").write_text(
        """
[project]
name = "relaydeck-plugin-src-layout"
version = "0.1.0"

[project.entry-points."relaydeck.plugins"]
src-layout = "src_layout.plugin:PLUGIN"
""".strip()
    )
    installed: list[str] = []

    result = install_plugin_source(
        str(plugin_root),
        config_home,
        editable=True,
        install_editable_python_package=lambda src: installed.append(src),
    )

    assert result.names == ["src-layout"]
    assert installed == [str(plugin_root.resolve())]
    lock_entry = load_lock(config_home)["src-layout"]
    assert lock_entry.installed_via == "editable-package"
    assert lock_entry.source == str(plugin_root.resolve())


def test_plugin_update_skips_editable_package_plugins(tmp_path, monkeypatch):
    from relaydeck.transports import cli as cli_mod

    config_home = tmp_path / ".relaydeck"
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo"
version = "0.1.0"
description = "demo plugin"
license = "MIT"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    manifest = load_manifest(manifest_path)
    save_lock(config_home, {
        "demo": entry_from_manifest(
            manifest,
            source=str(tmp_path),
            scope="user",
            installed_via="editable-package",
        )
    })
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)
    monkeypatch.setattr(cli_mod.plugin_install, "callback", lambda src, editable: calls.append(src))

    result = CliRunner().invoke(cli_mod.main, ["plugin", "update", "demo"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "editable install already points at source" in result.output


def test_plugin_install_api_uses_shared_installer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import relaydeck.orchestrator as orch_mod
    import relaydeck.plugin_install as install_mod
    from relaydeck.plugin_install import PluginInstallResult
    from relaydeck.transports.api import create_app

    config_home = tmp_path / ".relaydeck"
    orch_mod._orchestrator = None
    calls = []

    def fake_install(src, home, *, editable=False):
        calls.append((src, home, editable))
        return PluginInstallResult(
            names=["demo-pkg"],
            installed_via="package",
            source=src,
        )

    monkeypatch.setattr(install_mod, "install_plugin_source", fake_install)
    client = TestClient(create_app(config_home))

    response = client.post(
        "/api/plugins/install",
        json={"source": "relaydeck-plugin-demo-pkg"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == ["demo-pkg"]
    assert response.json()["restart_required"] is True
    assert calls == [("relaydeck-plugin-demo-pkg", config_home, False)]


def test_plugin_uninstall_api_uses_config_home(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import relaydeck.orchestrator as orch_mod
    import relaydeck.plugin_install as install_mod
    from relaydeck.transports.api import create_app

    config_home = tmp_path / ".relaydeck"
    orch_mod._orchestrator = None
    calls = []

    def fake_uninstall(name, home):
        calls.append((name, home))
        return True, "relaydeck-plugin-demo-pkg"

    monkeypatch.setattr(install_mod, "uninstall_plugin", fake_uninstall)
    client = TestClient(create_app(config_home))

    response = client.delete("/api/plugins/demo-pkg")

    assert response.status_code == 200, response.text
    assert response.json()["package"] == "relaydeck-plugin-demo-pkg"
    assert calls == [("demo-pkg", config_home)]


def test_plugin_uninstall_package_removes_lock_and_package_when_last_user(
    tmp_path, monkeypatch
):
    from relaydeck.transports import cli as cli_mod

    config_home = tmp_path / ".relaydeck"
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.write_text(
        """
[plugin]
name = "demo-pkg"
version = "0.1.0"
description = "demo"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    manifest = load_manifest(manifest_path)
    save_lock(config_home, {
        "demo-pkg": entry_from_manifest(
            manifest,
            source="relaydeck-plugin-demo-pkg",
            scope="user",
            installed_via="package",
        )
    })
    removed: list[str] = []
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)
    monkeypatch.setattr(cli_mod, "_uninstall_python_package", lambda src: removed.append(src))

    result = CliRunner().invoke(cli_mod.main, ["plugin", "uninstall", "demo-pkg"])

    assert result.exit_code == 0, result.output
    assert "demo-pkg" not in load_lock(config_home)
    assert removed == ["relaydeck-plugin-demo-pkg"]


def test_plugin_uninstall_package_keeps_package_when_source_has_other_plugins(
    tmp_path, monkeypatch
):
    from relaydeck.transports import cli as cli_mod

    config_home = tmp_path / ".relaydeck"
    manifest_a = tmp_path / "a.toml"
    manifest_b = tmp_path / "b.toml"
    manifest_a.write_text(
        """
[plugin]
name = "demo-a"
version = "0.1.0"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    manifest_b.write_text(manifest_a.read_text().replace('name = "demo-a"', 'name = "demo-b"'))
    source = "relaydeck-plugin-demo-bundle"
    save_lock(config_home, {
        "demo-a": entry_from_manifest(
            load_manifest(manifest_a), source=source, scope="user", installed_via="package"
        ),
        "demo-b": entry_from_manifest(
            load_manifest(manifest_b), source=source, scope="user", installed_via="package"
        ),
    })
    removed: list[str] = []
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)
    monkeypatch.setattr(cli_mod, "_uninstall_python_package", lambda src: removed.append(src))

    result = CliRunner().invoke(cli_mod.main, ["plugin", "uninstall", "demo-a"])

    assert result.exit_code == 0, result.output
    assert "demo-a" not in load_lock(config_home)
    assert "demo-b" in load_lock(config_home)
    assert removed == []


def test_plugin_new_scaffolds_entry_point_package(tmp_path):
    from relaydeck.transports import cli as cli_mod

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli_mod.main, ["plugin", "new", "Demo Tool"])

        assert result.exit_code == 0, result.output
        root = Path("relaydeck-plugin-demo-tool")
        pkg = root / "demo_tool"
        assert (pkg / "plugin.py").exists()
        assert (pkg / "plugin.toml").exists()
        assert (pkg / "py.typed").exists()
        pyproject = (root / "pyproject.toml").read_text()
        assert '[project.entry-points."relaydeck.plugins"]' in pyproject
        assert 'demo-tool = "demo_tool.plugin:PLUGIN"' in pyproject
        assert 'dependencies = ["relaydeck>=0.1.0"]' in pyproject
        assert (root / ".github" / "workflows" / "ci.yml").exists()
        assert (root / "RELEASE.md").exists()
        assert (root / ".gitignore").exists()
        plugin_body = (pkg / "plugin.py").read_text()
        assert "from relaydeck.sdk import Event, Plugin, PluginHost" in plugin_body
        assert "def _on_startup(self, event: Event) -> None:" in plugin_body
        test_body = (root / "tests" / "test_plugin.py").read_text()
        assert "from demo_tool.plugin import PLUGIN" in test_body


def test_plugin_new_skill_scaffolds_declared_skill(tmp_path):
    from relaydeck.transports import cli as cli_mod

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli_mod.main,
            ["plugin", "new", "Demo Skill", "--pattern", "skill"],
        )

        assert result.exit_code == 0, result.output
        root = Path("relaydeck-plugin-demo-skill")
        pkg = root / "demo_skill"
        manifest = load_manifest(pkg / "plugin.toml")
        assert manifest.category == "skill"
        assert manifest.workspace_scoped is True
        assert manifest.declared_capabilities == ("events.emit",)
        assert manifest.skills == {"demo-skill": "SKILL.md"}
        skill = (pkg / "SKILL.md").read_text()
        assert "name: demo-skill" in skill
        assert "description:" in skill
        plugin_body = (pkg / "plugin.py").read_text()
        assert "workspace_scoped = True" in plugin_body
        assert 'plugin.skills.changed' in plugin_body
        test_body = (root / "tests" / "test_plugin.py").read_text()
        assert "from pathlib import Path" in test_body
        assert 'load_manifest(Path("demo_skill/plugin.toml"))' in test_body
        assert "from relaydeck.testing import MockHost" in test_body
        assert "test_refresh_emits_skill_changed" in test_body


def test_plugin_publish_check_accepts_scaffolded_package(tmp_path, monkeypatch):
    import shutil

    from relaydeck.transports import cli as cli_mod

    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(cli_mod.main, ["plugin", "new", "demo publish"])
        assert created.exit_code == 0, created.output

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "publish-check", "relaydeck-plugin-demo-publish"],
        )

        assert result.exit_code == 0, result.output
        assert "demo-publish is ready" in result.output


def test_plugin_publish_check_builds_and_validates_wheel(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import zipfile

    from relaydeck.transports import cli as cli_mod

    calls: list[list[str]] = []
    cwds: list[str | None] = []

    def fake_run(argv, **kwargs):
        calls.append([str(part) for part in argv])
        cwd = kwargs.get("cwd")
        cwds.append(str(cwd) if cwd is not None else None)
        if argv[:3] == ["uv", "build", "--wheel"]:
            out_dir = Path(argv[argv.index("--out-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            wheel = out_dir / "relaydeck_plugin_demo_wheel-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr(
                    "relaydeck_plugin_demo_wheel-0.1.0.dist-info/entry_points.txt",
                    "[relaydeck.plugins]\n"
                    "demo-wheel = demo_wheel.plugin:PLUGIN\n",
                )
                zf.writestr("demo_wheel/plugin.toml", "[plugin]\nname='demo-wheel'\n")
                zf.writestr("demo_wheel/py.typed", "")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(cli_mod.main, ["plugin", "new", "demo wheel"])
        assert created.exit_code == 0, created.output

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "publish-check", "relaydeck-plugin-demo-wheel"],
        )

        assert result.exit_code == 0, result.output
        assert any(call[1:4] == ["-m", "pytest", "tests"] for call in calls)
        assert any(call[:3] == ["uv", "build", "--wheel"] for call in calls)
        pytest_index = next(
            i for i, call in enumerate(calls)
            if call[1:4] == ["-m", "pytest", "tests"]
        )
        assert cwds[pytest_index] == str(Path("relaydeck-plugin-demo-wheel"))


def test_plugin_wheel_validation_requires_manifest_next_to_entry_point(tmp_path):
    import zipfile

    from relaydeck.transports import cli as cli_mod

    wheel = tmp_path / "relaydeck_plugin_bad-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(
            "relaydeck_plugin_bad-0.1.0.dist-info/entry_points.txt",
            "[relaydeck.plugins]\nbad = bad_plugin.plugin:PLUGIN\n",
        )

    with pytest.raises(SystemExit, match="plugin.toml must ship"):
        cli_mod._validate_plugin_wheel(
            wheel,
            manifest_name="bad",
            entry_point_value="bad_plugin.plugin:PLUGIN",
        )


def test_plugin_wheel_validation_requires_declared_skill_files(tmp_path):
    import zipfile

    from relaydeck.transports import cli as cli_mod

    wheel = tmp_path / "relaydeck_plugin_bad_skill-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(
            "relaydeck_plugin_bad_skill-0.1.0.dist-info/entry_points.txt",
            "[relaydeck.plugins]\nbad-skill = bad_skill.plugin:PLUGIN\n",
        )
        zf.writestr("bad_skill/plugin.toml", "[plugin]\nname='bad-skill'\n")
        zf.writestr("bad_skill/py.typed", "")

    with pytest.raises(SystemExit, match="declared SKILL.md files must ship"):
        cli_mod._validate_plugin_wheel(
            wheel,
            manifest_name="bad-skill",
            entry_point_value="bad_skill.plugin:PLUGIN",
            skill_paths=("SKILL.md",),
        )


def test_plugin_wheel_validation_requires_py_typed_marker(tmp_path):
    import zipfile

    from relaydeck.transports import cli as cli_mod

    wheel = tmp_path / "relaydeck_plugin_bad_typed-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(
            "relaydeck_plugin_bad_typed-0.1.0.dist-info/entry_points.txt",
            "[relaydeck.plugins]\nbad-typed = bad_typed.plugin:PLUGIN\n",
        )
        zf.writestr("bad_typed/plugin.toml", "[plugin]\nname='bad-typed'\n")

    with pytest.raises(SystemExit, match="missing bad_typed/py.typed"):
        cli_mod._validate_plugin_wheel(
            wheel,
            manifest_name="bad-typed",
            entry_point_value="bad_typed.plugin:PLUGIN",
        )


def test_plugin_publish_check_requires_release_checklist(tmp_path, monkeypatch):
    import shutil

    from relaydeck.transports import cli as cli_mod

    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(cli_mod.main, ["plugin", "new", "demo release"])
        assert created.exit_code == 0, created.output
        Path("relaydeck-plugin-demo-release/RELEASE.md").unlink()

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "publish-check", "relaydeck-plugin-demo-release"],
        )

        assert result.exit_code != 0
        assert "RELEASE.md not found" in result.output


def test_plugin_publish_check_requires_source_py_typed_marker(tmp_path, monkeypatch):
    import shutil

    from relaydeck.transports import cli as cli_mod

    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(cli_mod.main, ["plugin", "new", "demo untyped"])
        assert created.exit_code == 0, created.output
        Path("relaydeck-plugin-demo-untyped/demo_untyped/py.typed").unlink()

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "publish-check", "relaydeck-plugin-demo-untyped"],
        )

        assert result.exit_code != 0
        assert "py.typed not found beside plugin.py" in result.output


def test_plugin_publish_check_requires_relaydeck_dependency(tmp_path, monkeypatch):
    import shutil

    from relaydeck.transports import cli as cli_mod

    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(cli_mod.main, ["plugin", "new", "demo dependency"])
        assert created.exit_code == 0, created.output
        pyproject = Path("relaydeck-plugin-demo-dependency/pyproject.toml")
        pyproject.write_text(
            pyproject.read_text().replace(
                'dependencies = ["relaydeck>=0.1.0"]',
                'dependencies = []',
            )
        )

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "publish-check", "relaydeck-plugin-demo-dependency"],
        )

        assert result.exit_code != 0
        assert "project.dependencies must include relaydeck" in result.output


def test_plugin_publish_check_requires_declared_skill_file(tmp_path, monkeypatch):
    import shutil

    from relaydeck.transports import cli as cli_mod

    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(
            cli_mod.main,
            ["plugin", "new", "demo skill missing", "--pattern", "skill"],
        )
        assert created.exit_code == 0, created.output
        Path("relaydeck-plugin-demo-skill-missing/demo_skill_missing/SKILL.md").unlink()

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "publish-check", "relaydeck-plugin-demo-skill-missing"],
        )

        assert result.exit_code != 0
        assert "declared skill 'demo-skill-missing' file not found" in result.output


def test_plugin_verify_accepts_local_plugin_path_with_declared_skill(tmp_path):
    from relaydeck.transports import cli as cli_mod

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(
            cli_mod.main,
            ["plugin", "new", "demo verify skill", "--pattern", "skill"],
        )
        assert created.exit_code == 0, created.output

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "verify", "relaydeck-plugin-demo-verify-skill"],
        )

        assert result.exit_code == 0, result.output
        assert "verified 1 local plugin: demo-verify-skill" in result.output


def test_plugin_verify_local_path_requires_declared_skill_file(tmp_path):
    from relaydeck.transports import cli as cli_mod

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        created = runner.invoke(
            cli_mod.main,
            ["plugin", "new", "demo verify missing", "--pattern", "skill"],
        )
        assert created.exit_code == 0, created.output
        Path("relaydeck-plugin-demo-verify-missing/demo_verify_missing/SKILL.md").unlink()

        result = runner.invoke(
            cli_mod.main,
            ["plugin", "verify", "relaydeck-plugin-demo-verify-missing"],
        )

        assert result.exit_code != 0
        assert "declared skill 'demo-verify-missing' file not found" in result.output


def test_plugin_lint_finds_nested_manifest(tmp_path):
    from relaydeck.transports import cli as cli_mod

    plugin_dir = tmp_path / "relaydeck-plugin-demo" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "demo"
version = "0.1.0"
host_api_version = 1
declared_capabilities = []
""".strip()
    )

    result = CliRunner().invoke(
        cli_mod.main,
        ["plugin", "lint", str(tmp_path / "relaydeck-plugin-demo")],
    )

    assert result.exit_code == 0, result.output
    assert "demo v0.1.0" in result.output


def test_plugin_new_harness_scaffolds_public_harness_facade(tmp_path):
    from relaydeck.transports import cli as cli_mod

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli_mod.main,
            ["plugin", "new", "demo harness", "--pattern", "harness"],
        )

        assert result.exit_code == 0, result.output
        root = Path("relaydeck-plugin-demo-harness")
        manifest = (root / "demo_harness" / "plugin.toml").read_text()
        plugin_py = (root / "demo_harness" / "plugin.py").read_text()
        assert 'category = "harness"' in manifest
        assert 'declared_capabilities = ["harnesses.register"]' in manifest
        assert "from relaydeck.harness import HarnessAgent" in plugin_py
        assert 'host.harnesses.register("demo-harness", ExampleAgent)' in plugin_py


def test_plugin_new_provider_scaffolds_public_provider_facade(tmp_path):
    from relaydeck.transports import cli as cli_mod

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli_mod.main,
            ["plugin", "new", "demo provider", "--pattern", "provider"],
        )

        assert result.exit_code == 0, result.output
        root = Path("relaydeck-plugin-demo-provider")
        manifest = (root / "demo_provider" / "plugin.toml").read_text()
        plugin_py = (root / "demo_provider" / "plugin.py").read_text()
        test_py = (root / "tests" / "test_plugin.py").read_text()
        assert 'category = "provider"' in manifest
        assert "declared_capabilities = []" in manifest
        assert "from relaydeck.provider import ModelEntry, ProviderPlugin" in plugin_py
        assert 'provider_name = "demo-provider"' in plugin_py
        assert "from relaydeck.provider import ProviderPlugin" in test_py


def test_plugin_dev_installs_editable_without_tests(tmp_path, monkeypatch):
    from relaydeck.transports import cli as cli_mod

    config_home = tmp_path / ".relaydeck"
    plugin_root = tmp_path / "relaydeck-plugin-demo"
    pkg = plugin_root / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.toml").write_text(
        """
[plugin]
name = "demo"
version = "0.1.0"
description = "demo plugin"
license = "MIT"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (pkg / "plugin.py").write_text("PLUGIN = object()\n")
    monkeypatch.setattr(cli_mod, "_get_config_home", lambda: config_home)

    result = CliRunner().invoke(
        cli_mod.main,
        ["plugin", "dev", str(plugin_root), "--no-test"],
    )

    assert result.exit_code == 0, result.output
    assert (config_home / "plugins" / "demo").is_symlink()


def test_workspace_plugin_loads_one_instance_per_workspace(tmp_path):
    config_home = tmp_path / ".relaydeck"
    workspace = tmp_path / "workspaces" / "demo"
    plugin_dir = workspace / "plugins" / "counter"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "counter"
version = "0.1.0"
category = "tool"
host_api_version = 1
workspace_scoped = true
declared_capabilities = ["kv.read", "kv.write"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost

class Counter(Plugin):
    def on_load(self, host: PluginHost) -> None:
        self.host = host
        host.kv.set("workspace", host.workspace)

PLUGIN = Counter()
""".strip()
    )

    registry = PluginRegistry(config_home)
    first = registry.load_workspace_plugins("demo", workspace)
    second = registry.load_workspace_plugins("demo", workspace)

    assert len(first) == 1
    assert second == first
    assert registry.get("counter@demo") is not None
    assert registry.unload_workspace_plugins("demo") == ["counter"]
    assert registry.get("counter@demo") is None


def test_host_settings_workspace_tier_overrides_global_default(tmp_path):
    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "plugin-settings.toml").write_text(
        """
[demo]
mode = "workspace"
""".strip()
    )
    host = MockHost(
        name="demo",
        workspace="demo",
        config_home=tmp_path / ".relaydeck",
        declared_capabilities=set(),
        workspace_path=workspace,
    )

    host.settings._defaults["mode"] = "default"

    assert host.settings.get("mode") == "workspace"
    assert host.settings.source("mode") == "workspace"


def test_host_vault_requires_declared_key(monkeypatch, tmp_path):
    from plugins.vault import plugin as vault_plugin

    monkeypatch.setattr(vault_plugin, "get_secret", lambda key: f"value:{key}")
    host = MockHost(
        name="demo",
        config_home=tmp_path,
        declared_capabilities={"vault.read"},
        vault_keys=["ALLOWED"],
    )

    assert host.vault.get("ALLOWED") == "value:ALLOWED"
    with pytest.raises(CapabilityNotDeclared):
        host.vault.get("OTHER")


def test_host_vault_uses_public_vault_facade(monkeypatch, tmp_path):
    import relaydeck.vault as vault_facade

    monkeypatch.setattr(vault_facade, "get_secret", lambda key: f"facade:{key}")
    host = MockHost(
        name="demo",
        config_home=tmp_path,
        declared_capabilities={"vault.read"},
        vault_keys=["ALLOWED"],
    )

    assert host.vault.get("ALLOWED") == "facade:ALLOWED"


def test_provider_plugin_resolves_api_key_through_public_vault_facade(monkeypatch):
    import relaydeck.vault as vault_facade
    from relaydeck.provider import ProviderPlugin

    class DemoProvider(ProviderPlugin):
        key_env = "DEMO_API_KEY"

    monkeypatch.setattr(vault_facade, "get_secret", lambda key: f"secret:{key}")

    assert DemoProvider().resolved_api_key() == "secret:DEMO_API_KEY"


def test_host_vault_write_delete_require_capability_and_declared_key(monkeypatch, tmp_path):
    from plugins.vault import plugin as vault_plugin

    stored: dict[str, str] = {}
    monkeypatch.setattr(
        vault_plugin, "set_secret", lambda key, value: stored.__setitem__(key, value)
    )
    monkeypatch.setattr(
        vault_plugin,
        "delete_secret",
        lambda key: stored.pop(key, None) is not None,
    )
    host = MockHost(
        name="demo",
        config_home=tmp_path,
        declared_capabilities={"vault.write", "vault.delete"},
        vault_keys=["ALLOWED_*"],
    )

    host.vault.set("ALLOWED_ONE", "value")
    assert stored == {"ALLOWED_ONE": "value"}
    assert host.vault.delete("ALLOWED_ONE") is True
    with pytest.raises(CapabilityNotDeclared):
        host.vault.set("OTHER", "value")


def test_host_models_routes_to_provider_complete(monkeypatch, tmp_path):
    class Provider:
        def complete(self, prompt, *, model, max_tokens, **kwargs):
            return f"{model}:{max_tokens}:{prompt}"

        def embed(self, text, *, model):
            return [1, "2.5", 3]

    import relaydeck.plugin as plugin_mod

    monkeypatch.setattr(plugin_mod, "get_provider", lambda name: Provider())
    host = MockHost(
        name="demo",
        config_home=tmp_path,
        declared_capabilities={"models.complete", "models.embed"},
    )

    assert host.models.complete("hello", model="openai/gpt-test", max_tokens=12) == (
        "gpt-test:12:hello"
    )
    assert host.models.embed("hello", model="openai/text-embedding-test") == [1.0, 2.5, 3.0]


def test_top_level_api_route_is_not_prefixed(tmp_path):
    """A plugin that declares `top_level_api = true` and calls
    `host.api.route(..., top_level=True)` mounts the raw path on the
    FastAPI app — no `/api/plugins/<plugin>/...` prefix. That's the
    contract that lets gateway keep `/api/gateway/webhook/<channel>`
    stable for external webhook senders."""
    from fastapi import FastAPI

    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "wh"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "wh"
version = "0.1.0"
category = "infrastructure"
host_api_version = 1
top_level_api = true
declared_capabilities = ["api.register"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost


class WH(Plugin):
    def on_load(self, host: PluginHost) -> None:
        @host.api.route("/api/wh/webhook/{ch}", methods=["POST"], top_level=True)
        async def webhook(ch: str):
            return {"ch": ch}

        @host.api.route("/info", methods=["GET"])
        async def info():
            return {"ok": True}


PLUGIN = WH()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))
    app = FastAPI()
    for entry in registry.all():
        if entry.name != "wh":
            continue
        entry.instance.register_api_routes(app)

    paths = {getattr(r, "path", None) for r in app.routes}
    # top_level=True keeps the raw path
    assert "/api/wh/webhook/{ch}" in paths
    # the default route is still namespaced under /api/plugins/<name>/
    assert "/api/plugins/wh/info" in paths


def test_top_level_api_requires_manifest_flag(tmp_path):
    """A plugin that declares `api.register` but NOT `top_level_api` in
    the manifest cannot call `host.api.route(..., top_level=True)` —
    the gate raises CapabilityNotDeclared at decoration time."""
    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "naughty"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "naughty"
version = "0.1.0"
category = "tool"
host_api_version = 1
declared_capabilities = ["api.register"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost


class Naughty(Plugin):
    def on_load(self, host: PluginHost) -> None:
        @host.api.route("/api/raw", top_level=True)
        async def raw():
            return {}


PLUGIN = Naughty()
""".strip()
    )

    registry = PluginRegistry(config_home)
    # Load should fail (the plugin raises during on_load), but the
    # registry tolerates and logs the failure — so we assert by
    # confirming the plugin entry isn't loaded.
    registry.load_all(PluginContext(config_home=config_home))
    assert "naughty" not in [e.name for e in registry.all()]


def test_plugin_extend_cli_escape_hatch_attaches_to_existing_groups(tmp_path):
    """Plugins that need to attach commands to *existing* top-level
    groups (e.g. messaging extending `workspace`) can override
    `Plugin.extend_cli(root)`. HostPluginAdapter.register_cli calls
    it after processing the declarative host.cli.commands list, so
    both surfaces coexist."""
    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "ext"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "ext"
version = "0.1.0"
category = "tool"
host_api_version = 1
declared_capabilities = []
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
import click
from relaydeck.sdk import Plugin, PluginHost


class Ext(Plugin):
    def on_load(self, host: PluginHost) -> None:
        pass

    def extend_cli(self, root) -> None:
        # Reach into an existing top-level group and add a subcommand.
        ws = root.commands.get("workspace")
        if ws is not None:
            @ws.command("custom")
            def _custom():
                return "ok"


PLUGIN = Ext()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    cli = click.Group()
    workspace = click.Group("workspace")
    cli.add_command(workspace)

    for entry in registry.all():
        if entry.name == "ext":
            entry.instance.register_cli(cli)

    assert "custom" in cli.commands["workspace"].commands


def test_top_level_cli_registrar_conflict_first_wins(tmp_path):
    config_home = tmp_path / ".relaydeck"

    for name, value in (("alpha", "first"), ("beta", "second")):
        plugin_dir = config_home / "plugins" / name
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.toml").write_text(
            f"""
[plugin]
name = "{name}"
version = "0.1.0"
category = "tool"
host_api_version = 1
top_level_cli = true
declared_capabilities = ["cli.register"]
""".strip()
        )
        (plugin_dir / "plugin.py").write_text(
            f"""
from relaydeck.sdk import Plugin, PluginHost


class Demo(Plugin):
    def on_load(self, host: PluginHost) -> None:
        @host.cli.command("dup", top_level=True)
        def dup():
            return "{value}"


PLUGIN = Demo()
""".strip()
        )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))
    cli = click.Group()
    for entry in registry.all():
        if entry.name not in {"alpha", "beta"}:
            continue
        entry.instance.register_cli(cli)

    assert cli.commands["dup"].callback() == "first"


def test_failed_new_style_plugin_load_rolls_back_host_side_effects(tmp_path):
    config_home = tmp_path / ".relaydeck"
    plugin_dir = config_home / "plugins" / "rollbacker"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
name = "rollbacker"
version = "0.1.0"
category = "tool"
host_api_version = 1
declared_capabilities = ["events.subscribe", "workers.spawn", "kv.write"]
""".strip()
    )
    (plugin_dir / "plugin.py").write_text(
        """
from relaydeck.sdk import Plugin, PluginHost


class BadPlugin(Plugin):
    def on_load(self, host: PluginHost) -> None:
        self.host = host
        host.events.subscribe("rollback.probe", self._on_probe)
        host.workers.spawn("tick", lambda worker: worker.stop(), interval=60)
        raise RuntimeError("boom")

    def _on_probe(self, event):
        self.host.kv.set("leaked", True)

    def on_unload(self) -> None:
        self.host.config_home.joinpath("rollback_unloaded").write_text("yes")


PLUGIN = BadPlugin()
""".strip()
    )

    registry = PluginRegistry(config_home)
    registry.load_all(PluginContext(config_home=config_home))

    assert registry.get("rollbacker") is None
    assert (config_home / "rollback_unloaded").read_text() == "yes"
    registry.event_bus.emit(Event(type="rollback.probe", data={}))
    assert not (config_home / "plugin-data" / "rollbacker" / "kv.json").exists()
    workers = get_worker_registry().by_plugin("rollbacker")
    assert workers
    assert all(worker.status == WorkerStatus.STOPPED for worker in workers)


def test_remotehost_exposes_models_and_plugins_surfaces():
    calls: list[tuple[str, str, dict | None]] = []

    class FakeRemote(RemoteHost):
        def _request(self, path, *, method="GET", body=None):
            calls.append((path, method, body))
            if path == "/api/providers":
                return [{"name": "openai"}]
            if path == "/api/providers/openai/models":
                return [{"id": "gpt"}]
            if path == "/api/providers/openai/refresh":
                return {"name": "openai", "model_count": 1}
            if path == "/api/plugins":
                return [{"name": "messaging"}]
            if path == "/api/plugins/messaging/settings":
                return {"plugin": "messaging", "values": body or {}}
            if path == "/api/plugins/messaging/enable":
                return {"enabled": True}
            if path == "/api/plugins/messaging/disable":
                return {"enabled": False}
            if path == "/api/plugins/install":
                return {"plugins": ["demo"], "installed_via": "package", "source": body["source"]}
            if path == "/api/plugins/demo":
                return {"plugin": "demo", "uninstalled": True}
            raise AssertionError(path)

    host = FakeRemote("http://daemon")

    assert host.models.providers() == [{"name": "openai"}]
    assert host.models.list("openai") == [{"id": "gpt"}]
    assert host.models.refresh("openai")["model_count"] == 1
    assert host.plugins.list() == [{"name": "messaging"}]
    assert host.plugins.settings("messaging")["plugin"] == "messaging"
    assert host.plugins.set_settings("messaging", {"x": 1})["values"] == {"x": 1}
    assert host.plugins.enable("messaging")["enabled"] is True
    assert host.plugins.disable("messaging")["enabled"] is False
    assert host.plugins.install("relaydeck-plugin-demo")["plugins"] == ["demo"]
    assert host.plugins.uninstall("demo")["uninstalled"] is True
    assert ("/api/plugins/messaging/settings", "POST", {"x": 1}) in calls
    assert (
        "/api/plugins/install",
        "POST",
        {"source": "relaydeck-plugin-demo", "editable": False},
    ) in calls
    assert ("/api/plugins/demo", "DELETE", None) in calls


def test_all_builtin_plugins_have_manifests():
    builtin_root = Path(__file__).resolve().parent.parent / "relaydeck" / "plugins"
    plugin_files = sorted(builtin_root.rglob("plugin.py"))
    missing = [
        str(path.parent.relative_to(builtin_root))
        for path in plugin_files
        if not (path.parent / "plugin.toml").exists()
    ]

    assert missing == []


# ── SDK platform gaps: agent lifecycle, durable subs, worker policy ──
#
# These complete the host surface so a plugin (e.g. a GitHub issue→PR
# spawner) can create/start agents, subscribe durably to CI/review
# events, and run a supervised lifecycle worker — without reaching into
# private orchestrator methods or shelling out to `relaydeck`.


def test_agent_host_create_then_start(tmp_path):
    host = MockHost(config_home=tmp_path)
    info = host.agents.create("w1", "claude-code", "Worker 1", purpose="ci-fixer")
    assert info is not None and info.id == "w1"
    assert info.purpose == "ci-fixer"
    assert host.agents.get("w1").status == "stopped"

    started = host.agents.start("w1")
    assert started is not None
    assert host.agents.get("w1").status == "running"


def test_agent_host_create_pins_to_host_workspace(tmp_path):
    """A workspace-scoped host must not let a plugin create agents in
    another workspace — the host workspace wins over the arg."""
    host = MockHost(config_home=tmp_path, workspace="proj")
    info = host.agents.create("w2", "pi", "W2", workspace="somewhere-else")
    assert info is not None
    assert info.workspace == "proj"


def test_agent_host_create_and_start_require_capabilities(tmp_path):
    host = MockHost(config_home=tmp_path, declared_capabilities={"agents.list"})
    with pytest.raises(CapabilityNotDeclared):
        host.agents.create("w3", "pi", "W3")
    host.add_agent(id="w4", type="pi", name="W4", workspace=None, status="stopped")
    with pytest.raises(CapabilityNotDeclared):
        host.agents.start("w4")


def test_event_host_durable_subscribe_routes_to_bus(tmp_path):
    from relaydeck.sdk import EventBusHost, _CapabilityGate

    class _SpyBus:
        def __init__(self):
            self.durable = []
            self.plain = []

        def subscribe(self, pattern, handler):
            self.plain.append((pattern, handler))

        def subscribe_durable(self, pattern, handler, *, key):
            self.durable.append((pattern, handler, key))

        def unsubscribe(self, handler):
            pass

    spy = _SpyBus()
    host_bus = EventBusHost("p", spy, _CapabilityGate(["events.subscribe"]))

    def h(_e):
        pass

    host_bus.subscribe("github.*", h, durable=True, key="ci-cursor")
    assert spy.durable == [("github.*", h, "ci-cursor")]
    assert spy.plain == []

    # Non-durable still uses the plain path.
    host_bus.subscribe("agent.*", h)
    assert spy.plain == [("agent.*", h)]


def test_event_host_durable_requires_key(tmp_path):
    from relaydeck.sdk import EventBusHost, _CapabilityGate

    host_bus = EventBusHost("p", object(), _CapabilityGate(["events.subscribe"]))
    with pytest.raises(ValueError, match="stable `key`"):
        host_bus.subscribe("x.*", lambda e: None, durable=True)


def test_event_host_durable_degrades_without_bus_support(tmp_path):
    """A bus lacking subscribe_durable (a bare stub) must not crash —
    durable degrades to a plain subscribe."""
    from relaydeck.sdk import EventBusHost, _CapabilityGate

    class _PlainOnly:
        def __init__(self):
            self.plain = []

        def subscribe(self, pattern, handler):
            self.plain.append(pattern)

        def unsubscribe(self, handler):
            pass

    bus = _PlainOnly()
    host_bus = EventBusHost("p", bus, _CapabilityGate(["events.subscribe"]))
    host_bus.subscribe("x.*", lambda e: None, durable=True, key="k")
    assert bus.plain == ["x.*"]


def test_event_host_durable_replays_across_restart(tmp_path):
    """End-to-end: durable subscribe through the SDK surface persists and
    replays an event emitted before the subscriber existed (the daemon-
    restart case)."""
    from relaydeck.plugin import Event as _Event
    from relaydeck.plugin import PluginEventBus
    from relaydeck.sdk import EventBusHost, _CapabilityGate

    db = str(tmp_path / "relaydeck.db")
    from relaydeck.db import open_db
    open_db(db).close()

    # Emit on a first bus instance, with no subscriber yet.
    bus1 = PluginEventBus(db_path=db)
    bus1.emit(_Event(type="github.ci.failed", data={"pr": 7}, source_plugin="t"))

    # A fresh bus instance (simulating a restart) subscribes durably and
    # must replay the persisted, unacked event.
    bus2 = PluginEventBus(db_path=db)
    host_bus = EventBusHost("p", bus2, _CapabilityGate(["events.subscribe"]))
    got = []
    host_bus.subscribe("github.*", lambda e: got.append(e), durable=True, key="ci")
    assert [e.data["pr"] for e in got] == [7]


def test_worker_host_spawn_threads_supervision_knobs(tmp_path, monkeypatch):
    import relaydeck.workers as workers_mod

    captured: dict = {}

    class _FakeWorker:
        id = "wk1"
        name = "p.tick"

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    def fake_register(**kw):
        captured.update(kw)
        return _FakeWorker()

    monkeypatch.setattr(workers_mod, "register_worker", fake_register)
    host = MockHost(config_home=tmp_path)
    handle = host.workers.spawn(
        "tick", lambda w: None, interval=2.0,
        restart_policy="restart", crash_loop_threshold=3,
        crash_loop_window_s=30.0, restart_backoff_s=0.5,
    )
    assert handle.id == "wk1"
    assert captured["interval_s"] == 2.0
    assert captured["restart_policy"] == "restart"
    assert captured["crash_loop_threshold"] == 3
    assert captured["crash_loop_window_s"] == 30.0
    assert captured["restart_backoff_s"] == 0.5


def test_worker_host_spawn_defaults_to_stop_policy(tmp_path, monkeypatch):
    import relaydeck.workers as workers_mod
    from relaydeck.workers import RestartPolicy

    captured: dict = {}

    class _FakeWorker:
        id = "wk2"
        name = "p.tick"

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(
        workers_mod,
        "register_worker",
        lambda **kw: (captured.update(kw), _FakeWorker())[1],
    )
    host = MockHost(config_home=tmp_path)
    host.workers.spawn("tick", lambda w: None)
    assert captured["restart_policy"] == RestartPolicy.STOP


# ── Workspace-scoped host must use the registered name, not basename ──
#
# Regression: a workspace registered as `api` can live at a dir named
# `relaydeck`. HostPluginAdapter used to derive the scoped host's workspace
# from workspace_path.name, so host.agents.create() would pin agents to
# `relaydeck`, not `api`. PluginContext now carries the registered name.


def test_host_adapter_scopes_by_registered_workspace_name(tmp_path):
    from relaydeck.plugin import HostPluginAdapter, PluginContext, PluginEventBus
    from relaydeck.plugin_manifest import PluginManifest
    from relaydeck.sdk import Plugin

    captured: dict = {}

    class _P(Plugin):
        def on_load(self, host):
            captured["workspace"] = host.workspace

    ws_dir = tmp_path / "relaydeck"  # path basename differs from registered name
    ws_dir.mkdir()
    manifest = PluginManifest(
        name="probe", version="0.0.1", workspace_scoped=True,
        declared_capabilities=("agents.create", "agents.list"),
    )
    adapter = HostPluginAdapter(_P(), manifest, ws_dir)
    ctx = PluginContext(
        config_home=tmp_path, workspace_path=ws_dir,
        workspace_name="api", event_bus=PluginEventBus(),
    )
    adapter.on_load(ctx)
    assert captured["workspace"] == "api", (
        "scoped host must use the registered workspace name, not the dir basename"
    )


def test_host_adapter_falls_back_to_path_basename(tmp_path):
    """No registered name on the context (e.g. a global load) → fall back
    to the path basename, preserving prior behavior."""
    from relaydeck.plugin import HostPluginAdapter, PluginContext, PluginEventBus
    from relaydeck.plugin_manifest import PluginManifest
    from relaydeck.sdk import Plugin

    captured: dict = {}

    class _P(Plugin):
        def on_load(self, host):
            captured["workspace"] = host.workspace

    ws_dir = tmp_path / "myws"
    ws_dir.mkdir()
    manifest = PluginManifest(name="probe", version="0.0.1", workspace_scoped=True)
    adapter = HostPluginAdapter(_P(), manifest, ws_dir)
    ctx = PluginContext(
        config_home=tmp_path, workspace_path=ws_dir, event_bus=PluginEventBus(),
    )
    adapter.on_load(ctx)
    assert captured["workspace"] == "myws"


def test_load_workspace_plugins_threads_registered_name(tmp_path):
    """End-to-end: load_workspace_plugins passes the registered name into
    the PluginContext it builds for on_load, even when the path basename
    differs. The plugin records the workspace it was handed to a file so
    the assertion doesn't depend on registry internals."""
    from relaydeck.plugin import PluginContext, PluginEventBus, PluginRegistry

    marker = tmp_path / "seen_workspace.txt"
    # Dir basename ("relaydeck") differs from the registered name ("api").
    ws_dir = tmp_path / "relaydeck"
    plugin_dir = ws_dir / "plugins" / "probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        '[plugin]\n'
        'name = "probe"\n'
        'version = "0.0.1"\n'
        'workspace_scoped = true\n'
        'declared_capabilities = ["agents.list"]\n'
    )
    (plugin_dir / "plugin.py").write_text(
        "from relaydeck.sdk import Plugin\n"
        "from pathlib import Path\n"
        f"_MARKER = Path({str(marker)!r})\n"
        "class P(Plugin):\n"
        "    def on_load(self, host):\n"
        "        _MARKER.write_text(host.workspace or '')\n"
        "PLUGIN = P()\n"
    )

    reg = PluginRegistry(tmp_path)
    reg._event_bus = PluginEventBus()
    ctx = PluginContext(config_home=tmp_path, event_bus=reg._event_bus)
    reg.load_workspace_plugins("api", ws_dir, ctx)

    assert marker.exists(), "workspace-scoped plugin was not loaded"
    assert marker.read_text() == "api", (
        "load_workspace_plugins must hand the registered name to the host, "
        "not the path basename"
    )
