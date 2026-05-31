"""Agent id validation — one rule enforced at the create chokepoint so
CLI, API, and web all reject the same bad names.

Rule (`config.AGENT_ID_RE`): lowercase letters, digits, single dashes;
must start with a letter; ≤64; no spaces/underscores/leading/trailing/
double dashes. Machine-generated ids coerce via `sanitize_agent_id`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relaydeck.config import sanitize_agent_id, validate_agent_id

VALID = ["a", "pr-reviewer", "agent1", "web-2", "x9", "a-b-c", "rev2"]
INVALID = [
    "",            # empty
    "PR",          # uppercase
    "my agent",    # space
    "my_agent",    # underscore
    "-foo",        # leading dash
    "foo-",        # trailing dash
    "foo--bar",    # double dash
    "1agent",      # leading digit
    "a" * 65,      # too long
    "a/b",         # slash
    "agent!",      # punctuation
]


@pytest.mark.parametrize("aid", VALID)
def test_valid_ids_pass(aid):
    assert validate_agent_id(aid) == aid


@pytest.mark.parametrize("aid", INVALID)
def test_invalid_ids_raise(aid):
    with pytest.raises(ValueError):
        validate_agent_id(aid)


def test_validate_strips_whitespace():
    assert validate_agent_id("  rev  ") == "rev"


def test_sanitize_coerces_to_valid():
    for raw, expect in [
        ("PR Reviewer", "pr-reviewer"),
        ("my_agent", "my-agent"),
        ("1agent", "a-1agent"),
        ("hermes.chat", "hermes-chat"),
        ("--weird--", "weird"),
        ("", "agent"),
        ("###", "agent"),
    ]:
        out = sanitize_agent_id(raw)
        assert out == expect
        # whatever it produced must itself be a valid id
        assert validate_agent_id(out) == out


# ── enforcement: orchestrator + API ─────────────────────────────────


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
    return app, orch


def test_orchestrator_create_agent_rejects_bad_id(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _, orch = _make_app(tmp_path)
    with pytest.raises(ValueError, match="invalid agent id"):
        orch.create_agent("Bad_Name", "pi", "Bad_Name")


def test_api_create_agent_400_on_bad_id(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents", json={"id": "My Agent", "type": "pi", "workspace": "w"})
    assert r.status_code == 400
    assert "invalid agent id" in r.json()["detail"]


def test_api_create_agent_ok_on_good_id(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents", json={"id": "pr-reviewer", "type": "pi", "workspace": "w"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "pr-reviewer"


def test_create_agent_rejects_duplicate(tmp_path, monkeypatch):
    """create must NOT silently overwrite an existing agent's spec/DB row —
    the new-agent wizard assumes a duplicate id fails (reviewer's repro)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _, orch = _make_app(tmp_path)
    orch.create_agent("dup", "pi", "dup", workspace="w")
    with pytest.raises(ValueError, match="already exists"):
        orch.create_agent("dup", "codex", "dup", workspace="other")
    with pytest.raises(ValueError, match="workspace 'w'"):
        orch.create_agent("dup", "codex", "dup")


def test_api_create_agent_400_on_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        first = c.post("/api/agents", json={"id": "dup", "type": "pi", "workspace": "w"})
        assert first.status_code == 200, first.text
        second = c.post("/api/agents", json={"id": "dup", "type": "codex", "workspace": "w"})
    assert second.status_code == 400
    assert "already exists" in second.json()["detail"]
