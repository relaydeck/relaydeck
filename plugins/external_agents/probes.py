"""Read-only health + risk-posture probes for a registered external agent.

Everything here is non-mutating and conservative:

  - CLI presence via `which`; optional `<cli> --version` with a hard timeout.
    We never run `hermes setup` / `openclaw onboard` / any interactive or
    state-changing command.
  - Config presence: does the config home have a config file? Is there an
    `.env`? We report THAT a secrets file exists, never its contents.
  - OpenClaw gateway: a loopback TCP connect to the default port (no data
    sent) to tell "daemon listening" from "not running".

Subprocess and socket calls go through small seams (`_which`, `_run_cli`,
`_tcp_open`) so tests are deterministic without a real Hermes/OpenClaw
install. Health is a map of independent probe -> status (never one boolean);
risk is the label list.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

from .models import (
    HERMES,
    OPENCLAW,
    RISK_CLI_AVAILABLE,
    RISK_CLI_MISSING,
    RISK_CLI_ONLY,
    RISK_CONFIG_PRESENT,
    RISK_GATEWAY_CONFIGURED,
    RISK_IN_PROCESS_PLUGINS,
    RISK_LOCAL_ONLY,
    RISK_MCP_AVAILABLE,
    RISK_SECRETS_FILE_PRESENT,
    ExternalAgent,
    ProbeReport,
)

_CLI_FOR = {HERMES: "hermes", OPENCLAW: "openclaw"}
OPENCLAW_GATEWAY_PORT = 18789
_VERSION_TIMEOUT_S = 3.0
_TCP_TIMEOUT_S = 1.0


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run_cli(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run a non-mutating CLI command. Returns (returncode, combined-output).
    A timeout or OS error returns a sentinel returncode (124 / 127)."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built from a which()-resolved path
            argv, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except (OSError, ValueError):
        return 127, ""
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
    return proc.returncode, out


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def run_probes(agent: ExternalAgent, *, run_cli_probe: bool = True) -> ProbeReport:
    """Probe one external agent. `run_cli_probe=False` skips the
    `<cli> --version` exec (used by list views that want filesystem posture
    only and must stay fast)."""
    health: dict[str, str] = {}
    risk: list[str] = []
    cli_name = _CLI_FOR.get(agent.kind)
    cli_path = _which(cli_name) if cli_name else None

    # ── CLI presence + version ──────────────────────────────────────
    version_line = ""
    if cli_path:
        risk.append(RISK_CLI_AVAILABLE)
        if run_cli_probe:
            rc, out = _run_cli([cli_path, "--version"], _VERSION_TIMEOUT_S)
            if rc == 0:
                health["cli"] = "ok"
                version_line = _first_line(out)
            elif rc == 124:
                health["cli"] = "timeout"
            else:
                # Non-zero (or 127) — the binary exists but --version failed;
                # still "installed", just couldn't confirm a version.
                health["cli"] = "error"
        else:
            health["cli"] = "installed"
    else:
        health["cli"] = "missing"
        risk.append(RISK_CLI_MISSING)

    # ── Config + secrets posture (filesystem only, never reads values) ──
    config_home = Path(agent.config_home).expanduser() if agent.config_home else None
    if config_home and config_home.is_dir():
        if (config_home / "config.yaml").exists() or (config_home / "config.json").exists():
            health["config"] = "ok"
            risk.append(RISK_CONFIG_PRESENT)
        else:
            health["config"] = "absent"
        if (config_home / ".env").exists():
            risk.append(RISK_SECRETS_FILE_PRESENT)
    else:
        health["config"] = "unknown"

    # ── Kind-specific transport posture ─────────────────────────────
    if agent.kind == HERMES:
        # We don't spawn `hermes mcp serve` during a health check (it's a
        # long-running server). Presence of the CLI means the MCP path is
        # *available* to open later; mark it softly and stay honest that v1
        # talks to nothing structured yet (cli-only).
        health["mcp"] = "available" if cli_path else "unknown"
        if cli_path:
            risk.append(RISK_MCP_AVAILABLE)
        # Hermes plugins documented to run in-process with full privileges.
        risk.append(RISK_IN_PROCESS_PLUGINS)
        risk.append(RISK_CLI_ONLY)
    elif agent.kind == OPENCLAW:
        reachable = _tcp_open("127.0.0.1", OPENCLAW_GATEWAY_PORT, _TCP_TIMEOUT_S)
        health["gateway"] = "reachable" if reachable else "closed"
        if reachable:
            risk.append(RISK_GATEWAY_CONFIGURED)
        risk.append(RISK_CLI_ONLY)

    # Only probed loopback / local filesystem — say so.
    risk.append(RISK_LOCAL_ONLY)

    # De-dup while preserving order.
    seen: set[str] = set()
    risk = [r for r in risk if not (r in seen or seen.add(r))]

    summary = _summarize(agent, health, version_line)
    return ProbeReport(health=health, risk=risk, summary=summary)


def _summarize(agent: ExternalAgent, health: dict[str, str], version_line: str) -> str:
    cli = health.get("cli", "unknown")
    parts = [f"{agent.kind} cli={cli}"]
    if version_line:
        parts.append(version_line)
    if agent.kind == OPENCLAW and "gateway" in health:
        parts.append(f"gateway={health['gateway']}")
    if agent.kind == HERMES and "mcp" in health:
        parts.append(f"mcp={health['mcp']}")
    return " · ".join(parts)
