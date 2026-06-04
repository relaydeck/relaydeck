"""
The `relaydeck` skill bundle is a *contract with an agent*: its whole job is
to lower an agent's tool-call failure rate, so a hallucinated flag in it is a
regression, not a typo. These tests pin the verified-command invariants that a
2026-06 refactor established (general-purpose framing, the consolidated
`skills/` folder + catalog, and "no flags the CLI doesn't have").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from relaydeck.skills import read_skill_metadata, validate_skill_dir

ROOT = Path(__file__).resolve().parent.parent

# Standalone, packaged-with-relaydeck skills (installed into an agent's own
# skill root). Plugin-owned skills live with their plugin and are covered by
# test_skills_plugin.py.
STANDALONE_SKILLS = [
    ROOT / "skills" / "relaydeck",
    ROOT / "skills" / "relaydeck-fleet",
    ROOT / "skills" / "relaydeck-plugin-dev",
]

# Every first-party skill, wherever it physically lives.
ALL_SKILLS = STANDALONE_SKILLS + [
    ROOT / "plugins" / "messaging",   # relaydeck-cli
    ROOT / "plugins" / "telegram",    # relaydeck-telegram
    ROOT / "plugins" / "prompts",     # relaydeck-prompts
    ROOT / "plugins" / "dashboard",   # relaydeck-dashboard
    ROOT / "plugins" / "theme",       # relaydeck-theme
]

# Markdown shipped to agents: SKILL bodies + the driver skill's reference/.
SKILL_DOCS = sorted(
    {p for d in STANDALONE_SKILLS for p in d.rglob("*.md")}
) + [ROOT / "docs" / "USAGE.md"]


def test_all_skills_validate():
    for d in ALL_SKILLS:
        valid, errs, _warns = validate_skill_dir(d)
        assert valid, f"{d} failed validation: {errs}"


def test_no_hallucinated_autonomy_flag():
    """Autonomy is a config key (`-c autonomy=…`), NOT a `--autonomy` flag.
    The buggy invocation form `--autonomy auto` must appear nowhere we teach
    an agent — it would fail the moment the agent runs it."""
    bad = re.compile(r"--autonomy\s+(auto|bypass|locked|manual)")
    offenders = [str(p) for p in SKILL_DOCS if bad.search(p.read_text())]
    assert not offenders, f"`--autonomy <mode>` is not a real flag; use `-c autonomy=`: {offenders}"


def test_correct_autonomy_form_is_taught():
    driver = (ROOT / "skills" / "relaydeck" / "SKILL.md").read_text()
    assert "-c autonomy=" in driver


@pytest.mark.parametrize("bogus_type", ["gemini"])
def test_no_unregistered_harness_types_listed(bogus_type):
    """The driver skill must not advertise a harness `-t` type that isn't
    registered (there is no gemini harness)."""
    driver = (ROOT / "skills" / "relaydeck" / "SKILL.md").read_text()
    # Allow the word in prose, but not in the type list line.
    type_lines = [ln for ln in driver.splitlines() if "-t`)" in ln or "Types" in ln]
    for ln in type_lines:
        assert bogus_type not in ln, f"{bogus_type!r} is not a registered harness type: {ln!r}"


def test_driver_skill_is_general_purpose_not_coding_only():
    """The reframe: relaydeck manages the fleet; the purpose is the user's."""
    meta = read_skill_metadata(ROOT / "skills" / "relaydeck")
    desc = meta["description"].lower()
    assert "general-purpose" in desc
    body = (ROOT / "skills" / "relaydeck" / "SKILL.md").read_text().lower()
    # A non-coding purpose is named explicitly so the skill doesn't read as
    # coding-orchestration-only.
    assert any(word in body for word in ("research", "migration", "monitoring", "ops"))


def test_catalog_lists_every_skill():
    catalog = (ROOT / "skills" / "README.md").read_text()
    for name in (
        "relaydeck", "relaydeck-fleet", "relaydeck-plugin-dev", "relaydeck-cli",
        "relaydeck-telegram", "relaydeck-prompts", "relaydeck-dashboard",
        "relaydeck-theme",
    ):
        assert name in catalog, f"catalog skills/README.md is missing {name}"


def test_driver_skill_cross_links_siblings():
    driver = (ROOT / "skills" / "relaydeck" / "SKILL.md").read_text()
    for sibling in ("relaydeck-fleet", "relaydeck-cli", "relaydeck-telegram",
                    "relaydeck-plugin-dev"):
        assert sibling in driver, f"driver skill should point at {sibling}"
