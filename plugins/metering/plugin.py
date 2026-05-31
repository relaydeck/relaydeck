"""Metering plugin using the v1 PluginHost pilot surface."""

from __future__ import annotations

from fastapi import HTTPException

from relaydeck.sdk import Event, Plugin, PluginContext, PluginEventBus, PluginHost

PLUGIN_NAME = "metering"


_DEFAULT_PRICING: dict[str, dict[str, tuple[float, float]]] = {
    "openrouter": {
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "claude-opus-4-20250514": (15.0, 75.0),
        "claude-haiku-3.5": (0.8, 4.0),
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.6),
        "gemini-2.5-flash": (0.15, 0.6),
        "gemini-2.5-pro": (1.25, 10.0),
    },
    "anthropic": {
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "claude-opus-4-20250514": (15.0, 75.0),
        "claude-haiku-3.5": (0.8, 4.0),
    },
    "openai": {
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.6),
    },
    "ollama": {
        "*": (0.0, 0.0),
    },
}


_CATALOG_PRICE_CACHE: dict[tuple[str, str], tuple[float, float] | None] = {}
_MODELS_DEV_PRICE_CACHE: dict[tuple[str, str], tuple[float, float] | None] = {}


def _catalog_price(provider: str, model: str) -> tuple[float, float] | None:
    """Per-1M-token (prompt, completion) price from the provider's live
    catalog (openrouter publishes real per-model prices). Cached per
    (provider, model); best-effort — None when unavailable so the caller
    falls through to the static table / zero."""
    key = (provider, model)
    if key in _CATALOG_PRICE_CACHE:
        return _CATALOG_PRICE_CACHE[key]
    result: tuple[float, float] | None = None
    try:
        from relaydeck.provider import get_provider

        prov = get_provider(provider)
        if prov is not None:
            for entry in prov.list_models():
                if entry.id == model and entry.prompt_price is not None:
                    result = (float(entry.prompt_price), float(entry.completion_price or 0.0))
                    break
    except Exception:
        result = None
    _CATALOG_PRICE_CACHE[key] = result
    return result


def _models_dev_price(provider: str, model: str) -> tuple[float, float] | None:
    """Per-1M-token (prompt, completion) price from the cached models.dev
    metadata index. Fail-open + memoized per (provider, model); None when
    models.dev doesn't know the model (custom/local providers, or it's
    down with no cache) so the caller falls through to the zero/wildcard.

    `cache_only=True`: cost recording must never block on the network. A
    stale (>24h) cache serves its prices and kicks a background refresh
    rather than fetching synchronously on the metering thread (the daemon
    startup `warm()` covers the cold-start happy path)."""
    key = (provider, model)
    if key in _MODELS_DEV_PRICE_CACHE:
        return _MODELS_DEV_PRICE_CACHE[key]
    result: tuple[float, float] | None = None
    try:
        from relaydeck import models_dev

        result = models_dev.get_price(provider, model, cache_only=True)
    except Exception:
        result = None
    _MODELS_DEV_PRICE_CACHE[key] = result
    return result


def get_price(provider: str, model: str) -> tuple[float, float]:
    """Get (prompt_price, completion_price) per 1M tokens in USD.

    Ladder (most-trusted first):
      1. static exact-model table
      2. live provider catalog (real openrouter prices)
      3. provider wildcard (a deliberate "this provider is flat/free"
         declaration, e.g. local ollama — must win over generic metadata
         so a local mirror never meters at cloud prices)
      4. models.dev metadata index (fail-open fallback)
      5. zero

    models.dev sits BELOW the live catalog because openrouter's *routed*
    price can differ from direct-provider price, and ABOVE zero so a known
    cloud model never silently meters at $0."""
    provider_pricing = _DEFAULT_PRICING.get(provider, {})
    if model in provider_pricing:
        return provider_pricing[model]
    cat = _catalog_price(provider, model)
    if cat is not None:
        return cat
    if "*" in provider_pricing:
        return provider_pricing["*"]
    md = _models_dev_price(provider, model)
    if md is not None:
        return md
    return (0.0, 0.0)


