"""
End-to-end smoke test for the relaydeck-native agent: layered prompt + a real
pi turn via ``pi -p --mode json --continue`` (not the old in-process gateway).

If this passes, the native stack composes:
  - plugins load → openrouter provider registers + reads OPENROUTER_API_KEY
  - a ``relaydeck`` agent's preset resolves to openrouter/<model>
  - ``generate_reply`` runs pi, persists both chat turns, returns a reply

Runs only in the dedicated e2e workflow (``pytest -m e2e``) because it needs
``pi`` on PATH and a real LLM endpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_relaydeck_native_round_trip(
    pi_binary: str,
    openrouter_key: str,
    e2e_model: str,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import relaydeck.plugin as plug
    from relaydeck.config import AgentSpec
    from relaydeck.db import open_db
    from relaydeck.plugin import PluginContext, get_registry
    from plugins.harnesses.relaydeck_native.agent import generate_reply

    monkeypatch.setenv("OPENROUTER_API_KEY", openrouter_key)

    plug._registry = None
    reg = get_registry(isolated_home)
    reg.load_all(PluginContext(config_home=isolated_home))

    presets = isolated_home / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    (presets / "e2e.yaml").write_text(f"name: e2e\nprovider: openrouter\nmodel: {e2e_model}\n")

    AgentSpec(
        id="nativ", name="nativ", type="relaydeck", workspace="ws",
        config={"preset": "e2e", "tools": ["relaydeck"]},
    ).save(isolated_home)

    db = str(isolated_home / "runtime" / "relaydeck.db")
    open_db(db).close()

    out = generate_reply(isolated_home, db, "nativ", "Reply with the single word: pong")
    assert not out.get("error"), out
    reply = (out.get("reply") or "").strip()
    assert reply, f"native agent returned empty reply (model={out.get('model')})"
    assert not reply.startswith("(pi error:"), reply

    conn = open_db(db)
    try:
        n = conn.execute(
            "SELECT count(*) FROM agent_messages WHERE to_id='nativ' OR from_id='nativ'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n >= 2, "expected the user + assistant turns to persist"
