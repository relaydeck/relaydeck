"""Curated community plugin registry (plugins/registry.yaml +
relaydeck/plugin_registry.py).

The registry is relaydeck's "recommended list": maintainer-curated third-party
plugins, pinned, surfaced by `plugin search` and resolved by name on
`plugin install`. These tests pin the parse + search + install-spec resolution
and the `curated` trust tier, using a temp registry so they don't depend on the
(intentionally empty) shipped one.
"""

from __future__ import annotations

import textwrap

import pytest

from relaydeck import plugin_registry as reg


@pytest.fixture
def curated(tmp_path, monkeypatch):
    path = tmp_path / "registry.yaml"
    path.write_text(textwrap.dedent("""
        version: 1
        plugins:
          - name: acme-slack
            summary: "Route escalations to Slack."
            package: relaydeck-plugin-acme-slack
            version: ">=1.0,<2"
            repo: https://github.com/acme/relaydeck-slack
            maintainer: acme
            categories: [tool]
          - name: acme-git
            summary: "Mirror PRs."
            repo: https://github.com/acme/relaydeck-git
            git_ref: v0.3.0
            categories: [tool]
    """))
    monkeypatch.setattr(reg, "_registry_yaml_path", lambda: path)
    return reg


def test_shipped_registry_is_empty_but_valid():
    # The repo ships an empty curated registry (no community plugins yet).
    assert reg.load_registry() == []


def test_load_and_get(curated):
    entries = curated.load_registry()
    assert {e.name for e in entries} == {"acme-slack", "acme-git"}
    e = curated.get_entry("acme-slack")
    assert e is not None and e.maintainer == "acme"
    assert curated.get_entry("nope") is None


def test_search_matches_name_summary_category(curated):
    assert {e.name for e in curated.search("slack")} == {"acme-slack"}
    assert {e.name for e in curated.search("tool")} == {"acme-slack", "acme-git"}
    assert {e.name for e in curated.search("")} == {"acme-slack", "acme-git"}
    assert curated.search("nomatch") == []


def test_install_spec_pypi_vs_git(curated):
    # PyPI package with version constraint.
    assert curated.get_entry("acme-slack").install_spec() == (
        "relaydeck-plugin-acme-slack>=1.0,<2"
    )
    # git-only entry → pinned git URL.
    assert curated.get_entry("acme-git").install_spec() == (
        "git+https://github.com/acme/relaydeck-git@v0.3.0"
    )


def test_is_curated(curated):
    assert curated.is_curated("acme-slack") is True
    assert curated.is_curated("vault") is False


def test_curated_trust_tier(curated):
    """An installed+approved plugin named in the registry resolves to the
    `curated` trust tier (above local)."""
    from relaydeck.plugin import PluginEntry, effective_trust_level

    entry = PluginEntry(
        name="acme-slack", category="tool", version="1.0.0",
        instance=object(), source="installed", path=None,  # type: ignore[arg-type]
        manifest=None, lock_approved=True,
    )
    assert effective_trust_level(entry) == "curated"
    # Same entry, not in registry → plain local.
    entry.name = "something-else"
    assert effective_trust_level(entry) == "local"
    # Unapproved → untrusted regardless of registry.
    entry.name = "acme-slack"
    entry.lock_approved = False
    assert effective_trust_level(entry) == "untrusted"
