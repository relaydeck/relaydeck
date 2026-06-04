"""
Agent cross-orchestration meta — `purpose` + `tags` fields.

These tests pin the design ask: an agent doing `relaydeck agent list` or
`relaydeck agent find` must see what each peer is *for*, so it can delegate
intelligently. Skills do this via SKILL.md frontmatter; agents do it
via the spec's purpose/tags.

Covers: persistence (YAML + DB), API round-trip, CLI behavior (list /
edit / find), and the SKILL.md teaches both surfaces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.config import AgentSpec
from relaydeck.transports.api import create_app
from relaydeck.transports.cli import main as cli

# ── Spec persistence ────────────────────────────────────────────────


def test_agent_spec_defaults(tmp_path):
    """purpose/tags default to empty so old YAML specs still load."""
    spec = AgentSpec(id="x", name="x", type="pi")
    assert spec.purpose == ""
    assert spec.tags == []


def test_agent_spec_yaml_roundtrip(tmp_path):
    spec = AgentSpec(
        id="reviewer", name="Reviewer", type="claude-code",
        purpose="Reviews PRs for correctness + security",
        tags=["reviewer", "security"],
    )
    path = spec.save(tmp_path)
    loaded = AgentSpec.from_yaml(path)
    assert loaded.purpose == "Reviews PRs for correctness + security"
    assert loaded.tags == ["reviewer", "security"]


def test_agent_spec_loads_legacy_yaml_without_meta(tmp_path):
    """Old YAML without purpose/tags still loads — these are additive."""
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.dump({
        "id": "legacy", "name": "Legacy", "type": "pi",
        # No purpose/tags fields
    }))
    loaded = AgentSpec.from_yaml(path)
    assert loaded.purpose == ""
    assert loaded.tags == []


# ── API contract ────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    cfg_home.mkdir(parents=True)
    (cfg_home / "runtime").mkdir()
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    app = create_app(cfg_home)
    return TestClient(app)


def test_create_agent_with_purpose_and_tags(client):
    r = client.post("/api/agents", json={
        "id": "reviewer", "type": "pi", "name": "Reviewer",
        "purpose": "Reviews PRs for correctness",
        "tags": ["reviewer", "security"],
    })
    assert r.status_code == 200

    r = client.get("/api/agents/reviewer")
    assert r.status_code == 200
    data = r.json()
    assert data["purpose"] == "Reviews PRs for correctness"
    assert data["tags"] == ["reviewer", "security"]


def test_get_agent_returns_full_spec_including_yaml_only_fields(client):
    """The detail view needs everything in one call: purpose/tags from
    the DB mirror PLUS system_prompt + inject_identity_preamble + config
    from the YAML spec. Pre-merge UI had to make two roundtrips and
    couldn't tell when an agent had a custom prompt override."""
    r = client.post("/api/agents", json={
        "id": "guard", "type": "pi", "name": "Guard",
        "purpose": "Watches the gate",
        "tags": ["security"],
        "system_prompt": "Reject anything that looks like SQL injection.",
        "inject_identity_preamble": False,
        "config": {"model": "claude-sonnet-4"},
    })
    assert r.status_code == 200

    data = client.get("/api/agents/guard").json()
    assert data["purpose"] == "Watches the gate"
    assert data["tags"] == ["security"]
    assert data["system_prompt"] == "Reject anything that looks like SQL injection."
    assert data["inject_identity_preamble"] is False
    assert data["config"] == {"model": "claude-sonnet-4"}


def test_get_agent_defaults_yaml_fields_when_spec_missing(client, tmp_path):
    """If only the DB row exists (legacy / orphaned), get_agent still
    fills the YAML-only fields with sane defaults so the UI doesn't
    crash on missing keys."""
    client.post("/api/agents", json={"id": "orphan", "type": "pi"})
    # Simulate a lost YAML by deleting it.
    spec_path = tmp_path / ".relaydeck" / "agents" / "orphan.yaml"
    if spec_path.exists():
        spec_path.unlink()
    data = client.get("/api/agents/orphan").json()
    assert data["system_prompt"] == ""
    assert data["inject_identity_preamble"] is True
    assert data["config"] == {}


def test_list_agents_includes_meta(client):
    client.post("/api/agents", json={
        "id": "coder", "type": "pi",
        "purpose": "Implements features per spec",
        "tags": ["coder"],
    })
    rows = client.get("/api/agents").json()
    assert rows
    assert rows[0]["purpose"] == "Implements features per spec"
    assert rows[0]["tags"] == ["coder"]


def test_patch_agent_updates_purpose(client):
    client.post("/api/agents", json={"id": "a1", "type": "pi"})
    r = client.patch("/api/agents/a1", json={"purpose": "newly explained"})
    assert r.status_code == 200
    assert r.json()["purpose"] == "newly explained"
    assert client.get("/api/agents/a1").json()["purpose"] == "newly explained"


