"""
Usage-limits plugin.

Enforces Anthropic-Pro / -Max-style rolling-window quotas across the
fleet by subscribing to `usage.record` events emitted by every harness
that talks to a real model provider. The plugin doesn't replace the
metering plugin — it consumes the same source of truth (the
`usage_records` table) and adds budget classification + optional
auto-pause on top.

A fleet of agents on a shared plan can blow through a session/weekly cap
silently; this surfaces both early. Provider-agnostic: works for any harness
that emits `usage.record`.

Lifecycle:
  on_load
    - read settings (budgets, window sizes, thresholds, pause_at_limit)
    - subscribe to `usage.record` events
    - register CLI (`relaydeck usage-limits status` / `tail`)
    - register API (`GET/POST /api/usage-limits/*`)
    - register UI tile + tab

  on each `usage.record`
    - recompute the affected agent's session + weekly windows
    - if any window crosses warn / exceeded → emit
      `usage_limits.threshold` / `usage_limits.exceeded` so other
      plugins / the dashboard can react
    - if exceeded AND `pause_at_limit` is true → ask the orchestrator
      to stop the agent (declared capability `agents.stop`)
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Any

from relaydeck.sdk import Event, Plugin, PluginContext, PluginEventBus, PluginHost

from .core import (
    WindowConfig,
    WindowState,
    compute_window_state,
    fmt_eta,
    resolve_agent_budget,
)

PLUGIN_NAME = "usage-limits"

logger = logging.getLogger(__name__)


_DEFAULTS: dict[str, Any] = {
    "session_window_hours": 5.0,
    "session_token_budget": 0,        # 0 = unlimited
    "weekly_window_hours": 168.0,
    "weekly_token_budget": 0,
    "warn_threshold_pct": 75.0,
    "pause_at_limit": False,
    # Provider-ACCOUNT-wide budgets (summed across every agent on that
    # provider/key) — the shared 5h/weekly cap, not the per-agent one.
    # 0 = unlimited (default off, so no behaviour change unless configured).
    "provider_session_token_budget": 0,
    "provider_weekly_token_budget": 0,
}


def _load_usage_records(
    db_path: str, *, agent_id: str, since_ts: float
) -> list[tuple[float, int]]:
    """Return raw (ts, total_tokens) rows for one agent since `since_ts`.

    Reads the existing `usage_records` table — populated by the
    metering plugin's `usage.record` subscriber — and projects only
    the two columns the window math needs. Index `usage_agent_idx`
    covers the WHERE.
    """
    from relaydeck.db import open_db

    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, total_tokens FROM usage_records "
            "WHERE agent_id = ? AND ts >= ? ORDER BY ts",
            (agent_id, since_ts),
        ).fetchall()
        return [(float(r[0]), int(r[1] or 0)) for r in rows]
    finally:
        conn.close()


def _load_provider_usage(
    db_path: str, *, provider: str, since_ts: float
) -> list[tuple[float, int]]:
    """Return (ts, total_tokens) rows for ALL agents on `provider` since
    `since_ts` — the account-wide roll-up. Index `usage_model_idx`
    (model, provider, ts) covers the provider/ts filter."""
    from relaydeck.db import open_db

    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, total_tokens FROM usage_records "
            "WHERE provider = ? AND ts >= ? ORDER BY ts",
            (provider, since_ts),
        ).fetchall()
        return [(float(r[0]), int(r[1] or 0)) for r in rows]
    finally:
        conn.close()


def _agent_overrides(agent_config: dict | None, window: str) -> dict | None:
    """Pull `agent.config.usage_limits.<window>` if present.

    Operators set per-agent overrides in the agent's spec yaml:

        config:
          usage_limits:
            session:
              absolute: 100000
            weekly:
              multiplier: 2.0
    """
    if not isinstance(agent_config, dict):
        return None
    block = agent_config.get("usage_limits")
    if not isinstance(block, dict):
        return None
    sub = block.get(window)
    return sub if isinstance(sub, dict) else None


class UsageLimitsPlugin(Plugin):
    """Subscribes to `usage.record`, computes window state, emits
    `usage_limits.*` notifications, and (optionally) pauses agents."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self.db_path = str(host.config_home / "runtime" / "relaydeck.db")
        # Emit-suppression so we don't spam threshold events on every
        # incoming usage record once a window is already in `warn` or
        # `exceeded`. Map: (agent_id, window_name) → last emitted state.
        self._last_state: dict[tuple[str, str], str] = {}
        # Same suppression for provider-account-wide windows.
        self._last_provider_state: dict[tuple[str, str], str] = {}

        host.events.subscribe("usage.record", self._on_usage_event)
        self._register_cli(host)
        self._register_api(host)
        # Register plugin metrics — counters bumped each time we emit
        # a threshold/exceeded transition, gauge for the most-recent
        # window state per agent. Demonstrates the host.metrics SDK
        # surface; declare `metrics.register` in plugin.toml.
        try:
            self._threshold_counter = host.metrics.counter(
                "threshold_total",
                "Times a usage window crossed the warn threshold.",
            )
            self._exceeded_counter = host.metrics.counter(
                "exceeded_total",
                "Times a usage window exceeded its budget.",
            )
            self._paused_counter = host.metrics.counter(
                "auto_pauses_total",
                "Agents auto-paused after exceeding their budget.",
            )
        except Exception:
            # Older host with no metrics surface — degrade quietly.
            self._threshold_counter = None
            self._exceeded_counter = None
            self._paused_counter = None
        # UI is manifest-declared in plugin.toml; no runtime
        # host.ui.* calls — see comment near class bottom.

    # ── Settings resolution ─────────────────────────────────────────

    def _settings(self) -> dict[str, Any]:
        """Read live settings, coercing into the expected types.

        Pulled fresh on every event so changes via
        `relaydeck plugin settings usage-limits set ...` take effect
        immediately without needing a daemon reload.
        """
        vals = dict(_DEFAULTS)
        with suppress(Exception):
            vals.update(self.host.settings.all())
        # Settings round-trip through env / file — strings need coercion.
        return {
            "session_window_hours": float(vals.get("session_window_hours") or 0),
            "session_token_budget": int(vals.get("session_token_budget") or 0),
            "weekly_window_hours": float(vals.get("weekly_window_hours") or 0),
            "weekly_token_budget": int(vals.get("weekly_token_budget") or 0),
            "warn_threshold_pct": float(vals.get("warn_threshold_pct") or 75.0),
            "pause_at_limit": str(vals.get("pause_at_limit") or "").lower()
                              in ("1", "true", "yes", "on"),
            "provider_session_token_budget": int(vals.get("provider_session_token_budget") or 0),
            "provider_weekly_token_budget": int(vals.get("provider_weekly_token_budget") or 0),
        }

    def _agent_window_configs(self, agent_id: str) -> tuple[WindowConfig, WindowConfig]:
        """Build the two window configs for this agent, applying any
        per-agent overrides from the agent spec's `config.usage_limits`
        block."""
        s = self._settings()
        agent_config = self._agent_config(agent_id)
        session_budget = resolve_agent_budget(
            base_budget=s["session_token_budget"],
            agent_overrides=_agent_overrides(agent_config, "session"),
        )
        weekly_budget = resolve_agent_budget(
            base_budget=s["weekly_token_budget"],
            agent_overrides=_agent_overrides(agent_config, "weekly"),
        )
        return (
            WindowConfig(
                name="session",
                duration_hours=s["session_window_hours"],
                budget_tokens=session_budget,
            ),
            WindowConfig(
                name="weekly",
                duration_hours=s["weekly_window_hours"],
                budget_tokens=weekly_budget,
            ),
        )

    def _agent_config(self, agent_id: str) -> dict | None:
        """Read the agent's spec config (yaml) so per-agent overrides
        on `usage_limits` are picked up. Tolerates a missing/orphaned
        agent — returns None and the caller falls back to plugin
        defaults."""
        try:
            orch = self.host._orchestrator  # type: ignore[attr-defined]
        except AttributeError:
            return None
        try:
            spec = orch._load_spec(agent_id)
            return spec.config if spec else None
        except Exception:
            return None

    # ── Computation ─────────────────────────────────────────────────

    def state_for(self, agent_id: str, *, now_ts: float | None = None
                  ) -> dict[str, WindowState]:
        """Return current window state for one agent. Pure read; safe
        to call from CLI, API, or event handlers.

        Reads `usage_records` directly. Ordering correctness is
        enforced by the `dependencies = ["metering"]` line in
        plugin.toml — the loader topologically sorts by `requires`
        (relaydeck/plugin.py:`load_all`), so metering subscribes before us
        and has committed the row by the time our handler runs. We do
        NOT fold the event payload's tokens into the rollup here,
        because that would double-count under the normal load order.
        """
        session_cfg, weekly_cfg = self._agent_window_configs(agent_id)
        now = now_ts if now_ts is not None else time.time()
        thresh = self._settings()["warn_threshold_pct"]

        # Read enough history to cover the longer window.
        lookback = max(session_cfg.duration_hours, weekly_cfg.duration_hours) * 3600.0
        records = _load_usage_records(
            self.db_path, agent_id=agent_id, since_ts=now - lookback,
        )

        return {
            "session": compute_window_state(
                config=session_cfg, records=records, now_ts=now,
                warn_threshold_pct=thresh,
            ),
            "weekly": compute_window_state(
                config=weekly_cfg, records=records, now_ts=now,
                warn_threshold_pct=thresh,
            ),
        }

    # ── Event handler ──────────────────────────────────────────────

    def _on_usage_event(self, event: Event) -> None:
        data = event.data or {}
        agent_id = str(data.get("agent_id") or "")
        if not agent_id:
            return
        try:
            windows = self.state_for(agent_id)
        except Exception:
            logger.exception("usage_limits: state_for failed for %s", agent_id)
            return

        settings = self._settings()
        for window_name, state in windows.items():
            self._maybe_emit_threshold(agent_id, window_name, state, settings)

        # Provider-account-wide roll-up (only when a provider budget is set).
        provider = str(data.get("provider") or "")
        if provider and (settings["provider_session_token_budget"] > 0
                         or settings["provider_weekly_token_budget"] > 0):
            try:
                pwindows = self.provider_state_for(provider)
            except Exception:
                logger.exception("usage_limits: provider_state_for failed for %s", provider)
                pwindows = {}
            for window_name, state in pwindows.items():
                self._maybe_emit_provider(provider, window_name, state)

    def provider_state_for(self, provider: str, *, now_ts: float | None = None
                           ) -> dict[str, WindowState]:
        """Account-wide window state for one provider — usage summed across
        EVERY agent on that provider/key, vs the provider budget. The shared
        5h/weekly cap a fleet blows through collectively."""
        s = self._settings()
        now = now_ts if now_ts is not None else time.time()
        session_cfg = WindowConfig(
            name="session", duration_hours=s["session_window_hours"],
            budget_tokens=s["provider_session_token_budget"],
        )
        weekly_cfg = WindowConfig(
            name="weekly", duration_hours=s["weekly_window_hours"],
            budget_tokens=s["provider_weekly_token_budget"],
        )
        lookback = max(session_cfg.duration_hours, weekly_cfg.duration_hours) * 3600.0
        records = _load_provider_usage(
            self.db_path, provider=provider, since_ts=now - lookback,
        )
        thresh = s["warn_threshold_pct"]
        return {
            "session": compute_window_state(
                config=session_cfg, records=records, now_ts=now,
                warn_threshold_pct=thresh,
            ),
            "weekly": compute_window_state(
                config=weekly_cfg, records=records, now_ts=now,
                warn_threshold_pct=thresh,
            ),
        }

    def _maybe_emit_provider(self, provider: str, window_name: str,
                             state: WindowState) -> None:
        if state.is_disabled():
            return
        key = (provider, window_name)
        if self._last_provider_state.get(key) == state.state:
            return
        self._last_provider_state[key] = state.state
        payload = {
            "provider": provider,
            "window": window_name,
            "state": state.state,
            "pct_used": state.pct_used,
            "used_tokens": state.used_tokens,
            "budget_tokens": state.budget_tokens,
            "reset_at_ts": state.reset_at_ts,
        }
        if state.state == "warn":
            self.host.events.emit("usage_limits.provider_threshold", payload)
        elif state.state == "exceeded":
            self.host.events.emit("usage_limits.provider_exceeded", payload)

    def _maybe_emit_threshold(
        self,
        agent_id: str,
        window_name: str,
        state: WindowState,
        settings: dict[str, Any],
    ) -> None:
        if state.is_disabled():
            return
        key = (agent_id, window_name)
        prev = self._last_state.get(key)
        if prev == state.state:
            return  # no transition
        self._last_state[key] = state.state

        payload = {
            "agent_id": agent_id,
            "window": window_name,
            "state": state.state,
            "pct_used": state.pct_used,
            "used_tokens": state.used_tokens,
            "budget_tokens": state.budget_tokens,
            "reset_at_ts": state.reset_at_ts,
        }
        if state.state == "warn":
            self.host.events.emit("usage_limits.threshold", payload)
            if self._threshold_counter is not None:
                self._threshold_counter.inc(labels={"window": window_name})
        elif state.state == "exceeded":
            self.host.events.emit("usage_limits.exceeded", payload)
            if self._exceeded_counter is not None:
                self._exceeded_counter.inc(labels={"window": window_name})
            if settings["pause_at_limit"]:
                self._pause_agent(agent_id, window_name)
                if self._paused_counter is not None:
                    self._paused_counter.inc(labels={"window": window_name})

    def _pause_agent(self, agent_id: str, window_name: str) -> None:
        """Stop the over-budget agent via the declared `agents.stop`
        capability. The capability gate refuses if we forgot to
        declare it in plugin.toml — that's intentional, the failure
        is loud."""
        agents = self.host.agents
        if agents is None:
            logger.warning("usage_limits: no orchestrator bound; can't pause %s", agent_id)
            return
        try:
            ok = agents.stop(agent_id)
        except Exception as exc:
            logger.warning("usage_limits: stop(%s) raised: %s", agent_id, exc)
            return
        logger.info(
            "usage_limits: paused %s after %s budget exceeded (stop=%s)",
            agent_id, window_name, ok,
        )

    # ── CLI ────────────────────────────────────────────────────────

    def _register_cli(self, host: PluginHost) -> None:
        import click
        from rich.console import Console
        from rich.table import Table

        console = Console()

        @host.cli.command("status")
        @click.option("--agent", "-a", "agent_filter",
                      help="Limit to one agent (default: all)")
        def status(agent_filter: str | None):
            """Show current session + weekly window usage per agent."""
            orch = self.host._orchestrator  # type: ignore[attr-defined]
            agents = orch.list_agents()
            if agent_filter:
                agents = [a for a in agents if a["id"] == agent_filter]
            if not agents:
                console.print("[dim]No matching agents.[/]")
                return

            table = Table(title="Usage limits")
            table.add_column("Agent", style="cyan")
            table.add_column("Window")
            table.add_column("Used", justify="right")
            table.add_column("Budget", justify="right")
            table.add_column("%", justify="right")
            table.add_column("State")
            table.add_column("Resets in", justify="right")

            for row in agents:
                aid = row["id"]
                try:
                    windows = self.state_for(aid)
                except Exception as exc:
                    table.add_row(aid, "[red]error[/]", "", "", "", str(exc)[:32], "")
                    continue
                for wname, st in windows.items():
                    state_s = {
                        "ok": "[green]ok[/]",
                        "warn": "[yellow]warn[/]",
                        "exceeded": "[red]exceeded[/]",
                        "disabled": "[dim]disabled[/]",
                    }.get(st.state, st.state)
                    budget_s = (
                        f"{st.budget_tokens:,}" if st.budget_tokens > 0 else "—"
                    )
                    pct_s = f"{st.pct_used:.1f}%" if st.budget_tokens > 0 else "—"
                    table.add_row(
                        aid, wname,
                        f"{st.used_tokens:,}",
                        budget_s,
                        pct_s,
                        state_s,
                        fmt_eta(st.seconds_until_reset),
                    )
            console.print(table)

    # ── API ────────────────────────────────────────────────────────

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/state", ["GET"])
        async def get_state(agent: str | None = None):
            """JSON window state for one agent or all."""
            orch = self.host._orchestrator  # type: ignore[attr-defined]
            agents = orch.list_agents()
            if agent:
                agents = [a for a in agents if a["id"] == agent]
            out = []
            for row in agents:
                aid = row["id"]
                try:
                    windows = self.state_for(aid)
                except Exception as exc:
                    out.append({"agent_id": aid, "error": str(exc)})
                    continue
                out.append({
                    "agent_id": aid,
                    "windows": {n: _state_to_dict(s) for n, s in windows.items()},
                })
            return {"agents": out}

        @host.api.route("/providers", ["GET"])
        async def get_provider_state():
            """Account-wide window state per provider (usage summed across all
            agents). Surfaces the shared 5h/weekly cap the per-agent view
            can't show."""
            from relaydeck.db import open_db
            conn = open_db(self.db_path)
            try:
                providers = [
                    str(r[0]) for r in conn.execute(
                        "SELECT DISTINCT provider FROM usage_records "
                        "WHERE provider != '' ORDER BY provider"
                    ).fetchall()
                ]
            finally:
                conn.close()
            out = []
            for prov in providers:
                try:
                    windows = self.provider_state_for(prov)
                except Exception as exc:
                    out.append({"provider": prov, "error": str(exc)})
                    continue
                out.append({
                    "provider": prov,
                    "windows": {n: _state_to_dict(s) for n, s in windows.items()},
                })
            return {"providers": out}

        @host.api.route("/settings/effective", ["GET"])
        async def effective_settings():
            """Return the resolved settings the plugin will apply on
            the next event. Useful for debugging env > file > default
            precedence."""
            return self._settings()

    # ── UI ─────────────────────────────────────────────────────────
    #
    # The tab + agent tile are declared in plugin.toml under
    # [plugin.ui]. HostPluginAdapter.register_ui() concatenates the
    # manifest entries with anything the plugin registers at runtime
    # via `host.ui.*`, so registering both would surface duplicate
    # tabs/tiles in the dashboard. We use manifest-only.


