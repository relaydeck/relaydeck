"""
Vendor-side hook integrations (`relaydeck integration ...`).

Coverage:

  - Registry round-trip
  - Claude installer:
      * writes the script + makes it executable
      * registers entries in ~/.claude/settings.json for every
        mapped event
      * idempotent — re-install doesn't duplicate entries
      * preserves operator-owned hooks on the same events
  - Uninstaller:
      * removes the script
      * strips only our entries from settings.json
      * leaves operator hooks untouched
  - `is_installed` reflects partial / clean / fully-installed
    states
  - The rendered script contains all four event-to-status
    mappings (the contract the daemon endpoint relies on)
"""

from __future__ import annotations

import http.server
import json
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from relaydeck.integrations import (
    all_integrations,
    get,
    register_builtin_integrations,
)
from relaydeck.integrations.claude import _EVENT_MAPPING, ClaudeIntegration


def test_register_builtin_registers_claude():
    register_builtin_integrations()
    names = {it.name for it in all_integrations()}
    assert "claude" in names
    assert get("claude") is not None


def test_register_builtin_registers_classifier_sources():
    """Codex/pi/opencode don't have native hook systems —
    their semantic-status signal flows through the mood
    classifier. They appear in the registry as
    `kind="classifier"` so `relaydeck integration list` is honest
    about every harness's actual signal source, not just the
    ones that ship installable scripts."""
    register_builtin_integrations()
    by_name = {it.name: it for it in all_integrations()}
    for name in ("codex", "pi", "opencode"):
        assert name in by_name, f"missing classifier integration: {name}"
        assert by_name[name].kind == "classifier"


def test_classifier_install_is_safe_noop(tmp_path, monkeypatch):
    """`install()` for a classifier-source returns a human
    explanation and writes nothing to disk. Re-running it is
    safe — no files to duplicate."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    register_builtin_integrations()
    it = get("codex")
    assert it is not None
    msg1 = it.install()
    msg2 = it.install()
    assert msg1 == msg2
    assert "classifier" in msg1.lower()
    # No claude-style settings.json or script gets written.
    assert not (tmp_path / ".claude").exists()


def test_classifier_uninstall_is_safe_noop(tmp_path, monkeypatch):
    """No state on disk to remove — uninstall must report
    `False` (nothing-to-do) without raising."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    register_builtin_integrations()
    it = get("pi")
    assert it is not None
    assert it.uninstall() is False


def test_claude_kind_is_hook():
    """Drift-pin: the only `hook` integration shipped today is
    claude. If a new hook integration lands, this test should be
    updated alongside the registration — that way the
    `hook`/`classifier` split stays an explicit decision."""
    register_builtin_integrations()
    assert get("claude").kind == "hook"  # type: ignore[union-attr]


# ── Claude installer ──────────────────────────────────────────────


def test_install_writes_executable_script(tmp_path, monkeypatch):
    """The hook script must land at the canonical path and be
    executable — Claude Code spawns it directly, so without +x it
    would fail with ENOEXEC on the operator's first tool use."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    it = ClaudeIntegration()
    path = it.install()
    assert Path(path).exists()
    mode = Path(path).stat().st_mode
    assert mode & stat.S_IXUSR, "script must be executable"


def _nested_commands(entry: dict) -> list[str]:
    return [
        h.get("command")
        for h in entry.get("hooks") or []
        if isinstance(h, dict)
    ]


def test_install_registers_entries_for_every_event(tmp_path, monkeypatch):
    """settings.json must end up with one hook entry per mapped
    event, all pointing at our script. The mapping table is the
    contract — every event we promise to surface must actually be
    wired."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    it = ClaudeIntegration()
    script_path = it.install()

    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    hooks = data["hooks"]

    for event, _ in _EVENT_MAPPING:
        assert event in hooks, f"missing event {event}"
        commands = [c for e in hooks[event] for c in _nested_commands(e)]
        assert script_path in commands


