"""
Tests for git-style cwd-based workspace resolution.

The resolution order in `relaydeck.state.get_current_workspace`:

  1. RELAYDECK_WORKSPACE env var
  2. cwd is inside a registered workspace's path  ← NEW
  3. state.yaml `current_workspace`
  4. first workspace in registry (last-resort fallback)

We exercise each rank and the interactions between them so a
future refactor can't quietly invert the priority order.
"""

from __future__ import annotations

from pathlib import Path


def _seed_workspace(tmp_path: Path, name: str, plugins=()):
    """Write a workspace into the registry config.toml + agent.toml.
    Returns the workspace path on disk (a real directory so
    Path.resolve() works the same way it would in production).

    config.toml uses the `[[workspace]]` array-of-tables shape that
    `relaydeck.config.load_workspace_registry` reads."""
    ws_path = tmp_path / "code" / name
    ws_path.mkdir(parents=True)

    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True, exist_ok=True)
    config_toml = cfg / "config.toml"

    plugins_repr = ", ".join(f'"{p}"' for p in plugins)
    config_toml.write_text(
        (config_toml.read_text() if config_toml.exists() else "")
        + f'\n[[workspace]]\nname = "{name}"\npath = "{ws_path}"\nplugins = [{plugins_repr}]\n'
    )

    # Workspace-scoped agent.toml — the harness reads plugins here at
    # spawn time. Tests don't actually spawn, but `load_workspace_registry`
    # prefers agent.toml over the config.toml fallback so we keep them
    # consistent.
    (cfg / "workspaces" / name).mkdir(parents=True, exist_ok=True)
    (cfg / "workspaces" / name / "agent.toml").write_text(
        f'[workspace]\nplugins = [{plugins_repr}]\n'
    )
    return ws_path


# ── Single workspace, plain cases ──────────────────────────────────


def test_cwd_inside_workspace_resolves_to_it(tmp_path, monkeypatch):
    """`cd ~/code/myapi` → relaydeck infers `myapi` even without
    `relaydeck workspace set` or `RELAYDECK_WORKSPACE`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)

    ws_path = _seed_workspace(tmp_path, "myapi")
    from relaydeck.state import get_current_workspace
    assert get_current_workspace(cwd=ws_path) == "myapi"


def test_cwd_in_subdirectory_still_resolves(tmp_path, monkeypatch):
    """The match works against any descendant, not just the
    workspace root."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)

    ws_path = _seed_workspace(tmp_path, "myapi")
    nested = ws_path / "src" / "handlers"
    nested.mkdir(parents=True)

    from relaydeck.state import get_current_workspace
    assert get_current_workspace(cwd=nested) == "myapi"


def test_cwd_outside_any_workspace_falls_through(tmp_path, monkeypatch):
    """A cwd that isn't under any registered workspace must fall
    through to state.yaml (so `relaydeck workspace set` still applies in
    `/tmp` or in a stranger's repo)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)

    _seed_workspace(tmp_path, "myapi")
    from relaydeck.state import get_current_workspace, set_current_workspace
    set_current_workspace("myapi")

    elsewhere = tmp_path / "wandering"
    elsewhere.mkdir()
    assert get_current_workspace(cwd=elsewhere) == "myapi"  # via state.yaml


# ── Priority order ─────────────────────────────────────────────────


def test_env_beats_cwd(tmp_path, monkeypatch):
    """RELAYDECK_WORKSPACE is the explicit per-shell override — it has
    to win even when the user is `cd`'d into a different workspace.
    Otherwise scripts can't reliably target a workspace from inside
    another one (e.g. a CI runner running from `/srv/ci`)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    a = _seed_workspace(tmp_path, "ws-a")
    _seed_workspace(tmp_path, "ws-b")
    monkeypatch.setenv("RELAYDECK_WORKSPACE", "ws-b")

    from relaydeck.state import get_current_workspace
    # cwd is inside ws-a, but env says ws-b → env wins.
    assert get_current_workspace(cwd=a) == "ws-b"


