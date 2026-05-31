"""Tests for pi_engine — argv builder and JSON event parsing."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from plugins.harnesses.relaydeck_native import pi_engine


@pytest.fixture
def home(tmp_path):
    ch = tmp_path / ".relaydeck"
    (ch / "agents").mkdir(parents=True)
    (ch / "runtime").mkdir(parents=True)
    return ch


def _write(ch: Path, agent_id: str, **config):
    import yaml
    (ch / "agents" / f"{agent_id}.yaml").write_text(yaml.safe_dump({
        "id": agent_id, "name": agent_id, "type": "relaydeck",
        "workspace": "w", "config": config,
    }))


def test_install_pi_npm_fails_when_npm_missing(monkeypatch):
    """No `npm` on the daemon's PATH → fail-soft with an actionable hint,
    don't crash. The button's whole point is friendly degradation when the
    operator's machine isn't quite ready."""
    pi_engine.clear_pi_status_cache()
    # npm absent. shutil.which falls through for everything except "pi"
    # (left unset → also None, so pi_available() stays False).
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.shutil.which",
        lambda binary: None,
    )
    r = pi_engine.install_pi_npm()
    assert r["ok"] is False
    assert r["pi_installed"] is False
    assert "npm" in (r["error"] or "").lower()
    assert "node" in (r["error"] or "").lower()  # tells operator what to install


def test_install_pi_npm_success_flips_status(monkeypatch):
    """npm exits zero, post-install probe sees pi → ok=True with version.
    The cache is busted before the probe so the answer doesn't lag behind
    the install by up to _PI_STATUS_TTL_S seconds."""
    pi_engine.clear_pi_status_cache()
    # Before install: which("pi") is None; after install (during the
    # post-install probe): which("pi") returns a real path.
    flips = {"installed": False}

    def fake_which(binary):
        if binary == "npm":
            return "/usr/local/bin/npm"
        if binary == "pi":
            return "/usr/local/bin/pi" if flips["installed"] else None
        return None

    def fake_run(cmd, **_kw):
        # npm install -g <pkg> → success, flips pi-on-PATH for the probe.
        assert cmd[:3] == ["npm", "install", "-g"]
        flips["installed"] = True
        return type("R", (), {"stdout": "added 204 packages in 8s", "stderr": "", "returncode": 0})()

    monkeypatch.setattr("plugins.harnesses.relaydeck_native.pi_engine.shutil.which", fake_which)
    monkeypatch.setattr("plugins.harnesses.relaydeck_native.pi_engine.subprocess.run", fake_run)
    # _build_pi_status also probes `pi -v`; stub that too.
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine._probe_pi_version",
        lambda _path: "0.73.1",
    )

    r = pi_engine.install_pi_npm()
    assert r["ok"] is True
    assert r["pi_installed"] is True
    assert r["pi_version"] == "0.73.1"
    assert r["install_hint"] is None
    assert "added 204 packages" in r["output"]


def test_install_pi_npm_nonzero_exit_surfaces_error(monkeypatch):
    """npm exits non-zero (EACCES, network failure, etc.) → ok=False with a
    permissions hint, not a stack trace."""
    pi_engine.clear_pi_status_cache()
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.shutil.which",
        lambda binary: "/usr/local/bin/npm" if binary == "npm" else None,
    )
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "", "stderr": "EACCES: permission denied", "returncode": 243})(),
    )
    r = pi_engine.install_pi_npm()
    assert r["ok"] is False
    assert r["pi_installed"] is False
    assert "243" in (r["error"] or "")
    assert "sudo" in (r["error"] or "")  # hints at the perms recovery path


def test_install_pi_npm_success_but_pi_missing_explains_path_problem(monkeypatch):
    """npm exits zero but pi is still not on the daemon's PATH (npm global
    prefix mismatched with the daemon's PATH). Operator must hear *what*
    is wrong; an "ok" response would be a lie."""
    pi_engine.clear_pi_status_cache()
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.shutil.which",
        lambda binary: "/usr/local/bin/npm" if binary == "npm" else None,
    )
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "added 204 packages", "stderr": "", "returncode": 0})(),
    )
    r = pi_engine.install_pi_npm()
    assert r["ok"] is False
    assert r["pi_installed"] is False
    assert "PATH" in (r["error"] or "")


def test_sync_native_pi_auth_writes_credentials_from_vault(monkeypatch, tmp_path):
    """The provider 401 in the operator's container was caused by pi reading
    auth.json (empty) instead of the OPENROUTER_API_KEY env var we'd injected.
    The sync must materialize every vault-keyed provider into auth.json so pi
    can authenticate without env-var awareness."""
    secrets = {
        "OPENROUTER_API_KEY": "sk-openrouter-test",
        "ANTHROPIC_API_KEY": "sk-anthropic-test",
        # Deepseek/openai/gemini intentionally unset — sync must skip those.
    }
    monkeypatch.setattr("relaydeck.vault.get_secret", lambda k: secrets.get(k))

    pi_engine._sync_native_pi_auth(tmp_path)

    auth = tmp_path / "runtime" / "native-pi-agent" / "auth.json"
    assert auth.is_file()
    data = json.loads(auth.read_text())
    assert data == {
        "openrouter": {"type": "api_key", "key": "sk-openrouter-test"},
        "anthropic": {"type": "api_key", "key": "sk-anthropic-test"},
    }
    # Mode 0600 by construction — operator-only readable. (Group/world bits
    # must be clear; this is the property the TOCTOU window in the old
    # implementation could violate between write_text + chmod.)
    mode = auth.stat().st_mode & 0o777
    assert mode == 0o600, f"auth.json must be mode 0600, got {oct(mode)}"


def test_sync_native_pi_auth_evicts_stale_keys(monkeypatch, tmp_path):
    """Operator rotated a key (vault still has it) and revoked another (vault
    no longer has it). Sync must rewrite auth.json from-scratch — never merge
    in stale entries — so the operator's revoke takes effect on next spawn."""
    auth_dir = tmp_path / "runtime" / "native-pi-agent"
    auth_dir.mkdir(parents=True)
    auth = auth_dir / "auth.json"
    # Pre-existing auth.json with TWO providers (e.g., from a prior sync).
    auth.write_text(json.dumps({
        "openrouter": {"type": "api_key", "key": "sk-openrouter-OLD"},
        "anthropic":  {"type": "api_key", "key": "sk-anthropic-revoked"},
    }))

    # Vault now only has openrouter (rotated), no anthropic. Sync must drop
    # anthropic entirely and update openrouter to the new value.
    monkeypatch.setattr(
        "relaydeck.vault.get_secret",
        lambda k: "sk-openrouter-NEW" if k == "OPENROUTER_API_KEY" else None,
    )
    pi_engine._sync_native_pi_auth(tmp_path)

    data = json.loads(auth.read_text())
    assert data == {"openrouter": {"type": "api_key", "key": "sk-openrouter-NEW"}}
    assert "anthropic" not in data


