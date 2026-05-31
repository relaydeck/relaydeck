"""Public vault facade for relaydeck core and plugins.

Core never imports the vault plugin's implementation path. Instead the vault
plugin registers a :class:`SecretBackend` at ``on_load`` and this facade
delegates to whatever backend is registered. That keeps the secrets contract
in core (so providers, the SDK ``host.vault``, and harnesses can resolve keys
without reaching into a plugin) while the actual encrypted store ships as the
movable vault plugin.

External plugins should prefer ``host.vault`` for capability-gated access.
When no backend is registered (vault plugin not loaded), ``get_secret`` reads
as empty and writes raise — the same shape as a vault that has never been
populated.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretBackend(Protocol):
    """The contract a secrets backend registers with core.

    The bundled vault plugin is the reference implementation; an alternate
    backend (e.g. an external secrets manager) can register in its place.
    """

    def get_secret(self, key: str) -> str | None: ...
    def set_secret(self, key: str, value: str) -> None: ...
    def delete_secret(self, key: str) -> bool: ...


_backend: SecretBackend | None = None


def register_secret_backend(backend: SecretBackend | None) -> None:
    """Register the daemon-local secrets backend. Called by the vault plugin
    at ``on_load``. Passing ``None`` clears it (vault plugin ``on_unload``)."""
    global _backend
    _backend = backend


def get_secret_backend() -> SecretBackend | None:
    return _backend


def get_secret(key: str) -> str | None:
    if _backend is None:
        return None
    return _backend.get_secret(key)


def set_secret(key: str, value: str) -> None:
    if _backend is None:
        raise RuntimeError(
            "no secret backend registered — the vault plugin is not loaded"
        )
    _backend.set_secret(key, value)


def delete_secret(key: str) -> bool:
    if _backend is None:
        return False
    return _backend.delete_secret(key)


__all__ = [
    "SecretBackend",
    "delete_secret",
    "get_secret",
    "get_secret_backend",
    "register_secret_backend",
    "set_secret",
]
