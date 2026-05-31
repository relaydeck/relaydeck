---
name: relaydeck-plugin-dev
description: Author, test, install, and ship a relaydeck plugin — and choose the right tier first (core PR, community PyPI package, or a private/local plugin you manage in your own git and never push upstream). Read this when scaffolding a new plugin, deciding how to distribute it, wiring SDK surfaces, or preparing a release. Skip if the task is using relaydeck, not extending it.
metadata:
  short-description: Build + ship relaydeck plugins (core / community / private)
---

# relaydeck Plugin Development

## Step 0 — pick a tier (decide this first)

Not every plugin belongs on GitHub or PyPI. relaydeck recognizes three
audiences, and they differ only in **where the code lives and how it's
distributed** — the plugin API is identical. The daemon labels each loaded
plugin with a **Source** and a **Trust** level (`relaydeck plugin list`), so
you can always see which ones are yours.

| Tier | For | Lives in | Source / Trust | Distribute by |
|------|-----|----------|----------------|---------------|
| **Core** | Plugins that belong in relaydeck itself | `plugins/<name>/` in the relaydeck repo | `builtin` / `bundled` | **PR to `relaydeck/relaydeck`** |
| **Community** | Shareable, reusable by anyone | its own package repo | `installed` / `curated` or `local` | **publish to PyPI**, then `relaydeck plugin install` |
| **Private / user** | Just for you / your team — workflow glue, secrets, opinions | `~/.relaydeck/plugins/<name>/` or `<workspace>/plugins/<name>/` | `user` / `local` | **your own git, not pushed upstream** |

Rule of thumb: **default to private.** Promote to community only when someone
else would actually install it; promote to core only when it's
engine-essential and you're willing to maintain it in-tree. You can always
start private and graduate later — the code doesn't change, only packaging.

The trust ladder (high→low): `bundled > curated > local > untrusted`. An
`untrusted` plugin (an open PyPI / third-party entry point not in the curated
registry or lockfile) won't load unless approved on install or
`RELAYDECK_ALLOW_UNTRUSTED_PLUGINS=1` — private and curated plugins sidestep
this.

## Scaffold

```sh
# Private / local (just for you) — a plain dir in ~/.relaydeck/plugins/<name>/:
relaydeck plugin new my-plugin --local
relaydeck plugin new my-plugin --workspace <ws>     # scoped to one workspace

# Community / core (a publishable package in ./relaydeck-plugin-<name>/):
relaydeck plugin new my-plugin
relaydeck plugin new my-x --pattern harness|provider|skill
```

The **package** form (default) gives you, for community/core:

- `pyproject.toml` — name `relaydeck-plugin-<slug>`, depends on `relaydeck>=0.1.0`,
  declares `[project.entry-points."relaydeck.plugins"]`.
- `<module>/plugin.py` exposes a `PLUGIN = MyPlugin()` instance.
- `<module>/plugin.toml` beside it (manifest), with `py.typed`.

The **`--local` / `--workspace`** form gives you a private plain directory —
just `plugin.py` (exposing `PLUGIN`) + `plugin.toml` (+ `SKILL.md` for the
skill pattern), dropped in `~/.relaydeck/plugins/<name>/` (daemon-wide) or
`<workspace>/plugins/<name>/` (scoped). No `pyproject.toml`, no entry point,
no publish step — the loader discovers it directly and it loads as `local`
trust. This is the fastest path for a plugin only you use.

## Manifest essentials (`plugin.toml`)

```toml
[plugin]
host_api_version = 1
category = "harness" | "tool" | "cognitive" | "infrastructure"
workspace_scoped = false        # true only when this opts into individual workspaces
declared_capabilities = [        # must match the host APIs the code actually calls
  "events.subscribe", "events.emit",   # bus
  "kv.read", "kv.write",               # per-plugin kv store
  "workers.spawn",                     # background workers
  "cli.register", "api.register", "ui.register",
  "harnesses.register",                # for harness plugins
  "channels.register",                 # register a messaging channel (telegram/web/…)
  "prompts.read", "prompts.write",     # raise/resolve interactive approval prompts
  "vault.read",                        # + list narrow needs_vault keys
]

[plugin.settings]   # operator-editable; types: text/textarea/number/bool/enum/preset_ref/secret_ref
poll_interval_s = { type = "number", default = 60, description = "..." }

[plugin.skills]     # shipped skill files (materialized into workspaces by the bundled skills plugin)
my-skill = "SKILL.md"
```

## Imports — only public facades

```python
from relaydeck.sdk import Event, Plugin, PluginHost   # core (new-style plugin)
from relaydeck.harness import HarnessAgent             # harness plugins
from relaydeck.provider import ModelEntry, ProviderPlugin
from relaydeck.vault import get_secret
from relaydeck.automation import ActionContext, dispatch, parse_schedule  # automations
from relaydeck.testing import MockHost, MockBus, MockContext, make_plugin   # tests
```

Never import the official-plugins package (`plugins.*`) or core internals/loaders —
`publish-check` rejects it. Use only the public facades above.

## Build + verify (all tiers)