def test_sync_native_pi_auth_unlinks_when_vault_empty(monkeypatch, tmp_path):
    """Operator revoked every provider key. Sync must NOT leave the last-known
    auth.json sitting on disk — a stale file could let pi authenticate with a
    key the operator believes they've revoked."""
    auth_dir = tmp_path / "runtime" / "native-pi-agent"
    auth_dir.mkdir(parents=True)
    auth = auth_dir / "auth.json"
    auth.write_text(json.dumps({"openrouter": {"type": "api_key", "key": "stale"}}))

    monkeypatch.setattr("relaydeck.vault.get_secret", lambda _: None)
    pi_engine._sync_native_pi_auth(tmp_path)

    assert not auth.exists(), "auth.json must be unlinked when vault is empty"


def test_sync_native_pi_auth_is_idempotent(monkeypatch, tmp_path):
    """Re-running sync without vault changes must NOT touch auth.json (no
    writes, no mtime bump). Cheapens the spawn path and avoids inotify
    storms if a watcher is listening."""
    monkeypatch.setattr("relaydeck.vault.get_secret", lambda k: "sk-test" if k == "OPENROUTER_API_KEY" else None)
    pi_engine._sync_native_pi_auth(tmp_path)
    auth = tmp_path / "runtime" / "native-pi-agent" / "auth.json"
    mtime_first = auth.stat().st_mtime_ns

    # Sleep a bit so any mtime change would actually register.
    time.sleep(0.01)
    pi_engine._sync_native_pi_auth(tmp_path)
    assert auth.stat().st_mtime_ns == mtime_first, "idempotent re-sync must not rewrite the file"