def _state_to_dict(s: WindowState) -> dict[str, Any]:
    return {
        "name": s.name,
        "duration_hours": s.duration_hours,
        "budget_tokens": s.budget_tokens,
        "used_tokens": s.used_tokens,
        "pct_used": s.pct_used,
        "state": s.state,
        "reset_at_ts": s.reset_at_ts,
        "seconds_until_reset": s.seconds_until_reset,
    }


PLUGIN = UsageLimitsPlugin()


def _legacy_on_load(ctx: PluginContext) -> UsageLimitsPlugin:
    """Compatibility helper so plugin discovery can boot the plugin
    via the same `PLUGIN.on_load(ctx)` shape used by other built-ins
    until full SDK loader parity ships.

    Returns the plugin instance it operated on. Tests should use this
    return value rather than re-importing `PLUGIN`: the plugin discovery
    path re-executes this module's code via `importlib.spec_from_file_
    location`, which produces a NEW `PLUGIN` singleton in `sys.modules`,
    while pre-existing top-level imports keep referring to the original.
    Returning the instance explicitly side-steps that aliasing trap.
    """
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe",
            "events.emit",
            "api.register",
            "cli.register",
            "ui.register",
            "agents.list",
            "agents.stop",
            "metrics.register",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
        settings_defaults=_DEFAULTS,
    )
    PLUGIN.on_load(host)
    return PLUGIN
