"""Static system-prompt composition breakdown.

Answers "how is this agent's system prompt built?" by assembling the real
pieces relaydeck composes/injects — the auto identity preamble, the
operator's free-form `system_prompt`, each injected skill body, and the
per-agent fleet-context file — and measuring each one.

Tokens: there is no tokenizer in-tree, and the runtime token counts we
tail from harness JSONL (`usage_records`) are *per LLM call* (system
prompt + conversation history + tool defs together), so they cannot be
sliced per-component. We therefore report **exact character counts**
(real) plus a clearly-labeled **~chars/4 token estimate**. The UI surfaces
both and never presents the estimate as exact.

Pure + read-only so the Identity tab, the spawn-modal preview, and tests
share one source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Rough English/code average. The UI labels derived token counts "~est".
CHARS_PER_TOKEN = 4


def est_tokens(chars: int) -> int:
    """A deliberately coarse token estimate from a character count."""
    return round(chars / CHARS_PER_TOKEN)


def _component(
    index: int, label: str, source: str, source_plugin: str, text: str,
) -> dict[str, Any]:
    chars = len(text or "")
    return {
        "index": index,
        "label": label,
        "source": source,
        "source_plugin": source_plugin,
        "chars": chars,
        "est_tokens": est_tokens(chars),
        "text": text or "",
    }


def _result(agent_id: str, comps: list[dict[str, Any]]) -> dict[str, Any]:
    total_chars = sum(c["chars"] for c in comps)
    return {
        "agent_id": agent_id,
        "components": comps,
        "total_chars": total_chars,
        "total_est_tokens": est_tokens(total_chars),
        "chars_per_token": CHARS_PER_TOKEN,
    }


def _workspace_plugins(config_home: Path, workspace: str) -> set[str]:
    """Plugins enabled for a workspace (agent.toml) — the same gate list the
    harness reads at spawn. Empty on any read error."""
    import tomllib
    toml = config_home / "workspaces" / workspace / "agent.toml"
    try:
        data = tomllib.loads(toml.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    plugins = data.get("workspace", {}).get("plugins", [])
    return set(plugins) if isinstance(plugins, list) else set()


def compose_prompt_components(
    config_home: Path,
    *,
    agent_id: str,
    workspace: str | None,
    agent_type: str = "",
    purpose: str = "",
    system_prompt: str = "",
    peers: list[dict] | None = None,
    inject_preamble: bool = True,
) -> dict[str, Any]:
    """Build the ordered list of real prompt components for an agent — exactly
    what the harness injects, gated the same way it gates at spawn.

    Returns ``{agent_id, components[], total_chars, total_est_tokens,
    chars_per_token}``. Mirrors the spawn-time composition order: identity
    preamble → operator system_prompt → injected skills → fleet-context.

    Gating (so the Identity tab never shows what the model won't receive):
      - user-authored skills are included only when ``skills`` is in the
        workspace's ``agent.toml`` (the harness gate); runtime/plugin skills
        (e.g. messaging) are always injected.
      - fleet-context is included only when the ``fleet-context`` gate is on.
    """
    from relaydeck.harness import compose_identity_preamble

    peers = peers or []
    comps: list[dict[str, Any]] = []
    i = 0

    enabled = _workspace_plugins(config_home, workspace) if workspace else set()

    ws_path = None
    if workspace:
        from relaydeck.config import load_workspace_registry
        ws_row = next(
            (w for w in load_workspace_registry(config_home) if w.name == workspace),
            None,
        )
        if ws_row:
            ws_path = ws_row.path

    # 1. Auto identity preamble (id/purpose/peers/messaging contract).
    if inject_preamble:
        text = compose_identity_preamble(
            agent_id, workspace, purpose, peers,
            workspace_path=ws_path, config_home=config_home,
        )
        if text.strip():
            i += 1
            comps.append(_component(
                i, "identity preamble", "_build_identity_preamble · auto",
                "core", text,
            ))

    # 2. Operator's free-form system_prompt (YAML).
    if (system_prompt or "").strip():
        i += 1
        comps.append(_component(
            i, "system_prompt (yaml)", f"agents/{agent_id}.yaml", "core",
            system_prompt,
        ))

    if workspace:
        # 3. Injected skills — exactly the valid+injectable set the harness
        #    materializes (skills.py is the single source of truth), gated the
        #    same way the harness gates: user-authored skills only when the
        #    `skills` plugin is enabled; runtime/plugin skills always.
        from relaydeck.skills import (
            SOURCE_RUNTIME_PLUGIN,
            SOURCE_WORKSPACE,
            discover_workspace_skills,
        )

        skills_enabled = "skills" in enabled
        for ref in discover_workspace_skills(config_home, workspace):
            if not ref.valid or not ref.injectable:
                continue
            if ref.source_type == SOURCE_WORKSPACE and not skills_enabled:
                continue  # user skills are gated behind `skills` in agent.toml
            try:
                body = (Path(ref.path) / "SKILL.md").read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                continue
            if ref.source_type == SOURCE_RUNTIME_PLUGIN:
                plug = ref.owner_plugin or "skills"
                src = f"plugin · {ref.owner_plugin}" if ref.owner_plugin else "runtime"
            else:
                plug = "skills"
                src = f"workspaces/{workspace}/skills/"
            i += 1
            comps.append(_component(i, f"skill · {ref.name}", src, plug, body))

        # 4. Per-agent generated fleet context — only when the fleet-context
        #    gate is enabled (the harness injects it on the same condition).
        if "fleet-context" in enabled:
            fc = (config_home / "workspaces" / workspace / "runtime"
                  / "fleet-context" / f"{agent_id}.md")
            if fc.exists():
                try:
                    text = fc.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                if text.strip():
                    i += 1
                    comps.append(_component(
                        i, "fleet context",
                        f"runtime/fleet-context/{agent_id}.md", "fleet-context",
                        text,
                    ))

    return _result(agent_id, comps)
