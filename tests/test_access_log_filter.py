"""
uvicorn.access log noise filter.

The dashboard polls a handful of GET endpoints every second per open
tab; without filtering the daemon log fills with repetitive 200 OK
lines that drown out anything actionable. The filter drops 2xx/3xx
GETs to a known-noisy allowlist and keeps everything else (mutations,
errors, redirects on non-polling paths).

Pinning so:
  - heartbeat polls are dropped
  - non-2xx on the same path are NOT dropped (e.g. 401 surfaces)
  - mutation methods are NOT dropped
  - paths outside the allowlist are NOT dropped (catch-all)
"""

from __future__ import annotations

import logging

import pytest

from relaydeck.transports.cli import _install_access_log_filter


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="",
        lineno=0, msg=msg, args=(), exc_info=None,
    )


def _passes(msg: str) -> bool:
    """True if any installed filter on uvicorn.access lets the record through."""
    rec = _record(msg)
    return all(f.filter(rec) for f in logging.getLogger("uvicorn.access").filters)


@pytest.fixture(autouse=True)
def fresh_filter():
    """Each test gets a freshly-installed filter and a clean removal
    so we don't leak filters across the test session."""
    log = logging.getLogger("uvicorn.access")
    saved = log.filters[:]
    log.filters.clear()
    _install_access_log_filter()
    yield
    log.filters.clear()
    for f in saved:
        log.addFilter(f)


@pytest.mark.parametrize("line", [
    '127.0.0.1:1 - "GET /api/agents?workspace=demo HTTP/1.1" 200 OK',
    '127.0.0.1:1 - "GET /api/agents HTTP/1.1" 200 OK',
    '127.0.0.1:1 - "GET /api/plugins/handover/state HTTP/1.1" 200 OK',
    '127.0.0.1:1 - "GET /healthz HTTP/1.1" 200 OK',
    '127.0.0.1:1 - "GET /api/usage?agent_id=review HTTP/1.1" 200 OK',
])
def test_drops_successful_dashboard_polls(line):
    assert not _passes(line)


@pytest.mark.parametrize("line", [
    # Errors on noisy paths are actionable.
    '127.0.0.1:1 - "GET /api/agents?workspace=demo HTTP/1.1" 401 Unauthorized',
    '127.0.0.1:1 - "GET /api/agents HTTP/1.1" 404 Not Found',
    '127.0.0.1:1 - "GET /api/agents HTTP/1.1" 500 Internal Server Error',
    # Mutations are actionable.
    '127.0.0.1:1 - "POST /api/agents HTTP/1.1" 201 Created',
    '127.0.0.1:1 - "PATCH /api/agents/review HTTP/1.1" 200 OK',
    '127.0.0.1:1 - "DELETE /api/agents/review HTTP/1.1" 204 No Content',
    # Non-polling GETs remain visible.
    '127.0.0.1:1 - "GET /api/integrations HTTP/1.1" 200 OK',
    '127.0.0.1:1 - "GET /api/events HTTP/1.1" 200 OK',
])
def test_keeps_actionable_access_lines(line):
    assert _passes(line)


def test_keeps_non_access_log_unaffected():
    """The filter only acts on records matching the allowlist pattern.
    Non-HTTP lines (uvicorn boot, errors) flow through verbatim."""
    assert _passes('Started server process [12345]')
    assert _passes('Uvicorn running on http://127.0.0.1:8765')