def test_install_writes_claude_code_nested_schema(tmp_path, monkeypatch):
    """Drift-pin: Claude Code's hook schema is matcher + nested
    `hooks` array of typed command entries. The legacy flat shape
    `{command, matcher}` triggers /doctor warnings and is silently
    ignored by newer Claude Code. Pinned at the on-disk level so a
    refactor can't quietly regress."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    script_path = ClaudeIntegration().install()
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())

    for event, _ in _EVENT_MAPPING:
        entries = data["hooks"][event]
        ours = [e for e in entries if script_path in _nested_commands(e)]
        assert len(ours) == 1, f"expected one entry for {event}, got {entries}"
        entry = ours[0]
        assert "matcher" in entry
        assert isinstance(entry["hooks"], list)
        inner = entry["hooks"][0]
        assert inner == {"type": "command", "command": script_path}
        # Legacy flat key must not be present on our entry.
        assert "command" not in entry


def test_install_is_idempotent(tmp_path, monkeypatch):
    """Re-running `install` doesn't duplicate hook entries — the
    second run sees its own command already present and skips."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    it = ClaudeIntegration()
    it.install()
    it.install()  # second run
    it.install()  # third for good measure

    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    for event, _ in _EVENT_MAPPING:
        entries = data["hooks"][event]
        ours = [
            e for e in entries
            if any("relaydeck-state.sh" in (c or "") for c in _nested_commands(e))
        ]
        assert len(ours) == 1, f"duplicate entries for {event}: {entries}"


def test_install_preserves_operator_hooks(tmp_path, monkeypatch):
    """If the operator has an existing hook on PreToolUse (some
    other tool, their own script), our installer must add to that
    list, not overwrite it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Seed an operator-owned hook before installing.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"command": "/some/operator-script.sh", "matcher": "*"}
            ]
        }
    }))

    ClaudeIntegration().install()
    data = json.loads(settings.read_text())
    pre = data["hooks"]["PreToolUse"]
    # Operator's pre-existing flat-shape hook stays where it was;
    # our entry is appended in the nested shape next to it.
    operator_commands = [e.get("command") for e in pre]
    nested_commands = [c for e in pre for c in _nested_commands(e)]
    assert "/some/operator-script.sh" in operator_commands
    assert any("relaydeck-state.sh" in (c or "") for c in nested_commands)


# ── Uninstaller ───────────────────────────────────────────────────


def test_uninstall_removes_script_and_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    it = ClaudeIntegration()
    it.install()
    assert it.is_installed() is True

    removed = it.uninstall()
    assert removed is True
    assert it.is_installed() is False

    # Script gone.
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    assert not script.exists()

    # And settings.json no longer references our script — neither
    # at the top-level command key (legacy) nor inside the nested
    # hooks array (current schema).
    settings = tmp_path / ".claude" / "settings.json"
    if settings.exists():
        data = json.loads(settings.read_text())
        target = str(script)
        for event, _ in _EVENT_MAPPING:
            for entry in data.get("hooks", {}).get(event, []):
                assert entry.get("command") != target
                assert target not in _nested_commands(entry)


def test_uninstall_leaves_operator_hooks(tmp_path, monkeypatch):
    """Operator-owned hooks on the same events must survive
    uninstall."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    it = ClaudeIntegration()
    it.install()

    # Add an operator hook AFTER our install.
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["hooks"].setdefault("PreToolUse", []).append({
        "command": "/operator/own.sh", "matcher": "*",
    })
    settings.write_text(json.dumps(data, indent=2))

    it.uninstall()
    data = json.loads(settings.read_text())
    pre = data.get("hooks", {}).get("PreToolUse", [])
    operator = [e for e in pre if e.get("command") == "/operator/own.sh"]
    assert len(operator) == 1, (
        "operator's own PreToolUse hook should survive uninstall"
    )


def test_uninstall_when_not_installed_is_safe_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert ClaudeIntegration().uninstall() is False


