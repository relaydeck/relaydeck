"""
Default-private logging: PTY byte streams stay out of structured
logs unless the operator opts in via `RELAYDECK_LOG_PTY=1`.

Today no log statement in the codebase actually emits PTY
content — but the defensive filter on the JSON formatter
prevents future regressions. A debug breadcrumb added in a hurry
("logger.debug('got chunk', extra={'pty_bytes': data})") would
otherwise silently land sensitive terminal output in the logs.
"""

from __future__ import annotations

import json
import logging

from relaydeck.metrics import (
    _PTY_SENSITIVE_KEYS,
    _pty_logging_allowed,
    configure_json_logging,
)


def _capture_logs(monkeypatch, body_fn):
    """Configure JSON logging, run `body_fn(logger)`, return the
    captured stderr lines as parsed dicts."""
    import io
    sink = io.StringIO()
    configure_json_logging(stream=sink)
    body_fn(logging.getLogger("test"))
    # Drain handlers so the buffer is flushed.
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    lines = [ln for ln in sink.getvalue().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_pty_keys_are_redacted_by_default(monkeypatch):
    monkeypatch.delenv("RELAYDECK_LOG_PTY", raising=False)
    logs = _capture_logs(
        monkeypatch,
        lambda log: log.warning(
            "something happened",
            extra={
                "pty_bytes": b"sensitive secrets",
                "pty_buffer": b"more secrets",
                "agent_id": "alice",
            },
        ),
    )
    assert len(logs) == 1
    row = logs[0]
    # Sensitive keys → "<redacted>".
    assert row["pty_bytes"] == "<redacted>"
    assert row["pty_buffer"] == "<redacted>"
    # Non-sensitive keys pass through.
    assert row["agent_id"] == "alice"
    # And the bytes content NEVER appears anywhere in the record.
    blob = json.dumps(row)
    assert "sensitive" not in blob
    assert "secrets" not in blob


def test_pty_keys_pass_through_when_opted_in(monkeypatch):
    """`RELAYDECK_LOG_PTY=1` is the diagnostic-mode opt-in. With it set,
    sensitive values are included in their normal serialized form
    (bytes → repr because JSON can't carry raw bytes)."""
    monkeypatch.setenv("RELAYDECK_LOG_PTY", "1")
    logs = _capture_logs(
        monkeypatch,
        lambda log: log.warning(
            "diagnostic mode",
            extra={"pty_bytes": b"now visible"},
        ),
    )
    assert len(logs) == 1
    # bytes don't go through json.dumps directly — the formatter
    # falls back to repr() for unserializable values. Either way,
    # the content surface should now contain the marker.
    blob = json.dumps(logs[0])
    assert "now visible" in blob
    assert logs[0]["pty_bytes"] != "<redacted>"


def test_pty_logging_allowed_default_false(monkeypatch):
    monkeypatch.delenv("RELAYDECK_LOG_PTY", raising=False)
    assert _pty_logging_allowed() is False


def test_pty_logging_allowed_when_env_set(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("RELAYDECK_LOG_PTY", v)
        assert _pty_logging_allowed() is True, v


def test_sensitive_keys_set_is_non_empty():
    """If the constant ever empties out, the redaction filter
    becomes a no-op — catch that early."""
    assert len(_PTY_SENSITIVE_KEYS) >= 4
    assert "pty_bytes" in _PTY_SENSITIVE_KEYS
