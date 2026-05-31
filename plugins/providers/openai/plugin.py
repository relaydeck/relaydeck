"""
OpenAI provider plugin — static catalog (auth-required upstream).

Same pattern as the Anthropic plugin: static seeded list, live fetch if
`OPENAI_API_KEY` is present at refresh time.
"""

from __future__ import annotations

from relaydeck.provider import ModelEntry, ProviderPlugin

_STATIC = [
    # (id, display_name, context_length, prompt_price, completion_price)
    # Seeded catalog — live `/v1/models` fetch replaces this when OPENAI_API_KEY is set.
    ("gpt-4o",       "GPT-4o",         128_000,  2.50, 10.00),
    ("gpt-4o-mini",  "GPT-4o mini",    128_000,  0.15,  0.60),
    ("gpt-4.1",      "GPT-4.1",      1_000_000,  2.00,  8.00),
    ("gpt-4.1-mini", "GPT-4.1 mini", 1_000_000,  0.40,  1.60),
    ("o1",           "o1",             200_000, 15.00, 60.00),
    ("o1-mini",      "o1 mini",        128_000,  3.00, 12.00),
    ("o3-mini",      "o3 mini",        200_000,  1.10,  4.40),
]


class OpenAIProvider(ProviderPlugin):
    name = "openai"
    provider_name = "openai"
    key_env = "OPENAI_API_KEY"
    default_base_url = None
    version = "0.1.0"
    description = "OpenAI model catalog (static — refresh with OPENAI_API_KEY set)"

    def fetch_catalog(self) -> list[ModelEntry]:
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
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        static_prices = {m[0]: (m[3], m[4]) for m in _STATIC}
        out: list[ModelEntry] = []
        for row in payload.get("data") or []:
            mid = row.get("id", "")
            prices = static_prices.get(mid, (None, None))
            out.append(ModelEntry(
                id=mid, display_name=mid,
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

    def embed(self, text: str, *, model: str, **kwargs) -> list[float]:
        from openai import OpenAI

        client = OpenAI(
            api_key=self.resolved_api_key(),
            base_url=kwargs.get("base_url") or self.resolved_base_url(),
        )
        response = client.embeddings.create(model=model, input=text)
        return list(response.data[0].embedding)


PLUGIN = OpenAIProvider()
