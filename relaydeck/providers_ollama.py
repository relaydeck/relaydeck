"""Native Ollama provider implementation.

This lives in core so custom Ollama endpoints can be created without importing
the bundled ``plugins`` package. The bundled plugin re-exports this
class and provides the default localhost provider instance.
"""

from __future__ import annotations

import os

from relaydeck.provider import ModelEntry, ProviderPlugin


class OllamaProvider(ProviderPlugin):
    name = "ollama"
    provider_name = "ollama"
    default_base_url = "http://127.0.0.1:11434"
    version = "0.1.0"
    description = "Ollama local model catalog (live - http://localhost:11434/api/tags)"

    def __init__(
        self,
        name: str = "ollama",
        base_url: str | None = None,
        description: str | None = None,
        custom: bool = False,
    ) -> None:
        super().__init__()
        self.name = name
        self.provider_name = name
        self.default_base_url = base_url or "http://127.0.0.1:11434"
        if description is not None:
            self.description = description
        self.custom = custom

    @property
    def base_url(self) -> str:
        try:
            from relaydeck.provider_config import get_base_url
            override = get_base_url(self.provider_name or self.name)
        except Exception:
            override = None
        if override:
            return override.rstrip("/")
        if not self.custom:
            env = os.environ.get("OLLAMA_HOST")
            if env:
                return env.rstrip("/")
        return (self.default_base_url or "http://127.0.0.1:11434").rstrip("/")

    def fetch_catalog(self) -> list[ModelEntry]:
        import json
        import urllib.request

        req = urllib.request.Request(self.base_url + "/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            payload = json.loads(resp.read())
        out: list[ModelEntry] = []
        for model in payload.get("models") or []:
            mid = model.get("name") or model.get("model") or ""
            if not mid:
                continue
            out.append(ModelEntry(
                id=mid,
                display_name=mid,
                prompt_price=0.0,
                completion_price=0.0,
                capabilities=["local"],
            ))
        return out

    def _generate(self, prompt, model, max_tokens, temperature, **kwargs) -> dict:
        import json
        import urllib.request

        body = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                **dict(kwargs.get("options") or {}),
            },
        }).encode()
        req = urllib.request.Request(
            self.base_url + "/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        return str(
            self._generate(prompt, model, max_tokens, temperature, **kwargs).get("response")
            or ""
        )

    def complete_ex(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        **kwargs,
    ) -> dict:
        payload = self._generate(prompt, model, max_tokens, temperature, **kwargs)
        prompt_tokens = int(payload.get("prompt_eval_count") or 0)
        completion_tokens = int(payload.get("eval_count") or 0)
        return {
            "text": str(payload.get("response") or ""),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def embed(self, text: str, *, model: str, **kwargs) -> list[float]:
        import json
        import urllib.request

        body = json.dumps({"model": model, "input": text}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/embed",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
        embeddings = payload.get("embeddings") or []
        if embeddings and isinstance(embeddings[0], list):
            return [float(value) for value in embeddings[0]]
        return [float(value) for value in (payload.get("embedding") or [])]
