"""Detection of Hermes / OpenClaw runtimes (filesystem-first, side-effect-free)."""

from __future__ import annotations

from plugins.external_agents import detector
from plugins.external_agents.models import HERMES, OPENCLAW, UNKNOWN


def _no_cli(monkeypatch):
    # Default: neither CLI installed, so confidence comes from filesystem
    # markers only (deterministic regardless of the test host).
    monkeypatch.setattr(detector, "_which", lambda name: None)


def test_hermes_repo_via_pyproject(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\n')
    det = detector.detect(tmp_path)
    assert det.kind == HERMES
    assert det.matched
    assert det.confidence >= 0.5
    assert det.recommended_transport == "mcp"
    assert any("pyproject" in s for s in det.signals)


def test_hermes_config_home(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: x\n")
    det = detector.detect(home)
    assert det.kind == HERMES
    assert det.confidence >= 0.9  # home-name + config file
    assert det.config_home == str(home)


def test_openclaw_repo_via_mjs(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    (tmp_path / "openclaw.mjs").write_text("// entry\n")
    (tmp_path / "package.json").write_text('{"name":"openclaw"}')
    det = detector.detect(tmp_path)
    assert det.kind == OPENCLAW
    assert det.confidence >= 0.9
    assert det.recommended_transport == "gateway-ws"


def test_openclaw_config_home(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    home = tmp_path / ".openclaw"
    home.mkdir()
    det = detector.detect(home)
    assert det.kind == OPENCLAW
    assert det.config_home == str(home)


def test_non_matching_dir_is_unknown(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    (tmp_path / "README.md").write_text("# just a repo\n")
    det = detector.detect(tmp_path)
    assert det.kind == UNKNOWN
    assert det.confidence == 0.0
    assert not det.matched


def test_nonexistent_path_warns(monkeypatch):
    _no_cli(monkeypatch)
    det = detector.detect("/nope/does/not/exist-xyz")
    assert det.kind == UNKNOWN
    assert det.warnings


def test_cli_on_path_boosts_confidence(tmp_path, monkeypatch):
    # A weak repo signal alone is below the candidate bar; the CLI being
    # installed should push it up and add a signal.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myproj"\ndependencies=["hermes"]\n'
    )
    monkeypatch.setattr(
        detector, "_which",
        lambda name: "/usr/bin/hermes" if name == "hermes" else None,
    )
    det = detector.detect(tmp_path)
    assert det.kind == HERMES
    assert any("CLI on PATH" in s for s in det.signals)


def test_hermes_beats_openclaw_when_stronger(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    # Mixed markers: strong hermes (pyproject) + weak openclaw (apps/ dir).
    (tmp_path / "pyproject.toml").write_text('name = "hermes-agent"\n')
    (tmp_path / "apps").mkdir()
    det = detector.detect(tmp_path)
    assert det.kind == HERMES


def test_scan_candidates_finds_homes(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() honors $HOME on posix
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text("x: 1\n")
    cands = detector.scan_candidates()
    kinds = {c.kind for c in cands}
    assert HERMES in kinds


def test_scan_candidates_includes_extra_roots(tmp_path, monkeypatch):
    _no_cli(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    repo = tmp_path / "src" / "openclaw"
    repo.mkdir(parents=True)
    (repo / "openclaw.mjs").write_text("//\n")
    cands = detector.scan_candidates(extra_roots=[str(repo)])
    assert any(c.kind == OPENCLAW and c.root == str(repo) for c in cands)
