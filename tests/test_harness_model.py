"""Harness-type model policy — native vs flex vs relaydeck spawn paths."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from relaydeck.harness_model import (
    cli_model_for_agent,
    model_policy_for,
    validate_harness_model,
)
from plugins.harnesses.codex.agent import CodexAgent


def test_model_policy_native_flex_relaydeck():
    assert model_policy_for("codex-cli") == "native"
    assert model_policy_for("codex") == "native"
    assert model_policy_for("claude-code") == "native"
    assert model_policy_for("cursor-cli") == "native"
    assert model_policy_for("pi") == "flex"
    assert model_policy_for("opencode-cli") == "flex"
    assert model_policy_for("relaydeck") == "relaydeck"


def test_codex_cli_model_from_openai_preset(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir(parents=True)
    (presets / "codex.yaml").write_text(
        "name: codex\nprovider: openai\nmodel: gpt-5.3-codex\n"
    )
    cli = cli_model_for_agent(
        "codex-cli", {"preset": "codex"}, config_home=tmp_path,
    )
    assert cli == "gpt-5.3-codex"


def test_codex_rejects_deepseek_preset(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir(parents=True)
    (presets / "review.yaml").write_text(
        "name: review\nprovider: deepseek\nmodel: deepseek-v4-flash\n"
    )
    ok, err = validate_harness_model(
        "codex-cli", {"preset": "review"}, config_home=tmp_path,
    )
    assert ok is False
    assert err is not None
    assert err["reason"] == "native_provider_mismatch"
    assert "deepseek" in err["message"]


def test_codex_allows_codex_model_override(tmp_path):
    ok, err = validate_harness_model(
        "codex-cli", {"codex_model": "gpt-5.3-codex"}, config_home=tmp_path,
    )
    assert ok is True
    assert err is None


def test_cursor_rejects_relaydeck_preset(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir(parents=True)
    (presets / "hands.yaml").write_text(
        "name: hands\nprovider: deepseek\nmodel: deepseek-v4-pro\n"
    )
    ok, err = validate_harness_model(
        "cursor-cli", {"preset": "hands"}, config_home=tmp_path,
    )
    assert ok is False
    assert err["reason"] == "cursor_preset_ignored"


def test_cursor_allows_cursor_model_override(tmp_path):
    ok, err = validate_harness_model(
        "cursor-cli", {"cursor_model": "composer-2"}, config_home=tmp_path,
    )
    assert ok is True
    cli = cli_model_for_agent(
        "cursor-cli", {"cursor_model": "composer-2"}, config_home=tmp_path,
    )
    assert cli == "composer-2"


def test_pi_passes_provider_model(tmp_path):
    cli = cli_model_for_agent(
        "pi", {"preset": "ollama/llama3"}, config_home=tmp_path,
    )
    assert cli == "ollama/llama3"


def test_relaydeck_native_resolves_role_fast(tmp_path):
    from relaydeck import model_roles as mr

    mr.set_role_default("fast", "openrouter/deepseek/deepseek-v4-flash", tmp_path)
    cli = cli_model_for_agent(
        "relaydeck", {"model": "role:fast"}, config_home=tmp_path,
    )
    assert cli == "openrouter/deepseek/deepseek-v4-flash"


def test_relaydeck_native_unresolved_role_returns_none(tmp_path):
    # Regression: fresh install, operator hasn't picked a fast default yet.
    # `--model role:fast` would crash pi; harness must omit --model entirely
    # so pi falls back to its own configured default.
    cli = cli_model_for_agent(
        "relaydeck", {"model": "role:fast"}, config_home=tmp_path,
    )
    assert cli is None


def test_pi_unresolved_role_returns_none(tmp_path):
    cli = cli_model_for_agent(
        "pi", {"preset": "role:classifier"}, config_home=tmp_path,
    )
    assert cli is None


def test_native_harnesses_unresolved_role_returns_none(tmp_path):
    # Regression: the role-leak fix originally only covered flex/relaydeck.
    # Native harnesses (_native_cli_model) leaked the literal 'role:fast' as
    # --model when the role was unset → claude/codex would reject it at the
    # CLI. The central guard must drop it to None for native too.
    for htype in ("claude-code", "codex-cli", "cursor-cli"):
        cli = cli_model_for_agent(htype, {"model": "role:fast"}, config_home=tmp_path)
        assert cli is None, f"{htype} leaked unresolved role: {cli!r}"


def test_native_guard_fires_on_resolved_cross_provider_role(tmp_path):
    # A *resolved* role pointing at a non-native provider must still be
    # rejected by the native-provider guard (the unresolved-role short-circuit
    # must not bypass it).
    from relaydeck import model_roles as mr

    mr.set_role_default("fast", "deepseek/deepseek-chat", tmp_path)
    ok, err = validate_harness_model(
        "claude-code", {"model": "role:fast"}, config_home=tmp_path,
    )
    assert ok is False
    assert err["reason"] == "native_provider_mismatch"
    assert cli_model_for_agent("claude-code", {"model": "role:fast"}, config_home=tmp_path) is None

    # Same role on a flex harness routes fine.
    assert cli_model_for_agent("pi", {"model": "role:fast"}, config_home=tmp_path) == "deepseek/deepseek-chat"

    # And a role that resolves to the native provider is accepted.
    mr.set_role_default("fast", "anthropic/claude-sonnet-4-5", tmp_path)
    assert cli_model_for_agent("claude-code", {"model": "role:fast"}, config_home=tmp_path) == "claude-sonnet-4-5"


def test_codex_agent_spawn_blocks_deepseek_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    presets = cfg_home / "presets"
    presets.mkdir(parents=True)
    (presets / "bad.yaml").write_text(
        "name: bad\nprovider: deepseek\nmodel: deepseek-v4-flash\n"
    )
    ws = tmp_path / "repo"
    ws.mkdir()
    (cfg_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{ws}"\n'
    )
    agent = CodexAgent(
        agent_id="r", name="r", config={"preset": "bad"},
        workspace="demo", db_path=str(tmp_path / "db"), stop_flag=threading.Event(),
    )
    ok, err = agent._validate_preset()
    assert ok is False
    assert "deepseek" in err["message"]