def test_state_orphaned_registration_when_script_deleted(tmp_path, monkeypatch):
    """Deleting the hook script but leaving settings.json entries is visible."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    script.unlink()
    assert it.state() == "orphaned-registration"
    assert it.is_installed() is False


def test_state_orphaned_script_when_settings_missing(tmp_path, monkeypatch):
    """A script on disk without vendor registration is partial/orphaned."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    (tmp_path / ".claude" / "settings.json").unlink()
    assert it.state() == "orphaned-script"
    assert it.is_installed() is False


def test_state_outdated_when_hook_body_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    script.write_text("#!/bin/sh\n# stale body\n")
    assert it.state() == "outdated"
    assert it.is_installed() is False


def test_uninstall_cleans_orphaned_registration_without_script(tmp_path, monkeypatch):
    """uninstall() strips settings.json even when the script is already gone."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    script.unlink()
    assert it.state() == "orphaned-registration"
    assert it.uninstall() is True
    assert it.state() == "not-installed"
    settings = tmp_path / ".claude" / "settings.json"
    if settings.exists():
        data = json.loads(settings.read_text())
        blob = json.dumps(data.get("hooks", {}))
        assert "relaydeck-state.sh" not in blob


def test_uninstall_hook_integrations_before_config_removal(tmp_path, monkeypatch):
    from relaydeck.integrations import (
        register_builtin_integrations,
        uninstall_hook_integrations_before_config_removal,
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    register_builtin_integrations()
    ClaudeIntegration().install()
    removed = uninstall_hook_integrations_before_config_removal()
    assert removed == ["claude"]
    assert ClaudeIntegration().state() == "not-installed"


def test_fix_orphaned_hook_integrations_uninstalls_orphan(tmp_path, monkeypatch):
    """orphaned-registration (settings.json wired but script gone) heals
    by uninstalling — there's no script body to regenerate, and the
    registration points at nothing."""
    from relaydeck.integrations import fix_orphaned_hook_integrations

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    (tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh").unlink()
    fixed = fix_orphaned_hook_integrations()
    assert fixed == [("claude", "uninstalled")]
    assert it.state() == "not-installed"


def test_fix_orphaned_hook_integrations_regenerates_outdated(tmp_path, monkeypatch):
    """outdated (both halves wired, script body stale) heals by REINSTALL,
    not uninstall. An uninstall here would silently disable telemetry
    until the next claude-code spawn re-installs via _ensure_state_hook —
    bad behavior for a --fix flag. The idempotent install() rewrites the
    script body and leaves settings.json untouched."""
    from relaydeck.integrations import fix_orphaned_hook_integrations

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    # Stub a stale body — any text that doesn't match _render_script().
    script.write_text("#!/bin/sh\n# stale body — not a real hook\n")
    assert it.state() == "outdated"

    fixed = fix_orphaned_hook_integrations()
    assert fixed == [("claude", "regenerated")]
    # Script is back to the rendered template (state recovers to installed)
    # AND the settings.json registration was never touched.
    assert it.state() == "installed"
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "hooks" in settings


def test_fix_orphaned_hook_integrations_skips_classifier_kind(tmp_path, monkeypatch):
    """Classifier integrations never have a script-on-disk to fix — the
    helper must skip them, not crash."""
    from relaydeck.integrations import (
        fix_orphaned_hook_integrations,
        register_builtin_integrations,
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    register_builtin_integrations()
    # Without a claude install + no orphans, fix is a no-op across all
    # registered integrations (incl. the four classifier ones).
    assert fix_orphaned_hook_integrations() == []


def test_installed_hook_integrations_lists_installed_and_drifted(tmp_path, monkeypatch):
    """The daemon-stop hint reads this to decide whether to remind the
    operator about `cleanup-all` before `rm -rf ~/.relaydeck`. It must
    include states with VENDOR-SIDE traces — installed, outdated,
    orphaned-registration — and exclude classifier kinds, clean
    not-installed, AND orphaned-script (script-only, no vendor entry to
    clean)."""
    from relaydeck.integrations import installed_hook_integrations

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Clean home — nothing installed.
    assert installed_hook_integrations() == []

    it = ClaudeIntegration()
    it.install()
    assert installed_hook_integrations() == ["claude"]

    # Outdated: still has settings.json traces, still must surface.
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    script.write_text("#!/bin/sh\n# stale\n")
    assert it.state() == "outdated"
    assert installed_hook_integrations() == ["claude"]

    # Orphaned-registration: script gone, settings.json still wired.
    script.unlink()
    assert it.state() == "orphaned-registration"
    assert installed_hook_integrations() == ["claude"]

    # Fully uninstalled: nothing left, no hint.
    it.uninstall()
    assert installed_hook_integrations() == []


def test_installed_hook_integrations_excludes_orphaned_script(tmp_path, monkeypatch):
    """`orphaned-script` is the script-on-disk-but-no-settings.json case.
    A `rm -rf ~/.relaydeck` deletes the script and there's nothing on the
    vendor side to clean up afterwards — so the daemon-stop hint pointing
    at `cleanup-all` would just be noise. Predicate must exclude it."""
    from relaydeck.integrations import installed_hook_integrations

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    # Strip the vendor side only — keeps the script, drops the wiring.
    (tmp_path / ".claude" / "settings.json").unlink()
    assert it.state() == "orphaned-script"
    assert installed_hook_integrations() == [], (
        "orphaned-script has no vendor trace; daemon-stop hint must not fire"
    )


def test_fix_orphaned_hook_integrations_records_install_failure(tmp_path, monkeypatch):
    """When `install()` raises (e.g. permission denied on the script
    write), `fix_orphaned_hook_integrations` must record the failure with
    a `regenerate-failed` action — not silently drop it. The doctor's
    --fix output then prints an explicit failure line instead of a stale
    warning followed by the same fix hint the operator just tried."""
    from relaydeck.integrations import fix_orphaned_hook_integrations

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    script.write_text("#!/bin/sh\n# stale\n")
    assert it.state() == "outdated"

    # Force install() to raise (simulates a permission / disk error).
    boom = OSError("simulated write failure")
    monkeypatch.setattr(ClaudeIntegration, "install", lambda self: (_ for _ in ()).throw(boom))

    fixed = fix_orphaned_hook_integrations()
    assert fixed == [("claude", "regenerate-failed")], (
        f"failure path should record `regenerate-failed`, got {fixed!r}"
    )


def test_fix_orphaned_hook_integrations_records_uninstall_failure(tmp_path, monkeypatch):
    """Symmetric to the regenerate-failed test: an uninstall() raise
    surfaces as `uninstall-failed` rather than silent drop."""
    from relaydeck.integrations import fix_orphaned_hook_integrations

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    it = ClaudeIntegration()
    it.install()
    (tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh").unlink()
    assert it.state() == "orphaned-registration"

    boom = OSError("simulated unlink failure")
    monkeypatch.setattr(ClaudeIntegration, "uninstall", lambda self: (_ for _ in ()).throw(boom))

    fixed = fix_orphaned_hook_integrations()
    assert fixed == [("claude", "uninstall-failed")]


def test_prepare_config_home_removal(tmp_path, monkeypatch):
    from relaydeck import maintenance

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ClaudeIntegration().install()
    removed = maintenance.prepare_config_home_removal()
    assert removed == ["claude"]


def test_uninstall_cleans_legacy_flat_entries(tmp_path, monkeypatch):
    """Earlier versions of this installer wrote the flat shape
    `{command, matcher}`. Operators upgrading from those versions
    will have malformed entries in settings.json that newer Claude
    Code rejects. Uninstall must recognise and strip them so a
    re-install lands a clean nested-shape entry."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    script = tmp_path / ".relaydeck" / "integrations" / "claude" / "relaydeck-state.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")

    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            event: [{"command": str(script), "matcher": "*"}]
            for event, _ in _EVENT_MAPPING
        }
    }))

    removed = ClaudeIntegration().uninstall()
    assert removed is True
    data = json.loads(settings.read_text())
    assert "hooks" not in data, (
        f"legacy entries should be fully cleaned up, got {data}"
    )


