"""`POST /api/agents/preview-prompt` — "what gets baked" preview.

The new-agent modal shows, before spawn, exactly what the model will see:
the auto identity preamble (id/purpose/peers), the operator's free-form
system prompt, and the prompt-shaping plugins active in the target
workspace. Read-only — no agent is created.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


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


def test_preview_includes_identity_preamble_and_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/preview-prompt", json={
            "agent_id": "reviewer", "workspace": "proj",
            "purpose": "Gate PRs for risk",
            "system_prompt": "Prefer rejection over speculative fixes.",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "You are agent `reviewer`" in body["preamble"]
    assert "Gate PRs for risk" in body["preamble"]
    assert body["system_prompt"] == "Prefer rejection over speculative fixes."
    # composed = preamble + system_prompt
    assert "Prefer rejection" in body["composed"]
    assert "You are agent" in body["composed"]


def test_preview_lists_existing_peers(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    from relaydeck.db import open_db, upsert_agent
    conn = open_db(orch.db_path)
    upsert_agent(conn, "builder", "pi", "builder", workspace="proj",
                 purpose="writes code")
    conn.close()

    with TestClient(app) as c:
        r = c.post("/api/agents/preview-prompt", json={
            "agent_id": "reviewer", "workspace": "proj", "purpose": "reviews",
        })
    body = r.json()
    assert "builder" in body["peers"]
    assert "`builder` (pi) — writes code" in body["preamble"]


def test_preview_respects_inject_off(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/preview-prompt", json={
            "agent_id": "x", "workspace": "proj", "purpose": "p",
            "inject_identity_preamble": False,
        })
    body = r.json()
    assert body["preamble"] == ""


def test_preview_reports_active_workspace_plugins_with_skill_count(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _, home = _make_app(tmp_path)
    from relaydeck.config import register_workspace

    ws_root = tmp_path / "proj"
    ws_root.mkdir()
    register_workspace(home, "proj", ws_root, plugins=["skills"])
    # Materialize one user skill so the count is non-zero.
    skill = home / "workspaces" / "proj" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: ship it\n---\n")

    with TestClient(app) as c:
        r = c.post("/api/agents/preview-prompt", json={
            "agent_id": "x", "workspace": "proj",
        })
    inj = {i["plugin"]: i for i in r.json()["injections"]}
    assert "skills" in inj
    assert "1 skill" in inj["skills"]["detail"]


def test_preview_no_workspace_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/preview-prompt", json={"agent_id": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["injections"] == []
    assert body["peers"] == []
