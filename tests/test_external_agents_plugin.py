"""External-agents plugin: HTTP API, events, and CLI read-path.

The API is exercised against a FastAPI TestClient with the plugin's collected
routes mounted under the same `/api/plugins/external` prefix the adapter uses.
Mutating CLI commands (add/remove/health) talk to the daemon and are covered
via the API instead — the tests never touch a real daemon.
"""

from __future__ import annotations

import click
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.external_agents import detector, probes, store
from plugins.external_agents.plugin import (
    ExternalAgentsPlugin,
    _registered_workspace_paths,
    build_agent,
)
from relaydeck.testing import MockHost


def _hermes_repo(tmp_path):
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\n')
    return repo


def _build(tmp_path, monkeypatch, collect_events=False):
    monkeypatch.setattr(detector, "_which", lambda n: None)  # deterministic
    # Probes use their OWN `_which` seam — patch it too so the cli=missing
    # assertion is hermetic on a machine that actually has hermes/openclaw
    # installed (otherwise the probe finds the real binary → cli=ok).
    monkeypatch.setattr(probes, "_which", lambda n: None)
    home = tmp_path / "cfg"
    home.mkdir()
    host = MockHost(name="external", config_home=home)
    events: list[str] = []
    if collect_events:
        host.events._bus.subscribe("external_agent.*", lambda e: events.append(e.type))
    plugin = ExternalAgentsPlugin()
    plugin.on_load(host)
    app = FastAPI()
    for r in host.api.routes:
        app.add_api_route(
            f"/api/plugins/external/{r['path'].strip('/')}",
            r["handler"], methods=r["methods"],
        )
    return TestClient(app), home, events


def test_add_list_get_delete_flow(tmp_path, monkeypatch):
    client, home, events = _build(tmp_path, monkeypatch, collect_events=True)
    repo = _hermes_repo(tmp_path)

    # add
    r = client.post("/api/plugins/external/agents", json={"path": str(repo)})
    assert r.status_code == 200, r.text
    agent = r.json()
    assert agent["kind"] == "hermes"
    assert agent["id"] == "hermes-agent"
    assert "external_agent.added" in events

    # list (+ candidates key present)
    r = client.get("/api/plugins/external/agents")
    body = r.json()
    assert [a["id"] for a in body["agents"]] == ["hermes-agent"]
    assert "candidates" in body

    # get one
    r = client.get("/api/plugins/external/agents/hermes-agent")
    assert r.status_code == 200
    assert r.json()["id"] == "hermes-agent"

    # delete
    r = client.delete("/api/plugins/external/agents/hermes-agent")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "external_agent.removed" in events
    assert client.get("/api/plugins/external/agents/hermes-agent").status_code == 404


def test_duplicate_add_conflicts(tmp_path, monkeypatch):
    client, home, _ = _build(tmp_path, monkeypatch)
    repo = _hermes_repo(tmp_path)
    assert client.post("/api/plugins/external/agents", json={"path": str(repo)}).status_code == 200
    r = client.post("/api/plugins/external/agents", json={"path": str(repo)})
    assert r.status_code == 409


def test_add_undetectable_is_400(tmp_path, monkeypatch):
    client, home, _ = _build(tmp_path, monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("hi\n")
    r = client.post("/api/plugins/external/agents", json={"path": str(plain), "kind": "auto"})
    assert r.status_code == 400


def test_add_missing_path_is_400(tmp_path, monkeypatch):
    client, home, _ = _build(tmp_path, monkeypatch)
    assert client.post("/api/plugins/external/agents", json={}).status_code == 400


def test_probe_updates_and_emits(tmp_path, monkeypatch):
    client, home, events = _build(tmp_path, monkeypatch, collect_events=True)
    repo = _hermes_repo(tmp_path)
    client.post("/api/plugins/external/agents", json={"path": str(repo)})
    r = client.post("/api/plugins/external/agents/hermes-agent/probe")
    assert r.status_code == 200
    rep = r.json()
    assert "health" in rep and "risk" in rep
    assert rep["health"]["cli"] == "missing"  # _which patched to None
    assert "external_agent.health" in events
    # cached on the agent now
    got = client.get("/api/plugins/external/agents/hermes-agent").json()
    assert got["last_probe"] is not None


def test_probe_missing_agent_404(tmp_path, monkeypatch):
    client, home, _ = _build(tmp_path, monkeypatch)
    assert client.post("/api/plugins/external/agents/nope/probe").status_code == 404


def test_detect_endpoint(tmp_path, monkeypatch):
    client, home, _ = _build(tmp_path, monkeypatch)
    repo = _hermes_repo(tmp_path)
    r = client.post("/api/plugins/external/detect", json={"path": str(repo)})
    assert r.status_code == 200
    d = r.json()
    assert d["kind"] == "hermes" and d["matched"] is True


def test_forced_kind_overrides_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "_which", lambda n: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    # auto would fail, but an explicit kind is allowed (user knows best). A
    # forced kind on an undetected path still gets the kind's preferred native
    # transport instead of "unknown".
    oc = build_agent(str(plain), kind="openclaw")
    assert oc.kind == "openclaw"
    assert oc.transport == "gateway-ws"
    h = build_agent(str(plain), kind="hermes")
    assert h.kind == "hermes"
    assert h.transport == "mcp"


def test_registered_workspace_paths_use_public_config_loader(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_home = tmp_path / "cfg"
    config_home.mkdir()
    (config_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{repo}"\n'
    )

    assert _registered_workspace_paths(config_home) == [str(repo)]


def test_delete_failure_returns_error(tmp_path, monkeypatch):
    client, home, events = _build(tmp_path, monkeypatch, collect_events=True)
    repo = _hermes_repo(tmp_path)
    client.post("/api/plugins/external/agents", json={"path": str(repo)})
    # Simulate an unlink that fails: the endpoint must NOT report success or
    # emit removed, since the agent is still on disk.
    monkeypatch.setattr(store, "delete_agent", lambda *a, **k: False)
    r = client.delete("/api/plugins/external/agents/hermes-agent")
    assert r.status_code == 500
    assert "external_agent.removed" not in events


# ── CLI read-path smoke (no daemon) ─────────────────────────────────


def _cli_group(plugin, host):
    group = click.Group(name="external")
    for name, fn, _attrs in host.cli.commands:
        group.add_command(click.command(name=name)(fn))
    return group


def test_cli_list_and_detect(tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "_which", lambda n: None)
    home = tmp_path / "cfg"
    home.mkdir()
    host = MockHost(name="external", config_home=home)
    plugin = ExternalAgentsPlugin()
    plugin.on_load(host)
    repo = _hermes_repo(tmp_path)
    store.save_agent(home, build_agent(str(repo), kind="auto"))

    group = _cli_group(plugin, host)
    res = CliRunner().invoke(group, ["list"])
    assert res.exit_code == 0
    assert "hermes-agent" in res.output

    res = CliRunner().invoke(group, ["detect", str(repo)])
    assert res.exit_code == 0
    assert "hermes" in res.output

    res = CliRunner().invoke(group, ["show", "hermes-agent"])
    assert res.exit_code == 0
    assert "hermes-agent" in res.output