# ── Script contents ───────────────────────────────────────────────


def test_rendered_script_contains_every_event_mapping(tmp_path, monkeypatch):
    """The mapping table in the integration module is the source
    of truth for which events map to which semantic status. The
    rendered script must include every entry, because the daemon
    endpoint validates against the semantic-status enum and a
    drift would silently lose telemetry on one event."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    it = ClaudeIntegration()
    path = Path(it.install())
    body = path.read_text()
    for event, status in _EVENT_MAPPING:
        assert event in body, f"script missing event {event}"
        assert status in body, f"script missing status {status}"


# ── Auto-install on spawn + the hook actually working ────────────────────


def test_claude_code_agent_auto_installs_hook_on_spawn(tmp_path, monkeypatch):
    """A claude-code agent installs the status hook itself (idempotently) on
    spawn, so live semantic status works without a manual `relaydeck
    integration install claude`. Opt-out via RELAYDECK_DISABLE_HOOK_AUTOINSTALL."""
    from plugins.harnesses.claude_code.agent import ClaudeCodeAgent

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_DISABLE_HOOK_AUTOINSTALL", raising=False)

    def _agent():
        return ClaudeCodeAgent(
            agent_id="probe", name="probe", config={}, workspace="ws",
            db_path=str(tmp_path / "x.db"), stop_flag=threading.Event(),
        )

    assert not ClaudeIntegration().is_installed()
    _agent()._ensure_state_hook()
    assert ClaudeIntegration().is_installed(), "spawn should auto-install the hook"
    _agent()._ensure_state_hook()  # idempotent — no error, no duplicate
    assert ClaudeIntegration().is_installed()

    # Opt-out: with the env set, a fresh home stays clean.
    monkeypatch.setenv("RELAYDECK_DISABLE_HOOK_AUTOINSTALL", "1")
    home2 = tmp_path / "home2"
    home2.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home2)
    _agent()._ensure_state_hook()
    assert not ClaudeIntegration().is_installed()


def _stub_state_server():
    """A throwaway HTTP server that records POSTs (path/body/auth) — stands in
    for the daemon's /api/agents/<id>/state endpoint."""
    captured: list[dict] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n).decode()
            captured.append({
                "path": self.path, "body": body,
                "auth": self.headers.get("Authorization"),
            })
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], captured


