"""
Anthropic provider plugin — static catalog.

Anthropic's `/v1/models` endpoint requires auth. To avoid blocking on
credentials we ship a curated static catalog of currently-recommended
model ids; users can refresh from the live endpoint by setting
`ANTHROPIC_API_KEY` and running `relaydeck provider refresh anthropic`.
"""

from __future__ import annotations

from relaydeck.provider import ModelEntry, ProviderPlugin

_STATIC = [
    # (id, display_name, context_length, prompt_price, completion_price)
    ("claude-opus-4-7",      "Claude Opus 4.7",     1_000_000, 15.0, 75.0),
    ("claude-opus-4-6",      "Claude Opus 4.6",       200_000, 15.0, 75.0),
    ("claude-sonnet-4-6",    "Claude Sonnet 4.6",   1_000_000,  3.0, 15.0),
    ("claude-sonnet-4-5",    "Claude Sonnet 4.5",     200_000,  3.0, 15.0),
    ("claude-3-7-sonnet",    "Claude 3.7 Sonnet",     200_000,  3.0, 15.0),
    ("claude-3-5-sonnet",    "Claude 3.5 Sonnet",     200_000,  3.0, 15.0),
    ("claude-haiku-4-5",     "Claude Haiku 4.5",      200_000,  1.0,  5.0),
    ("claude-3-5-haiku",     "Claude 3.5 Haiku",      200_000,  0.8,  4.0),
]


class AnthropicProvider(ProviderPlugin):
    name = "anthropic"
    provider_name = "anthropic"
    key_env = "ANTHROPIC_API_KEY"
    default_base_url = "https://api.anthropic.com"
    version = "0.1.0"
    description = "Anthropic model catalog (static — refresh with ANTHROPIC_API_KEY set)"

    def fetch_catalog(self) -> list[ModelEntry]:
        # If a live API key is present, prefer the upstream list.
        key = self.resolved_api_key()
        if key:
            try:
                return self._fetch_live(key)
            except Exception:
                pass
        return [ModelEntry(
            id=m[0], display_name=m[1], context_length=m[2],
            prompt_price=m[3], completion_price=m[4],
        ) for m in _STATIC]

    def _fetch_live(self, api_key: str) -> list[ModelEntry]:
        import json
        import urllib.request

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        # Merge live ids with our static pricing (Anthropic's /v1/models
        # doesn't return prices).
        static_prices = {m[0]: (m[3], m[4]) for m in _STATIC}
        out: list[ModelEntry] = []
        for row in payload.get("data") or []:
            mid = row.get("id", "")
            prices = static_prices.get(mid, (None, None))
            out.append(ModelEntry(
                id=mid,
                display_name=row.get("display_name", mid),
                prompt_price=prices[0], completion_price=prices[1],
            ))
        return out

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        import json
        import urllib.request

        api_key = kwargs.get("api_key") or self.resolved_api_key()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic completion")
        body = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
        parts = payload.get("content") or []
        return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


PLUGIN = AnthropicProvider()
