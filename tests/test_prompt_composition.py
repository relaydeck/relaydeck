"""Prompt-composition breakdown — `GET /api/agents/{id}/prompt-composition`
and the pure `compose_prompt_components` it wraps.

Pins that the breakdown reports the REAL components (identity preamble,
operator system_prompt, each injected skill body, fleet context) with
exact character counts + a ~chars/4 token estimate — never fabricated, and
in spawn-composition order.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from relaydeck.prompt_composition import compose_prompt_components, est_tokens


def _make_app(tmp_path: Path):
    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.transports.api import create_app

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch, home


def _enable_plugins(home: Path, ws: str, plugins: list[str]) -> None:
    """Write the workspace agent.toml gate list the composition reads."""
    d = home / "workspaces" / ws
    d.mkdir(parents=True, exist_ok=True)
    body = "[workspace]\nplugins = [" + ", ".join(f'"{p}"' for p in plugins) + "]\n"
    (d / "agent.toml").write_text(body)


def test_est_tokens_is_chars_over_four():
    assert est_tokens(0) == 0
    assert est_tokens(400) == 100
    assert est_tokens(402) == 100  # rounds


def test_components_are_real_and_measured(tmp_path):
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _enable_plugins(home, "proj", ["skills"])  # gate user skills ON
    # one injected user skill
    skill = home / "workspaces" / "proj" / "skills" / "deploy"
    skill.mkdir(parents=True)
    body = "---\nname: deploy\ndescription: ship it\n---\n\nRun the deploy.\n"
    (skill / "SKILL.md").write_text(body)

    out = compose_prompt_components(
        home, agent_id="rev", workspace="proj", agent_type="pi",
        purpose="gate PRs", system_prompt="Prefer rejection.",
        peers=[{"id": "builder", "type": "pi", "purpose": "writes code"}],
    )
    labels = [c["label"] for c in out["components"]]
    assert labels[0] == "identity preamble"
    assert labels[1] == "system_prompt (yaml)"
    assert any(c["label"] == "skill · deploy" for c in out["components"])

    # exact chars + estimate are honest
    sysc = next(c for c in out["components"] if c["label"] == "system_prompt (yaml)")
    assert sysc["chars"] == len("Prefer rejection.")
    assert sysc["est_tokens"] == est_tokens(sysc["chars"])
    assert sysc["source_plugin"] == "core"

    skillc = next(c for c in out["components"] if c["label"] == "skill · deploy")
    assert skillc["chars"] == len(body)
    assert skillc["source_plugin"] == "skills"

    # totals add up across components
    assert out["total_chars"] == sum(c["chars"] for c in out["components"])
    assert out["total_est_tokens"] == est_tokens(out["total_chars"])
    assert out["chars_per_token"] == 4


def test_inject_preamble_off_drops_preamble(tmp_path):
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    out = compose_prompt_components(
        home, agent_id="x", workspace="proj", purpose="p",
        system_prompt="body", inject_preamble=False,
    )
    assert all(c["label"] != "identity preamble" for c in out["components"])


def test_invalid_skill_is_skipped(tmp_path):
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _enable_plugins(home, "proj", ["skills"])  # gate ON, so absence is the invalidity
    # no frontmatter -> invalid -> not injected
    bad = home / "workspaces" / "proj" / "skills" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("just a body, no frontmatter")
    out = compose_prompt_components(
        home, agent_id="x", workspace="proj", agent_type="pi", system_prompt="s",
    )
    assert all("bad" not in c["label"] for c in out["components"])


# ── Gating: composition must match what the harness actually injects ──────


def _write_user_skill(home: Path, ws: str, name: str) -> None:
    d = home / "workspaces" / ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: a user skill\n---\n\nBody.\n")


def _write_runtime_skill(home: Path, ws: str, name: str) -> None:
    import json
    d = home / "workspaces" / ws / "runtime" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: reply via CLI\n---\n\nBody.\n")
    (d / ".relaydeck-skill.json").write_text(json.dumps({"owner_plugin": "messaging"}))


def test_user_skill_absent_when_skills_gate_disabled(tmp_path):
    """A user-authored skill must NOT appear when `skills` isn't in agent.toml —
    the harness wouldn't inject it, so neither should the breakdown."""
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _enable_plugins(home, "proj", [])  # skills gate OFF
    _write_user_skill(home, "proj", "deploy")
    out = compose_prompt_components(home, agent_id="x", workspace="proj", agent_type="pi")
    assert all("deploy" not in c["label"] for c in out["components"])


def test_runtime_skill_always_present(tmp_path):
    """Runtime/plugin skills (e.g. messaging) are injected regardless of the
    `skills` gate — they must show even with the gate off."""
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _enable_plugins(home, "proj", [])  # skills gate OFF
    _write_runtime_skill(home, "proj", "relay-cli")
    out = compose_prompt_components(home, agent_id="x", workspace="proj", agent_type="pi")
    assert any("relay-cli" in c["label"] for c in out["components"])


def test_fleet_context_absent_when_gate_disabled(tmp_path):
    """fleet-context only injects when the `fleet-context` plugin is enabled."""
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    _enable_plugins(home, "proj", [])  # fleet-context gate OFF
    fc = home / "workspaces" / "proj" / "runtime" / "fleet-context" / "x.md"
    fc.parent.mkdir(parents=True)
    fc.write_text("# fleet context\nlots of context\n")
    out = compose_prompt_components(home, agent_id="x", workspace="proj", agent_type="pi")
    assert all("fleet context" not in c["label"] for c in out["components"])

    # …and present when the gate is on.
    _enable_plugins(home, "proj", ["fleet-context"])
    out2 = compose_prompt_components(home, agent_id="x", workspace="proj", agent_type="pi")
    assert any("fleet context" in c["label"] for c in out2["components"])


def test_endpoint_returns_composition_for_live_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    from relaydeck.db import open_db, upsert_agent

    conn = open_db(orch.db_path)
    upsert_agent(conn, "rev", "pi", "rev", workspace="proj", purpose="gate PRs")
    conn.close()
    # YAML spec carries the system_prompt (no DB mirror)
    agents_dir = home / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "rev.yaml").write_text(
        "id: rev\nname: rev\ntype: pi\nworkspace: proj\n"
        "purpose: gate PRs\ntags: []\n"
        "system_prompt: 'Prefer rejection over speculative fixes.'\n"
        "inject_identity_preamble: true\nauto_start: false\nconfig: {}\n",
    )

    with TestClient(app) as c:
        r = c.get("/api/agents/rev/prompt-composition")
    assert r.status_code == 200, r.text
    body = r.json()
    labels = [c["label"] for c in body["components"]]
    assert "identity preamble" in labels
    assert "system_prompt (yaml)" in labels
    sysc = next(c for c in body["components"] if c["label"] == "system_prompt (yaml)")
    assert "Prefer rejection" in sysc["text"]
    assert body["total_est_tokens"] == est_tokens(body["total_chars"])


def test_endpoint_404_for_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/nope/prompt-composition")
    assert r.status_code == 404