def test_patch_agent_updates_tags(client):
    client.post("/api/agents", json={"id": "a1", "type": "pi", "tags": ["x"]})
    r = client.patch("/api/agents/a1", json={"tags": ["y", "z"]})
    assert r.status_code == 200
    assert r.json()["tags"] == ["y", "z"]


def test_create_agent_rejects_non_list_tags(client):
    r = client.post("/api/agents", json={"id": "a1", "type": "pi", "tags": "reviewer"})
    assert r.status_code == 400
    assert "tags must be a list of strings" in r.json()["detail"]


def test_patch_agent_rejects_non_string_tags(client):
    client.post("/api/agents", json={"id": "a1", "type": "pi"})
    r = client.patch("/api/agents/a1", json={"tags": ["ok", 3]})
    assert r.status_code == 400
    assert "tags must be a list of strings" in r.json()["detail"]


def test_patch_agent_404_for_unknown(client):
    r = client.patch("/api/agents/ghost", json={"purpose": "x"})
    assert r.status_code == 404


def test_patch_agent_400_when_no_meta_keys(client):
    client.post("/api/agents", json={"id": "a1", "type": "pi"})
    r = client.patch("/api/agents/a1", json={"name": "ignored"})
    assert r.status_code == 400


def test_find_agents_by_tag(client):
    client.post("/api/agents", json={
        "id": "rev", "type": "pi", "tags": ["reviewer", "security"]})
    client.post("/api/agents", json={
        "id": "coder", "type": "pi", "tags": ["coder"]})
    client.post("/api/agents", json={
        "id": "rev2", "type": "claude-code", "tags": ["reviewer"]})

    rows = client.get("/api/agents/find?tag=reviewer").json()
    assert {a["id"] for a in rows} == {"rev", "rev2"}

    # Multiple tags AND together — only `rev` has both
    rows = client.get("/api/agents/find?tag=reviewer").json()
    assert "rev" in {a["id"] for a in rows}


def test_find_agents_by_purpose_substring(client):
    client.post("/api/agents", json={
        "id": "rev", "type": "pi",
        "purpose": "Reviews PRs for security issues"})
    client.post("/api/agents", json={
        "id": "coder", "type": "pi",
        "purpose": "Implements features"})

    rows = client.get("/api/agents/find?purpose=security").json()
    assert [a["id"] for a in rows] == ["rev"]

    # Case-insensitive
    rows = client.get("/api/agents/find?purpose=REVIEWS").json()
    assert [a["id"] for a in rows] == ["rev"]


def test_find_agents_filters_by_workspace(client, tmp_path):
    """find should respect workspace scoping so cross-workspace peers
    don't pollute results."""
    # Workspaces need to exist for the create_agent calls
    (tmp_path / "demo").mkdir()
    (tmp_path / "other").mkdir()
    client.post("/api/workspaces", json={
        "name": "demo", "path": str(tmp_path / "demo"), "plugins": []})
    client.post("/api/workspaces", json={
        "name": "other", "path": str(tmp_path / "other"), "plugins": []})

    client.post("/api/agents", json={
        "id": "a", "type": "pi", "workspace": "demo", "tags": ["reviewer"]})
    client.post("/api/agents", json={
        "id": "b", "type": "pi", "workspace": "other", "tags": ["reviewer"]})

    rows = client.get("/api/agents/find?tag=reviewer&workspace=demo").json()
    assert [a["id"] for a in rows] == ["a"]


# ── CLI ────────────────────────────────────────────────────────────


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    return cfg_home


def test_cli_create_with_purpose_and_tag(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "agent", "create", "rev", "--type", "pi",
        "--purpose", "PR reviewer",
        "--tag", "reviewer", "--tag", "security",
    ])
    assert result.exit_code == 0, result.output

    spec = AgentSpec.from_yaml(tmp_path / ".relaydeck" / "agents" / "rev.yaml")
    assert spec.purpose == "PR reviewer"
    assert set(spec.tags) == {"reviewer", "security"}


