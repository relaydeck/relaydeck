"""
OpenRouter provider plugin — live catalog from https://openrouter.ai/api/v1/models.

OpenRouter exposes the full model list without authentication. We fetch
on first access, cache to ~/.relaydeck/cache/providers/openrouter.json,
and refresh on demand. Model ids are in `provider/model` form which is
exactly what pi's `--model` flag expects.
"""

from __future__ import annotations

import contextlib

from relaydeck.provider import ModelEntry, ProviderPlugin
from relaydeck.sdk import PluginContext


def _capabilities_from_modality(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if not isinstance(value, str) or not value.strip():
        return []
    parts = value.replace("->", "+").replace(",", "+").split("+")
    return [part.strip() for part in parts if part.strip()]


class OpenRouterProvider(ProviderPlugin):
    name = "openrouter"
    provider_name = "openrouter"
    key_env = "OPENROUTER_API_KEY"
    default_base_url = "https://openrouter.ai/api/v1"
    version = "0.1.0"
    description = "OpenRouter model catalog (live fetch from openrouter.ai/api/v1/models)"

    CATALOG_URL = "https://openrouter.ai/api/v1/models"

    def fetch_catalog(self) -> list[ModelEntry]:
        import json
        import urllib.request

        req = urllib.request.Request(self.CATALOG_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        rows = payload.get("data") or []
        entries: list[ModelEntry] = []
        for row in rows:
            pricing = row.get("pricing") or {}
            try:
                # OpenRouter prices are USD per token — multiply for per-1M-tokens.
                prompt = (
                    float(pricing.get("prompt", 0)) * 1_000_000
                    if pricing.get("prompt")
                    else None
                )
                completion = (
                    float(pricing.get("completion", 0)) * 1_000_000
                    if pricing.get("completion")
                    else None
                )
            except (TypeError, ValueError):
                prompt = completion = None
            entries.append(ModelEntry(
                id=row.get("id", ""),
                display_name=row.get("name", row.get("id", "")),
                context_length=row.get("context_length"),
                prompt_price=prompt,
                completion_price=completion,
                capabilities=_capabilities_from_modality(
                    (row.get("architecture") or {}).get("modality"),
                ),
            ))
        return [e for e in entries if e.id]

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        from openai import OpenAI

        client = OpenAI(
            api_key=self.resolved_api_key(),
            base_url=kwargs.get("base_url") or self.resolved_base_url(),
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def complete_ex(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        **kwargs,
    ) -> dict:
        """Like `complete` but returns real token counts from the API's
        `usage` block — so usage_records / cost / the agent stat strip
        reflect actual tokens instead of zeros."""
        from openai import OpenAI

        client = OpenAI(
            api_key=self.resolved_api_key(),
            base_url=kwargs.get("base_url") or self.resolved_base_url(),
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        u = getattr(response, "usage", None)
        pt = int(getattr(u, "prompt_tokens", 0) or 0)
        ct = int(getattr(u, "completion_tokens", 0) or 0)
        msg = response.choices[0].message
        # OpenRouter returns a reasoning model's chain-of-thought in
        # `message.reasoning` (an extension the OpenAI SDK keeps in
        # model_extra). Surface it so the UI can show the thinking.
        reasoning = getattr(msg, "reasoning", None)
        if reasoning is None:
            reasoning = (getattr(msg, "model_extra", None) or {}).get("reasoning")
        reasoning = str(reasoning or "")
        text = msg.content or ""
        if not str(text).strip() and reasoning.strip():
            text = reasoning
        return {
            "text": text,
            "reasoning": reasoning,
            "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
            # Honest: True only if the API actually returned a usage block.
            "tokens_known": u is not None,
        }

    def complete_stream(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        on_delta=None,
        **kwargs,
    ) -> dict:
        """Streaming completion: calls `on_delta(kind, text)` for each chunk
        (kind = 'reasoning' | 'content') as it arrives, and returns the same
        dict shape as complete_ex once done. Lets the UI show the model
        thinking + answering live instead of one batch at the end."""
        from openai import OpenAI

        client = OpenAI(
            api_key=self.resolved_api_key(),
            base_url=kwargs.get("base_url") or self.resolved_base_url(),
        )
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature,
            stream=True, stream_options={"include_usage": True},
        )
        content: list[str] = []
        reasoning: list[str] = []
        pt = ct = 0
        saw_usage = False
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if choices:
                delta = getattr(choices[0], "delta", None)
                if delta is not None:
                    c = getattr(delta, "content", None)
                    if c:
                        content.append(c)
                        if on_delta:
                            on_delta("content", c)
                    r = getattr(delta, "reasoning", None)
                    if r is None:
                        r = (getattr(delta, "model_extra", None) or {}).get("reasoning")
                    if r:
                        reasoning.append(r)
                        if on_delta:
                            on_delta("reasoning", r)
            u = getattr(chunk, "usage", None)
            if u:
                saw_usage = True
                pt = int(getattr(u, "prompt_tokens", 0) or 0)
                ct = int(getattr(u, "completion_tokens", 0) or 0)
        return {
            "text": "".join(content), "reasoning": "".join(reasoning),
            "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
            # Honest: only "known" if a usage chunk actually arrived (a
            # missing/interrupted usage frame must not record a confident 0).
            "tokens_known": saw_usage,
        }

    def embed(self, text: str, *, model: str, **kwargs) -> list[float]:
        from openai import OpenAI

        client = OpenAI(
            api_key=self.resolved_api_key(),
            base_url=kwargs.get("base_url") or self.resolved_base_url(),
        )
        response = client.embeddings.create(model=model, input=text)
        return list(response.data[0].embedding)


PLUGIN = OpenRouterProvider()


def _on_load(ctx: PluginContext) -> None:
    # Warm the catalog in the background so the Models tab is responsive.
    # If offline, we silently fall through to whatever's on disk.
    with contextlib.suppress(Exception):
        PLUGIN.list_models()


PLUGIN.on_load = _on_load
