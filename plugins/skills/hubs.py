"""Curated public skill discovery entry points.

These are lightweight pointers, not a marketplace dependency. Operators
still import by Git/GitHub URL, npm package, or local path.
"""

from __future__ import annotations

from typing import Any


def curated_hubs() -> list[dict[str, Any]]:
    return [
        {
            "id": "openai-skills",
            "name": "OpenAI Skills",
            "kind": "git",
            "url": "https://github.com/openai/skills",
            "import_hint": "https://github.com/openai/skills/tree/main/skills/.experimental/create-plan",
            "description": "Official Codex-oriented Agent Skills catalog.",
        },
        {
            "id": "awesome-skills",
            "name": "Awesome Skills",
            "kind": "catalog",
            "url": "https://www.awesomeskills.dev/",
            "description": "Searchable directory of public GitHub skill repositories.",
        },
        {
            "id": "skillsmp",
            "name": "SkillsMP",
            "kind": "marketplace",
            "url": "https://skillsmp.com/",
            "description": "Marketplace/catalog for Claude, Codex, and ChatGPT skills.",
        },
        {
            "id": "agensi",
            "name": "Agensi",
            "kind": "marketplace",
            "url": "https://www.agensi.io/browse",
            "description": "SKILL.md marketplace focused on agent workflow skills.",
        },
        {
            "id": "claudskills",
            "name": "ClaudSkills",
            "kind": "catalog",
            "url": "https://claudskills.com/",
            "description": "Open SKILL.md registry crawled from public GitHub repositories.",
        },
        {
            "id": "claude-docs",
            "name": "Claude Skills Docs",
            "kind": "docs",
            "url": "https://code.claude.com/docs/en/skills",
            "description": "Official Claude Code docs for skill layout and loading behavior.",
        },
    ]