def test_cli_list_shows_purpose_column(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(cli, [
        "agent", "create", "rev", "--type", "pi",
        "--purpose", "Reviews PRs for correctness"])
    result = runner.invoke(cli, ["agent", "list"])
    assert result.exit_code == 0
    assert "Purpose" in result.output
    # The purpose string might be wrapped/truncated in the table, so
    # just check a distinctive substring rather than the full line.
    assert "Reviews" in result.output or "PR" in result.output


def test_cli_edit_purpose(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(cli, ["agent", "create", "a", "--type", "pi"])
    result = runner.invoke(cli, [
        "agent", "edit", "a", "--purpose", "now defined"])
    assert result.exit_code == 0, result.output

    spec = AgentSpec.from_yaml(tmp_path / ".relaydeck" / "agents" / "a.yaml")
    assert spec.purpose == "now defined"


def test_cli_edit_tags_add_remove(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(cli, ["agent", "create", "a", "--type", "pi",
                        "--tag", "old1", "--tag", "old2"])
    runner.invoke(cli, ["agent", "edit", "a",
                        "--add-tag", "new1", "--remove-tag", "old1"])

    spec = AgentSpec.from_yaml(tmp_path / ".relaydeck" / "agents" / "a.yaml")
    assert set(spec.tags) == {"old2", "new1"}


def test_orchestrator_start_syncs_purpose_and_tags_from_yaml(tmp_path, monkeypatch):
    """Regression for P2.2: daemon startup loads agent YAML specs and
    upserts the DB row. Without `purpose`/`tags` in the upsert call,
    a YAML spec with meta would land in the DB with empty purpose/tags,
    breaking `relaydeck agent find`, peer lookup, and identity preambles
    after every restart."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir()

    # Hand-author a YAML spec with purpose+tags, no DB row yet.
    spec_yaml = (
        "id: rev\n"
        "name: rev\n"
        "type: pi\n"
        "workspace: demo\n"
        "purpose: Reviews PRs for security\n"
        "tags:\n  - reviewer\n  - security\n"
    )
    (cfg_home / "agents" / "rev.yaml").write_text(spec_yaml)

    # Fresh orchestrator → .start() runs the sync.
    import relaydeck.orchestrator as orch_mod
    orch_mod._orchestrator = None
    from relaydeck.orchestrator import Orchestrator
    orch = Orchestrator(cfg_home)
    orch.start()
    try:
        rows = orch.list_agents()
    finally:
        orch.stop()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "rev"
    assert row["purpose"] == "Reviews PRs for security", \
        "DB row must reflect YAML purpose after startup sync"
    assert row["tags"] == ["reviewer", "security"], \
        "DB row must reflect YAML tags after startup sync"


def test_orchestrator_start_clears_removed_yaml_tags(tmp_path, monkeypatch):
    """Startup sync must treat an empty YAML tag list as authoritative.

    Otherwise removing tags from an agent spec leaves stale DB metadata behind,
    and discovery still routes by tags the agent no longer has.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_home = tmp_path / ".relaydeck"
    (cfg_home / "runtime").mkdir(parents=True)
    (cfg_home / "agents").mkdir()

    from relaydeck.db import open_db, upsert_agent
    from relaydeck.orchestrator import Orchestrator

    db_path = cfg_home / "runtime" / "relaydeck.db"
    conn = open_db(db_path)
    try:
        upsert_agent(
            conn,
            "rev",
            "pi",
            "rev",
            workspace="demo",
            purpose="Reviews PRs",
            tags=["reviewer", "security"],
        )
    finally:
        conn.close()

    (cfg_home / "agents" / "rev.yaml").write_text(
        "id: rev\n"
        "name: rev\n"
        "type: pi\n"
        "workspace: demo\n"
        "purpose: Reviews PRs\n"
        "tags: []\n"
    )

    orch = Orchestrator(cfg_home)
    orch.start()
    try:
        rows = orch.list_agents()
    finally:
        orch.stop()

    assert rows[0]["tags"] == []


def test_cli_find_by_tag(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(cli, ["agent", "create", "rev", "--type", "pi",
                        "--tag", "reviewer"])
    runner.invoke(cli, ["agent", "create", "coder", "--type", "pi",
                        "--tag", "coder"])

    result = runner.invoke(cli, ["agent", "find", "--tag", "reviewer"])
    assert result.exit_code == 0
    assert "rev" in result.output
    assert "coder" not in result.output


# ── SKILL.md teaches the new flow ──────────────────────────────────


def test_skill_md_mentions_peer_discovery():
    """Regression — agents should learn about `relaydeck agent find` and
    `relaydeck agent edit` without having to discover them accidentally.
    Discovery commands live in the fleet skill; introduce-yourself stays
    in the messaging skill (where 'peers can't find me' is felt)."""
    repo = Path(__file__).resolve().parent.parent
    fleet = (repo / "skills" / "relaydeck-fleet" / "SKILL.md").read_text()
    cli = (repo / "plugins" / "messaging" / "SKILL.md").read_text()
    assert "relaydeck agent find" in fleet, \
        "fleet skill must teach peer discovery via `relaydeck agent find`"
    assert "purpose" in fleet.lower()
    assert "relaydeck agent edit" in cli, \
        "cli skill must teach agents how to introduce themselves"
