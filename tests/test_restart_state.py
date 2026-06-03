"""Restart-to-apply detection (relaydeck/restart_state.py).

Spawn captures a config snapshot; later edits to spawn-time inputs (prompt,
plugins, skills, model, launch flags) should surface as a pending restart with
a precise reason. These tests exercise the real compose_prompt_components reuse
path (same gating as the harness), not a mock.
"""

from pathlib import Path

from relaydeck import restart_state as rs
from relaydeck.config import AgentSpec, register_workspace
from relaydeck.db import open_db


def _db(home: Path) -> str:
    p = home / "runtime" / "relaydeck.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    open_db(str(p)).close()
    return str(p)


def _ws(home: Path, name: str, plugins) -> Path:
    src = home / "src" / name
    src.mkdir(parents=True, exist_ok=True)
    register_workspace(home, name, src, plugins)
    return home / "workspaces" / name


def _skill(ws: Path, name: str, body: str = "body") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}")


def test_no_change_is_not_pending(tmp_path):
    home = tmp_path / ".relaydeck"
    db = _db(home)
    _ws(home, "w", ["skills"])
    a = rs.compute_snapshot(home, db, agent_id="a", workspace="w", system_prompt="hi")
    b = rs.compute_snapshot(home, db, agent_id="a", workspace="w", system_prompt="hi")
    assert rs.restart_state(a, b)["restart_pending"] is False


def test_added_skill_and_prompt_change_detected(tmp_path):
    home = tmp_path / ".relaydeck"
    db = _db(home)
    ws = _ws(home, "w", ["skills"])
    _skill(ws, "alpha")
    base = rs.compute_snapshot(home, db, agent_id="a", workspace="w", system_prompt="hi")

    _skill(ws, "beta")
    after = rs.compute_snapshot(home, db, agent_id="a", workspace="w", system_prompt="hi")
    st = rs.restart_state(base, after)
    assert st["restart_pending"] is True
    assert {"component": "skills", "summary": "skill beta added"} in st["pending_changes"]

    changed = rs.compute_snapshot(home, db, agent_id="a", workspace="w", system_prompt="DIFFERENT")
    assert {"component": "prompt", "summary": "system prompt changed"} in rs.diff_snapshots(base, changed)


def test_skill_content_edit_detected(tmp_path):
    home = tmp_path / ".relaydeck"
    db = _db(home)
    ws = _ws(home, "w", ["skills"])
    _skill(ws, "alpha", body="v1")
    base = rs.compute_snapshot(home, db, agent_id="a", workspace="w")
    _skill(ws, "alpha", body="v2 edited")
    after = rs.compute_snapshot(home, db, agent_id="a", workspace="w")
    assert any(c["summary"] == "skill alpha edited" for c in rs.restart_state(base, after)["pending_changes"])


def test_skills_gate_removal_drops_user_skills(tmp_path):
    home = tmp_path / ".relaydeck"
    db = _db(home)
    ws = _ws(home, "w", ["skills"])
    _skill(ws, "alpha")
    with_gate = rs.compute_snapshot(home, db, agent_id="a", workspace="w")
    assert "alpha" in with_gate["skills"]
    # Disable the `skills` gate (the only thing _workspace_plugins reads) —
    # user-authored skills must drop out of the desired snapshot.
    (ws / "agent.toml").write_text('[workspace]\nplugins = []\n')
    without = rs.compute_snapshot(home, db, agent_id="a", workspace="w")
    assert "alpha" not in without["skills"]
    assert any("alpha removed" in c["summary"] for c in rs.diff_snapshots(with_gate, without))


def test_no_snapshot_is_not_pending(tmp_path):
    home = tmp_path / ".relaydeck"
    db = _db(home)
    _ws(home, "w", [])
    desired = rs.compute_snapshot(home, db, agent_id="a", workspace="w")
    assert rs.restart_state(None, desired)["restart_pending"] is False


def test_native_model_override_change_detected(tmp_path):
    # Native harnesses carry the model in a harness-specific override key
    # (codex_model), not preset/model — swapping it must surface a restart.
    home = tmp_path / ".relaydeck"
    db = _db(home)
    _ws(home, "w", [])
    base = rs.compute_snapshot(
        home, db, agent_id="a", agent_type="codex-cli", workspace="w",
        config={"codex_model": "gpt-5.3-codex"},
    )
    after = rs.compute_snapshot(
        home, db, agent_id="a", agent_type="codex-cli", workspace="w",
        config={"codex_model": "gpt-5.3-codex-mini"},
    )
    assert any(c["component"] == "model" for c in rs.restart_state(base, after)["pending_changes"])


def test_command_override_change_detected(tmp_path):
    # A full argv override (config["command"]) changes the spawn command.
    home = tmp_path / ".relaydeck"
    db = _db(home)
    _ws(home, "w", [])
    base = rs.compute_snapshot(home, db, agent_id="a", agent_type="pi", workspace="w",
                              config={"command": ["pi", "--foo"]})
    after = rs.compute_snapshot(home, db, agent_id="a", agent_type="pi", workspace="w",
                               config={"command": ["pi", "--bar"]})
    assert any(c["component"] == "launch" for c in rs.restart_state(base, after)["pending_changes"])


def test_snapshot_for_agent_reads_spec(tmp_path):
    home = tmp_path / ".relaydeck"
    db = _db(home)
    ws = _ws(home, "w", ["skills"])
    _skill(ws, "alpha")
    AgentSpec(
        id="a1", name="a1", type="pi", workspace="w",
        system_prompt="hello", inject_identity_preamble=False,
    ).save(home)
    desired = rs.snapshot_for_agent(home, db, "a1")
    assert desired is not None
    assert "alpha" in desired["skills"]
    assert rs.snapshot_for_agent(home, db, "missing") is None
