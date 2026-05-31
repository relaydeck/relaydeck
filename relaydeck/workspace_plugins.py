"""
Canonical catalog of names a workspace can opt into via its
`plugins = [...]` list in `agent.toml`.

Two kinds of entries:

  - **plugin-backed**: a real `RelaydeckPlugin` that opted into being
    workspace-scoped by setting `workspace_scoped = True`. The plugin
    itself gates some of its behavior on the workspace listing it
    (e.g. messaging materializes a SKILL.md only for opted-in workspaces).

  - **harness-gate**: a bare name that the pi/codex harnesses recognize
    in their `_INJECTORS` / `_PROMPT_INJECTORS` dicts. No Python plugin
    backs them — the injection logic lives in the harness. Centralized
    here so the dashboard, CLI, and any future harness all see the
    same list.

Everything else relaydeck ships (harnesses, providers, infrastructure like
file-watcher, gateway, metering, vault, ...)
is **always-on per machine**. Putting them in a workspace's plugin
list has no effect — and the dashboard now hides them so users
don't try.
"""

from __future__ import annotations

from typing import Any


# Hardcoded harness-gate names — must match keys in
# `pi/agent.py:PiAgent._INJECTORS` and
# `codex/agent.py:CodexAgent._PROMPT_INJECTORS`.
# The plugins a fresh agent benefits from out of the box — pre-checked in
# the new-agent modal, but always opt-out. Not domain-specific: these are
# relaydeck's core collaboration/memory/skill capabilities, useful for any
# fleet. The operator can de-select any of them per workspace.
RECOMMENDED: frozenset[str] = frozenset({"messaging", "skills"})


HARNESS_GATES: list[dict[str, str]] = [
    {
        "name": "skills",
        "description": (
            "Load user-authored skills from `workspaces/<ws>/skills/*/SKILL.md` "
            "and inject them via pi `--skill` / codex prompt addendum."
        ),
        "category": "harness-gate",
    },
    {
        "name": "fleet-context",
        "description": (
            "Inject generated fleet context from "
            "`workspaces/<ws>/runtime/fleet-context/<agent>.md`."
        ),
        "category": "harness-gate",
    },
    {
        "name": "forbidden-tools",
        "description": (
            "Tell the agent it must avoid certain tools or command families "
            "(read from `agent.config.forbidden_tools`)."
        ),
        "category": "harness-gate",
    },
]


def list_workspace_plugins(registry) -> list[dict[str, Any]]:
    """Return the union of plugin-backed workspace-scoped plugins and
    harness gates, with metadata the dashboard can render directly.

    Sorted alphabetically by `name`. Each entry has:
      - name, description, category
      - kind: "plugin" | "harness-gate"
      - For plugin-kind entries also: version, source (builtin/user),
        globally_enabled (False if disabled via Plugins tab)
    """
    from relaydeck.plugin_disabled import disabled_set

    disabled = disabled_set()
    out: list[dict[str, Any]] = []

    # Plugin-backed entries
    for entry in registry.discovered_all():
        if not getattr(entry.instance, "workspace_scoped", False):
            continue
        out.append({
            "name": entry.name,
            "description": getattr(entry.instance, "description", ""),
            "category": entry.category,
            "version": entry.version,
            "source": entry.source,
            "kind": "plugin",
            "globally_enabled": entry.name not in disabled,
            "recommended": entry.name in RECOMMENDED,
        })

    # Harness-gate entries — no python plugin behind them
    for gate in HARNESS_GATES:
        out.append({
            "name": gate["name"],
            "description": gate["description"],
            "category": gate["category"],
            "version": "—",
            "source": "harness",
            "kind": "harness-gate",
            "globally_enabled": True,  # always available; no daemon-load gate
            "recommended": gate["name"] in RECOMMENDED,
        })

    return sorted(out, key=lambda x: x["name"])