def test_pi_runtime_status_reports_install_state(monkeypatch):
    pi_engine.clear_pi_status_cache()
    monkeypatch.setattr("plugins.harnesses.relaydeck_native.pi_engine.shutil.which", lambda _: None)
    st = pi_engine.pi_runtime_status(force_refresh=True)
    assert st["pi_installed"] is False
    assert st["install_hint"]
    assert "pi_path" not in st

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return type("R", (), {"stdout": "pi 1.2.3", "stderr": ""})()

    monkeypatch.setattr("plugins.harnesses.relaydeck_native.pi_engine.shutil.which", lambda _: "/usr/bin/pi")
    monkeypatch.setattr("plugins.harnesses.relaydeck_native.pi_engine.subprocess.run", fake_run)
    pi_engine.clear_pi_status_cache()
    st2 = pi_engine.pi_runtime_status(force_refresh=True)
    assert st2["pi_installed"] is True
    assert st2["pi_version"] == "pi 1.2.3"
    assert pi_engine.pi_runtime_status()["pi_version"] == "pi 1.2.3"
    assert calls["n"] == 1


def test_build_pi_argv_honors_empty_tools_list(home):
    """Explicit `tools: []` must drop pi's built-in fs/bash tools.

    Pi's `--tools` is an allowlist — omitting it falls back to ALL default
    tools (read/write/bash/edit). Operator-set empty must translate to
    `--no-builtin-tools` so only the fleet extension remains active."""
    _write(home, "bare", tools=[])
    argv, _env, _cwd = pi_engine.build_pi_argv(home, "bare")
    assert "--tools" not in argv
    assert "--no-builtin-tools" in argv


def test_build_pi_argv_unset_tools_uses_defaults(home):
    """Unset `tools` keeps pi's default allowlist, NOT --no-builtin-tools."""
    _write(home, "stock")
    argv, _env, _cwd = pi_engine.build_pi_argv(home, "stock")
    assert "--no-builtin-tools" not in argv
    assert "--tools" in argv


def test_extension_path_is_packaged():
    assert pi_engine.extension_path().is_file()
    assert pi_engine.startup_extension_path().is_file()


def test_build_pi_argv_isolates_and_loads_startup_interactive(home):
    _write(home, "op", tools=["read", "relaydeck"])
    argv, env, _cwd = pi_engine.build_pi_argv(home, "op", interactive=True)
    joined = " ".join(argv)
    assert "--no-extensions" in argv
    assert "--no-skills" in argv
    assert "--no-themes" in argv
    assert "--no-prompt-templates" in argv
    assert "--no-context-files" in argv
    assert "--skill" not in argv
    assert "pi_startup.ts" in joined
    assert "pi_extension.ts" in joined
    assert env.get("PI_CODING_AGENT_DIR", "").endswith("native-pi-agent")
    startup = __import__("json").loads(env["RELAYDECK_STARTUP"])
    assert startup["agent_id"] == "op"
    assert startup["identity"] == "relaydeck-native"
    assert "relaydeck-startup" in startup["extensions"]


def test_build_pi_argv_skips_startup_in_print_mode(home):
    _write(home, "op")
    argv, _env, _cwd = pi_engine.build_pi_argv(home, "op", print_mode=True)
    joined = " ".join(argv)
    assert "pi_extension.ts" in joined
    assert "pi_startup.ts" not in joined
    assert "--no-extensions" in argv


def test_build_pi_argv_includes_extension_and_session(home):
    _write(home, "sup", preset="fast", tools=["read", "relaydeck", "manage"])
    argv, env, _cwd = pi_engine.build_pi_argv(home, "sup")
    assert argv[0] == "pi"
    assert "--session-dir" in argv
    assert any("native-sessions" in str(x) for x in argv)
    assert any("pi_extension.ts" in str(x) for x in argv)
    assert "relaydeck_message" in " ".join(argv)
    assert "relaydeck_agents" in " ".join(argv)
    assert env["RELAYDECK_AGENT_ID"] == "sup"
    assert env["RELAYDECK_TOOLS"] == "relaydeck,manage,dashboard"
    assert env["TERM_PROGRAM"] == "relaydeck-native"
    assert env["PI_CODING_AGENT_IDENTITY"] == "relaydeck-native"


def test_build_pi_argv_resolves_provider_model_via_harness_model(home):
    """relaydeck-native uses harness_model (not legacy preset cache)."""
    _write(home, "m1", model="openrouter/deepseek/deepseek-v4-pro")
    argv, _env, _cwd = pi_engine.build_pi_argv(home, "m1")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "openrouter/deepseek/deepseek-v4-pro"


