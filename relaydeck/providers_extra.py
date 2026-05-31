"""
Extra model providers — known OpenAI-compatible backends (vLLM, Groq,
Together, Fireworks, Mistral, DeepSeek, xAI, Cerebras, Gemini) plus
user-defined custom providers — registered outside the plugin-dir
discovery path.

Why a separate registry: these aren't plugin directories, they're
config/table-driven. Rather than synthesize PluginEntry objects into the
PluginRegistry (touchy lifecycle), `get_provider`/`list_providers` merge
this registry with the discovered plugin providers.

No model catalogs are hardcoded — the generic provider fetches them LIVE
from `{base_url}/models` (OpenAI shape). Only base URLs + key env names
are baked in, and those are verified against each provider's official
docs (see KNOWN). API keys live in the vault under `key_env`.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from relaydeck.provider import ModelEntry, ProviderPlugin


def _openai_message_parts(msg: Any) -> tuple[str, str]:
    """Extract `(text, reasoning)` from an OpenAI chat completion message.

    Reasoning models (DeepSeek R1, OpenRouter extensions) may put the
    answer in `reasoning` / `reasoning_content` while leaving `content`
    empty. When that happens we surface the reasoning as `text` so
    automations and messaging sinks get a non-empty body."""
    content = getattr(msg, "content", None) or ""
    reasoning = getattr(msg, "reasoning", None)
    extra = getattr(msg, "model_extra", None) or {}
    if reasoning is None:
        reasoning = extra.get("reasoning") or extra.get("reasoning_content") or ""
    reasoning = str(reasoning or "")
    text = str(content or "")
    if not text.strip() and reasoning.strip():
        text = reasoning
    return text, reasoning


# ── verified known providers (all OpenAI-compatible) ──────────────────
# base_url + key_env confirmed against each provider's official docs
# 2026-05. Catalogs are fetched live; nothing here is invented.
KNOWN: list[dict[str, str]] = [
    {"name": "vllm", "base_url": "http://localhost:8000/v1", "key_env": "VLLM_API_KEY",
     "description": "vLLM — self-hosted OpenAI-compatible server (set your base URL)"},
    {"name": "groq", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
     "description": "Groq — OpenAI-compatible"},
    {"name": "together", "base_url": "https://api.together.xyz/v1", "key_env": "TOGETHER_API_KEY",
     "description": "Together AI — OpenAI-compatible"},
    {"name": "fireworks", "base_url": "https://api.fireworks.ai/inference/v1", "key_env": "FIREWORKS_API_KEY",
     "description": "Fireworks AI — OpenAI-compatible"},
    {"name": "mistral", "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
     "description": "Mistral La Plateforme — OpenAI-compatible"},
    {"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY",
     "description": "DeepSeek — OpenAI-compatible"},
    {"name": "xai", "base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",
     "description": "xAI Grok — OpenAI-compatible"},
    {"name": "cerebras", "base_url": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY",
     "description": "Cerebras Inference — OpenAI-compatible"},
    {"name": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
     "key_env": "GEMINI_API_KEY",
     "description": "Google Gemini — OpenAI-compatibility layer"},
]


class OpenAICompatProvider(ProviderPlugin):
    """A provider speaking the OpenAI API at a configurable base URL.

    Catalog is fetched live from `{base_url}/models`; completion/embedding
    go through the openai client. Powers vLLM, the known providers, and
    user-defined custom OpenAI endpoints — one implementation, per-instance
    config (so we never hardcode a model list)."""

    api = "openai"

    def __init__(self, *, name: str, key_env: str = "", base_url: str | None = None,
                 description: str = "", custom: bool = False) -> None:
        super().__init__()
        self.name = name
        self.provider_name = name
        self.key_env = key_env
        self.default_base_url = base_url
        self.description = description or f"{name} (OpenAI-compatible)"
        self.custom = custom

    def fetch_catalog(self) -> list[ModelEntry]:
        base = (self.resolved_base_url() or "").rstrip("/")
        if not base:
            return []
        key = self.resolved_api_key()
        req = urllib.request.Request(base + "/models")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read())
        except Exception:
            return []  # fail open — no key / unreachable / non-OpenAI /models
        rows = payload.get("data") if isinstance(payload, dict) else payload
        out: list[ModelEntry] = []
        for row in (rows or []):
            mid = row.get("id") if isinstance(row, dict) else str(row)
            if mid:
                out.append(ModelEntry(id=mid, display_name=mid))
        return out

    def _client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.resolved_api_key() or "x",
                      base_url=self.resolved_base_url())

    def complete(self, prompt: str, *, model: str, max_tokens: int = 256,
                 temperature: float = 0.7, **kwargs) -> str:
        resp = self._client().chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    def complete_ex(self, prompt: str, *, model: str, max_tokens: int = 256,
                    temperature: float = 0.7, **kwargs) -> dict:
        resp = self._client().chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature,
        )
        u = getattr(resp, "usage", None)
        pt = int(getattr(u, "prompt_tokens", 0) or 0)
        ct = int(getattr(u, "completion_tokens", 0) or 0)
        text, reasoning = _openai_message_parts(resp.choices[0].message)
        return {"text": text, "reasoning": reasoning,
                "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                "tokens_known": u is not None}

    def embed(self, text: str, *, model: str, **kwargs) -> list[float]:
        resp = self._client().embeddings.create(model=model, input=text)
        return list(resp.data[0].embedding)


class AnthropicCompatProvider(ProviderPlugin):
    """A provider speaking the Anthropic Messages API at a configurable
    base URL — for user-defined Anthropic-compatible endpoints."""

    api = "anthropic"

    def __init__(self, *, name: str, key_env: str = "", base_url: str | None = None,
                 description: str = "", custom: bool = True) -> None:
        super().__init__()
        self.name = name
        self.provider_name = name
        self.key_env = key_env
        self.default_base_url = base_url or "https://api.anthropic.com"
        self.description = description or f"{name} (Anthropic-compatible)"
        self.custom = custom

    def fetch_catalog(self) -> list[ModelEntry]:
        base = (self.resolved_base_url() or "").rstrip("/")
        key = self.resolved_api_key()
        if not base or not key:
            return []
        req = urllib.request.Request(
            base + "/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read())
        except Exception:
            return []
        out: list[ModelEntry] = []
        for row in (payload.get("data") or []):
            mid = row.get("id", "")
            if mid:
                out.append(ModelEntry(id=mid, display_name=row.get("display_name", mid)))
        return out

    def complete(self, prompt: str, *, model: str, max_tokens: int = 256,
                 temperature: float = 0.7, **kwargs) -> str:
        key = self.resolved_api_key()
        if not key:
            raise RuntimeError(f"{self.key_env} is required for {self.provider_name}")
        base = (self.resolved_base_url() or "https://api.anthropic.com").rstrip("/")
        body = json.dumps({
            "model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            base + "/v1/messages", data=body, method="POST",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
        parts = payload.get("content") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


# ── extra-provider registry (merged into get_provider/list_providers) ──

_extra: dict[str, ProviderPlugin] = {}
_inited = False


def _build(spec: dict[str, Any], *, custom: bool) -> ProviderPlugin:
    api = (spec.get("api") or "openai").lower()
    if api == "anthropic":
        return AnthropicCompatProvider(
            name=spec["name"], key_env=spec.get("key_env", ""),
            base_url=spec.get("base_url"), description=spec.get("description", ""),
            custom=custom)
    if api == "ollama":
        # A native Ollama endpoint (real /api/tags catalog + real token
        # counts), not the generic OpenAI shim. The implementation lives in
        # core so custom providers do not import the separable plugins package.
        from relaydeck.providers_ollama import OllamaProvider
        return OllamaProvider(
            name=spec["name"], base_url=spec.get("base_url"),
            description=spec.get("description", ""), custom=custom)
    return OpenAICompatProvider(
        name=spec["name"], key_env=spec.get("key_env", ""),
        base_url=spec.get("base_url"), description=spec.get("description", ""),
        custom=custom)


def ensure_extra(config_home=None) -> None:
    """Register known + custom providers once (idempotent)."""
    global _inited
    if _inited:
        return
    for spec in KNOWN:
        _extra.setdefault(spec["name"], _build(spec, custom=False))
    try:
        from relaydeck import provider_config
        for spec in provider_config.list_custom(config_home):
            if spec.get("name"):
                _extra[spec["name"]] = _build(spec, custom=True)
    except Exception:
        pass
    _inited = True


def reload_custom(config_home=None) -> None:
    """Re-sync custom providers from provider_config (after add/remove)."""
    # Drop existing custom entries, re-add from config.
    for name in [n for n, p in _extra.items() if getattr(p, "custom", False)]:
        _extra.pop(name, None)
    try:
        from relaydeck import provider_config
        for spec in provider_config.list_custom(config_home):
            if spec.get("name"):
                _extra[spec["name"]] = _build(spec, custom=True)
    except Exception:
        pass


def get_extra(name: str, config_home=None) -> ProviderPlugin | None:
    ensure_extra(config_home)
    return _extra.get(name)


def list_extra(config_home=None) -> list[ProviderPlugin]:
    ensure_extra(config_home)
    return list(_extra.values())


def add_custom(spec: dict[str, Any], config_home=None) -> ProviderPlugin:
    """Persist + register a user-defined provider."""
    from relaydeck import provider_config
    provider_config.upsert_custom(spec, config_home)
    inst = _build(spec, custom=True)
    _extra[spec["name"]] = inst
    return inst


def remove_custom(name: str, config_home=None) -> bool:
    from relaydeck import provider_config
    ok = provider_config.remove_custom(name, config_home)
    _extra.pop(name, None)
    return ok