```sh
relaydeck plugin verify .            # manifest + declared SKILL.md sanity
relaydeck plugin lint .              # static checks
relaydeck plugin test .              # run the plugin's tests
relaydeck plugin dev .               # editable install + iterate (edits apply live)
uv run pytest                        # your tests
```

## Plugin UI (optional `static/`)

If your plugin ships a dashboard UI (`ui.register` + assets under `static/`),
build it on the shared **`@relaydeck/ui`** kit — the core dashboard and every
bundled plugin do. It is build-less, light-DOM [Lit](https://lit.dev): the daemon
importmaps the bare specifiers `lit` and `@relaydeck/ui`, so a panel/tile imports
the kit by name (no bundler, no per-plugin `esc()`/`<style>`):

```js
import { RelayElement, defineTile, html, button, card, openModal } from '@relaydeck/ui';
```

Keep the framework-neutral `mount(container, api, ctx)` / `unmount()` boundary
(`defineTile` exposes a `RelayElement` through it). Reach for the kit's themed
components (`button`, `card`, `<rd-toggle>`, `<rd-settings-form>`, modals) + the
CSS design tokens instead of hand-rolling chrome, so community UIs match the
dashboard for free. Full authoring guide + token contract:
**`relaydeck/web/static/uikit/README.md`**.

## Tier workflows

### Core — contribute to relaydeck

A core plugin is part of the relaydeck wheel (discovered by package scan, no
entry point). To add one:

1. Create `plugins/<name>/` in the relaydeck repo: `plugin.py` (`PLUGIN = …`)
   + `plugin.toml` (+ `SKILL.md`, `static/` as needed). No `pyproject.toml`.
2. If it should ship in the default experience, add its manifest `name` to
   `plugins/bundle.toml` under `[bundle.default]` (a test enforces that every
   discovered official plugin is in the bundle).
3. Add tests under `tests/`, run the full suite, `relaydeck plugin verify`.
4. **Open a PR against `relaydeck/relaydeck`.** Core plugins are reviewed and
   maintained in-tree; they load as `bundled` trust.

### Community — publish + share

A reusable package others install by name.

```sh
relaydeck plugin publish-check .     # wheel build + entry-point + packaged plugin.toml/py.typed/SKILL.md
relaydeck plugin publish             # validates, then `uv build` → dist/ artifacts
# upload dist/* with twine when ready (PyPI name: relaydeck-plugin-<slug>)
```

Then anyone can find and install it:

```sh
relaydeck plugin search <query>                 # curated registry first, then PyPI relaydeck-plugin-*
relaydeck plugin install relaydeck-plugin-<slug>
```

- **Version every release**: bump `plugin.toml` and `pyproject.toml` together;
  tag the git repo. Git installs must be pinned —
  `relaydeck plugin install git+https://…/plugin.git@v0.1.0` (floating URLs are
  rejected so `plugins.lock` keeps commit provenance).
- Open PyPI installs land as `untrusted` (approved to `local` on install). To
  reach `curated` trust + first-class discovery, get the package added to the
  relaydeck **curated registry** (pinned in `plugins.lock`).

### Private / user — manage with git, keep it yours

The default for personal or team-internal plugins. **Nothing is pushed
upstream**; you own the lifecycle.

Two homes (pick by scope):
- `~/.relaydeck/plugins/<name>/` — available to every workspace on this daemon.
- `<workspace>/plugins/<name>/` — scoped to one workspace (loads as
  `workspace:<ws>`); good for project-specific glue you don't want global.

Manage it with **your own git repo** (this is the recommended pattern):

```sh
# 0. Fastest start — scaffold straight into your config home (or a workspace):
relaydeck plugin new my-tool --local         # ~/.relaydeck/plugins/my-tool/
#    Then `git init` that dir for history; iterate; it loads on reload.

# Or keep it in its own repo and link it live (good if you'll publish later):
relaydeck plugin install --editable /path/to/my-tool   # or: relaydeck plugin dev .
#    Editable = your edits apply on the next daemon reload, no reinstall.

# Iterate + commit to YOUR remote (origin), never to relaydeck/relaydeck or PyPI:
git add -A && git commit -m "tweak my workflow plugin" && git push origin main

# See ONLY your custom plugins (bundled ones hidden) — Source/Trust flag them:
relaydeck plugin list --mine     # yours: Source=user|workspace:… , Trust=local
relaydeck plugin show <name>     # details for one

# Pull in changes for non-editable local/git installs:
relaydeck plugin update [name]   # reinstalls from recorded source
#    (editable installs already point at your working tree — nothing to do)

relaydeck plugin disable <name>  # turn off without removing
relaydeck plugin uninstall <name>
```

Notes for private plugins:
- A plain directory in `~/.relaydeck/plugins/` needs **no packaging** — fastest
  path. Add a `pyproject.toml` only if you later want to publish or `pip`-install it.
- They load as `local` trust (no untrusted gate), so they "just work" for you
  without the PyPI/curated approval dance.
- Keep secrets in the **vault** (`needs_vault` + `host.vault`), not in the
  plugin source you commit — even to a private repo.
- Graduating later is free: a private package is already community-shaped;
  `publish` it to share, or move the dir into the relaydeck repo + open a PR to
  make it core.
