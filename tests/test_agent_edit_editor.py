"""
`relaydeck agent edit` editor flow.

Defaults to opening $EDITOR (falling back to vi) on the editable
subset of the agent's YAML. Flag-driven updates still skip the editor.
`--show` forces the old print-current behaviour. Non-TTY stdout also
forces print-current so piped invocations don't hang in vi.
"""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.config import AgentSpec
from relaydeck.transports.cli import main as cli


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".relaydeck"
    (cfg / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    return cfg


def _make_spec(cfg, agent_id="alice", purpose="orig purpose",
               tags=("a", "b"), system_prompt="orig prompt"):
    # Go through the CLI so the DB mirror gets populated --
    # update_agent_meta() expects the agent row to exist.
    args = ["agent", "create", agent_id, "--type", "pi",
            "--purpose", purpose,
            "--system-prompt", system_prompt]
    for t in tags:
        args.extend(["--tag", t])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    return AgentSpec.from_yaml(cfg / "agents" / f"{agent_id}.yaml")


def test_edit_no_flags_opens_editor_when_tty(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg)

    edited_payload = (
        "purpose: new purpose\n"
        "tags:\n  - x\n  - y\n"
        "inject_identity_preamble: true\n"
        "system_prompt: |\n  new prompt\n"
    )
    monkeypatch.setattr("click.edit", lambda *a, **kw: edited_payload)
    # Force the isatty branch in the cli even though pytest's stdout
    # isn't a real terminal.
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: True)

    result = CliRunner().invoke(cli, ["agent", "edit", "alice"])
    assert result.exit_code == 0, result.output

    spec = AgentSpec.from_yaml(cfg / "agents" / "alice.yaml")
    assert spec.purpose == "new purpose"
    assert spec.tags == ["x", "y"]
    assert spec.system_prompt.strip() == "new prompt"


def test_edit_with_flags_skips_editor(tmp_path, monkeypatch):
    """Flag-driven updates must not open an editor -- automation
    (CI, scripts, agent self-introduction via PTY) depends on this."""
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg)
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: True)

    called: list[bool] = []

    def _fail_if_called(*a, **kw):
        called.append(True)
        return None

    monkeypatch.setattr("click.edit", _fail_if_called)
    result = CliRunner().invoke(
        cli, ["agent", "edit", "alice", "--purpose", "via flag"]
    )
    assert result.exit_code == 0, result.output
    assert called == [], "editor must not open when --purpose was given"

    spec = AgentSpec.from_yaml(cfg / "agents" / "alice.yaml")
    assert spec.purpose == "via flag"


def test_edit_show_prints_current_without_editor(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg, system_prompt="hello world")
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: True)
    monkeypatch.setattr(
        "click.edit",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("editor opened")),
    )

    result = CliRunner().invoke(cli, ["agent", "edit", "alice", "--show"])
    assert result.exit_code == 0, result.output
    assert "purpose" in result.output
    assert "hello world" in result.output


def test_edit_non_tty_prints_current_without_editor(tmp_path, monkeypatch):
    """Piped / non-TTY invocations must not hang waiting for an editor."""
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg, system_prompt="hello world")
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: False)
    monkeypatch.setattr(
        "click.edit",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("editor opened")),
    )

    result = CliRunner().invoke(cli, ["agent", "edit", "alice"])
    assert result.exit_code == 0, result.output
    assert "purpose" in result.output


def test_edit_show_escapes_rich_markup_in_system_prompt(tmp_path, monkeypatch):
    """rich treats `[...]` as markup -- a system_prompt mentioning
    e.g. `[relay from=...]` used to get swallowed by the renderer.
    Pinned because that bracket pattern is the canonical
    chat-block format taught by the messaging skill."""
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg, system_prompt="reply to [relay from=alice id=x] like so")
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: True)

    result = CliRunner().invoke(cli, ["agent", "edit", "alice", "--show"])
    assert result.exit_code == 0, result.output
    assert "[relay from=alice id=x]" in result.output


def test_edit_aborts_when_editor_returns_none(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg)
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: True)
    monkeypatch.setattr("click.edit", lambda *a, **kw: None)

    result = CliRunner().invoke(cli, ["agent", "edit", "alice"])
    assert result.exit_code == 0
    assert "no changes" in result.output.lower()
    spec = AgentSpec.from_yaml(cfg / "agents" / "alice.yaml")
    assert spec.purpose == "orig purpose"


def test_edit_rejects_invalid_yaml_from_editor(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    _make_spec(cfg)
    monkeypatch.setattr("relaydeck.transports.cli._stdout_isatty", lambda: True)
    monkeypatch.setattr("click.edit", lambda *a, **kw: "purpose: : :")

    result = CliRunner().invoke(cli, ["agent", "edit", "alice"])
    assert result.exit_code == 0
    assert "invalid yaml" in result.output.lower()
    spec = AgentSpec.from_yaml(cfg / "agents" / "alice.yaml")
    assert spec.purpose == "orig purpose"
