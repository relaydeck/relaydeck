"""Lightweight skill metadata for import and inventory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from relaydeck import skills as relaydeck_skills


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def skill_metadata(skill_dir: Path) -> dict[str, Any]:
    valid, errors, warnings = relaydeck_skills.validate_skill_dir(skill_dir)
    md = skill_dir / "SKILL.md"
    text = ""
    body = ""
    fm: dict[str, Any] = {}
    try:
        text = md.read_text()
        fm, body = relaydeck_skills.parse_skill_md(text)
    except OSError:
        pass

    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "frontmatter": fm,
        "name": str(fm.get("name") or skill_dir.name),
        "display_name": str(fm.get("display_name") or fm.get("name") or skill_dir.name),
        "description": str(fm.get("description") or ""),
        "body_chars": len(body),
        "token_estimate": estimate_tokens(text),
        "size": len(text.encode()) if text else 0,
        "content_hash": relaydeck_skills.content_hash(skill_dir),
    }


def enrich_ref(ref: relaydeck_skills.SkillRef) -> relaydeck_skills.SkillRef:
    data = skill_metadata(Path(ref.path))
    ref.body_chars = int(data.get("body_chars") or 0)
    ref.token_estimate = int(data.get("token_estimate") or 0)
    ref.risk_level = "low"
    ref.risk_flags = []
    return ref
