"""Public provider-plugin facade for external relaydeck plugins.

Provider plugin authors should import from here (or from ``relaydeck.sdk``)
instead of the internal ``relaydeck.plugin`` module. The implementation still
lives in the core plugin runtime; this module is the stable split-repo seam.
"""

from __future__ import annotations

from relaydeck.plugin import get_provider, list_providers
from relaydeck.sdk import ModelEntry, ProviderPlugin

__all__ = ["ModelEntry", "ProviderPlugin", "get_provider", "list_providers"]