def test_build_pi_argv_defaults_empty_config_to_global_fast_role(home):
    """Empty agent config must inherit role:fast — otherwise pi picks its own
    default provider and 401s despite a valid OpenRouter key in vault."""
    from relaydeck import model_roles as mr

    mr.set_role_default("fast", "openrouter/deepseek/deepseek-v4-flash", home)
    _write(home, "bare")
    argv, _env, _cwd = pi_engine.build_pi_argv(home, "bare")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "openrouter/deepseek/deepseek-v4-flash"


def test_build_pi_argv_syncs_vault_auth_on_every_build(home, monkeypatch):
    """Each argv build (chat turn + spawn) must refresh auth.json from vault."""
    from relaydeck import model_roles as mr

    mr.set_role_default("fast", "openrouter/deepseek/deepseek-v4-flash", home)
    monkeypatch.setattr(
        "relaydeck.vault.get_secret",
        lambda k: "sk-or-test" if k == "OPENROUTER_API_KEY" else None,
    )
    _write(home, "synced")
    pi_engine.build_pi_argv(home, "synced")
    auth = home / "runtime" / "native-pi-agent" / "auth.json"
    assert auth.is_file()
    data = json.loads(auth.read_text())
    assert data["openrouter"]["key"] == "sk-or-test"


def test_vault_set_secret_triggers_pi_auth_sync(monkeypatch, tmp_path):
    """Provider vault-key writes must push credentials into pi auth.json."""
    from plugins.vault.plugin import Vault
    from plugins.vault import plugin as vp
    from plugins.vault.store import YamlStore

    home = tmp_path / "cfg"
    home.mkdir()
    vp._vault = Vault(YamlStore(home / "vault.yaml"))
    synced: list[Path] = []
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.sync_native_pi_auth",
        lambda h: synced.append(h),
    )
    invalidated = {"n": 0}

    class _StubOrch:
        def invalidate_relaydeck_native_spawn_caches(self):
            invalidated["n"] += 1

    monkeypatch.setattr(
        "relaydeck.orchestrator.get_orchestrator",
        lambda _h: _StubOrch(),
    )

    vp.set_secret("OPENROUTER_API_KEY", "sk-test")
    assert synced == [home]
    assert invalidated["n"] == 1

    synced.clear()
    vp.set_secret("TELEGRAM_BOT_TOKEN", "123:abc")
    assert synced == []


def test_vault_set_emits_credentials_rotated_for_running_native_agents(
    monkeypatch, tmp_path,
):
    """Operator rotates a provider key → orch emits a stale-agent list so
    the dashboard can prompt for a restart (PTY child has old env baked in)."""
    from plugins.vault.plugin import Vault
    from plugins.vault import plugin as vp
    from plugins.vault.store import YamlStore

    home = tmp_path / "cfg"
    home.mkdir()
    vp._vault = Vault(YamlStore(home / "vault.yaml"))
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.sync_native_pi_auth",
        lambda _h: None,
    )

    events: list[tuple[str, dict]] = []

    class _StubBus:
        def emit(self, ev):
            events.append((ev.type, ev.data))

    class _StubOrch:
        _plugin_event_bus = _StubBus()
        def invalidate_relaydeck_native_spawn_caches(self):
            return ["op", "scout"]   # running stale agents

    monkeypatch.setattr(
        "relaydeck.orchestrator.get_orchestrator",
        lambda _h: _StubOrch(),
    )

    vp.set_secret("OPENROUTER_API_KEY", "sk-rotated")
    assert events
    name, data = events[0]
    assert name == "relaydeck-native.credentials_rotated"
    assert data["key"] == "OPENROUTER_API_KEY"
    assert set(data["stale_agent_ids"]) == {"op", "scout"}


