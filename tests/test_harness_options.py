"""Per-harness launch-option catalog (`relaydeck/harness_options.py`).

The new-agent modal is type-first: the catalog drives which options each
harness shows (yolo/plan/continue/sandbox/extra-args), marks availability
from the live type registry, and surfaces linked external runtimes as
spawnable cards. These are the contract tests for that data.
"""

from __future__ import annotations

from relaydeck.harness_options import HARNESS_META, build_harness_catalog


def _by_type(catalog):
    return {e["type"]: e for e in catalog}


def _opt(entry, key):
    return next((o for o in entry["launch_options"] if o["key"] == key), None)


def test_catalog_lists_native_types_and_operator(tmp_path):
    cat = _by_type(build_harness_catalog(tmp_path))
    for t in ("claude-code", "codex-cli", "cursor-cli", "pi", "opencode-cli"):
        assert cat[t]["kind"] == "native"
    assert cat["relaydeck"]["kind"] == "operator"
    # operator has a bespoke section, not the generic launch grid
    assert cat["relaydeck"]["launch_options"] == []


def test_pi_is_first_class(tmp_path):
    cat = build_harness_catalog(tmp_path)
    # pi leads the order — it's the first-class harness.
    assert cat[0]["type"] == "pi"
    pi = cat[0]
    keys = {o["key"] for o in pi["launch_options"]}
    assert {"continue", "session", "thinking", "extra_args"} <= keys
    cont = _opt(pi, "continue")
    assert cont["apply"]["arg"] == "--continue"
    # thinking is a select that maps to an ARG (--thinking <level>), not a config key
    thinking = _opt(pi, "thinking")
    assert thinking["kind"] == "select" and thinking["apply"]["arg"] == "--thinking"


def test_every_native_type_offers_extra_args(tmp_path):
    cat = _by_type(build_harness_catalog(tmp_path))
    for _t, e in cat.items():
        if e["kind"] != "native":
            continue
        ea = _opt(e, "extra_args")
        assert ea is not None and ea["kind"] == "args"
        assert ea["apply"].get("args_passthrough") is True


def test_claude_options_cover_yolo_plan_continue_resume(tmp_path):
    e = _by_type(build_harness_catalog(tmp_path))["claude-code"]
    dangerous = _opt(e, "dangerous")
    assert dangerous["danger"] is True
    assert dangerous["apply"]["arg"] == "--dangerously-skip-permissions"
    assert _opt(e, "plan")["apply"]["arg"] == "--permission-mode plan"
    assert _opt(e, "continue")["apply"]["arg"] == "--continue"
    resume = _opt(e, "resume")
    assert resume["kind"] == "text"
    assert resume["apply"] == {"arg": "--resume", "from": "text"}


def test_native_providers_drive_compat_warning(tmp_path):
    cat = _by_type(build_harness_catalog(tmp_path))
    # CLIs that only speak one provider out of the box.
    assert cat["claude-code"]["native_providers"] == ["anthropic"]
    assert cat["codex-cli"]["native_providers"] == ["openai"]
    assert cat["claude-code"]["model_policy"] == "native"
    assert cat["codex-cli"]["model_policy"] == "native"
    assert cat["cursor-cli"]["model_policy"] == "native"
    assert cat["cursor-cli"]["model_config_key"] == "cursor_model"
    # pi/opencode bring their own provider auth → flex preset picker.
    assert cat["pi"]["model_policy"] == "flex"
    assert cat["opencode-cli"]["model_policy"] == "flex"
    assert cat["pi"]["native_providers"] is None
    assert cat["relaydeck"]["model_policy"] == "relaydeck"


def test_cursor_options_cover_plan_continue_resume_force(tmp_path):
    e = _by_type(build_harness_catalog(tmp_path))["cursor-cli"]
    assert e["cli"] == "cursor-agent"
    assert _opt(e, "plan")["apply"]["arg"] == "--plan"
    assert _opt(e, "ask")["apply"]["arg"] == "--mode ask"
    assert _opt(e, "continue")["apply"]["arg"] == "--continue"
    resume = _opt(e, "resume")
    assert resume["kind"] == "text" and resume["apply"] == {"arg": "--resume", "from": "text"}
    force = _opt(e, "force")
    assert force["danger"] is True and force["apply"]["arg"] == "--force"


def test_codex_options_use_config_keys(tmp_path):
    e = _by_type(build_harness_catalog(tmp_path))["codex-cli"]
    dangerous = _opt(e, "dangerous")
    assert dangerous["danger"] is True
    assert dangerous["apply"]["config"] == "dangerously_bypass_approvals_and_sandbox"
    sandbox = _opt(e, "sandbox")
    assert sandbox["kind"] == "select"
    assert sandbox["apply"]["config"] == "sandbox"
    assert {o["value"] for o in sandbox["options"]} >= {"workspace-write", "read-only"}


def test_opencode_continue_and_session(tmp_path):
    e = _by_type(build_harness_catalog(tmp_path))["opencode-cli"]
    assert _opt(e, "continue")["apply"]["config"] == "continue"
    assert _opt(e, "session")["apply"] == {"config": "session", "from": "text"}


def test_autonomy_option_on_prompting_harnesses_only(tmp_path):
    """The high-level Autonomy knob (config.autonomy) is offered by the CLI
    harnesses that have approval/sandbox prompts — claude/codex/opencode — so
    the web modal stays at CLI parity with `--config autonomy=...`. pi never
    prompts (no knob)."""
    cat = _by_type(build_harness_catalog(tmp_path))
    for t in ("claude-code", "codex-cli", "cursor-cli", "opencode-cli"):
        opt = _opt(cat[t], "autonomy")
        assert opt is not None, f"{t} should offer the autonomy option"
        assert opt["kind"] == "select"
        assert opt["apply"]["config"] == "autonomy"
        values = {o["value"] for o in opt["options"]}
        assert {"", "bypass", "locked", "manual"} == values
        # Default (first option) is the unset "auto" path.
        assert opt["options"][0]["value"] == ""
    # pi has no per-command approval, so no autonomy knob.
    assert _opt(cat["pi"], "autonomy") is None


