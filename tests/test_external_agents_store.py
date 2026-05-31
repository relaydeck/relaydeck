"""On-disk external-agent registry CRUD."""

from __future__ import annotations

from plugins.external_agents import store
from plugins.external_agents.models import ExternalAgent


def _agent(aid="hermes-main", kind="hermes"):
    return ExternalAgent(id=aid, kind=kind, name=aid, root=f"/src/{aid}")


def test_save_get_roundtrip(tmp_path):
    a = _agent()
    store.save_agent(tmp_path, a)
    got = store.get_agent(tmp_path, "hermes-main")
    assert got is not None
    assert got.id == "hermes-main"
    assert got.kind == "hermes"
    assert got.root == "/src/hermes-main"


def test_list_sorted(tmp_path):
    store.save_agent(tmp_path, _agent("zeta"))
    store.save_agent(tmp_path, _agent("alpha"))
    ids = [a.id for a in store.list_agents(tmp_path)]
    assert ids == ["alpha", "zeta"]


def test_exists_and_delete(tmp_path):
    store.save_agent(tmp_path, _agent("oc", kind="openclaw"))
    assert store.exists(tmp_path, "oc")
    assert store.delete_agent(tmp_path, "oc") is True
    assert not store.exists(tmp_path, "oc")
    assert store.delete_agent(tmp_path, "oc") is False  # already gone


def test_get_missing_returns_none(tmp_path):
    assert store.get_agent(tmp_path, "nope") is None


def test_empty_registry_lists_nothing(tmp_path):
    assert store.list_agents(tmp_path) == []


def test_save_is_upsert(tmp_path):
    a = _agent()
    store.save_agent(tmp_path, a)
    a.name = "renamed"
    store.save_agent(tmp_path, a)
    assert store.get_agent(tmp_path, "hermes-main").name == "renamed"
    assert len(store.list_agents(tmp_path)) == 1


def test_id_is_path_safe(tmp_path):
    # A crafted id can't escape the agents dir — it gets slugified.
    a = ExternalAgent(id="../../etc/passwd", kind="hermes", name="x", root="/x")
    store.save_agent(tmp_path, a)
    files = list((tmp_path / "plugin-data" / "external" / "agents").glob("*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name
    # And nothing was written outside the agents dir.
    assert not (tmp_path / "etc").exists()


def test_corrupt_file_is_skipped(tmp_path):
    store.save_agent(tmp_path, _agent("good"))
    bad = tmp_path / "plugin-data" / "external" / "agents" / "bad.json"
    bad.write_text("{not json")
    ids = [a.id for a in store.list_agents(tmp_path)]
    assert ids == ["good"]
