"""Claude Code token metering — tail the transcript JSONL into usage.record.

claude-code (unlike pi/codex) had no usage tailer, so its agents always showed
0 tokens in the stat strip / usage rollup even while burning real tokens. This
pins the tailer:

  - an assistant transcript line's `message.usage` becomes a usage.record with
    prompt = input + cache_creation (cache_read EXCLUDED — it bills ~0.1x and
    would inflate the table-derived cost), completion = output_tokens, model
    from message.model, provider anthropic, cost None (the metering plugin
    prices it)
  - non-assistant / no-usage lines are ignored
  - the transcript dir is the claude CWD munge (`[^A-Za-z0-9]` -> `-`)
  - attribution is conservative: tail only when this is the SOLE claude agent in
    the workspace (else stay at honest 0 rather than double-count a shared dir)
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from plugins.harnesses.claude_code.agent import ClaudeCodeAgent


@pytest.fixture(autouse=True)
def _clear_env():
    yield
    os.environ.pop("RELAYDECK_CONFIG_HOME", None)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)


def _agent(tmp_path, *, cwd="/Users/dev/projects/myapp", workspace="myapp"):
    cfg = tmp_path / ".relaydeck"
    (cfg / "agents").mkdir(parents=True)
    os.environ["RELAYDECK_CONFIG_HOME"] = str(cfg)
    a = ClaudeCodeAgent(
        agent_id="cc", name="cc", config={"cwd": cwd}, workspace=workspace,
        db_path=str(tmp_path / "relaydeck.db"), stop_flag=threading.Event(),
    )
    return a


_USAGE_LINE = json.dumps({
    "type": "assistant",
    "message": {
        "model": "claude-opus-4-7",
        "usage": {
            "input_tokens": 2605,
            "cache_creation_input_tokens": 2954,
            "cache_read_input_tokens": 9209,
            "output_tokens": 153,
        },
    },
}).encode()


def test_transcript_line_becomes_usage_record(tmp_path):
    a = _agent(tmp_path)
    seen = []
    a.emit = lambda t, p=None: seen.append((t, p)) or 0
    a._emit_bus = lambda t, d: seen.append((t, d))

    a._handle_transcript_line(_USAGE_LINE)

    payloads = [p for (t, p) in seen if t == "usage.record"]
    assert payloads, "an assistant usage line must emit usage.record"
    p = payloads[0]
    # input + cache_creation, but NOT cache_read (9209) — that bills ~0.1x and
    # would inflate the table-derived cost.
    assert p["prompt"] == 2605 + 2954
    assert p["completion"] == 153
    assert p["model"] == "claude-opus-4-7"
    assert p["provider"] == "anthropic"
    assert p["cost_usd"] is None  # subscription — no per-call cost


def test_non_usage_lines_ignored(tmp_path):
    a = _agent(tmp_path)
    seen = []
    a.emit = lambda t, p=None: seen.append((t, p)) or 0
    a._emit_bus = lambda t, d: seen.append((t, d))

    a._handle_transcript_line(json.dumps({"type": "user", "message": {"role": "user"}}).encode())
    a._handle_transcript_line(b'{"type":"system"}')
    a._handle_transcript_line(b"not json at all")
    assert seen == []


def test_transcript_dir_munges_cwd(tmp_path):
    a = _agent(tmp_path, cwd="/Users/dev/projects/myapp")
    base = tmp_path / "claude-home"
    os.environ["CLAUDE_CONFIG_DIR"] = str(base)
    expected = base / "projects" / "-Users-dev-projects-myapp"
    expected.mkdir(parents=True)
    assert a._transcript_dir() == expected


def test_transcript_dir_none_when_absent(tmp_path):
    a = _agent(tmp_path)
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp_path / "nope")
    assert a._transcript_dir() is None


def test_sole_claude_guard(tmp_path):
    """Tail only when this agent is the lone claude in the workspace — a second
    claude sharing the CWD-keyed transcript dir would double-count."""
    from relaydeck.db import open_db, upsert_agent

    a = _agent(tmp_path)
    conn = open_db(a.db_path)
    upsert_agent(conn, "cc", "claude-code", "cc", workspace="myapp")
    conn.commit()
    assert a._sole_claude_in_workspace() is True

    upsert_agent(conn, "cc2", "claude-code", "cc2", workspace="myapp")
    conn.commit()
    conn.close()
    assert a._sole_claude_in_workspace() is False


def test_scan_transcript_dir_end_to_end(tmp_path):
    """A full sweep of a transcript file emits one record per usage line and
    tracks the byte offset so a re-sweep doesn't double-count."""
    a = _agent(tmp_path)
    seen = []
    a.emit = lambda t, p=None: seen.append(p) or 0
    a._emit_bus = lambda t, d: None  # avoid double-append; emit() is enough here

    tdir = tmp_path / "tx"
    tdir.mkdir()
    f = tdir / "session.jsonl"
    f.write_bytes(_USAGE_LINE + b"\n")

    a._scan_transcript_dir(tdir)
    assert len(seen) == 1
    # Re-sweep with no new bytes → no new records (offset honored).
    a._scan_transcript_dir(tdir)
    assert len(seen) == 1
    # Append another usage line → exactly one more record.
    with f.open("ab") as fh:
        fh.write(_USAGE_LINE + b"\n")
    a._scan_transcript_dir(tdir)
    assert len(seen) == 2