def test_claude_state_hook_script_posts_status(tmp_path, monkeypatch):
    """The installed hook script, run for each Claude Code event, POSTs the
    mapped semantic status (source=hook, with the auth token) to
    /api/agents/<id>/state — and no-ops when launched outside relaydeck (no
    agent id) so a user's own Claude sessions aren't broken."""
    if not (shutil.which("bash") and shutil.which("curl")):
        pytest.skip("bash + curl required to run the hook script")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = ClaudeIntegration().install()

    srv, port, captured = _stub_state_server()
    try:
        import os
        env = {
            **os.environ,
            "RELAYDECK_AGENT_ID": "probe",
            "RELAYDECK_DAEMON_URL": f"http://127.0.0.1:{port}",
            "RELAYDECK_AUTH_TOKEN": "tok",
        }
        env.pop("CLAUDE_AGENT_ID", None)
        for event, status in _EVENT_MAPPING:
            captured.clear()
            # Arg-form fallback (no stdin payload): the script still resolves
            # the event from $1. DEVNULL stdin so the `! -t 0` cat path sees EOF.
            subprocess.run(
                ["bash", script, event], env=env, timeout=10, check=True,
                stdin=subprocess.DEVNULL,
            )
            assert len(captured) == 1, f"{event}: expected one POST, got {captured}"
            req = captured[0]
            assert req["path"] == "/api/agents/probe/state"
            assert json.loads(req["body"]) == {"status": status, "source": "hook"}
            assert req["auth"] == "Bearer tok"

        # No agent id in env → no POST (Claude run outside relaydeck).
        captured.clear()
        no_id = {k: v for k, v in env.items() if k != "RELAYDECK_AGENT_ID"}
        subprocess.run(
            ["bash", script, "PreToolUse"], env=no_id, timeout=10, check=True,
            stdin=subprocess.DEVNULL,
        )
        assert captured == []

        # Unknown event → no POST.
        captured.clear()
        subprocess.run(
            ["bash", script, "SomethingElse"], env=env, timeout=10, check=True,
            stdin=subprocess.DEVNULL,
        )
        assert captured == []
    finally:
        srv.shutdown()


