"""
`relaydeck workspace view` recipe builder.

The tmux invocation is the load-bearing part; we keep it as a pure
function (`_build_tmux_recipe`) so tests can pin the exact command
sequence without spawning tmux. The actual subprocess call lives in
the CLI handler.
"""

from __future__ import annotations

from relaydeck.transports.cli import _build_tmux_recipe


def _ids(agents):
    return [{"id": a, "status": "running"} for a in agents]


def test_recipe_starts_detached_session_with_first_agent():
    """tmux new-session must use `-d` (detached) so the operator
    gets their prompt back, and the first agent gets the initial
    pane via `relaydeck attach`."""
    cmds = _build_tmux_recipe("relaydeck-demo", "demo", _ids(["alice"]))
    first = cmds[0]
    assert first[:4] == ["tmux", "new-session", "-d", "-s"]
    assert "relaydeck-demo" in first
    # The inline command must invoke `relaydeck attach <id>`.
    assert any("relaydeck attach alice" in part for part in first)


def test_recipe_splits_one_pane_per_extra_agent():
    cmds = _build_tmux_recipe("relaydeck-demo", "demo", _ids(["alice", "bob", "carol"]))
    splits = [c for c in cmds if c[1] == "split-window"]
    # 2 extras (bob, carol) + 1 inbox = 3 splits total.
    assert len(splits) == 3
    # The first two are agent attaches; the last is the inbox tail.
    agent_splits = splits[:2]
    inbox_split = splits[2]
    assert any("relaydeck attach bob" in p for p in agent_splits[0])
    assert any("relaydeck attach carol" in p for p in agent_splits[1])
    assert any("workspace inbox -f" in p for p in inbox_split)


def test_recipe_includes_workspace_in_inbox_command():
    """The inbox-tail pane must be scoped to the workspace, not to
    whatever the user's active workspace happens to be when tmux
    runs the spawn command. We pass --workspace explicitly so the
    layout is reproducible."""
    cmds = _build_tmux_recipe("relaydeck-demo", "demo", _ids(["alice"]))
    inbox_cmd = next(c for c in cmds if "workspace" in " ".join(c) and "inbox" in " ".join(c))
    joined = " ".join(inbox_cmd)
    assert "--workspace demo" in joined


def test_recipe_inbox_uses_full_so_bodies_arent_truncated():
    """The bottom pane in `workspace view` is the only place an
    operator reads what agents are saying to each other. Without
    `--full`, message bodies clip mid-word at 80 chars (the static
    inbox table's truncation default), which is useless in a live
    tail. Pin the `--full` flag so this can't slide back."""
    cmds = _build_tmux_recipe("relaydeck-x", "x", _ids(["alice"]))
    inbox_cmd = next(c for c in cmds if "inbox" in " ".join(c))
    joined = " ".join(inbox_cmd)
    assert "--full" in joined, (
        f"recipe must pass --full to inbox -f so bodies aren't "
        f"truncated mid-word in the tail pane; got: {joined}"
    )


def test_inbox_split_uses_percentage_length_not_deprecated_p_flag():
    """tmux 3.1 deprecated `-p <N>` (raw percent) in favor of
    `-l <N>%`. tmux 3.4 removed `-p` entirely — passing it now
    triggers a `size missing` error. Pin the `-l <N>%` form so a
    refactor doesn't slide back to the deprecated flag."""
    cmds = _build_tmux_recipe("relaydeck-x", "x", _ids(["alice"]))
    inbox_cmd = next(c for c in cmds if "inbox" in " ".join(c))
    # The flag we care about must be `-l` followed by a percent
    # length, not `-p` (which is the dead-since-3.4 form).
    assert "-l" in inbox_cmd, inbox_cmd
    li = inbox_cmd.index("-l")
    assert inbox_cmd[li + 1].endswith("%"), (
        f"expected `-l <N>%` form, got `{inbox_cmd[li + 1]}` — "
        f"tmux 3.4+ rejects raw integers here with `size missing`"
    )
    assert "-p" not in inbox_cmd, "deprecated `-p` flag must not appear"


def test_recipe_sets_remain_on_exit_so_panes_survive_failure():
    """If a pane's command exits immediately — e.g. `relaydeck attach`
    hitting a zombie agent — without `remain-on-exit on` the pane
    closes, the only window collapses, the session terminates, and
    the user sees `tmux attach: no sessions` instead of the actual
    error. Pin the set-window-option call so this regression can't
    come back silently."""
    cmds = _build_tmux_recipe("relaydeck-x", "x", _ids(["alice"]))
    rem = [c for c in cmds if c[1] == "set-window-option" and "remain-on-exit" in c]
    assert rem, "recipe must set remain-on-exit so failed panes stay visible"
    assert rem[0][-2:] == ["remain-on-exit", "on"]


def test_recipe_ends_with_tiled_layout_balance():
    """The grid should land in a balanced layout regardless of
    agent count — `tmux select-layout tiled` is the lowest-effort
    way to achieve that."""
    cmds = _build_tmux_recipe("relaydeck-demo", "demo", _ids(["a", "b", "c", "d"]))
    final = cmds[-1]
    assert final[1] == "select-layout"
    assert final[-1] == "tiled"


def test_recipe_targets_named_window_throughout():
    """All split-window / select-layout calls must reference the
    same `<session>:<window>` target so subsequent splits hit the
    right pane and don't spawn detached siblings."""
    cmds = _build_tmux_recipe("sess-x", "wsname", _ids(["a", "b"]))
    target = "sess-x:wsname"
    for c in cmds[1:]:  # skip new-session, which has no -t
        assert target in c, c


def test_recipe_single_agent_only_has_inbox_split():
    """Edge case: 1 running agent → new-session + inbox split +
    tiled. No extra split-window for the same agent."""
    cmds = _build_tmux_recipe("relaydeck-x", "x", _ids(["solo"]))
    splits = [c for c in cmds if c[1] == "split-window"]
    assert len(splits) == 1
    assert any("workspace inbox -f" in p for p in splits[0])
