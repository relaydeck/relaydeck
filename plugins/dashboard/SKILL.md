---
name: relaydeck-dashboard
description: Reshape the live relaydeck web dashboard. Read this when asked to rearrange, add, remove, move, or resize Home widgets, switch the theme/density/glow, or "make the dashboard look like X". Changes apply instantly to every open dashboard. Drive it with the `relaydeck dashboard` CLI.
metadata:
  short-description: Restyle and rearrange the relaydeck dashboard
---

# Controlling the relaydeck dashboard

The dashboard is a 12-column Home widget grid plus an appearance layer
(theme / density / glow). You change it through the **`relaydeck dashboard`**
CLI — every command POSTs to the daemon, which broadcasts the change so it
applies **instantly** to every open dashboard (no reload). A daemon must be
running.

## See what's there first

```sh
relaydeck dashboard get          # theme / density / glow + saved widget grid
```

For the full theme list use `relaydeck theme list`. To author or recolor a
theme (vs. just switching to one), use the **relaydeck-theme** skill.

## Appearance

```sh
relaydeck dashboard theme ink            # any theme from `relaydeck theme list`
relaydeck dashboard density compact      # compact | comfy | regular
relaydeck dashboard glow off             # on | off
```

## Widgets (the Home grid)

The grid is **12 columns wide**; `x`/`y` are 0-based cell coordinates and
`w`/`h` are cell spans. Widget keys:

`fleet` · `usage` · `agents` · `feed` · `workspaces` · `spawn` · `workers`
· `worktrees` · `clock` · `notes` · `focus`

```sh
relaydeck dashboard add workers          # add a widget
relaydeck dashboard remove clock         # remove it
relaydeck dashboard move usage 8 0       # move 'usage' to column 8, row 0
relaydeck dashboard resize agents 6 4    # resize 'agents' to 6 wide x 4 tall
relaydeck dashboard tidy                 # auto-arrange, remove gaps
relaydeck dashboard reset                # back to the default layout
```

## Notes

- Invalid values are rejected with the allowed set (e.g. an unknown widget or
  theme) — read the error and retry; nothing partial is applied.
- **`theme` / `density` / `glow` persist server-side** and `dashboard get`
  reflects them immediately — no browser needs to be open. They default to the
  global (dashboard-wide) scope; pass `--workspace <name>` to scope one
  workspace, and `dashboard get --workspace <name>` to read that scope.
- **The saved Home widget grid** is also in `dashboard get` (from
  `preferences.yaml`). When nothing is saved yet, `get` shows the package
  default layout. Live `add`/`move`/`resize`/`tidy`/`reset` ops apply instantly
  in open browsers; the browser persists the grid back to the server.
- **Widget ops (`add`/`remove`/`move`/`resize`/`tidy`/`reset`) apply to open
  dashboards live** — the `✓` from the command is your confirmation once the
  browser applies it.
- This is the same capability the relaydeck-native agent's `dashboard` tool
  has, exposed as a CLI so any harness can use it.
