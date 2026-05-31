"""Detect model servers running locally (Ollama, vLLM, LM Studio).

Side-effect-free, fail-open, fast — mirrors the external-agents probe
pattern (injectable ``_tcp`` / ``_fetch`` seams). Returns the reachable
local endpoints plus their live model count so onboarding can offer
"we found Ollama with 8 models — add + use?". This module never registers
anything itself; it only reports.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

_TCP_TIMEOUT_S = 0.6
_HTTP_TIMEOUT_S = 2.0

# Local model servers we know how to probe. `api` is the provider kind to
# register the endpoint as; `catalog` is the path that lists its models.
KNOWN_LOCAL: list[dict] = [
    {"kind": "ollama", "name": "ollama", "host": "127.0.0.1", "port": 11434,
     "api": "ollama", "catalog": "/api/tags", "label": "Ollama"},
    {"kind": "vllm", "name": "vllm", "host": "127.0.0.1", "port": 8000,
     "api": "openai", "catalog": "/v1/models", "label": "vLLM"},
    {"kind": "lmstudio", "name": "lmstudio", "host": "127.0.0.1", "port": 1234,
     "api": "openai", "catalog": "/v1/models", "label": "LM Studio"},
]


@dataclass
class LocalCandidate:
    kind: str
    label: str
    suggested_name: str
    base_url: str          # endpoint to register (api-appropriate, incl. /v1)
    api: str               # provider kind: "ollama" | "openai"
    model_count: int
    models: list[str]
    already_configured: bool  # a registered provider already points here

    def to_dict(self) -> dict:
        return asdict(self)


def _tcp_open(host: str, port: int, timeout: float = _TCP_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch_json(url: str, timeout: float = _HTTP_TIMEOUT_S):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return json.loads(resp.read())
    except Exception:
        return None


def _models_from(payload, catalog_path: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    if "/api/tags" in catalog_path:  # ollama native shape
        for m in payload.get("models") or []:
            mid = (m or {}).get("name") or (m or {}).get("model")
            if mid:
                out.append(mid)
    else:  # openai /v1/models shape
        for m in payload.get("data") or []:
            mid = (m or {}).get("id")
            if mid:
                out.append(mid)
    return out


def _endpoint_key(url: str) -> str:
    """Normalize a base URL to host:port for de-dup against configured
    providers (localhost == 127.0.0.1; path/scheme ignored)."""
    p = urlparse(url if "://" in url else "http://" + url)
    host = (p.hostname or "").replace("localhost", "127.0.0.1")
    return f"{host}:{p.port}"


def configured_endpoint_keys(config_home: Path | None = None) -> set[str]:
    keys: set[str] = set()
    try:
        from relaydeck.plugin import list_providers
        for prov in list_providers():
            try:
                bu = prov.resolved_base_url(config_home) or getattr(prov, "default_base_url", None)
            except Exception:
                bu = getattr(prov, "default_base_url", None)
            if bu:
                keys.add(_endpoint_key(bu))
    except Exception:
        pass
    return keys


def detect_local_providers(
    config_home: Path | None = None,
    *,
    _tcp=None,
    _fetch=None,
    configured_keys: set[str] | None = None,
) -> list[LocalCandidate]:
    """Probe the known local ports; return reachable endpoints that answer
    with a model catalog. Each carries `already_configured` so a caller can
    show "configured" vs "add". Seams default to the module functions
    (resolved at call time, so tests can monkeypatch) but can be passed
    explicitly for hermetic unit tests."""
    _tcp = _tcp or _tcp_open
    _fetch = _fetch or _fetch_json
    if configured_keys is None:
        configured_keys = configured_endpoint_keys(config_home)
    out: list[LocalCandidate] = []
    for ep in KNOWN_LOCAL:
        host, port = str(ep["host"]), int(ep["port"])
        catalog, api = str(ep["catalog"]), str(ep["api"])
        if not _tcp(host, port, _TCP_TIMEOUT_S):
            continue
        probe_base = f"http://{host}:{port}"
        payload = _fetch(probe_base + catalog, _HTTP_TIMEOUT_S)
        if payload is None:
            # Port open but not the server we expected — skip (no false +).
            continue
        models = _models_from(payload, catalog)
        # Native Ollama registers at the bare base; OpenAI-compat needs /v1.
        reg_base = probe_base if api == "ollama" else probe_base + "/v1"
        out.append(LocalCandidate(
            kind=str(ep["kind"]), label=str(ep["label"]),
            suggested_name=str(ep["name"]),
            base_url=reg_base, api=api,
            model_count=len(models), models=models[:50],
            already_configured=_endpoint_key(probe_base) in configured_keys,
        ))
    return out
