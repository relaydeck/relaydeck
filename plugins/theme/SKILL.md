---
name: relaydeck-theme
description: Author dashboard themes. Read this when asked to create, edit, recolor, or restyle the dashboard — change the accent, build a dark/high-contrast palette, set a per-workspace look, or share a theme as a file. Skip if the task isn't about appearance.
metadata:
  short-description: Create + edit relaydeck dashboard themes
---

# Authoring relaydeck themes

A **theme** is a named bundle of design-token overrides (CSS custom
properties). It may `extends` another theme, inheriting its tokens and
overriding a few. Themes apply live — no restart — and can be set globally
or per workspace. Everything below is on the `relaydeck theme` CLI (the web
Appearance lens is the GUI).

## The token contract

Don't fabricate token names — pull the live list:

```sh
relaydeck theme show base --resolved   # the source-of-truth token map
relaydeck theme show amber             # see what a builtin overrides
```

Roughly: `bg-0…bg-4` (surfaces), `t-1…t-4` (text), `line-1…line-4` (borders),
`acc` + `acc-d`/`-soft`/`-line`/`-glow`/`-text` (accent set), `ok`/`warn`/`err`/
`info`/`pink`/`violet` (+ `-soft`) status, `f-sans`/`f-mono` fonts, `r-0…r-4`
radii, `row-h`/`pad-x`/`pad-y`/`side-w`/`hdr-h` spacing. Color tokens accept
any CSS color (`#67e8f9`, `rgba(…)`); sizes take CSS lengths. Unknown
tokens are rejected — confirm against `theme show base`.

Builtin starting points: `base` (paper default), `ink`, `cyan`, `green`,
`amber`, `violet`, `mono`, `gruvbox-dark`, `daylight`.

## Recipe — create + edit + apply

```sh
# Recolor the accent on top of a builtin
relaydeck theme create ocean --extends base \
  --display-name "Ocean" \
  --set acc=#38bdf8 --set acc-d=#0ea5e9 \
  --set acc-soft="rgba(56,189,248,.10)" --set acc-line="rgba(56,189,248,.30)"

# Tweak it later
relaydeck theme edit ocean --set bg-0=#020617 --unset acc-glow

# Apply (global vs one workspace)
relaydeck theme set ocean                # global default
relaydeck theme set ocean -w prod        # only the `prod` workspace
relaydeck theme appearance -w prod       # what's resolved for `prod`
```

**relaydeck-native agents** also have a `relaydeck_dashboard` pi tool —
`op=theme value=daylight` applies to the agent's workspace when
`RELAYDECK_WORKSPACE` is set. Do not use peer messaging for theme changes.

Editing a builtin (`theme edit amber …`) saves an editable copy that
**shadows** the builtin; `theme rm <name>` removes the copy and the builtin
returns. `extends` is preferred over copying every token so future base
tweaks still flow through.

## Inspect, share, clean up

```sh
relaydeck theme list
relaydeck theme show ocean --resolved          # flattened token map (what paints)
relaydeck theme export ocean --out ocean.yaml  # shareable file
relaydeck theme import ocean.yaml [--name x]
relaydeck theme rm ocean                       # user themes only; builtins refuse
```