def test_vault_no_event_when_no_running_native_agents(monkeypatch, tmp_path):
    """Empty stale list → no event (avoid noisy toast when no PTY is affected)."""
    from plugins.vault.plugin import Vault
    from plugins.vault import plugin as vp
    from plugins.vault.store import YamlStore

    home = tmp_path / "cfg"
    home.mkdir()
    vp._vault = Vault(YamlStore(home / "vault.yaml"))
    monkeypatch.setattr(
        "plugins.harnesses.relaydeck_native.pi_engine.sync_native_pi_auth",
        lambda _h: None,
    )

    events: list = []

    class _StubBus:
        def emit(self, ev):
            events.append(ev)

    class _StubOrch:
        _plugin_event_bus = _StubBus()
        def invalidate_relaydeck_native_spawn_caches(self):
            return []

    monkeypatch.setattr(
        "relaydeck.orchestrator.get_orchestrator",
        lambda _h: _StubOrch(),
    )

    vp.set_secret("OPENROUTER_API_KEY", "sk-rotated")
    assert events == []


def test_vault_route_through_daemon_happy_path(monkeypatch):
    """When the daemon is reachable, the vault CLI POSTs through it so the
    daemon's in-memory cache stays in sync (no stale-overwrite regression)."""
    import urllib.request

    from plugins.vault import plugin as vp

    seen: dict = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def _fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        return _Resp()

    monkeypatch.setattr("relaydeck.state.get_daemon_url", lambda: "http://127.0.0.1:8765")
    monkeypatch.setattr("relaydeck.state.get_daemon_ca", lambda: None)
    monkeypatch.setattr("relaydeck.auth.read_token", lambda: "tok-123")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    handled, err = vp._route_through_daemon("POST", "OPENROUTER_API_KEY", "sk-x")
    assert handled is True
    assert err is None
    assert seen["url"].endswith("/api/vault/keys/OPENROUTER_API_KEY")
    assert seen["method"] == "POST"
    assert b"sk-x" in seen["body"]


def test_vault_route_through_daemon_unreachable(monkeypatch):
    """Daemon down → returns (False, error) so the caller falls back to a
    direct disk write."""
    import urllib.error
    import urllib.request

    from plugins.vault import plugin as vp

    def _boom(req, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("relaydeck.state.get_daemon_url", lambda: "http://127.0.0.1:8765")
    monkeypatch.setattr("relaydeck.state.get_daemon_ca", lambda: None)
    monkeypatch.setattr("relaydeck.auth.read_token", lambda: None)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    handled, err = vp._route_through_daemon("DELETE", "OPENROUTER_API_KEY")
    assert handled is False
    assert err and "URLError" in err


def test_build_pi_argv_merges_dashboard_for_legacy_tool_list(home):
    """Agents created before dashboard was a default still get the tool."""
    _write(home, "legacy", tools=["read", "relaydeck"])
    argv, env, _cwd = pi_engine.build_pi_argv(home, "legacy")
    assert "relaydeck_dashboard" in " ".join(argv)
    assert "dashboard" in env["RELAYDECK_TOOLS"]


def test_resolved_tools_default_is_full_operator_set():
    from plugins.harnesses.relaydeck_native import tools as tools_mod
    assert pi_engine.resolved_tools({}) == set(tools_mod.TOOL_NAMES)


def test_resolved_tools_merges_fleet_minimum_for_explicit_list():
    assert pi_engine.resolved_tools({"tools": ["read", "relaydeck"]}) == {
        "read", "relaydeck", "dashboard",
    }
    assert pi_engine.resolved_tools({"tools": []}) == set()


def test_inject_provider_env_bridges_vault_key(home):
    """Vault keys must reach pi env — broken import silently skipped injection."""
    from plugins.vault import plugin as vp

    class _StubVault:
        def get(self, key):
            return "sk-test" if key == "DEEPSEEK_API_KEY" else None

    vp._vault = _StubVault()
    (home / "presets").mkdir(parents=True, exist_ok=True)
    import yaml
    (home / "presets" / "fast.yaml").write_text(yaml.safe_dump({
        "name": "fast", "provider": "deepseek", "model": "deepseek-v4-pro",
    }))
    env: dict[str, str] = {}
    pi_engine._inject_provider_env({"preset": "fast"}, env, home)
    assert env.get("DEEPSEEK_API_KEY") == "sk-test"


def test_parse_json_events_extracts_reply_and_tools():
    raw = "\n".join([
        '{"type":"agent_start"}',
        '{"type":"tool_execution_start","toolName":"read"}',
        '{"type":"tool_execution_end","toolName":"read","result":{"content":[{"type":"text","text":"ok"}]}}',
        '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Done reading."}]}}',
    ])
    reply, tools, _usage = pi_engine._parse_json_events(raw)
    assert reply == "Done reading."
    assert tools and tools[0]["calls"] == ["read"]
