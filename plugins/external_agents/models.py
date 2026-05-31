"""Data model for the external-agent primitive.

An *external agent* is an agent runtime relaydeck does NOT run — Hermes Agent
or OpenClaw living in its own repo / config home, with its own daemon,
channels, and lifecycle. relaydeck observes it (read-only): detect what it is,
report health + risk posture, and surface it in the dashboard/CLI alongside
the agents relaydeck actually runs.

This is deliberately NOT a `BaseAgent` subclass. relaydeck's one runtime
primitive stays "the agent it runs"; an `ExternalAgent` is a plugin-owned
record describing a runtime someone else runs. Keeping it a plain serializable
dataclass (persisted as JSON by `store.py`) means adapters can evolve without
a core DB migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ── Kinds ───────────────────────────────────────────────────────────
HERMES = "hermes"
OPENCLAW = "openclaw"
UNKNOWN = "unknown"
KINDS = (HERMES, OPENCLAW)

# ── Transports (how relaydeck would talk to it — informational in v1, which
#    is read-only; no transport is actually opened yet) ───────────────
T_CLI = "cli"
T_MCP = "mcp"
T_GATEWAY_WS = "gateway-ws"
T_UNKNOWN = "unknown"

# Preferred native transport per kind — the one relaydeck would open first
# (Hermes → MCP, OpenClaw → Gateway WS). Detection reports this for matched
# paths; it's also the fallback when a kind is *forced* on an undetected path
# so a forced agent still carries a meaningful transport instead of "unknown".
DEFAULT_TRANSPORT = {HERMES: T_MCP, OPENCLAW: T_GATEWAY_WS}

# ── Risk labels (boringly explicit; shown in the dashboard) ──────────
# Mirrors the label vocabulary in PLAN-HERMES-FIRST-CLASS-INTEGRATION.md.
RISK_CLI_AVAILABLE = "cli-available"
RISK_CLI_ONLY = "cli-only"
RISK_CLI_MISSING = "cli-missing"
RISK_MCP_AVAILABLE = "mcp-available"
RISK_GATEWAY_CONFIGURED = "gateway-configured"
RISK_CONFIG_PRESENT = "config-present"
RISK_SECRETS_FILE_PRESENT = "secrets-file-present"
RISK_REMOTE_CHANNEL = "remote-channel"
RISK_LOCAL_ONLY = "local-only"
RISK_IN_PROCESS_PLUGINS = "in-process-plugins"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


_ID_RE = re.compile(r"[^a-z0-9._-]+")


def slug_id(value: str) -> str:
    """Normalize a name/path into a safe external-agent id.

    Lowercased, non `[a-z0-9._-]` collapsed to `-`, trimmed. Used so a
    user can `relaydeck external add ~/src/hermes-agent` without inventing
    an id; the basename becomes the id."""
    s = _ID_RE.sub("-", value.strip().lower()).strip("-.")
    return s or "external"


@dataclass
class Detection:
    """Result of `detector.detect(path)` — scored evidence, no side effects."""

    kind: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    recommended_transport: str = T_UNKNOWN
    root: str = ""
    config_home: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.kind in KINDS and self.confidence > 0.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "signals": list(self.signals),
            "recommended_transport": self.recommended_transport,
            "root": self.root,
            "config_home": self.config_home,
            "warnings": list(self.warnings),
            "matched": self.matched,
        }


@dataclass
class ProbeReport:
    """Cached read-only health/posture snapshot for one external agent.

    `health` is a map of independent probe -> status string (never a single
    boolean): cli/config/mcp/gateway each report on their own. `risk` is the
    label list. Secrets are never read — at most we report that an `.env`
    exists (`secrets-file-present`)."""

    health: dict[str, str] = field(default_factory=dict)
    risk: list[str] = field(default_factory=list)
    summary: str = ""
    checked_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "health": dict(self.health),
            "risk": list(self.risk),
            "summary": self.summary,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> ProbeReport | None:
        if not isinstance(d, dict):
            return None
        return cls(
            health=dict(d.get("health") or {}),
            risk=list(d.get("risk") or []),
            summary=str(d.get("summary") or ""),
            checked_at=str(d.get("checked_at") or now_iso()),
        )


@dataclass
class ExternalAgent:
    """A registered external agent runtime (read-only observed)."""

    id: str
    kind: str
    name: str
    root: str
    config_home: str | None = None
    workspace: str | None = None
    transport: str = T_UNKNOWN
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_probe: dict | None = None  # ProbeReport.to_dict()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "root": self.root,
            "config_home": self.config_home,
            "workspace": self.workspace,
            "transport": self.transport,
            "confidence": round(self.confidence, 3),
            "signals": list(self.signals),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_probe": self.last_probe,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ExternalAgent:
        return cls(
            id=str(d["id"]),
            kind=str(d.get("kind") or UNKNOWN),
            name=str(d.get("name") or d.get("id") or ""),
            root=str(d.get("root") or ""),
            config_home=d.get("config_home"),
            workspace=d.get("workspace"),
            transport=str(d.get("transport") or T_UNKNOWN),
            confidence=float(d.get("confidence") or 0.0),
            signals=list(d.get("signals") or []),
            created_at=str(d.get("created_at") or now_iso()),
            updated_at=str(d.get("updated_at") or now_iso()),
            last_probe=d.get("last_probe"),
        )
