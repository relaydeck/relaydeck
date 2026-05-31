"""
End-to-end coverage for cwd-scoped CLI commands.

`agent list`, `agent start --status`, `agent stop --status`,
`agent find`, `agent create`, `agent restart`, `workspace list`,
and `doctor` all key off the cwd-inferred workspace by default,
with `-A` / `--all-workspaces` (or explicit `--workspace`) to opt
out. These tests pin the resolution + opt-out semantics so a
future refactor can't quietly invert them.

We exercise the click commands via `CliRunner` against the real
orchestrator so the test surface mirrors what a user sees.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from relaydeck.transports.cli import main as cli


def _seed_two_workspaces(tmp_path: Path) -> tuple[Path, Path]:
    """Register `wa` (at tmp/code/wa) and `wb` (at tmp/code/wb)
    with two agents apiece. Returns their root paths so tests can
    cd in via the `cwd=` keyword on get_current_workspace, which
    the production code threads through every workspace-scoped
    command."""
    wa = tmp_path / "code" / "wa"
    wb = tmp_path / "code" / "wb"
    wa.mkdir(parents=True)
    wb.mkdir(parents=True)

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'[[workspace]]\nname = "wa"\npath = "{wa}"\nplugins = []\n'
        f'[[workspace]]\nname = "wb"\npath = "{wb}"\nplugins = []\n'
    )
    for nm in ("wa", "wb"):
        d = cfg / "workspaces" / nm
        d.mkdir(parents=True)
        (d / "agent.toml").write_text("[workspace]\nplugins = []\n")

    # Seed agents directly in the DB by going through the orchestrator
    # the same way `relaydeck agent create` does. Lazy import so we share
    # the test config_home.
    from relaydeck.orchestrator import get_orchestrator
    import relaydeck.orchestrator as _orch_mod
    _orch_mod._orchestrator = None  # reset singleton for the fresh home
    orch = get_orchestrator(cfg)
    for nm in ("a1", "a2"):
        orch.create_agent(
            agent_id=f"wa-{nm}", agent_type="pi", name=f"wa-{nm}",
            workspace="wa", config={}, auto_start=False,
        )
    for nm in ("b1", "b2"):
        orch.create_agent(
            agent_id=f"wb-{nm}", agent_type="pi", name=f"wb-{nm}",
            workspace="wb", config={}, auto_start=False,
        )
    return wa, wb


def _runner_in(workspace_path: Path, monkeypatch) -> CliRunner:
    """Build a CliRunner that simulates being `cd`'d into the given
    workspace path. `Path.cwd()` is what `get_current_workspace`
    reads, so we monkeypatch it for the test."""
    monkeypatch.setattr("relaydeck.state.Path.cwd", lambda: workspace_path)
    return CliRunner()


# ── agent list ─────────────────────────────────────────────────────


def test_agent_list_defaults_to_cwd_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["agent", "list"])
    assert result.exit_code == 0, result.output
    # Title carries the scope so the output is self-explaining.
    assert "Agents · wa" in result.output
    # Only wa's agents show up.
    assert "wa-a1" in result.output
    assert "wa-a2" in result.output
    assert "wb-b1" not in result.output
    assert "wb-b2" not in result.output


def test_agent_list_all_workspaces_flag_shows_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["agent", "list", "-A"])
    assert result.exit_code == 0, result.output
    assert "Agents · all workspaces" in result.output
    assert "wa-a1" in result.output
    assert "wb-b1" in result.output


def test_agent_list_explicit_workspace_overrides_cwd(tmp_path, monkeypatch):
    """If I'm in `wa` but pass `--workspace wb`, I see wb only.
    The explicit flag must beat both cwd and -A."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["agent", "list", "--workspace", "wb"])
    assert result.exit_code == 0, result.output
    assert "Agents · wb" in result.output
    assert "wb-b1" in result.output
    assert "wa-a1" not in result.output


def test_agent_list_quiet_mode_is_ids_only(tmp_path, monkeypatch):
    """`-q` skips the table and prints just IDs, one per line, so
    `xargs relaydeck agent start` works."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["agent", "list", "-q"])
    assert result.exit_code == 0
    ids = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
    assert set(ids) == {"wa-a1", "wa-a2"}


# ── agent find ─────────────────────────────────────────────────────


def test_agent_find_defaults_to_cwd_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    # No filter → all agents in wa, none in wb.
    result = runner.invoke(cli, ["agent", "find"])
    assert result.exit_code == 0, result.output
    assert "wa-a1" in result.output
    assert "wb-b1" not in result.output


def test_agent_find_all_workspaces_returns_global(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["agent", "find", "-A"])
    assert result.exit_code == 0, result.output
    assert "wa-a1" in result.output
    assert "wb-b1" in result.output


# ── agent create ───────────────────────────────────────────────────


def test_agent_create_defaults_workspace_to_cwd(tmp_path, monkeypatch):
    """With no --workspace flag, a new agent binds to the cwd-
    inferred workspace and the CLI prints a one-line notice so the
    user knows."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["agent", "create", "newbie", "--type", "pi"])
    assert result.exit_code == 0, result.output
    assert "newbie" in result.output
    assert "binding to workspace" in result.output

    from relaydeck.orchestrator import get_orchestrator
    orch = get_orchestrator(tmp_path / ".relaydeck")
    rows = [a for a in orch.list_agents() if a["id"] == "newbie"]
    assert rows
    assert rows[0]["workspace"] == "wa"