def test_available_flag_reflects_type_registry(tmp_path):
    # `_TYPES` is a process-global the real plugins populate, so fully
    # control it here (snapshot → clear → restore) for a hermetic check.
    import relaydeck.orchestrator as orch

    saved = dict(orch._TYPES)
    try:
        orch._TYPES.clear()
        cat = _by_type(build_harness_catalog(tmp_path))
        assert cat["claude-code"]["available"] is False

        # Registering an alias ("claude") makes the "claude-code" card available.
        orch.register_agent_type("claude", object)
        cat = _by_type(build_harness_catalog(tmp_path))
        assert cat["claude-code"]["available"] is True
    finally:
        orch._TYPES.clear()
        orch._TYPES.update(saved)


def test_linked_external_runtime_becomes_a_card(tmp_path):
    from relaydeck.harness_options import (
        register_external_catalog_provider,
        unregister_external_catalog_provider,
    )
    from plugins.external_agents import store
    from plugins.external_agents.models import ExternalAgent
    from plugins.external_agents.plugin import _external_type_cards

    store.save_agent(
        tmp_path,
        ExternalAgent(id="hermes-1", kind="hermes", name="My Hermes", root="/tmp/h"),
    )

    # Core stays decoupled: no external cards until the external plugin
    # contributes its catalog provider (i.e. is loaded). Start from a clean
    # baseline regardless of whether another test left the provider registered.
    unregister_external_catalog_provider(_external_type_cards)
    before = [e for e in build_harness_catalog(tmp_path) if e["kind"] == "external"]
    assert before == []

    register_external_catalog_provider(_external_type_cards)
    try:
        cards = [e for e in build_harness_catalog(tmp_path) if e["kind"] == "external"]
    finally:
        unregister_external_catalog_provider(_external_type_cards)
    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "hermes"
    assert card["external_id"] == "hermes-1"
    assert card["external_kind"] == "hermes"
    assert card["label"] == "My Hermes"
    assert card["launch_options"] == []


def test_recommended_plugins_are_collaboration_essentials():
    from relaydeck.workspace_plugins import RECOMMENDED

    assert {"messaging", "skills"} <= RECOMMENDED


def test_meta_aliases_never_duplicate_primary_names():
    # Each alias must differ from every primary key so a registered alias
    # never produces a second card for the same harness.
    primaries = set(HARNESS_META)
    for _t, meta in HARNESS_META.items():
        for alias in meta.get("aliases", []):
            assert alias not in primaries, f"{alias} collides with a primary type"


def test_relaydeck_operator_uses_pi_cli(tmp_path, monkeypatch):
    import shutil
    cat = _by_type(build_harness_catalog(tmp_path))
    rd = cat["relaydeck"]
    assert rd["kind"] == "operator"
    assert rd["cli"] == "pi"
    monkeypatch.setattr(shutil, "which", lambda _: None)
    cat2 = _by_type(build_harness_catalog(tmp_path))
    assert cat2["relaydeck"]["cli_installed"] is False
    assert "install_hint" in cat2["relaydeck"]


def test_catalog_entries_are_json_serializable(tmp_path):
    import json

    json.dumps(build_harness_catalog(tmp_path))  # must not raise


# ── session intent: apply_session_mode (fresh vs. resume) ───────────


def test_apply_session_mode_fresh_strips_continue():
    from relaydeck.harness_options import apply_session_mode
    c = {"args": ["--continue", "--verbose"]}
    apply_session_mode("claude-code", c, "fresh")
    assert c["args"] == ["--verbose"]


def test_apply_session_mode_fresh_strips_flag_with_value():
    """A resume flag that carries an id (--resume <id>) loses both tokens."""
    from relaydeck.harness_options import apply_session_mode
    c = {"args": ["--resume", "abc123", "--verbose"]}
    apply_session_mode("claude-code", c, "fresh")
    assert c["args"] == ["--verbose"]


def test_apply_session_mode_resume_adds_continue():
    from relaydeck.harness_options import apply_session_mode
    c = {}
    apply_session_mode("claude-code", c, "resume")
    assert c["args"] == ["--continue"]


def test_apply_session_mode_resume_is_idempotent():
    from relaydeck.harness_options import apply_session_mode
    c = {"args": ["--continue"]}
    apply_session_mode("pi", c, "resume")
    assert c["args"].count("--continue") == 1


def test_apply_session_mode_config_key_harness():
    """codex resumes via a config key (resume_last), not a CLI arg."""
    from relaydeck.harness_options import apply_session_mode
    c = {}
    apply_session_mode("codex-cli", c, "resume")
    assert c.get("resume_last") is True
    apply_session_mode("codex-cli", c, "fresh")
    assert "resume_last" not in c


def test_apply_session_mode_alias_resolves_and_unknown_is_noop():
    from relaydeck.harness_options import apply_session_mode
    c = {"args": ["--continue"]}
    apply_session_mode("claude", c, "fresh")  # alias of claude-code
    assert c["args"] == []
    c2 = {"args": ["--continue"]}
    apply_session_mode("totally-unknown", c2, "fresh")  # graceful no-op
    assert c2["args"] == ["--continue"]
