# Contributing to relaydeck

relaydeck is a micro-agent orchestrator: a web-primary daemon, a CLI at parity,
and a plugin system that everything else is built on. Two things make a change
mergeable: it ships with tests, and it respects the hard rules in
[`AGENTS.md`](AGENTS.md) (everything is a plugin, YAML is the spec, the web
dashboard is the primary interface, migrations are additive, …).

## Contribution gate

To keep the tracker readable, **issues and PRs from first-time contributors are
auto-closed by default**, with a comment explaining how to get reopened.
Repository collaborators are always exempt.

- **Understand your code.** This is the one rule. Using AI to help write a change
  is fine; opening a PR you can't explain is not — it will be closed.
- **Open an issue first** using one of the
  [issue templates](.github/ISSUE_TEMPLATE). Keep it short, concrete, and in your
  own voice — if it doesn't fit on a screen, it's too long.
- A maintainer reply of **`lgtmi`** keeps your future *issues* open; **`lgtm`**
  keeps your future *issues and PRs* open. Don't open a PR before you've been
  approved with `lgtm`. Approval takes effect after the bot updates
  [`.github/APPROVED_CONTRIBUTORS`](.github/APPROVED_CONTRIBUTORS) — give it a
  minute before opening your next issue or PR.
- Mass-submitting agent-generated issues/PRs will get an account blocked.

This is a guardrail against tracker spam and maintainer burnout, not hostility —
thoughtful, reproducible contributions are very welcome.

## Development setup

```sh
uv sync --group dev          # install runtime + dev deps into .venv
uv run pytest                # the fast suite (see TESTING.md)
uv run relaydeck doctor      # sanity-check the local install
```

See [`TESTING.md`](TESTING.md) for how the suite is layered (unit, plugin
contract, e2e, install) and the conventions for adding tests — real I/O at the
boundaries, no mocks.

## Plugin packages

Plugins are the unit of extension. There are two shapes:

- **Official** — every relaydeck-managed plugin lives in the root `plugins/`
  package: the infra plugins (vault, github, loop, harnesses, external_agents)
  alongside the extensions (messaging, telegram, skills, theme, metering,
  gateway, file_watcher, usage_limits, hitl, dashboard, providers).
- **External package** — a standalone distribution discovered through the
  `relaydeck.plugins` Python entry-point group. Recommend one via the curated
  registry (`plugins/registry.yaml`); see [AGENTS.md](AGENTS.md) and the plugin
  rules below.

### Layout of an external plugin package

Scaffold one with `relaydeck plugin new my-plugin --pattern <pattern>`. The
pattern picks the starter code; choices are
`reactor` (default) `| workflow | harness | provider | ui | cli | skill`. The
expected layout:

```
relaydeck-plugin-my-plugin/
  pyproject.toml            # depends on relaydeck; declares the entry point
  my_plugin/
    __init__.py
    plugin.py               # exposes a module-level PLUGIN instance
    plugin.toml             # manifest, beside the plugin module
    py.typed                # ship inline types
    SKILL.md                # if it contributes a skill ([plugin.skills])
  tests/
    test_plugin.py          # uses relaydeck.testing (MockHost/MockBus/MockContext)
```

Requirements for a publishable plugin package:

- Depend on **`relaydeck`** (the published package).
- Declare `[project.entry-points."relaydeck.plugins"]` pointing at the module
  exposing `PLUGIN` (e.g. `my-plugin = "my_plugin.plugin:PLUGIN"`).
- Keep `plugin.toml` and `py.typed` **beside** the entry-point module, and list
  any declared `SKILL.md` files.
- Import only the **public facades** — `relaydeck.sdk`, `relaydeck.harness`,
  `relaydeck.provider`, `relaydeck.testing`, `relaydeck.vault` — not bundled
  implementation modules (`plugins.*`, internal loaders).

### Authoring workflow

```sh
relaydeck plugin new my-plugin                 # scaffold
relaydeck plugin dev ./relaydeck-plugin-my-plugin       # editable dev checkout
relaydeck plugin verify ./relaydeck-plugin-my-plugin    # manifest + skill-file validation
relaydeck plugin publish-check ./relaydeck-plugin-my-plugin  # tests + wheel + packaged contents
relaydeck plugin install --editable ./relaydeck-plugin-my-plugin
```

`publish-check` runs the package's tests (when `uv` is present), builds a
temporary wheel, and verifies the wheel's `relaydeck.plugins` entry plus the
packaged `plugin.toml`, `py.typed`, and declared `SKILL.md` files. Run it before
sharing or publishing.

Git installs must be **pinned** so `plugins.lock` can record provenance:

```sh
relaydeck plugin install git+https://github.com/acme/relaydeck-plugin-my-plugin.git@v0.1.0
```

## Package boundary

One repo, two packages: `relaydeck/` (engine) and `plugins/` (bundled
plugins). Keep them decoupled:

- **core must not import `plugins`** — bundled plugins are discovered by
  the package scanner / `relaydeck.plugins` entry points, not imported by core.
- plugin / harness / provider / test code imports only the public facades:
  `relaydeck.sdk`, `relaydeck.harness`, `relaydeck.provider`, `relaydeck.testing`,
  `relaydeck.vault` — never internal modules.

## Sending a change

- Ship the web affordance for any new CLI capability in the same change — the
  dashboard is the primary interface, the CLI is at parity, not ahead.
- Keep files under the ~600 LOC soft cap; prefer editing over new files.
- Run `uv run pytest` green before merge.
- Verify dashboard/UI changes in a real browser via the Playwright MCP
  (`docs/playwright.md` is the cheat sheet).

## Commit / authorship

Commits use the author's own identity; do not add AI co-author trailers.

## Thanks

relaydeck's contribution gate — the auto-close flow, the `lgtm`/`lgtmi`
approval, and the "understand your code" rule — is adapted, with gratitude, from
the [pi](https://github.com/earendil-works/pi) project's `CONTRIBUTING.md`. Thank
you to the pi maintainers. See [CREDITS.md](CREDITS.md) for full
acknowledgements.
