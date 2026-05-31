"""Read-only health/posture probes — deterministic via injected seams."""

from __future__ import annotations

from plugins.external_agents import probes
from plugins.external_agents.models import (
    HERMES,
    OPENCLAW,
    RISK_CLI_AVAILABLE,
    RISK_CLI_MISSING,
    RISK_CONFIG_PRESENT,
    RISK_GATEWAY_CONFIGURED,
    RISK_IN_PROCESS_PLUGINS,
    RISK_LOCAL_ONLY,
    RISK_MCP_AVAILABLE,
    RISK_SECRETS_FILE_PRESENT,
    ExternalAgent,
)


def _hermes(tmp_path, config=True):
    home = tmp_path / ".hermes"
    home.mkdir()
    if config:
        (home / "config.yaml").write_text("model: x\n")
    return ExternalAgent(id="h", kind=HERMES, name="h", root=str(home), config_home=str(home))


def test_hermes_cli_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_which", lambda n: "/usr/bin/hermes")
    monkeypatch.setattr(probes, "_run_cli", lambda argv, timeout: (0, "hermes 1.2.3"))
    rep = probes.run_probes(_hermes(tmp_path))
    assert rep.health["cli"] == "ok"
    assert "1.2.3" in rep.summary
    assert RISK_CLI_AVAILABLE in rep.risk
    assert RISK_MCP_AVAILABLE in rep.risk
    assert RISK_IN_PROCESS_PLUGINS in rep.risk
    assert RISK_CONFIG_PRESENT in rep.risk
    assert RISK_LOCAL_ONLY in rep.risk


def test_hermes_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_which", lambda n: None)
    rep = probes.run_probes(_hermes(tmp_path))
    assert rep.health["cli"] == "missing"
    assert RISK_CLI_MISSING in rep.risk
    assert RISK_MCP_AVAILABLE not in rep.risk


def test_cli_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_which", lambda n: "/usr/bin/hermes")
    monkeypatch.setattr(probes, "_run_cli", lambda argv, timeout: (124, ""))
    rep = probes.run_probes(_hermes(tmp_path))
    assert rep.health["cli"] == "timeout"


def test_skip_cli_probe(tmp_path, monkeypatch):
    # run_cli_probe=False must NOT exec the CLI (list views stay fast).
    monkeypatch.setattr(probes, "_which", lambda n: "/usr/bin/hermes")
    def _boom(argv, timeout):
        raise AssertionError("CLI must not be executed when run_cli_probe=False")
    monkeypatch.setattr(probes, "_run_cli", _boom)
    rep = probes.run_probes(_hermes(tmp_path), run_cli_probe=False)
    assert rep.health["cli"] == "installed"


def test_secrets_file_present_but_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_which", lambda n: None)
    a = _hermes(tmp_path)
    secret = "SUPER_SECRET_TOKEN=abc123"
    (tmp_path / ".hermes" / ".env").write_text(secret)
    rep = probes.run_probes(a)
    assert RISK_SECRETS_FILE_PRESENT in rep.risk
    # The secret value must never appear anywhere in the report.
    blob = str(rep.to_dict())
    assert "SUPER_SECRET_TOKEN" not in blob
    assert "abc123" not in blob


def test_openclaw_gateway_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_which", lambda n: None)
    monkeypatch.setattr(probes, "_tcp_open", lambda h, p, t: True)
    home = tmp_path / ".openclaw"
    home.mkdir()
    a = ExternalAgent(id="o", kind=OPENCLAW, name="o", root=str(home), config_home=str(home))
    rep = probes.run_probes(a)
    assert rep.health["gateway"] == "reachable"
    assert RISK_GATEWAY_CONFIGURED in rep.risk


def test_openclaw_gateway_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_which", lambda n: None)
    monkeypatch.setattr(probes, "_tcp_open", lambda h, p, t: False)
    home = tmp_path / ".openclaw"
    home.mkdir()
    a = ExternalAgent(id="o", kind=OPENCLAW, name="o", root=str(home), config_home=str(home))
    rep = probes.run_probes(a)
    assert rep.health["gateway"] == "closed"
    assert RISK_GATEWAY_CONFIGURED not in rep.risk