def test_claude_state_hook_reads_event_from_stdin_json(tmp_path, monkeypatch):
    """Regression pin for the real Claude Code invocation: Claude passes the
    hook payload as JSON on **stdin** (`hook_event_name`), NOT as a positional
    arg. An older script keyed off $1 — which Claude never sets — so status
    updates silently never fired. The script must parse the event from stdin."""
    if not (shutil.which("bash") and shutil.which("curl")):
        pytest.skip("bash + curl required to run the hook script")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = ClaudeIntegration().install()

    srv, port, captured = _stub_state_server()
    try:
        import os
        env = {
            **os.environ,
            "RELAYDECK_AGENT_ID": "probe",
            "RELAYDECK_DAEMON_URL": f"http://127.0.0.1:{port}",
            "RELAYDECK_AUTH_TOKEN": "tok",
        }
        env.pop("CLAUDE_AGENT_ID", None)
        # Exactly the compact JSON Claude Code 2.x writes to the hook's stdin
        # (no positional arg at all).
        for event, status in _EVENT_MAPPING:
            captured.clear()
            payload = json.dumps({
                "session_id": "abc",
                "cwd": str(tmp_path),
                "permission_mode": "bypassPermissions",
                "hook_event_name": event,
            })
            subprocess.run(
                ["bash", script], env=env, timeout=10, check=True,
                input=payload.encode(),
            )
            assert len(captured) == 1, (
                f"{event}: stdin-JSON event not parsed; got {captured}"
            )
            assert json.loads(captured[0]["body"]) == {"status": status, "source": "hook"}

        # Unknown event on stdin → no POST.
        captured.clear()
        subprocess.run(
            ["bash", script], env=env, timeout=10, check=True,
            input=json.dumps({"hook_event_name": "SomethingElse"}).encode(),
        )
        assert captured == []
    finally:
        srv.shutdown()


def test_claude_notification_idle_vs_permission(tmp_path, monkeypatch):
    """Claude fires `Notification` for both a real approval prompt AND the idle
    "waiting for your input" reminder. relaydeck spawns claude with
    --dangerously-skip-permissions, so a finished agent must read `idle`, not be
    stuck at `awaiting-input` (which outranks idle in the health roll-up). The
    script distinguishes them by the message text."""
    if not (shutil.which("bash") and shutil.which("curl")):
        pytest.skip("bash + curl required to run the hook script")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = ClaudeIntegration().install()

    srv, port, captured = _stub_state_server()
    try:
        import os
        env = {
            **os.environ,
            "RELAYDECK_AGENT_ID": "probe",
            "RELAYDECK_DAEMON_URL": f"http://127.0.0.1:{port}",
            "RELAYDECK_AUTH_TOKEN": "tok",
        }
        env.pop("CLAUDE_AGENT_ID", None)

        cases = [
            ("Claude is waiting for your input", "idle"),
            ("Claude needs your permission to use Bash", "awaiting-input"),
        ]
        for message, expected in cases:
            captured.clear()
            payload = json.dumps({"hook_event_name": "Notification", "message": message})
            subprocess.run(
                ["bash", script], env=env, timeout=10, check=True,
                input=payload.encode(),
            )
            assert len(captured) == 1, f"{message!r}: expected one POST"
            assert json.loads(captured[0]["body"])["status"] == expected, (
                f"{message!r} should map to {expected}"
            )
    finally:
        srv.shutdown()
