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

from relaydeck.plugin_manifest import load_manifest
from relaydeck.skills import read_skill_metadata, validate_skill_dir

ROOT = Path(__file__).resolve().parent.parent

# Standalone, packaged-with-relaydeck skills (installed into an agent's own
# skill root). Plugin-owned skills live with their plugin and are covered by
# test_skills_plugin.py.
STANDALONE_SKILLS = [
    ROOT / "skills" / "relaydeck",
]

# Every first-party skill, wherever it physically lives.
ALL_SKILLS = STANDALONE_SKILLS + [
    ROOT / "plugins" / "skills" / "relaydeck-fleet",
    ROOT / "plugins" / "skills" / "relaydeck-plugin-dev",
    ROOT / "plugins" / "messaging",   # relaydeck-cli
    ROOT / "plugins" / "telegram",    # relaydeck-telegram
    ROOT / "plugins" / "prompts",     # relaydeck-prompts
    ROOT / "plugins" / "dashboard",   # relaydeck-dashboard
    ROOT / "plugins" / "theme",       # relaydeck-theme
]

# Markdown shipped to agents: SKILL bodies + the driver skill's reference/.
SKILL_DOCS = sorted(
    {p for d in ALL_SKILLS for p in d.rglob("*.md")}
    | {ROOT / "docs" / "USAGE.md"}
)


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
    bad = re.compile(rf"(?:-t|--type)\s+{re.escape(bogus_type)}\b", re.IGNORECASE)
    offenders = [str(path) for path in SKILL_DOCS if bad.search(path.read_text())]
    assert not offenders, f"{bogus_type!r} is not a registered harness type: {offenders}"


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
    catalog_names = set(
        re.findall(r"^\| \*\*([^*]+)\*\* \|", catalog, re.MULTILINE)
    )
    skill_names = {read_skill_metadata(skill)["name"] for skill in ALL_SKILLS}
    assert catalog_names == skill_names


def test_official_plugin_skill_paths_stay_inside_their_plugin():
    """Keep every plugin independently packageable, as `plugin verify` requires."""
    for manifest_path in sorted((ROOT / "plugins").glob("**/plugin.toml")):
        manifest = load_manifest(manifest_path)
        for skill_name, raw_path in manifest.skills.items():
            relative = Path(raw_path)
            assert not relative.is_absolute(), (
                f"{manifest.name}:{skill_name} uses absolute path {raw_path}"
            )
            assert ".." not in relative.parts, (
                f"{manifest.name}:{skill_name} escapes its plugin: {raw_path}"
            )
            assert (manifest_path.parent / relative).is_file(), (
                f"{manifest.name}:{skill_name} is missing {raw_path}"
            )


def test_documented_commands_avoid_known_stale_forms():
    stale = {
        "relaydeck context status": "use `relaydeck context-watch status`",
        "relaydeck message <msg-id>": "use `relaydeck message show <msg-id>`",
        "relaydeck integration install claude-code": "the integration id is `claude`",
        "relaydeck integration uninstall codex": "codex uses the always-on engine",
        "Stop then start (fresh session)": "restart honors configured session flags",
        "agent's `skills` list": "user skills use the workspace `skills` gate",
    }
    for old, replacement in stale.items():
        offenders = [str(path) for path in SKILL_DOCS if old in path.read_text()]
        assert not offenders, f"stale command/claim {old!r}; {replacement}: {offenders}"


def test_high_risk_documented_commands_include_required_flags():
    usage = (ROOT / "docs" / "USAGE.md").read_text()
    permissions = (
        ROOT / "skills" / "relaydeck" / "reference" / "permissions.md"
    ).read_text()
    assert "worktree create feat/x --repo ." in usage
    assert "skills import-git <git-url> --workspace api" in usage
    assert "auth issue --label ci --scope read-only" in permissions


def test_driver_skill_cross_links_siblings():
    driver = (ROOT / "skills" / "relaydeck" / "SKILL.md").read_text()
    for sibling in ("relaydeck-fleet", "relaydeck-cli", "relaydeck-telegram",
                    "relaydeck-plugin-dev"):
        assert sibling in driver, f"driver skill should point at {sibling}"