def test_agent_create_empty_workspace_means_workspaceless(tmp_path, monkeypatch):
    """`--workspace ""` explicitly opts out — the cwd-inferred
    default doesn't sneak back in."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(
        cli,
        ["agent", "create", "global1", "--type", "pi", "--workspace", ""],
    )
    assert result.exit_code == 0, result.output

    from relaydeck.orchestrator import get_orchestrator
    orch = get_orchestrator(tmp_path / ".relaydeck")
    rows = [a for a in orch.list_agents() if a["id"] == "global1"]
    assert rows
    assert not rows[0].get("workspace")


def test_agent_create_rejects_malformed_config_kv(tmp_path, monkeypatch):
    """`--config foo` (no `=`) is a user error — surface it
    cleanly, not as a traceback."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(
        cli,
        ["agent", "create", "bad", "--type", "pi", "--config", "missingequals"],
    )
    assert result.exit_code == 2
    assert "key=value" in result.output


# ── batch start filter ─────────────────────────────────────────────


def test_agent_start_with_explicit_ids_and_status_is_rejected(tmp_path, monkeypatch):
    """Mixing explicit ids with --status (a second selector) is
    ambiguous UX. The CLI must refuse with exit code 2, not
    silently do one or the other. (--workspace, a pure scope flag, is
    a harmless no-op with explicit ids and is allowed — see
    test_resolve_explicit_ids_with_workspace_is_allowed.)"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(
        cli, ["agent", "start", "wa-a1", "--status", "stopped"],
    )
    assert result.exit_code == 2
    assert "either explicit agent ids" in result.output.lower() or "or" in result.output


def test_resolve_explicit_ids_with_workspace_is_allowed():
    """agent ids are globally unique, so `agent stop <id>
    --workspace <ws>` is a harmless no-op, not an ambiguity. It must
    resolve to those ids (scope ignored), NOT error like ids+--status —
    which under suppressed stderr looked like a fast successful stop."""
    from relaydeck.transports.cli import _resolve_agent_ids

    assert _resolve_agent_ids(("alice", "bob"), None, "myapi", False) == ["alice", "bob"]
    # -A is still rejected with explicit ids (contradictory scope intent).
    # explicit ids + --status is still rejected (two selectors).
    import pytest
    for status, all_ws in ((None, True), ("stopped", False)):
        with pytest.raises(SystemExit) as exc:
            _resolve_agent_ids(("alice",), status, None, all_ws)
        assert exc.value.code == 2


def test_agent_start_dash_A_alone_is_rejected(tmp_path, monkeypatch):
    """The regression that prompted the audit: `relaydeck agent start -A`
    used to mass-start every agent in every workspace because `-A`
    was treated as both a scope expansion AND a selector. It must
    now require an explicit `--status` (or explicit ids) — exit 2,
    error tells the user to add a selector. Same for `--workspace`
    alone."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    # `-A` alone → no selector → exit 2.
    r1 = runner.invoke(cli, ["agent", "start", "-A"])
    assert r1.exit_code == 2, r1.output
    assert "no selector" in r1.output.lower()
    assert "--status" in r1.output  # the error tells them what to add

    # `--workspace foo` alone → same story.
    r2 = runner.invoke(cli, ["agent", "start", "--workspace", "wa"])
    assert r2.exit_code == 2, r2.output
    assert "no selector" in r2.output.lower()

    # And same for stop and restart — symmetry matters.
    r3 = runner.invoke(cli, ["agent", "stop", "-A"])
    assert r3.exit_code == 2
    r4 = runner.invoke(cli, ["agent", "restart", "-A"])
    assert r4.exit_code == 2


def test_agent_start_workspace_and_dash_A_conflict(tmp_path, monkeypatch):
    """--workspace and -A say opposite things about scope. Refuse
    rather than picking one silently."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(
        cli,
        ["agent", "start", "--status", "stopped", "--workspace", "wa", "-A"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


# ── workspace list shows source label ──────────────────────────────


def test_workspace_list_marks_cwd_active(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wa, _wb = _seed_two_workspaces(tmp_path)
    runner = _runner_in(wa, monkeypatch)

    result = runner.invoke(cli, ["workspace", "list"])
    assert result.exit_code == 0, result.output
    # The marker character + the source label must both be present.
    assert "●" in result.output
    assert "inferred from cwd" in result.output