def test_cwd_beats_state_yaml(tmp_path, monkeypatch):
    """`relaydeck workspace set` is the durable default; cwd is the
    immediate intent. cwd has to win or the dev experience falls
    apart: walking into a new project shouldn't keep operating on
    the previous one just because nobody re-ran `workspace set`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)

    a = _seed_workspace(tmp_path, "ws-a")
    _seed_workspace(tmp_path, "ws-b")
    from relaydeck.state import get_current_workspace, set_current_workspace
    set_current_workspace("ws-b")
    assert get_current_workspace(cwd=a) == "ws-a"


def test_state_yaml_beats_registry_default(tmp_path, monkeypatch):
    """When cwd doesn't match and env isn't set, the durable
    setting still beats the alphabetical first-registered
    workspace."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)

    _seed_workspace(tmp_path, "alpha")
    _seed_workspace(tmp_path, "beta")
    from relaydeck.state import get_current_workspace, set_current_workspace
    set_current_workspace("beta")

    elsewhere = tmp_path / "x"
    elsewhere.mkdir()
    assert get_current_workspace(cwd=elsewhere) == "beta"


# ── Nested workspaces ──────────────────────────────────────────────


def test_nested_workspaces_deepest_wins(tmp_path, monkeypatch):
    """A monorepo at `~/code/mono` with a subproject workspace at
    `~/code/mono/frontend` is a legitimate setup. Walking into
    `frontend/src` must resolve to `frontend`, not `mono`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)

    mono = tmp_path / "code" / "mono"
    mono.mkdir(parents=True)
    frontend = mono / "frontend"
    frontend.mkdir()

    # Register both, by hand, so the paths nest the way we want.
    cfg = tmp_path / ".relaydeck"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'[[workspace]]\nname = "mono"\npath = "{mono}"\nplugins = []\n'
        f'[[workspace]]\nname = "frontend"\npath = "{frontend}"\nplugins = []\n'
    )
    for nm in ("mono", "frontend"):
        d = cfg / "workspaces" / nm
        d.mkdir(parents=True)
        (d / "agent.toml").write_text("[workspace]\nplugins = []\n")

    from relaydeck.state import get_current_workspace
    deep = frontend / "src" / "components"
    deep.mkdir(parents=True)
    assert get_current_workspace(cwd=deep) == "frontend"
    # And the mono root still resolves to mono, not frontend.
    assert get_current_workspace(cwd=mono) == "mono"


# ── resolve_workspace_source: explains *how* ───────────────────────


def test_resolve_workspace_source_labels_each_origin(tmp_path, monkeypatch):
    """`workspace info` uses this to print `(via RELAYDECK_WORKSPACE env)`
    vs `(inferred from cwd)` etc. Pin each of the four sources."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RELAYDECK_WORKSPACE", raising=False)
    from relaydeck.state import resolve_workspace_source, set_current_workspace

    # 1. unset (no registry at all)
    assert resolve_workspace_source(cwd=tmp_path / "nope") == (None, "unset")

    # 2. registry-default — single workspace, nothing else applies
    ws_path = _seed_workspace(tmp_path, "only-one")
    elsewhere = tmp_path / "x"
    elsewhere.mkdir()
    assert resolve_workspace_source(cwd=elsewhere) == ("only-one", "registry-default")

    # 3. state — explicit `workspace set` wins over registry-default
    _seed_workspace(tmp_path, "second")
    set_current_workspace("second")
    assert resolve_workspace_source(cwd=elsewhere) == ("second", "state")

    # 4. cwd — being inside a workspace dir wins over state
    assert resolve_workspace_source(cwd=ws_path) == ("only-one", "cwd")

    # 5. env — top of the stack
    monkeypatch.setenv("RELAYDECK_WORKSPACE", "second")
    assert resolve_workspace_source(cwd=ws_path) == ("second", "env")
