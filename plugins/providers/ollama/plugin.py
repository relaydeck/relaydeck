"""Bundled Ollama provider plugin.

The provider implementation lives in ``relaydeck.providers_ollama`` so core can
instantiate custom Ollama endpoints without importing the separable bundled
plugins package.
"""

from __future__ import annotations

from relaydeck.providers_ollama import OllamaProvider

PLUGIN = OllamaProvider()
