"""
Per-harness-type launch-option catalog for the new-agent flow.

The new-agent modal is **type-first**: the operator picks a harness, then
sees options appropriate to *that* CLI (dangerous/yolo mode, plan mode,
continue/resume a session, sandbox policy, free-form extra args). This
module is the single source of truth for that mapping so the web modal,
the CLI, and any future surface describe the same options.

An *option descriptor* is a JSON-serializable dict:

    {
      "key":   internal id (unique within a type),
      "label": short toggle/field label,
      "help":  one-line explanation,
      "kind":  "bool" | "text" | "select" | "args",
      "danger": True            # optional — render with a warning accent
      "default": <value>        # optional
      "placeholder": "..."      # optional (text/args)
      "options": [{value,label}]# select only
      "apply": {                # how a set value maps to the agent's launch
        "arg": "--flag",        #   bool→append flag tokens; text→[flag, value]
        "config": "key",        #   bool→config[key]=True; select/text→config[key]=value
        "from": "text",         #   marks a text field that supplies the value
        "args_passthrough": True#   kind="args": shell-split into config.args
      }
    }

`apply.arg` may contain spaces (e.g. "--permission-mode plan"); consumers
split it into tokens. Everything ultimately lands in the agent's
`config` (config keys + `config.args`), which every harness already reads
— so CLI parity is `relaydeck agent create --config key=value`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

# Free-form passthrough — offered by every harness as the escape hatch.
_EXTRA_ARGS: dict[str, Any] = {
    "key": "extra_args",
    "label": "Extra flags",
    "help": "Passed to the harness CLI as-is (space-separated).",
    "kind": "args",
    "placeholder": "--example-flag value",
    "apply": {"args_passthrough": True},
}

# High-level autonomy knob, shared by the CLI harnesses that have approval/
# sandbox prompts (claude/codex/opencode — NOT pi, which never prompts). A
# relaydeck agent runs unattended, so "auto" (the default) is the recommended
# path: each harness runs safe work without prompting and guards dangerous ops
# (claude classifier, codex sandbox, opencode allow), while the relaydeck CLI
# stays auto-allowed so peer messaging never stalls. The low-level
# sandbox/approval/yolo options below remain as operator overrides. Empty value
# ("") leaves config.autonomy unset → the "auto" default. See
# HarnessAgent._autonomy_mode.
_AUTONOMY_OPTION: dict[str, Any] = {
    "key": "autonomy",
    "label": "Autonomy",
    "help": "How the agent handles approval prompts when running unattended. "
            "auto = run safe work, guard dangerous ops (recommended); "
            "bypass = no checks; locked = only the relaydeck CLI + allowlist; "
            "manual = drive the harness's own flags below yourself.",
    "kind": "select",
    "options": [
        {"value": "", "label": "auto (recommended)"},
        {"value": "bypass", "label": "bypass — no guardrails"},
        {"value": "locked", "label": "locked — fail-safe allowlist"},
        {"value": "manual", "label": "manual — set flags below"},
    ],
    "apply": {"config": "autonomy"},
}


# Primary harness types in display order, with their metadata + curated
# launch options. `aliases` are the other registered names that resolve to
# the same harness (we never show both `codex` and `codex-cli`).
HARNESS_META: dict[str, dict[str, Any]] = {
    "claude-code": {
        "label": "Claude Code",
        "blurb": "Anthropic Claude Code — agentic coding in a PTY.",
        "icon": "command",
        # Brand identity for the new-agent wizard. `brand` is a stable id the
        # web layer maps to a logo (graceful monogram fallback for unknown
        # ids); `accent` tints the harness's curated step. Kept here so a new
        # harness plugin declares its own look without the frontend branching
        # on type names.
        "brand": "claude",
        "accent": "#C1623E",
        "kind": "native",
        "aliases": ["claude"],
        # claude's `--model` only takes Anthropic models out of the box (it
        # has no provider auth for others). A finite list here means the
        # wizard warns when a preset resolves to a different provider; a
        # missing key (pi/opencode) means "any — the CLI brings its own
        # provider auth". See `native_providers` in build_harness_catalog.
        "native_providers": ["anthropic"],
        "model_policy": "native",
        "model_config_key": "claude_model",
        "model_placeholder": "sonnet  ·  or deepseek/deepseek-chat (external)",
        "model_help": "Anthropic model alias or id (e.g. sonnet, claude-sonnet-4-6). "
                      "Routing to an external endpoint below? Put the EXTERNAL id "
                      "here instead (e.g. deepseek/deepseek-chat). "
                      "Empty = Claude Code account default.",
        "launch_options": [
            _AUTONOMY_OPTION,
            {
                "key": "claude_base_url",
                "label": "External endpoint (ANTHROPIC_BASE_URL)",
                "help": "Route Claude Code at an OpenRouter / deepseek-anthropic / "
                        "LiteLLM-style endpoint instead of api.anthropic.com — e.g. "
                        "https://openrouter.ai/api/v1, https://api.deepseek.com/anthropic, "
                        "or a local proxy (http://localhost:3010) to reach ollama/vllm. "
                        "Set Model above to the external id. Empty = your Anthropic account. "
                        "Note: Claude Code speaks the Anthropic Messages API — endpoints that "
                        "aren't Anthropic-compatible need a translating proxy.",
                "kind": "text", "placeholder": "https://openrouter.ai/api/v1",
                "apply": {"config": "claude_base_url"},
            },
            {
                "key": "claude_key_env",
                "label": "External token — vault key",
                "help": "Vault key holding the token for the external endpoint "
                        "(injected as ANTHROPIC_AUTH_TOKEN, the Bearer path proxies "
                        "and OpenRouter/deepseek accept). E.g. OPENROUTER_API_KEY, "
                        "DEEPSEEK_API_KEY. Only used when "
                        "an external endpoint is set; left blank, relaydeck infers it "
                        "from the endpoint host (openrouter.ai → OPENROUTER_API_KEY).",
                "kind": "text", "placeholder": "OPENROUTER_API_KEY",
                "apply": {"config": "claude_key_env"},
            },
            {
                "key": "dangerous",
                "label": "Skip permission prompts",
                "help": "--dangerously-skip-permissions: bypass all tool "
                        "permission checks. Use only in a trusted directory.",
                "kind": "bool", "danger": True,
                "apply": {"arg": "--dangerously-skip-permissions"},
            },
            {
                "key": "plan",
                "label": "Plan mode",
                "help": "--permission-mode plan: start read-only and propose a "
                        "plan before making any changes.",
                "kind": "bool",
                "apply": {"arg": "--permission-mode plan"},
            },
            {
                "key": "continue",
                "label": "Continue last session",
                "help": "--continue: resume the most recent session in this "
                        "workspace.",
                "kind": "bool",
                "apply": {"arg": "--continue"},
            },
            {
                "key": "resume",
                "label": "Resume a specific session",
                "help": "--resume <id>: continue a named past session.",
                "kind": "text", "placeholder": "session id",
                "apply": {"arg": "--resume", "from": "text"},
            },
            {
                "key": "verbose",
                "label": "Verbose diagnostics",
                "help": "--verbose: print harness diagnostics on stderr.",
                "kind": "bool",
                "apply": {"arg": "--verbose"},
            },
            _EXTRA_ARGS,
        ],
    },
    "codex-cli": {
        "label": "Codex",
        "blurb": "OpenAI Codex CLI — sandboxed agentic coding.",
        "icon": "command",
        "brand": "openai",
        "accent": "#0E8C6E",
        "kind": "native",
        "aliases": ["codex"],
        "native_providers": ["openai"],
        "model_policy": "native",
        "model_config_key": "codex_model",
        "model_placeholder": "gpt-5.3-codex · or llama3.2:latest (local)",
        "model_help": "OpenAI/Codex model id (e.g. gpt-5.3-codex). Empty = your "
                      "Codex account default (ChatGPT or API). Using a Local "
                      "model provider below? Put the LOCAL model id here instead "
                      "(e.g. llama3.2:latest).",
        "launch_options": [
            _AUTONOMY_OPTION,
            {
                "key": "oss_provider",
                "label": "Local model provider (ollama / LM Studio)",
                "help": "Route Codex at a LOCAL open-source model server instead "
                        "of your OpenAI/ChatGPT account, via Codex's built-in "
                        "providers. Set Model above to a local id (e.g. "
                        "llama3.2:latest). Codex needs the model to support "
                        "function/tool calling — small models may not. Note: "
                        "cloud Chat-Completions endpoints (DeepSeek, OpenRouter) "
                        "are NOT reachable — Codex requires the OpenAI Responses "
                        "API, which they don't implement.",
                "kind": "select",
                "options": [
                    {"value": "", "label": "default (OpenAI / ChatGPT account)"},
                    {"value": "ollama", "label": "ollama (localhost:11434)"},
                    {"value": "lmstudio", "label": "LM Studio (localhost:1234)"},
                ],
                "apply": {"config": "codex_local_provider"},
            },
            {
                "key": "continue",
                "label": "Continue last session",
                "help": "codex resume --last: continue the most recent session "
                        "(restores its model + sandbox).",
                "kind": "bool",
                "apply": {"config": "resume_last"},
            },
            {
                "key": "dangerous",
                "label": "YOLO — bypass approvals + sandbox",
                "help": "--dangerously-bypass-approvals-and-sandbox: full auto, "
                        "no prompts, no sandbox. Use carefully.",
                "kind": "bool", "danger": True,
                "apply": {"config": "dangerously_bypass_approvals_and_sandbox"},
            },
            {
                "key": "sandbox",
                "label": "Sandbox policy",
                "help": "How much filesystem access Codex's tools get.",
                "kind": "select",
                "options": [
                    {"value": "", "label": "default"},
                    {"value": "read-only", "label": "read-only"},
                    {"value": "workspace-write", "label": "workspace-write"},
                    {"value": "danger-full-access", "label": "danger-full-access"},
                ],
                "apply": {"config": "sandbox"},
            },
            {
                "key": "approval",
                "label": "Ask for approval",
                "help": "When Codex pauses to ask before acting.",
                "kind": "select",
                "options": [
                    {"value": "", "label": "default"},
                    {"value": "untrusted", "label": "untrusted"},
                    {"value": "on-failure", "label": "on-failure"},
                    {"value": "on-request", "label": "on-request"},
                    {"value": "never", "label": "never"},
                ],
                "apply": {"config": "ask_for_approval"},
            },
            {
                "key": "search",
                "label": "Web search tools",
                "help": "--search: let Codex use web search.",
                "kind": "bool",
                "apply": {"config": "search"},
            },
            _EXTRA_ARGS,
        ],
    },
    "pi": {
        "label": "pi",
        "blurb": "pi — agentic coding harness.",
        "icon": "command",
        "brand": "pi",
        "accent": "#6D5BA8",
        "kind": "native",
        "aliases": ["pi-harness"],
        "model_policy": "flex",
        "model_help": "relaydeck preset or provider/model — pi must have that "
                      "provider configured in ~/.pi.",
        "launch_options": [
            {
                "key": "continue",
                "label": "Continue last session",
                "help": "--continue: resume the previous session.",
                "kind": "bool",
                "apply": {"arg": "--continue"},
            },
            {
                "key": "session",
                "label": "Resume a specific session",
                "help": "--session <id>: resume a session by id.",
                "kind": "text", "placeholder": "session id",
                "apply": {"arg": "--session", "from": "text"},
            },
            {
                "key": "thinking",
                "label": "Thinking effort",
                "help": "--thinking <level>: reasoning budget.",
                "kind": "select",
                "options": [
                    {"value": "", "label": "default"},
                    {"value": "off", "label": "off"},
                    {"value": "minimal", "label": "minimal"},
                    {"value": "low", "label": "low"},
                    {"value": "medium", "label": "medium"},
                    {"value": "high", "label": "high"},
                    {"value": "xhigh", "label": "xhigh"},
                ],
                "apply": {"arg": "--thinking"},
            },
            _EXTRA_ARGS,
        ],
    },
    "opencode-cli": {
        "label": "OpenCode",
        "blurb": "OpenCode TUI — open-source agentic coding.",
        "icon": "command",
        "brand": "opencode",
        "accent": "#2F6F8F",
        "kind": "native",
        "aliases": ["opencode"],
        "model_policy": "flex",
        "model_config_key": "opencode_model",
        "model_help": "relaydeck preset or provider/model — run "
                      "`opencode providers` to configure auth for that provider.",
        "launch_options": [
            _AUTONOMY_OPTION,
            {
                "key": "continue",
                "label": "Continue last session",
                "help": "--continue: resume the most recent session.",
                "kind": "bool",
                "apply": {"config": "continue"},
            },
            {
                "key": "session",
                "label": "Resume a specific session",
                "help": "--session <id>: continue a named past session.",
                "kind": "text", "placeholder": "session id",
                "apply": {"config": "session", "from": "text"},
            },
            {
                "key": "opencode_agent",
                "label": "OpenCode sub-agent",
                "help": "--agent <name>: pick a configured OpenCode agent.",
                "kind": "text", "placeholder": "build",
                "apply": {"config": "opencode_agent", "from": "text"},
            },
            _EXTRA_ARGS,
        ],
    },
    "cursor-cli": {
        "label": "Cursor",
        "blurb": "Cursor CLI (cursor-agent) — agentic coding on your Cursor account.",
        "icon": "command",
        "brand": "cursor",
        "accent": "#5A6373",
        "kind": "native",
        "aliases": ["cursor", "cursor-agent"],
        # Cursor brings its own auth + model namespace (Composer/Pro), so any
        # provider is fine — set config.cursor_model (e.g. "sonnet-4") to pin
        # a model; otherwise it uses the account's selected model.
        "model_policy": "native",
        "model_config_key": "cursor_model",
        "model_placeholder": "composer-2 (optional)",
        "model_help": "Cursor model id (e.g. sonnet-4, composer-2). "
                      "Empty = account default. Run `cursor-agent models`.",
        "launch_options": [
            _AUTONOMY_OPTION,
            {
                "key": "plan",
                "label": "Plan mode",
                "help": "--plan: start read-only, propose a plan before editing.",
                "kind": "bool",
                "apply": {"arg": "--plan"},
            },
            {
                "key": "ask",
                "label": "Ask mode",
                "help": "--mode ask: read-only Q&A, no edits.",
                "kind": "bool",
                "apply": {"arg": "--mode ask"},
            },
            {
                "key": "continue",
                "label": "Continue last session",
                "help": "--continue: resume the most recent chat.",
                "kind": "bool",
                "apply": {"arg": "--continue"},
            },
            {
                "key": "resume",
                "label": "Resume a specific session",
                "help": "--resume <id>: continue a named past chat.",
                "kind": "text", "placeholder": "chat id",
                "apply": {"arg": "--resume", "from": "text"},
            },
            {
                "key": "force",
                "label": "Force — run everything",
                "help": "--force / --yolo: allow all commands unless explicitly "
                        "denied. Use carefully.",
                "kind": "bool", "danger": True,
                "apply": {"arg": "--force"},
            },
            _EXTRA_ARGS,
        ],
    },
    "antigravity": {
        "label": "Antigravity",
        "blurb": "Google Antigravity CLI (agy) — agentic coding on your Google account.",
        "icon": "command",
        "brand": "antigravity",
        "accent": "#3B6FB0",
        "kind": "native",
        "aliases": ["agy"],
        # agy brings its own auth (Google account, browser OAuth + keyring).
        # Verified against agy 1.0.3: there is NO `--model` flag — the model is
        # the account default, switched in-session via `/model`. So we omit
        # `model_config_key` (no relaydeck model picker; the modal shows
        # guidance only — a model input here would mislead, nothing to pass it
        # to). `native_providers: None` = no relaydeck-injected provider keys.
        "native_providers": None,
        "model_policy": "native",
        "model_help": "agy uses your Google-account model (Gemini 3.x; Claude / "
                      "GPT-OSS available). Switch in-session with `/model` — agy "
                      "has no CLI model flag, so there's nothing to set here.",
        "launch_options": [
            _AUTONOMY_OPTION,
            {
                "key": "dangerous",
                "label": "Skip permission prompts",
                "help": "--dangerously-skip-permissions: auto-approve every tool. "
                        "Required for unattended runs (agy has no allowlist). "
                        "Implied by auto/bypass; set here for manual mode.",
                "kind": "bool", "danger": True,
                "apply": {"arg": "--dangerously-skip-permissions"},
            },
            {
                "key": "continue",
                "label": "Continue last conversation",
                "help": "--continue: resume the most recent agy conversation.",
                "kind": "bool",
                "apply": {"arg": "--continue"},
            },
            {
                "key": "resume",
                "label": "Resume a conversation by id",
                "help": "--conversation <id>: continue a specific past conversation.",
                "kind": "text", "placeholder": "conversation id",
                "apply": {"arg": "--conversation", "from": "text"},
            },
            _EXTRA_ARGS,
        ],
    },
    "relaydeck": {
        "label": "relaydeck native",
        "blurb": "Fleet operator — customized pi agent with workspace context, "
                 "peer messaging, agent control, and live dashboard tools.",
        "icon": "cpu",
        "brand": "relaydeck",
        "accent": "#B7410E",
        "kind": "operator",
        "aliases": [],
        # Uses relaydeck presets + vault keys bridged into pi at spawn.
        "native_providers": None,
        "model_policy": "relaydeck",
        "model_help": "relaydeck preset, provider/model, or role:… — vault keys "
                      "are injected at spawn.",
        "launch_options": [],
    },
}

# Display order for native + operator cards. pi leads — it's relaydeck's
# first-class harness (own provider auth, richest option set).
_PRIMARY_ORDER = [
    "pi", "claude-code", "codex-cli", "cursor-cli", "opencode-cli",
    "antigravity", "relaydeck",
]

# The external CLI each native/operator harness wraps — used to report whether the
# binary is actually installed (vs. just the plugin being loaded). relaydeck-native
# runs customized pi (OpenClaw-style embed via CLI + extension), not an in-process
# relaydeck model loop.
HARNESS_CLI: dict[str, str | None] = {
    "pi": "pi",
    "claude-code": "claude",
    "codex-cli": "codex",
    "cursor-cli": "cursor-agent",
    "opencode-cli": "opencode",
    "antigravity": "agy",
    "relaydeck": "pi",
}

# Catalog-enrichment providers contributed by plugins. The external-agents
# plugin registers one at on_load so the new-agent modal can list linked
# Hermes/OpenClaw runtimes as spawnable cards — without core importing the
# plugin's module path. Each provider takes the config home and returns a
# list of type-card dicts (same shape as the native cards below).
_external_catalog_providers: list[Callable[[Path | str], list[dict[str, Any]]]] = []


def register_external_catalog_provider(
    provider: Callable[[Path | str], list[dict[str, Any]]],
) -> None:
    """Register a provider of extra new-agent type cards (e.g. linked
    external runtimes). Idempotent."""
    if provider not in _external_catalog_providers:
        _external_catalog_providers.append(provider)


def unregister_external_catalog_provider(
    provider: Callable[[Path | str], list[dict[str, Any]]],
) -> None:
    import contextlib
    with contextlib.suppress(ValueError):
        _external_catalog_providers.remove(provider)


def _linked_external(config_home: Path | str) -> list[dict[str, Any]]:
    """Type cards contributed by catalog providers (linked Hermes/OpenClaw
    runtimes). Returns [] when no provider is registered — the modal simply
    shows no external cards then. A misbehaving provider is skipped, not fatal."""
    out: list[dict[str, Any]] = []
    for provider in list(_external_catalog_providers):
        try:
            out.extend(provider(config_home))
        except Exception:
            continue
    return out


# ── Session intent (fresh vs. resume) ───────────────────────────────
#
# relaydeck can respawn an agent with an explicit *session intent* that is
# independent of its persisted config: "fresh" (clean session — strip any
# resume/continue flag) or "resume" (keep history — ensure the harness's
# "continue last session" flag is applied). This backs Telegram's
# /new|/clear|/fresh|/reset (fresh) and /restart (resume). It is harness-
# agnostic because it reuses each type's `launch_options` `apply` specs above
# — the same catalog the new-agent modal uses — so it works for both arg-based
# resume flags (claude/pi/cursor `--continue`) and config-key ones (codex
# `resume_last`, opencode `continue`). No PTY/spawn code is touched: the
# transform mutates the spawn `config` dict that `_build_command` already reads.

# launch_option keys that resume a prior session (rather than start clean).
_RESUME_OPTION_KEYS = {"continue", "resume", "session", "conversation"}


def _canonical_type(htype: str) -> str | None:
    """Resolve a harness type or alias (e.g. "claude") to its HARNESS_META key."""
    if htype in HARNESS_META:
        return htype
    for canon, meta in HARNESS_META.items():
        if htype in meta.get("aliases", []):
            return canon
    return None


def _resume_options(htype: str) -> list[dict[str, Any]]:
    canon = _canonical_type(htype)
    if canon is None:
        return []
    return [
        o for o in HARNESS_META[canon]["launch_options"]
        if o.get("key") in _RESUME_OPTION_KEYS
    ]


def _config_args_tokens(config: dict[str, Any]) -> list[str]:
    """Normalize config.args (list or shell string) to a token list."""
    extra = config.get("args")
    if isinstance(extra, list):
        return [str(a) for a in extra]
    if isinstance(extra, str) and extra.strip():
        import shlex
        return shlex.split(extra)
    return []


def apply_session_mode(htype: str, config: dict[str, Any], mode: str) -> dict[str, Any]:
    """Mutate `config` in place so the next spawn honours a session intent.

    - ``mode="fresh"``  → strip every resume/continue flag → a clean session.
    - ``mode="resume"`` → ensure the harness's "continue last session" flag is
      applied → keep history (unless some resume flag is already configured).
    - anything else, or a harness with no resume options → no-op.

    Derived from the per-type launch_options catalog, so it handles both
    arg-based resume flags and config-key ones. Returns the same `config`."""
    opts = _resume_options(htype)
    if not opts or mode not in ("fresh", "resume"):
        return config

    if mode == "fresh":
        # Which arg flags resume a session (and whether each carries a value),
        # plus which config keys do.
        flag_takes_value: dict[str, bool] = {}
        config_keys: list[str] = []
        for o in opts:
            apply = o.get("apply", {})
            if apply.get("arg"):
                flag_takes_value[apply["arg"].split()[0]] = "from" in apply
            if apply.get("config"):
                config_keys.append(apply["config"])
        tokens = _config_args_tokens(config)
        if tokens:
            cleaned: list[str] = []
            skip_next = False
            for tok in tokens:
                if skip_next:
                    skip_next = False
                    continue
                if tok in flag_takes_value:
                    skip_next = flag_takes_value[tok]  # also drop its value
                    continue
                cleaned.append(tok)
            config["args"] = cleaned
        for k in config_keys:
            config.pop(k, None)
        return config

    # mode == "resume": if any resume flag is already configured, leave it.
    tokens = _config_args_tokens(config)
    for o in opts:
        apply = o.get("apply", {})
        if apply.get("arg") and apply["arg"].split()[0] in tokens:
            return config
        if apply.get("config") and config.get(apply["config"]):
            return config
    # Otherwise apply the "continue last session" option, if the type has one.
    cont = next((o for o in opts if o.get("key") == "continue"), None)
    if cont is None:
        return config
    apply = cont.get("apply", {})
    if apply.get("arg"):
        config["args"] = tokens + apply["arg"].split()
    elif apply.get("config"):
        config[apply["config"]] = True
    return config


def build_harness_catalog(config_home: Path | str) -> list[dict[str, Any]]:
    """Full type catalog for the new-agent modal: native harnesses (with
    availability), the relaydeck operator, and any linked external
    runtimes. Native types are always listed (built-in) with an
    `available` flag reflecting whether their plugin is loaded."""
    import shutil

    from relaydeck.orchestrator import known_agent_types

    known = set(known_agent_types())
    out: list[dict[str, Any]] = []
    for t in _PRIMARY_ORDER:
        meta = HARNESS_META[t]
        names = {t, *meta.get("aliases", [])}
        cli = HARNESS_CLI.get(t)
        cli_installed = cli is None or shutil.which(cli) is not None
        entry: dict[str, Any] = {
            "type": t,
            "label": meta["label"],
            "blurb": meta["blurb"],
            "icon": meta["icon"],
            "brand": meta.get("brand"),
            "accent": meta.get("accent"),
            "kind": meta["kind"],
            "available": bool(names & known),
            "cli": cli,
            "cli_installed": cli_installed,
            "launch_options": meta["launch_options"],
            "native_providers": meta.get("native_providers"),
            "model_policy": meta.get("model_policy"),
            "model_config_key": meta.get("model_config_key"),
            "model_help": meta.get("model_help"),
            "model_placeholder": meta.get("model_placeholder"),
        }
        if t == "relaydeck" and not cli_installed:
            entry["install_hint"] = (
                "npm install -g @mariozechner/pi-coding-agent "
                "(https://github.com/badlogic/pi-mono)"
            )
        elif t == "antigravity" and not cli_installed:
            entry["install_hint"] = (
                "curl -fsSL https://antigravity.google/cli/install.sh | bash "
                "(installs `agy`; then run `agy login`)"
            )
        out.append(entry)
    out.extend(_linked_external(config_home))
    return out