def calculate_cost(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Calculate USD cost for a usage event."""
    ppu, cpu = get_price(provider, model)
    return (prompt_tokens / 1_000_000) * ppu + (completion_tokens / 1_000_000) * cpu


def register_pricing(
    provider: str,
    model: str,
    prompt_price: float,
    completion_price: float,
) -> None:
    """Register custom pricing. Called by provider plugins or user config."""
    _DEFAULT_PRICING.setdefault(provider, {})[model] = (prompt_price, completion_price)


def _record(
    agent_id: str,
    model: str,
    provider: str,
    prompt: int,
    completion: int,
    cost_usd: float | None,
    session_id: str | None,
    db_path: str,
) -> None:
    """Shared write path used by the API, CLI, and event subscription."""
    from relaydeck.db import open_db, record_usage

    if cost_usd is None:
        cost_usd = calculate_cost(provider, model, prompt, completion)
    conn = open_db(db_path)
    try:
        record_usage(
            conn,
            agent_id=agent_id,
            session_id=session_id or f"agent:{agent_id}",
            model=model,
            provider=provider,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=cost_usd,
        )
    finally:
        conn.close()


class MeteringPlugin(Plugin):
    """Low-risk pilot migration for the PluginHost contract."""

    def on_load(self, host: PluginHost) -> None:
        self.host = host
        self.db_path = str(host.config_home / "runtime" / "relaydeck.db")
        host.events.subscribe("usage.record", self._on_usage_event)
        self._register_cli(host)
        self._register_api(host)

    def _on_usage_event(self, event: Event) -> None:
        data = event.data or {}
        try:
            _record(
                agent_id=str(data.get("agent_id") or "unknown"),
                model=str(data.get("model") or ""),
                provider=str(data.get("provider") or ""),
                prompt=int(data.get("prompt", data.get("prompt_tokens", 0)) or 0),
                completion=int(
                    data.get("completion", data.get("completion_tokens", 0)) or 0
                ),
                cost_usd=data.get("cost_usd"),
                session_id=data.get("session_id"),
                db_path=self.db_path,
            )
        except Exception:
            host = getattr(self, "host", None)
            if host is not None:
                host.log.exception("failed to record usage event")

    def _register_cli(self, host: PluginHost) -> None:
        import click
        from rich.console import Console
        from rich.table import Table

        @host.cli.command("summary")
        @click.option("--agent", "-a", help="Filter by agent ID")
        @click.option("--days", "-d", type=int, default=30, help="Show last N days")
        def summary(agent: str | None, days: int):
            """Show usage summary by model/provider combo."""
            import time

            from relaydeck.db import get_usage_summary, open_db

            console = Console()
            db_path = host.config_home / "runtime" / "relaydeck.db"
            if not db_path.exists():
                console.print("[dim]No usage data yet.[/]")
                return
            conn = open_db(str(db_path))
            try:
                since = time.time() - (days * 86400) if days > 0 else None
                rows = get_usage_summary(conn, agent_id=agent, since_ts=since)
            finally:
                conn.close()
            if not rows:
                console.print("[dim]No usage in the selected period.[/]")
                return
            table = Table(title=f"Usage (last {days}d)" if days > 0 else "Usage")
            table.add_column("Model", style="cyan")
            table.add_column("Provider")
            table.add_column("Reqs", justify="right")
            table.add_column("Prompt", justify="right")
            table.add_column("Compl", justify="right")
            table.add_column("Total", justify="right")
            table.add_column("Cost", justify="right")
            grand_total = 0.0
            for row in rows:
                cost = row["total_cost"] or 0.0
                grand_total += cost
                table.add_row(
                    row["model"],
                    row["provider"],
                    str(row["requests"]),
                    f"{row['total_prompt']:,}",
                    f"{row['total_completion']:,}",
                    f"{row['total_tokens']:,}",
                    f"${cost:.4f}",
                )
            table.caption = f"Total cost: ${grand_total:.4f}"
            console.print(table)

        @host.cli.command("record")
        @click.option("--agent", "-a", required=True, help="Agent ID")
        @click.option("--model", "-m", required=True, help="Model name")
        @click.option("--provider", "-p", required=True, help="Provider")
        @click.option("--prompt", type=int, default=0, help="Prompt tokens")
        @click.option("--completion", type=int, default=0, help="Completion tokens")
        @click.option("--cost", type=float, default=None, help="Override computed cost")
        def record(
            agent: str,
            model: str,
            provider: str,
            prompt: int,
            completion: int,
            cost: float | None,
        ):
            """Record a single usage event."""
            console = Console()
            _record(
                agent_id=agent,
                model=model,
                provider=provider,
                prompt=prompt,
                completion=completion,
                cost_usd=cost,
                session_id=None,
                db_path=self.db_path,
            )
            effective = cost if cost is not None else calculate_cost(
                provider, model, prompt, completion
            )
            console.print(
                f"[green]✓[/] Recorded [bold]{prompt + completion:,}[/] tokens "
                f"({prompt:,} in / {completion:,} out) · "
                f"[bold]${effective:.6f}[/] · {model} @ {provider} for {agent}"
            )

        @host.cli.command("pricing")
        def pricing():
            """Show known pricing tables."""
            console = Console()
            table = Table(title="Pricing (per 1M tokens)")
            table.add_column("Provider", style="cyan")
            table.add_column("Model")
            table.add_column("Prompt", justify="right")
            table.add_column("Completion", justify="right")
            for provider, models in sorted(_DEFAULT_PRICING.items()):
                for model, (ppu, cpu) in sorted(models.items()):
                    table.add_row(provider, model, f"${ppu:.2f}", f"${cpu:.2f}")
            console.print(table)

    def _register_api(self, host: PluginHost) -> None:
        @host.api.route("/pricing", ["GET"])
        async def get_pricing():
            return {
                provider: {
                    model: {"prompt": ppu, "completion": cpu}
                    for model, (ppu, cpu) in models.items()
                }
                for provider, models in _DEFAULT_PRICING.items()
            }

        @host.api.route("/record", ["POST"])
        async def post_record(body: dict):
            try:
                _record(
                    agent_id=str(body["agent_id"]),
                    model=str(body["model"]),
                    provider=str(body["provider"]),
                    prompt=int(body.get("prompt", body.get("prompt_tokens", 0)) or 0),
                    completion=int(
                        body.get("completion", body.get("completion_tokens", 0)) or 0
                    ),
                    cost_usd=body.get("cost_usd"),
                    session_id=body.get("session_id"),
                    db_path=self.db_path,
                )
            except KeyError as exc:
                raise HTTPException(400, f"missing field: {exc.args[0]}") from exc
            except Exception as exc:
                raise HTTPException(400, str(exc)) from exc
            return {"status": "recorded"}


PLUGIN = MeteringPlugin()


def _legacy_on_load(ctx: PluginContext) -> None:
    """Compatibility helper for tests that call this module directly."""
    host = PluginHost(
        name=PLUGIN_NAME,
        config_home=ctx.config_home,
        declared_capabilities=[
            "events.subscribe",
            "api.register",
            "cli.register",
            "ui.register",
            "workers.spawn",
        ],
        event_bus=ctx.event_bus or PluginEventBus(),
        orchestrator=ctx.orchestrator,
    )
    PLUGIN.on_load(host)
