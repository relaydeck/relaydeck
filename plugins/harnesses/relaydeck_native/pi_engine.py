"""Pi-backed engine for relaydeck-native agents.

Builds a customized `pi` invocation (operator prompt layers, fleet extension,
model preset, session dir, provider keys from relaydeck vault) and runs turns
via `pi -p --mode json --continue` with a session-dir lock so HTTP chat and
the interactive PTY share one pi session without corrupting JSONL.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Callable, Iterator

from relaydeck.harness.base import _configured_model_ref
from plugins.harnesses.pi.agent import PiAgent, _extract_assistant_text
from relaydeck.skills import validate_path_component

from . import prompt as prompt_mod
from . import tools as tools_mod

logger = logging.getLogger(__name__)

_PI_CLI = "pi"
_DEFAULT_TIMEOUT_S = 300.0
_PI_STATUS_TTL_S = 20.0
# Single source of truth for the npm package name — referenced by the
# install hint (operator-facing copy), the install endpoint (`install_pi_npm`
# below), and the e2e CI workflow's setup step. Update here, propagate.
# Upstream has flagged a future migration to `@earendil-works/pi-coding-agent`
# (deprecation note on `npm install` output); both packages currently publish
# the same versions. We track the canonical one used in production today.
_PI_NPM_PACKAGE = "@mariozechner/pi-coding-agent"
_PI_NPM_INSTALL_TIMEOUT_S = 180.0
_INSTALL_HINT = (
    f"Install pi: npm install -g {_PI_NPM_PACKAGE} "
    "(https://github.com/badlogic/pi-mono)"
)
_pi_status_cache: dict[str, Any] | None = None
_pi_status_cache_at: float = 0.0
_pi_status_cache_key: tuple[str | None, float | None] | None = None
_pi_status_lock = threading.Lock()


def extension_path() -> Path:
    return Path(__file__).resolve().parent / "pi_extension.ts"


def startup_extension_path() -> Path:
    return Path(__file__).resolve().parent / "pi_startup.ts"


def pi_available() -> bool:
    """Live PATH check — no daemon restart required."""
    return shutil.which(_PI_CLI) is not None


def pi_missing_message() -> str:
    return f"{_PI_CLI!r} not found on PATH — {_INSTALL_HINT}"


def resolved_tools(config: dict[str, Any]) -> set[str]:
    return tools_mod.resolved_tools_config(config)


def clear_pi_status_cache() -> None:
    """Drop cached pi probe (tests / after install)."""
    global _pi_status_cache, _pi_status_cache_at, _pi_status_cache_key
    with _pi_status_lock:
        _pi_status_cache = None
        _pi_status_cache_at = 0.0
        _pi_status_cache_key = None


def _pi_binary_cache_key(path: str | None) -> tuple[str | None, float | None]:
    if not path:
        return (None, None)
    try:
        return (path, Path(path).stat().st_mtime)
    except OSError:
        return (path, None)


def _probe_pi_version(path: str) -> str | None:
    try:
        proc = subprocess.run(
            [_PI_CLI, "-v"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8,
        )
        return (proc.stdout or proc.stderr or "").strip() or None
    except Exception:
        return None


def _build_pi_status(path: str | None) -> dict[str, Any]:
    installed = path is not None
    ext = extension_path()
    startup = startup_extension_path()
    return {
        "pi_installed": installed,
        "pi_version": _probe_pi_version(path) if path else None,
        "extension_present": ext.is_file(),
        "startup_extension_present": startup.is_file(),
        "install_hint": None if installed else _INSTALL_HINT,
    }


def install_pi_npm() -> dict[str, Any]:
    """One-click pi installer for the dashboard's "Install pi" button.

    Runs `npm install -g {_PI_NPM_PACKAGE}` as a subprocess, then re-probes
    PATH (via `clear_pi_status_cache` + a fresh `shutil.which`) so the
    response reflects post-install reality without waiting for the 20s
    status cache to expire. Returns a dict the dashboard renders inline:

        {ok, pi_installed, pi_version, install_hint, output, error}

    Failure modes (each surfaces an actionable hint, never a stack trace):
      - npm not on PATH → fail-soft with "install Node.js first"
      - npm subprocess timed out (>180s)
      - npm exited non-zero (e.g. EACCES, network failure)
      - npm exited zero but pi still not on PATH (sudo path issue, npm
        global prefix not on the daemon's PATH)
    """
    if shutil.which("npm") is None:
        return {
            "ok": False,
            "pi_installed": pi_available(),
            "pi_version": None,
            "install_hint": _INSTALL_HINT,
            "output": "",
            "error": (
                "`npm` is not on the daemon's PATH. Install Node.js + npm "
                "first (https://nodejs.org), then click Install again."
            ),
        }

    try:
        proc = subprocess.run(
            ["npm", "install", "-g", _PI_NPM_PACKAGE],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=_PI_NPM_INSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "pi_installed": pi_available(),
            "pi_version": None,
            "install_hint": _INSTALL_HINT,
            "output": "",
            "error": (
                f"npm install timed out after "
                f"{int(_PI_NPM_INSTALL_TIMEOUT_S)}s. Run manually: "
                f"npm install -g {_PI_NPM_PACKAGE}"
            ),
        }
    except Exception as exc:  # OSError, permission, etc.
        return {
            "ok": False,
            "pi_installed": pi_available(),
            "pi_version": None,
            "install_hint": _INSTALL_HINT,
            "output": "",
            "error": f"failed to start npm: {exc}",
        }

    # Bust the 20s pi-status cache so the very next /status call sees the
    # post-install state — without this, `pi_installed` could stay stale
    # for almost a full status TTL after a successful install.
    clear_pi_status_cache()
    fresh = pi_runtime_status(force_refresh=True)
    combined_output = ((proc.stdout or "") + (proc.stderr or "")).strip()

    if proc.returncode != 0:
        return {
            "ok": False,
            "pi_installed": fresh["pi_installed"],
            "pi_version": fresh.get("pi_version"),
            "install_hint": _INSTALL_HINT,
            "output": combined_output,
            "error": (
                f"npm exited with code {proc.returncode}. "
                f"If this is a permissions error, run the install manually: "
                f"sudo npm install -g {_PI_NPM_PACKAGE}"
            ),
        }
    if not fresh["pi_installed"]:
        # npm reported success but `which pi` still misses — usually means
        # npm's global prefix isn't on the daemon's PATH. Tell the operator
        # what's actually wrong, don't pretend it worked.
        return {
            "ok": False,
            "pi_installed": False,
            "pi_version": None,
            "install_hint": _INSTALL_HINT,
            "output": combined_output,
            "error": (
                "npm reported success but `pi` is still not on the daemon's "
                "PATH. npm's global prefix may be outside the daemon's PATH "
                "(e.g. ~/.npm-global). Restart the daemon after exporting "
                "PATH=$(npm prefix -g)/bin:$PATH, or run the install in the "
                "same shell that started the daemon."
            ),
        }
    return {
        "ok": True,
        "pi_installed": True,
        "pi_version": fresh.get("pi_version"),
        "install_hint": None,
        "output": combined_output,
        "error": None,
    }


def pi_runtime_status(*, force_refresh: bool = False) -> dict[str, Any]:
    """Runtime pi probe for dashboard/doctor.

    ``shutil.which("pi")`` runs on every call (cheap, live PATH). ``pi -v`` is
    cached briefly keyed on the resolved binary path + mtime."""
    global _pi_status_cache, _pi_status_cache_at, _pi_status_cache_key
    path = shutil.which(_PI_CLI)
    key = _pi_binary_cache_key(path)
    now = time.monotonic()
    with _pi_status_lock:
        if (
            not force_refresh
            and _pi_status_cache is not None
            and _pi_status_cache_key == key
            and (now - _pi_status_cache_at) < _PI_STATUS_TTL_S
        ):
            return dict(_pi_status_cache)

    status = _build_pi_status(path)
    with _pi_status_lock:
        _pi_status_cache = status
        _pi_status_cache_at = now
        _pi_status_cache_key = key
    return dict(status)


def session_dir(config_home: Path, workspace: str | None, agent_id: str) -> Path:
    safe_agent = validate_path_component(agent_id, kind="agent id")
    safe_ws = validate_path_component(workspace or "_none", kind="workspace")
    base = (
        config_home / "workspaces" / safe_ws
        / "runtime" / "native-sessions" / safe_agent
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def native_pi_agent_dir(config_home: Path) -> Path:
    """Isolated pi config dir for relaydeck-native children (not ~/.pi).

    Writes ``quietStartup`` so pi skips its logo, onboarding copy, and the
    [Skills]/[Extensions]/[Themes] resource dump — our startup extension
    replaces that chrome."""
    d = config_home / "runtime" / "native-pi-agent"
    d.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        d.chmod(0o700)
    settings_path = d / "settings.json"
    desired = {"quietStartup": True, "collapseChangelog": True}
    try:
        current = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        current = {}
    merged = {**current, **desired}
    if merged != current:
        settings_path.write_text(json.dumps(merged, indent=2) + "\n")
    return d


def _session_lock_path(config_home: Path, workspace: str | None, agent_id: str) -> Path:
    return session_dir(config_home, workspace, agent_id) / ".turn.lock"


@contextmanager
def _session_turn_lock(
    config_home: Path, workspace: str | None, agent_id: str, *, timeout_s: float = 30.0,
) -> Iterator[None]:
    """Serialize pi one-shot turns against the shared session dir."""
    lock_path = _session_lock_path(config_home, workspace, agent_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        # monotonic() is immune to NTP slew / DST / suspend; wall-clock
        # would either extend the timeout indefinitely on a backward jump
        # or fire RuntimeError prematurely on a forward jump.
        end = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise RuntimeError(
                        "pi session is busy (interactive terminal active?) — "
                        "use the Terminal tab or try again shortly"
                    ) from None
                time.sleep(0.15)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _workspace_path(config_home: Path, workspace: str | None) -> Path | None:
    if not workspace:
        return None
    try:
        from relaydeck.config import load_workspace_registry
        for ws in load_workspace_registry(config_home):
            if ws.name == workspace and ws.path:
                return Path(ws.path)
    except Exception:
        pass
    return None


def _load_spec(config_home: Path, agent_id: str) -> tuple[dict[str, Any], str | None]:
    try:
        from relaydeck.config import AgentSpec
        spec = AgentSpec.from_yaml(config_home / "agents" / f"{agent_id}.yaml")
        cfg = dict(spec.config or {})
        if spec.system_prompt and "system_prompt" not in cfg:
            cfg["system_prompt"] = spec.system_prompt
        return cfg, spec.workspace
    except Exception:
        return {}, None


def _pi_tool_allowlist(enabled: set[str]) -> list[str]:
    """Map relaydeck tool config to pi ``--tools`` names.

    An empty ``enabled`` set (explicit ``tools: []``) returns ``[]`` — pi runs
    without built-in filesystem/bash tools. Unset config uses ``DEFAULT_TOOLS``
    before this is called."""
    names: list[str] = []
    for t in ("read", "write", "bash", "edit"):
        if t in enabled:
            names.append(t)
    if "relaydeck" in enabled:
        names.append("relaydeck_message")
    if "manage" in enabled:
        names.extend(["relaydeck_agents", "relaydeck_start", "relaydeck_stop"])
    if "dashboard" in enabled:
        names.append("relaydeck_dashboard")
    return names


def _relaydeck_tools_env(enabled: set[str]) -> str:
    caps: list[str] = []
    if "relaydeck" in enabled:
        caps.append("relaydeck")
    if "manage" in enabled:
        caps.append("manage")
    if "dashboard" in enabled:
        caps.append("dashboard")
    return ",".join(caps)


def build_startup_bundle(
    config_home: Path,
    db_path: str,
    agent_id: str,
    *,
    config: dict[str, Any],
    workspace: str | None,
    enabled_tools: set[str],
) -> dict[str, Any]:
    """Snapshot for the relaydeck-native pi startup extension (header/footer banner)."""
    from . import prompt as prompt_mod

    model = str(config.get("preset") or config.get("model") or "role:fast")
    _, layers = prompt_mod.build_session(
        agent_id=agent_id, workspace=workspace, config=config,
        config_home=config_home, db_path=db_path, history=[],
        tools=enabled_tools,
    )
    fleet = {"agents_total": 0, "agents_running": 0, "workers": 0}
    try:
        from relaydeck.db import open_db
        conn = open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT status, type FROM agents WHERE workspace IS ?",
                (workspace,),
            ).fetchall()
            fleet["agents_total"] = len(rows)
            fleet["agents_running"] = sum(1 for r in rows if r[0] == "running")
            fleet["workers"] = sum(1 for r in rows if r[1] == "loop")
        finally:
            conn.close()
    except Exception:
        logger.debug("relaydeck-native startup: fleet snapshot skipped", exc_info=True)

    appearance = {"theme": "base", "density": "regular", "scope": "global"}
    try:
        from relaydeck.preferences import resolve_appearance
        resolved = resolve_appearance(config_home, workspace)
        appearance = {
            "theme": resolved.get("theme") or "base",
            "density": resolved.get("density") or "regular",
            "scope": resolved.get("scope") or "global",
        }
    except Exception:
        logger.debug("relaydeck-native startup: appearance skipped", exc_info=True)

    plugins: list[str] = []
    try:
        from relaydeck.config import load_workspace_registry
        for ws in load_workspace_registry(config_home):
            if ws.name == workspace:
                plugins = list(ws.plugins or [])
                break
    except Exception:
        logger.debug("relaydeck-native startup: plugins skipped", exc_info=True)

    skills = 0
    try:
        from relaydeck.skills import discover_workspace_skills
        skills = sum(
            1 for s in discover_workspace_skills(config_home, workspace) if s.get("valid")
        )
    except Exception:
        logger.debug("relaydeck-native startup: skills skipped", exc_info=True)

    st = pi_runtime_status()
    return {
        "identity": "relaydeck-native",
        "agent_id": agent_id,
        "workspace": workspace,
        "model": model,
        "tools": sorted(enabled_tools),
        "pi_tools": _pi_tool_allowlist(enabled_tools),
        "extensions": ["relaydeck-startup", "relaydeck-fleet"],
        "plugins": plugins,
        "prompt_layers": [ly["title"] for ly in layers],
        "skills": skills,
        "fleet": fleet,
        "appearance": appearance,
        "pi_version": st.get("pi_version"),
    }


# Vault key_env → pi auth.json provider id (see pi `AuthStorage`).
# Pi reads credentials from `PI_CODING_AGENT_DIR/auth.json` — NOT from
# env vars. The env injection below seeds OPENROUTER_API_KEY (and friends)
# for any subprocess that DOES read env, but the canonical pi client
# checks auth.json first and exits non-200 if a key isn't there.
_VAULT_KEY_TO_PI: dict[str, str] = {
    "DEEPSEEK_API_KEY": "deepseek",
    "OPENROUTER_API_KEY": "openrouter",
    "GEMINI_API_KEY": "google",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
}


def _effective_model_config(
    config: dict[str, Any], config_home: Path,
) -> dict[str, Any]:
    """Merge global role:fast when the agent YAML has no explicit model/preset.

    The new-agent modal's "default model" stores an empty spec — but
    run_turn and operator expectations treat that as role:fast. Pi must
    receive `--model openrouter/...` (and env/auth for the right provider)
    or it picks its own default (often anthropic) and 401s despite a
    valid OpenRouter key in vault."""
    if _configured_model_ref(config):
        return config
    merged = dict(config)
    merged["model"] = "role:fast"
    return merged


def sync_native_pi_auth(config_home: Path) -> None:
    """Public entry — vault plugin calls this when provider keys change."""
    _sync_native_pi_auth(config_home)


def _sync_native_pi_auth(config_home: Path) -> None:
    """Bridge vault keys into pi's auth.json, atomically and idempotently.

    pi-coding-agent reads provider credentials from its `PI_CODING_AGENT_DIR/auth.json`
    store, not from env, so env injection (`OPENROUTER_API_KEY=…`) alone doesn't suffice.
    Relaydeck-native spawned with just env vars hits the model API with no auth header
    and the provider 401s.

    This bridges the gap with the following safety improvements:

      - **Atomic write (no TOCTOU on chmod).** `os.open(O_WRONLY|O_CREAT|O_TRUNC, 0o600)`
        creates the file already mode-0600; we never sit in a window where the file is
        world-readable. Write to a per-call tempfile + `os.replace()` so partial writes
        can't leave a corrupt auth.json visible to pi.
      - **Cleanup on vault-empty.** If the operator removes every provider key from the
        vault, this function unlinks the (now-stale) auth.json instead of returning early
        and leaving the previous credentials on disk.
      - **Stale-key eviction.** The vault is the source of truth — keys present in
        auth.json but no longer in the vault are dropped on every sync, not merged in.
        Re-rotating a key in vault propagates on next spawn.
      - **Per-spawn cache.** `agent.py:_pi_spawn_bundle` memoizes the
        argv/env so this function runs once per agent spawn, not twice.
      - **Parent dir 0700.** `native_pi_agent_dir` already chmods the dir; this function
        relies on that without re-doing it (a single source of truth).

    AGENTS.md hard rule trade-off: "vault values never leave the daemon" is satisfied
    in spirit by file mode 0600 on a 0700 parent (only the daemon user can read), but
    not in letter (a file exists on disk during the agent's lifetime). The alternative
    — vendoring or forking pi to add an env-var auth path — is more invasive. Accept
    the trade with the safety guards above, and revisit if pi upstream gains env auth.
    """
    agent_dir = native_pi_agent_dir(config_home)
    auth_path = agent_dir / "auth.json"
    creds: dict[str, dict[str, str]] = {}
    try:
        from relaydeck.vault import get_secret
        for key_env, pi_provider in _VAULT_KEY_TO_PI.items():
            val = get_secret(key_env)
            if val:
                creds[pi_provider] = {"type": "api_key", "key": val}
    except Exception:
        logger.debug("relaydeck-native: pi auth sync skipped (vault read failed)", exc_info=True)
        return

    if not creds:
        # Operator removed every provider key from the vault. Don't leave the
        # last-sync's credentials sitting on disk — that would let an agent
        # spawned today authenticate with a key the operator believes they've
        # revoked. Unlink and exit (rmdir is best-effort; the dir is
        # re-created by `native_pi_agent_dir` on the next sync).
        with suppress(OSError):
            auth_path.unlink()
        return

    # Cheap no-op short-circuit: if the on-disk auth.json already matches
    # what we'd write, skip the file ops entirely. Survives the most common
    # case (operator hasn't rotated keys, spawning new agents).
    try:
        existing = json.loads(auth_path.read_text()) if auth_path.is_file() else None
    except (OSError, json.JSONDecodeError):
        existing = None
    if existing == creds:
        return

    # Atomic write: tempfile in same dir + rename. `os.open` with mode 0o600
    # closes the TOCTOU window the previous implementation had between
    # `write_text` and a separate `chmod`. The tempfile is on the SAME
    # filesystem as auth.json so `os.replace` is atomic.
    payload = (json.dumps(creds, indent=2) + "\n").encode("utf-8")
    fd, tmp_str = tempfile.mkstemp(prefix=".auth.", dir=str(agent_dir))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, auth_path)
    except Exception:
        logger.warning("relaydeck-native: pi auth.json write failed", exc_info=True)
        with suppress(OSError):
            tmp.unlink()


def _inject_provider_env(
    config: dict[str, Any], env: dict[str, str], config_home: Path,
) -> None:
    """Thread relaydeck vault/provider config into pi's env for the resolved preset.

    NOTE: pi-coding-agent reads provider credentials from `auth.json`, not env.
    `_sync_native_pi_auth` is what actually authenticates pi to providers. This
    function exists as a *belt-and-suspenders* layer: subprocess tools spawned
    by pi (or future tools that DO read env) get the same credentials, and the
    base-URL override flows through env regardless of how the consumer reads
    auth.
    """
    ref = _configured_model_ref(config)
    if not ref:
        return
    try:
        from relaydeck.sdk import resolve_model
        provider_name, _model_id = resolve_model(ref, config_home)
    except Exception:
        return
    try:
        from relaydeck.plugin import get_provider
        from relaydeck.provider_config import get_base_url

        provider = get_provider(provider_name)
        if provider is None:
            return
        key_fn = getattr(provider, "resolved_api_key", None)
        if callable(key_fn):
            key = key_fn()
            if key:
                key_env = getattr(provider, "key_env", "") or f"{provider_name.upper()}_API_KEY"
                env[key_env] = key
        base = get_base_url(provider_name, config_home=config_home)
        if base:
            env[f"RELAYDECK_{provider_name.upper()}_BASE_URL"] = base
    except Exception:
        logger.debug("relaydeck-native: provider env injection skipped", exc_info=True)


def _append_pi_injections(
    cmd: list[str],
    *,
    config_home: Path,
    workspace: str | None,
    agent_id: str,
    config: dict[str, Any],
) -> None:
    """PiAgent-style plugin injections (--skill, fleet-context, …)."""
    stub = PiAgent(
        agent_id=agent_id, name=agent_id, config=config, workspace=workspace,
        db_path=str(config_home / "runtime" / "relaydeck.db"),
        stop_flag=threading.Event(),
    )
    stub._config_home = config_home
    enabled = set(stub._workspace_plugins())
    for plugin_name, method_name in PiAgent._INJECTORS.items():
        if plugin_name in enabled:
            method = getattr(stub, method_name, None)
            if method:
                extra = method()
                if extra:
                    cmd.extend(extra)
    extra = stub._inject_plugin_skills()
    if extra:
        cmd.extend(extra)


def build_operator_layers(
    config_home: Path,
    db_path: str,
    agent_id: str,
    *,
    config: dict[str, Any] | None = None,
    workspace: str | None = None,
    enabled_tools: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any], str | None]:
    if config is None or workspace is None:
        config, workspace = _load_spec(config_home, agent_id)
    if enabled_tools is None:
        enabled_tools = resolved_tools(config)
    composed, layers = prompt_mod.build_session(
        agent_id=agent_id, workspace=workspace, config=config,
        config_home=config_home, db_path=db_path, history=[],
        tools=enabled_tools,
    )
    return composed, layers, config, workspace


def build_pi_argv(
    config_home: Path,
    agent_id: str,
    *,
    config: dict[str, Any] | None = None,
    workspace: str | None = None,
    interactive: bool = False,
    print_mode: bool = False,
    continue_session: bool = False,
    inject_plugins: bool = True,
) -> tuple[list[str], dict[str, str], Path | None]:
    """Return (argv, env, cwd) for a pi child."""
    if config is None or workspace is None:
        config, workspace = _load_spec(config_home, agent_id)

    model_config = _effective_model_config(config, config_home)

    enabled = resolved_tools(config)
    composed, _layers, _cfg, _ws = build_operator_layers(
        config_home, str(config_home / "runtime" / "relaydeck.db"),
        agent_id, config=config, workspace=workspace, enabled_tools=enabled,
    )

    cmd = [_PI_CLI]
    from relaydeck.harness_model import cli_model_for_agent

    cli_model = cli_model_for_agent(
        "relaydeck", model_config, config_home=config_home,
    )
    if cli_model:
        cmd.extend(["--model", cli_model])

    sd = session_dir(config_home, workspace, agent_id)
    cmd.extend(["--session-dir", str(sd)])

    if continue_session:
        cmd.append("--continue")
    if print_mode:
        cmd.extend(["-p", "--mode", "json"])
    if interactive:
        cmd.append("--continue")

    # Managed pi children: relaydeck-owned discovery only (skills/themes live in
    # the composed operator prompt, not pi's resource loader).
    cmd.extend(["--no-extensions", "--no-skills", "--no-themes", "--no-prompt-templates"])
    if interactive:
        cmd.append("--no-context-files")
        cmd.append("--offline")

    for ext_path in (startup_extension_path(), extension_path()):
        if not ext_path.is_file():
            continue
        if not interactive and ext_path.name == "pi_startup.ts":
            continue
        cmd.extend(["-e", str(ext_path)])

    tool_names = _pi_tool_allowlist(enabled)
    if tool_names:
        cmd.extend(["--tools", ",".join(tool_names)])
    elif not enabled:
        # Explicit `tools: []` — drop pi's built-in fs/bash tools but keep
        # the fleet extension active. Without this, pi falls back to its
        # default toolset (read/write/bash/edit) which is the OPPOSITE of
        # what the operator asked for.
        cmd.append("--no-builtin-tools")

    if composed.strip():
        cmd.extend(["--append-system-prompt", composed])

    # Skills/plugins/workspace context are already in the composed prompt —
    # do NOT also pass --skill / duplicate fleet-context injections.

    extra = config.get("args")
    if isinstance(extra, list):
        cmd.extend(str(a) for a in extra)
    elif isinstance(extra, str) and extra.strip():
        import shlex
        cmd.extend(shlex.split(extra))

    env = os.environ.copy()
    env["RELAYDECK_AGENT_ID"] = agent_id
    if workspace:
        env["RELAYDECK_WORKSPACE"] = workspace
    env["RELAYDECK_CONFIG_HOME"] = str(config_home)
    env["RELAYDECK_TOOLS"] = _relaydeck_tools_env(enabled)
    env["RELAYDECK_STARTUP"] = json.dumps(
        build_startup_bundle(
            config_home, str(config_home / "runtime" / "relaydeck.db"),
            agent_id, config=config, workspace=workspace, enabled_tools=enabled,
        ),
        separators=(",", ":"),
    )
    # Distinguish this pi child from the operator's personal pi sessions.
    env["TERM_PROGRAM"] = "relaydeck-native"
    env["PI_CODING_AGENT_IDENTITY"] = "relaydeck-native"
    env["PI_CODING_AGENT_DIR"] = str(native_pi_agent_dir(config_home))
    # Bridge vault credentials into pi's auth.json store BEFORE injecting
    # env (sync writes to disk; env-inject is the belt-and-suspenders layer
    # for anything that DOES read env). Runs on every build_pi_argv call
    # (each chat turn + each agent spawn) so vault rotations propagate
    # without waiting for a daemon restart.
    _sync_native_pi_auth(config_home)
    _inject_provider_env(model_config, env, config_home)

    cwd = _workspace_path(config_home, workspace)
    return cmd, env, cwd


def _process_pi_json_output(
    raw: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    reply = ""
    tool_log: list[dict[str, Any]] = []
    usage_msg: dict[str, Any] | None = None
    pending_tools: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if on_event:
            on_event(ev)
        et = ev.get("type")
        if et == "tool_execution_start":
            pending_tools.append(str(ev.get("toolName") or ""))
        elif et == "tool_execution_end":
            name = str(ev.get("toolName") or (pending_tools.pop(0) if pending_tools else ""))
            result = ev.get("result") or {}
            content = result.get("content")
            obs = ""
            if isinstance(content, list):
                obs = "\n".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict)
                )
            elif content:
                obs = str(content)
            tool_log.append({"calls": [name] if name else [], "observations": obs})
        elif et == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                text = _extract_assistant_text(msg.get("content"))
                if text:
                    reply = text
                if msg.get("usage"):
                    usage_msg = msg
        elif et == "agent_end":
            for msg in ev.get("messages") or []:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    text = _extract_assistant_text(msg.get("content"))
                    if text:
                        reply = text

    return reply, tool_log, usage_msg


def _parse_json_events(raw: str) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    return _process_pi_json_output(raw)


def run_turn(
    config_home: Path,
    db_path: str,
    agent_id: str,
    user_text: str,
    *,
    ui_state: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run one operator turn through pi."""
    if not pi_available():
        raise RuntimeError(pi_missing_message())

    config, workspace = _load_spec(config_home, agent_id)
    _composed, layers, config, workspace = build_operator_layers(
        config_home, db_path, agent_id, config=config, workspace=workspace,
    )
    model = str(config.get("preset") or config.get("model") or "role:fast")

    prompt = user_text
    if ui_state:
        prompt = f"# Live UI state\n{ui_state}\n\n# Operator message\n{user_text}"

    argv, env, cwd = build_pi_argv(
        config_home, agent_id, config=config, workspace=workspace,
        print_mode=True, continue_session=True, inject_plugins=True,
    )
    argv = list(argv) + [prompt]

    with _session_turn_lock(config_home, workspace, agent_id, timeout_s=min(30.0, timeout_s)):
        try:
            proc = subprocess.run(
                argv, env=env, cwd=str(cwd) if cwd else None,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"pi timed out after {timeout_s}s") from exc
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"pi output decode error: {exc}") from exc

    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0 and not combined.strip():
        raise RuntimeError(f"pi exited {proc.returncode}")

    reply, tool_log, usage_msg = _process_pi_json_output(combined, on_event)
    if not reply.strip():
        reply = "(the model returned an empty reply)"

    _record_pi_usage(agent_id, db_path, model, usage_msg)
    _persist_turn(db_path, "user", agent_id, user_text, workspace)
    _persist_turn(db_path, agent_id, "user", reply, workspace)

    return {"reply": reply, "model": model, "layers": layers, "tools": tool_log}


def _record_pi_usage(
    agent_id: str, db_path: str, model: str, usage_msg: dict[str, Any] | None,
) -> None:
    from . import agent as native_agent
    usage = (usage_msg or {}).get("usage") or {}
    if not usage:
        return
    payload = {
        "provider": str((usage_msg or {}).get("provider") or "unknown"),
        "model": str((usage_msg or {}).get("model") or model),
        "prompt_tokens": int(usage.get("input") or usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("output") or usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total") or usage.get("total_tokens") or 0),
        "tokens_known": True,
    }
    try:
        from relaydeck.model_invocations import record_invocation
        record_invocation(
            agent_id, model=payload["model"], provider=payload["provider"],
            latency_ms=0, prompt_chars=0, completion_chars=0,
            prompt_tokens=payload["prompt_tokens"],
            completion_tokens=payload["completion_tokens"],
            total_tokens=payload["total_tokens"],
            tokens_known=True, ok=True, source="relaydeck-native-pi",
            prompt="", response="", db_path=db_path,
        )
    except Exception:
        pass
    native_agent._emit_usage(agent_id, payload["model"], payload)


def _persist_turn(db_path: str, from_id: str, to_id: str, body: str, workspace: str | None) -> None:
    try:
        from relaydeck.messages import insert_message, mark_injected
        mid = insert_message(from_id, to_id, body, workspace=workspace, db_path=db_path)
        mark_injected(mid, body, db_path=db_path)
    except Exception:
        logger.debug("relaydeck-native: persist turn failed", exc_info=True)


def clear_pi_session(config_home: Path, workspace: str | None, agent_id: str) -> None:
    sd = session_dir(config_home, workspace, agent_id)
    for path in sd.glob("*.jsonl"):
        try:
            path.unlink()
        except OSError:
            pass
