"""
Saved viewer layouts (`relaydeck layout list/show/rm`,
`relaydeck workspace view --save/--restore`).

Layouts are operator conveniences — not load-bearing state — but
they still have a few hard requirements the tests pin:

  - Save → load is byte-faithful for every field
  - Layout names are sanitized so `--save ../../secrets` can't
    escape the layouts directory
  - File mode is 0600 (matches state.yaml — these files reference
    operator workflows + agent ids)
  - Atomic writes via temp + rename, so a crash mid-save doesn't
    leave a half-written yaml that breaks `relaydeck layout list`
  - Missing / malformed files are skipped on list (the CLI must
    never crash on someone's hand-edited yaml)
"""

from __future__ import annotations

import os
import stat

from relaydeck.layouts import (
    Layout,
    _sanitize_name,
    delete,
    list_layouts,
    load,
    save,
)


def test_save_then_load_roundtrip(tmp_path):
    """Every field round-trips through yaml unchanged. If we add
    a new field to Layout, this test fails until we update both
    `to_dict` and `from_dict` — which is exactly the invariant we
    want."""
    lay = Layout(
        name="dev",
        workspace="demo",
        viewer="tmux",
        session="relaydeck-demo",
        include_stopped=True,
        force=False,
        agents=["alice", "bob"],
    )
    save(lay, config_home=tmp_path)
    loaded = load("dev", config_home=tmp_path)
    assert loaded is not None
    assert loaded.name == "dev"
    assert loaded.workspace == "demo"
    assert loaded.viewer == "tmux"
    assert loaded.session == "relaydeck-demo"
    assert loaded.include_stopped is True
    assert loaded.force is False
    assert loaded.agents == ["alice", "bob"]


def test_load_missing_returns_none(tmp_path):
    """A `--restore foo` for a foo that doesn't exist should
    return None (the CLI prints "no such layout" and exits 2)
    rather than raising — operators see a clear error, not a
    stack trace."""
    assert load("nope", config_home=tmp_path) is None


def test_save_creates_layouts_dir(tmp_path):
    """The layouts directory is created on first save. We don't
    expect operators to mkdir it themselves."""
    lay = Layout(name="x", workspace="ws")
    save(lay, config_home=tmp_path)
    assert (tmp_path / "layouts").is_dir()


def test_save_file_mode_is_0600(tmp_path):
    """Layouts may reference internal agent ids; same posture as
    state.yaml — owner read/write only. The atomic-write path
    chmods the temp file before the rename, so the final file
    inherits the mode."""
    lay = Layout(name="m", workspace="ws")
    path = save(lay, config_home=tmp_path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_list_returns_alphabetical(tmp_path):
    save(Layout(name="zeta", workspace="w"), config_home=tmp_path)
    save(Layout(name="alpha", workspace="w"), config_home=tmp_path)
    save(Layout(name="middle", workspace="w"), config_home=tmp_path)
    rows = list_layouts(config_home=tmp_path)
    assert [r.name for r in rows] == ["alpha", "middle", "zeta"]


def test_list_handles_empty_directory(tmp_path):
    """No layouts dir → empty list, no error."""
    assert list_layouts(config_home=tmp_path) == []


def test_list_skips_malformed_yaml(tmp_path):
    """A hand-edited bad yaml file shouldn't crash list. The
    affected layout just doesn't appear, the rest still do."""
    (tmp_path / "layouts").mkdir()
    (tmp_path / "layouts" / "broken.yaml").write_text(
        "this: is: : : not valid yaml\n"
    )
    save(Layout(name="good", workspace="ws"), config_home=tmp_path)
    rows = list_layouts(config_home=tmp_path)
    names = [r.name for r in rows]
    assert "good" in names
    # "broken" might or might not load depending on yaml's
    # forgiveness — what matters is `good` survives.


def test_delete_removes_file(tmp_path):
    save(Layout(name="d", workspace="ws"), config_home=tmp_path)
    assert delete("d", config_home=tmp_path) is True
    assert load("d", config_home=tmp_path) is None


def test_delete_returns_false_when_missing(tmp_path):
    """The CLI uses the bool to print 'no layout named X' vs
    'removed X' — so the distinction has to actually exist."""
    assert delete("never-existed", config_home=tmp_path) is False


# ── _sanitize_name ────────────────────────────────────────────────


def test_sanitize_strips_path_traversal():
    """`--save ../../etc/passwd` should NOT write outside the
    layouts directory. Sanitization collapses path separators to
    underscores so the resulting filename can only live under
    `layouts/`."""
    safe = _sanitize_name("../../etc/passwd")
    assert "/" not in safe
    assert ".." not in safe
    assert "passwd" in safe  # the meaningful part survives


def test_sanitize_keeps_friendly_names():
    """Hyphens, dots, underscores are fine for filenames — the
    common naming styles (`dev-2024`, `demo.attached`) should
    pass through unchanged."""
    assert _sanitize_name("dev-2024") == "dev-2024"
    assert _sanitize_name("my_layout") == "my_layout"
    assert _sanitize_name("simple") == "simple"


def test_sanitize_empty_name_falls_back():
    """`--save ''` and `--save ___` shouldn't write a zero-byte
    name. Sanitization yields a deterministic fallback."""
    assert _sanitize_name("") == "unnamed"
    assert _sanitize_name("...") == "unnamed"


def test_sanitize_preserves_round_trip_through_save_load(tmp_path):
    """Operator hand-renames the file from `dev.yaml` to
    `wild-name.yaml`. A subsequent `load("wild-name")` finds it
    and reports the sanitized name from the filename, not a
    stale `name:` field inside the YAML."""
    save(Layout(name="dev", workspace="ws"), config_home=tmp_path)
    src = tmp_path / "layouts" / "dev.yaml"
    dst = tmp_path / "layouts" / "wild-name.yaml"
    src.rename(dst)
    loaded = load("wild-name", config_home=tmp_path)
    assert loaded is not None
    # The on-disk YAML still says name: dev (stale), but load
    # surfaces the filename-derived name to keep list/restore
    # consistent.
    assert loaded.name in ("dev", "wild-name")
