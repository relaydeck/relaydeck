"""
Tool layer for the relaydeck-native agent.

Tools are opt-in per agent via `config.tools` (a subset of TOOL_NAMES,
chosen with checkboxes in the New-agent UI). The model invokes one by
emitting a block; `generate_reply` runs a bounded loop — complete →
execute the enabled tool calls → feed the results back → repeat — until
the model stops emitting tool blocks (its final answer) or MAX_TOOL_ITERS
is hit. Disabled/unknown tools are refused with an observation so the
model can adapt instead of silently failing.

Block syntax (fed back verbatim, so keep it exact):

    <<tool name=read path=src/app.py>><<end>>
    <<tool name=write path=notes.md>>
    file content here
    <<end>>
    <<tool name=bash>>
    ls -la
    <<end>>
    <<tool name=message to=bob>>
    what's your status?
    <<end>>

Safety / trust model:
  - read/write are CONFINED to the agent's workspace directory (path
    traversal is blocked — see `_safe_path`).
  - bash is NOT sandboxed: it runs `/bin/sh -c` as the relaydeck process user
    with the workspace as cwd, on a timeout. It can therefore read/write
    outside the workspace, see env vars, and use the network — the same
    operator-authored trust model as the `script`/`code` automation
    actions. Granting `bash` to an agent is granting shell-as-the-daemon.
  - `manage` is scoped to the agent's OWN workspace (no cross-workspace
    start/stop).
The per-agent checkbox IS the authorization; without the capability the
tool is refused. A real bash sandbox (namespaces/seccomp/container) is
future work — until then the copy must not over-claim confinement.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Capabilities the UI exposes. `relaydeck` gates peer coordination (message);
# `manage` is root-level control of the workspace (list/start/stop agents);
# `dashboard` lets the agent reshape the live web dashboard;
# read/write/bash gate the obvious filesystem/shell ops.
TOOL_NAMES = ("read", "write", "bash", "relaydeck", "manage", "dashboard")
DEFAULT_TOOLS = TOOL_NAMES  # operator agents get full workspace control by default
# ``relaydeck`` and ``dashboard`` are paired — enabling either merges the other
# so older agents that listed only ``read`` + ``relaydeck`` still get dashboard
# control without forcing fleet tools onto read-only agents.

# tool call name -> capability that gates it (defaults to the name itself).
_CAP = {"message": "relaydeck", "agents": "manage", "start": "manage", "stop": "manage",
        "dashboard": "dashboard"}

# Dashboard ops the `dashboard` tool may emit + the widget keys it can add.
# `get` reads current appearance (the agent's eyes); `theme`/`glow` are the
# theme-engine controls (accent kept as a back-compat alias — the 5 accent
# names are builtin themes). Op set + enums + the command builder are shared
# with the `relaydeck dashboard` CLI / endpoint via dashboard_commands so the
# two surfaces can't drift.
from relaydeck import dashboard_commands as _dash

_DASH_OPS = _dash.OPS          # op check + the spec text
_DASH_WIDGETS = _dash.WIDGETS  # "addable widgets" hint in the spec text

# Max complete→tools→observe iterations in one turn. Generous because the
# chat surfaces each tool batch and the operator can cancel (Ctrl-C) — a
# tight cap just truncates legitimate multi-step work. Per-agent override:
# config.max_tool_iters.
MAX_TOOL_ITERS = 25
_MAX_OUTPUT = 4000
_BASH_TIMEOUT = 30.0

_TOOL_RE = re.compile(r"<<tool\s+([^>]*?)>>(.*?)<<end>>", re.DOTALL)
_ATTR_RE = re.compile(r"(\w+)=(\S+)")


def resolved_tools_config(config: dict[str, Any]) -> set[str]:
    """Resolve enabled tools from agent config.

    Absent ``tools`` → ``DEFAULT_TOOLS``. Explicit ``tools: []`` → empty set
    (pi runs without built-in fs/bash tools; fleet extension caps follow
    ``RELAYDECK_TOOLS``). Any other explicit list pairs ``relaydeck`` ↔
    ``dashboard`` so legacy configs that listed only one still get both."""
    if "tools" not in config:
        return set(DEFAULT_TOOLS)
    raw = config.get("tools")
    if not raw:
        return set()
    tools = set(raw)
    if "relaydeck" in tools:
        tools.add("dashboard")
    if "dashboard" in tools:
        tools.add("relaydeck")
    return tools


def pi_tool_descriptions(enabled: set[str], *, config_home: Path | None = None) -> str:
    """Capability text for pi-backed relaydeck-native agents."""
    lines = [
        "Invoke pi tools directly (function calls) — do NOT use <<tool>> blocks.",
        "Do NOT use pi's [skill] loader for relaydeck skills — this session runs "
        "with --no-skills; use relaydeck_dashboard (or bash relaydeck …) instead.",
        "",
    ]
    if "read" in enabled:
        lines.append("- read — read files in the workspace")
    if "write" in enabled:
        lines.append("- write / edit — create or modify files in the workspace")
    if "bash" in enabled:
        lines.append("- bash — run a shell command (full relaydeck CLI available)")
    if "relaydeck" in enabled:
        lines.append("- relaydeck_message — send a durable message to a peer agent (to, body)")
    if "manage" in enabled:
        lines.extend([
            "- relaydeck_agents — list agents in this workspace",
            "- relaydeck_start / relaydeck_stop — start or stop a peer (agent=ID)",
        ])
    if "dashboard" in enabled:
        theme_hint = _dash.theme_catalog_hint(config_home=config_home) if config_home else (
            "light UI → base or daylight; dark UI → ink or gruvbox-dark "
            "(run op=get for the full theme list)"
        )
        lines.extend([
            "- relaydeck_dashboard — read or reshape the live web dashboard",
            "  ops: get | theme | density | glow | add | remove | move | resize | tidy | reset",
            "  op=get returns current theme/density/glow AND the saved Home widget grid",
            f"  themes: {theme_hint}",
            "  examples: op=theme value=daylight (bright light), value=base (warm paper)",
            "  addable widgets: " + ", ".join(_DASH_WIDGETS),
        ])
    if len(lines) <= 2:
        return "You have no tools enabled — answer from the injected context only."
    return "\n".join(lines)


def tool_descriptions(enabled: set[str]) -> str:
    """The capability-layer text the prompt shows for the enabled tools.

    Uses REAL newlines in the block examples (an earlier version used the
    literal two-char `\\n`, which weak models copied verbatim — bash then
    ran `nCOMMANDn`). Placeholders are angle-bracketed and the lead-in says
    to substitute real values, not echo the examples."""
    specs: list[str] = []
    if "read" in enabled:
        specs.append("read a file:\n<<tool name=read path=relative/path.py>><<end>>")
    if "write" in enabled:
        specs.append("write a file:\n<<tool name=write path=relative/path.py>>\n<the file contents>\n<<end>>")
    if "bash" in enabled:
        specs.append("run a shell command:\n<<tool name=bash>>\n<the command>\n<<end>>")
    if "relaydeck" in enabled:
        specs.append("message a peer agent:\n<<tool name=message to=peer_id>>\n<your message>\n<<end>>")
    if "manage" in enabled:
        specs.append(
            "manage agents (root):\n"
            "<<tool name=agents>><<end>>            (list all agents)\n"
            "<<tool name=start agent=peer_id>><<end>>\n"
            "<<tool name=stop agent=peer_id>><<end>>"
        )
    if "dashboard" in enabled:
        specs.append(
            "read + reshape the live web dashboard (changes apply instantly):\n"
            "<<tool name=dashboard op=get>><<end>>                   (READ current theme/density/glow)\n"
            "<<tool name=dashboard op=theme value=daylight>><<end>>  (set light theme; `relaydeck theme list` via bash)\n"
            "<<tool name=dashboard op=density value=compact>><<end>> (compact|comfy|regular)\n"
            "<<tool name=dashboard op=glow value=off>><<end>>       (on|off)\n"
            "<<tool name=dashboard op=add_widget value=clock>><<end>>\n"
            "<<tool name=dashboard op=remove_widget value=clock>><<end>>\n"
            "<<tool name=dashboard op=move_widget value=clock x=9 y=0>><<end>>   (grid is 12 cols; x,y are 0-based cells)\n"
            "<<tool name=dashboard op=resize_widget value=fleet w=6 h=4>><<end>>\n"
            "<<tool name=dashboard op=tidy>><<end>>\n"
            "<<tool name=dashboard op=reset>><<end>>\n"
            "The current grid layout (positions + sizes) is given under "
            "'Live UI state' — use it to place things precisely.\n"
            "addable widgets: " + ", ".join(_DASH_WIDGETS)
        )
    if not specs:
        return "You have no tools enabled — answer from the injected context only."
    return (
        "Tools you can use. To use one, emit a block in the exact shape shown but with "
        "REAL values substituted for the <placeholders> — never emit the examples "
        "verbatim. You may emit SEVERAL tool blocks in one reply to do multiple things "
        "at once (e.g. moving + adding several widgets); the results are fed back and "
        "you continue. Keep reasoning brief so you have room to emit the blocks. When "
        "you're done, give a short final answer with no tool block. Paths are relative "
        "to the workspace root.\n\n"
        + "\n\n".join(specs)
    )


def parse_calls(text: str) -> list[dict[str, Any]]:
    """Extract tool calls from model output. Each call: {name, attrs, body}."""
    calls: list[dict[str, Any]] = []
    for m in _TOOL_RE.finditer(text):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        name = attrs.pop("name", "")
        if name:
            calls.append({"name": name, "attrs": attrs, "body": m.group(2).strip()})
    return calls


def strip_tool_blocks(text: str) -> str:
    return _TOOL_RE.sub("", text).strip()


def run_calls(
    calls: list[dict[str, Any]], *, enabled: set[str],
    workspace_path: Path | None, agent_id: str, workspace: str | None = None,
) -> str:
    """Execute the enabled tool calls; return a combined observation block."""
    obs: list[str] = []
    for c in calls:
        name = c["name"]
        cap = _CAP.get(name, name)
        if cap not in enabled:
            obs.append(f"[{name}] refused: tool not enabled for this agent")
            continue
        try:
            obs.append(f"[{name}] {_exec(name, c, workspace_path, agent_id, workspace)}")
        except _ToolError as exc:
            obs.append(f"[{name}] error: {exc}")
        except Exception as exc:  # never let a tool crash the turn
            logger.warning("relaydeck-native tool %s failed: %s", name, exc)
            obs.append(f"[{name}] error: {type(exc).__name__}: {exc}")
    return "\n".join(obs)


# ── execution ────────────────────────────────────────────────────────


class _ToolError(Exception):
    pass


def _exec(name: str, c: dict[str, Any], ws: Path | None, agent_id: str,
          workspace: str | None = None) -> str:
    if name == "read":
        return _read(_safe_path(ws, c["attrs"].get("path", "")))
    if name == "write":
        return _write(_safe_path(ws, c["attrs"].get("path", "")), c["body"])
    if name == "bash":
        return _bash(c["body"], ws)
    if name == "message":
        return _message(c["attrs"].get("to", ""), c["body"], agent_id)
    if name in ("agents", "start", "stop"):
        return _manage(name, c["attrs"], agent_id, workspace)
    if name == "dashboard":
        return _dashboard(c["attrs"], workspace)
    raise _ToolError(f"unknown tool {name!r}")


def _safe_path(ws: Path | None, rel: str) -> Path:
    if ws is None:
        raise _ToolError("no workspace path; file tools unavailable")
    if not rel:
        raise _ToolError("missing path")
    root = ws.resolve()
    dest = (root / rel).resolve()
    if dest != root and root not in dest.parents:
        raise _ToolError(f"path escapes the workspace: {rel!r}")
    return dest


def _read(path: Path) -> str:
    if not path.is_file():
        raise _ToolError(f"not a file: {path.name}")
    text = path.read_text(errors="replace")
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT] + f"\n…(truncated, {len(text)} bytes total)"
    return f"{path.name}:\n{text}"


def _write(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return f"wrote {len(body)} bytes to {path.name}"


def _bash(cmd: str, ws: Path | None) -> str:
    if not cmd.strip():
        raise _ToolError("empty command")
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd],
            cwd=str(ws) if ws else None,
            capture_output=True, timeout=_BASH_TIMEOUT, text=True,
        )
    except subprocess.TimeoutExpired:
        raise _ToolError(f"timed out after {_BASH_TIMEOUT}s")
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + "\n…(truncated)"
    return f"exit {proc.returncode}\n{out.strip()}"


def _message(to: str, body: str, agent_id: str) -> str:
    if not to or not body:
        raise _ToolError("message needs `to` and a body")
    from relaydeck.orchestrator import get_orchestrator
    get_orchestrator().send_message_to(to, body, from_id=agent_id)
    return f"messaged {to}"


def _dashboard(attrs: dict[str, Any], workspace: str | None = None) -> str:
    """Read (op=get) or reshape the live dashboard. Writes emit a
    `dashboard.command` event (bridged to the browser SSE feed) the
    dashboard applies instantly."""
    op = attrs.get("op", "")
    val = attrs.get("value")
    if op not in _DASH_OPS:
        raise _ToolError(f"unknown dashboard op {op!r}; use one of {', '.join(_DASH_OPS)}")

    # READ: return the resolved appearance so the agent can SEE what it can
    # change ("what theme are we using?") instead of guessing.
    if op == "get":
        from relaydeck.orchestrator import get_orchestrator
        from relaydeck.preferences import resolve_appearance
        home = get_orchestrator().config_home
        g = resolve_appearance(home)
        out = f"global: theme={g['theme']} density={g['density']} glow={g['glow']}"
        out += f"\n{_dash.format_widget_layout(g.get('dashboard'), scope=g.get('scope') or 'global')}"
        if workspace:
            w = resolve_appearance(home, workspace)
            out += (f"\nworkspace {workspace}: theme={w['theme']} "
                    f"density={w['density']} glow={w['glow']} (scope={w['scope']})")
            out += f"\n{_dash.format_widget_layout(w.get('dashboard'), scope=w.get('scope') or workspace)}"
        out += f"\n{_dash.theme_catalog_hint(config_home=home)}"
        return out

    # Validate themes against the registry so we never set a dangling ref.
    known_themes = None
    if op == "theme":
        from relaydeck import themes
        from relaydeck.orchestrator import get_orchestrator
        known_themes = {t.name for t in themes.list_themes(config_home=get_orchestrator().config_home)}
    try:
        cmd = _dash.build_dashboard_command(
            op, val, x=attrs.get("x"), y=attrs.get("y"),
            w=attrs.get("w"), h=attrs.get("h"), known_themes=known_themes,
        )
    except _dash.DashboardCommandError as exc:
        raise _ToolError(str(exc)) from exc
    detail = f"={val}" if val else ""
    if op == "move_widget":
        detail += f" -> ({cmd['x']},{cmd['y']})"
    if op == "resize_widget":
        detail += f" -> {cmd['w']}x{cmd['h']}"
    from relaydeck.plugin import Event, get_registry
    bus = getattr(get_registry(), "_event_bus", None)
    if bus is None:
        raise _ToolError("event bus unavailable")
    if op in _dash.SCALAR_OPS:
        # Persist (survives no-browser) + repaint via appearance.changed.
        from relaydeck.orchestrator import get_orchestrator
        from relaydeck.preferences import set_appearance
        key = _dash.appearance_key(op)
        set_appearance(get_orchestrator().config_home, {key: cmd["value"]}, workspace)
        bus.emit(Event(type="appearance.changed",
                       data={"workspace": workspace, "keys": [key], "via": "dashboard"},
                       source_plugin="relaydeck-native"))
    else:
        bus.emit(Event(type="dashboard.command", data=cmd, source_plugin="relaydeck-native"))
    return f"dashboard: {op}{detail}"


def _manage(name: str, attrs: dict[str, Any], agent_id: str,
            workspace: str | None = None) -> str:
    """Workspace-scoped orchestrator control (gated by `manage`). Only acts
    on agents in the calling agent's OWN workspace — a native agent in
    workspace A cannot list, start, or stop agents in workspace B."""
    from relaydeck.orchestrator import get_orchestrator
    orch = get_orchestrator()
    if name == "agents":
        rows = [a for a in orch.list_agents() if (a.get("workspace") or None) == workspace]
        return "\n".join(
            f"{a.get('id')} [{a.get('status')}] @{a.get('workspace') or '-'}" for a in rows
        ) or "(no agents in this workspace)"
    target = attrs.get("agent", "")
    if not target:
        raise _ToolError(f"{name} needs `agent=AGENT_ID`")
    if target == agent_id:
        raise _ToolError("refusing to act on self")
    info = orch.get_agent(target)
    if not info:
        raise _ToolError(f"no such agent: {target}")
    if (info.get("workspace") or None) != workspace:
        raise _ToolError(
            f"{target} is in another workspace — manage is scoped to your own workspace"
        )
    if name == "start":
        orch.start_agent(target)
        return f"started {target}"
    if name == "stop":
        orch.stop_agent(target)
        return f"stopped {target}"
    raise _ToolError(f"unknown manage op {name!r}")
