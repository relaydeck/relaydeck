"""
In-process metrics registry + Prometheus exposition.

We roll a tiny Prometheus exporter rather than depending on
`prometheus_client` because:

  * The exposition format is well-defined and trivial to emit.
  * Avoiding a global default registry side-step makes the SDK
    surface (`host.metrics.counter(...)`) easier to reason about.
  * Tests don't need to coordinate with a process-wide singleton.

Two metric types ship today: `Counter` (monotonic) and `Gauge`
(point-in-time). Histograms can come later if a use case shows up;
the current set of built-in metrics doesn't need them.

## Cardinality discipline

Labels are deliberately cheap to add but expensive at scrape time —
high-cardinality labels (per-agent-id, per-message-id, per-user) are
the classic mistake. Keep label values to a small bounded set:
status, provider, model name, plugin name. Anything per-request or
per-agent-id-of-which-there-could-be-millions goes in logs, not
metrics.

## Public surface

Module-level `registry()` returns the process-wide registry; SDK's
`host.metrics` thin-wraps it with a plugin-scoped name prefix so two
plugins can't accidentally collide on a series name.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class _Series:
    """One metric series — counter or gauge — with labelled values."""

    __slots__ = ("name", "help_text", "metric_type", "_values", "_lock")

    def __init__(self, name: str, help_text: str, metric_type: str) -> None:
        self.name = name
        self.help_text = help_text
        self.metric_type = metric_type  # "counter" | "gauge"
        # Labels dict-tuple → value. Use a sorted-items tuple as key so
        # `{a=1,b=2}` and `{b=2,a=1}` resolve to the same series.
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        if not labels:
            return ()
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    def add(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = value

    def snapshot(self) -> list[tuple[tuple[tuple[str, str], ...], float]]:
        with self._lock:
            return list(self._values.items())


class _Registry:
    """Process-wide metric series registry. Thread-safe."""

    def __init__(self) -> None:
        self._series: dict[str, _Series] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str = "") -> _Series:
        return self._get_or_create(name, help_text, "counter")

    def gauge(self, name: str, help_text: str = "") -> _Series:
        return self._get_or_create(name, help_text, "gauge")

    def _get_or_create(self, name: str, help_text: str, metric_type: str) -> _Series:
        with self._lock:
            s = self._series.get(name)
            if s is None:
                s = _Series(name, help_text, metric_type)
                self._series[name] = s
            elif s.metric_type != metric_type:
                raise ValueError(
                    f"metric {name!r} already registered as "
                    f"{s.metric_type}, can't redefine as {metric_type}"
                )
            return s

    def render_prometheus(self) -> str:
        """Render every series in the Prometheus text exposition format.

        Lines:
          # HELP <name> <help_text>
          # TYPE <name> <type>
          <name>{label="value",...} <number>
        """
        lines: list[str] = []
        with self._lock:
            series_list = sorted(self._series.values(), key=lambda s: s.name)
        for s in series_list:
            if s.help_text:
                lines.append(f"# HELP {s.name} {_escape_help(s.help_text)}")
            lines.append(f"# TYPE {s.name} {s.metric_type}")
            for labels, value in sorted(s.snapshot()):
                if labels:
                    rendered = ",".join(
                        f'{k}="{_escape_label_value(v)}"' for k, v in labels
                    )
                    lines.append(f"{s.name}{{{rendered}}} {_fmt(value)}")
                else:
                    lines.append(f"{s.name} {_fmt(value)}")
        # Trailing newline is required by the format.
        return "\n".join(lines) + "\n"

    def clear(self) -> None:
        """Drop every registered series. Test-only — production code
        should never call this. The conftest fixture uses it to keep
        tests independent."""
        with self._lock:
            self._series.clear()


def _escape_help(text: str) -> str:
    # HELP lines can't contain newlines; replace + drop backslashes.
    return text.replace("\n", " ").replace("\\", "/")


def _escape_label_value(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _fmt(value: float) -> str:
    """Render a metric number. Integers stay integers; floats keep
    enough precision for sub-millisecond latency without going
    scientific for everyday counts."""
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


_REGISTRY = _Registry()


def registry() -> _Registry:
    """Return the process-wide registry. SDK + built-in metric
    initialization both go through this single instance so /metrics
    sees everything."""
    return _REGISTRY


# ── Built-in series (initialized lazily so the import-time graph
#    stays cheap) ─────────────────────────────────────────────────────


def init_builtin_series() -> None:
    """Pre-register the built-in metric series with HELP text. Called
    once at daemon boot so the exposition has documented metrics
    even before any first event tips a counter.

    Idempotent — `_get_or_create` returns the existing series on a
    repeated call."""
    r = _REGISTRY
    # Messages
    r.counter("relaydeck_messages_total",
              "Agent-to-agent messages by terminal delivery state.")
    r.counter("relaydeck_message_delivery_attempts_total",
              "Cumulative PTY-write delivery attempts (including retries).")
    # Agents (gauge, set on lifecycle transitions)
    r.gauge("relaydeck_agents",
            "Current count of agents by status.")
    # Usage records
    r.counter("relaydeck_usage_tokens_total",
              "Tokens consumed by provider+model+kind (prompt/completion).")
    # Plugin event bus
    r.counter("relaydeck_bus_events_total",
              "Plugin bus events delivered, keyed by event type.")
    r.counter("relaydeck_bus_subscriber_errors_total",
              "Plugin bus subscriber exceptions, keyed by handler.")
    # Harness PTY
    r.counter("relaydeck_harness_pty_overflow_bytes_total",
              "Bytes dropped from the harness PTY ring buffer.")
    r.counter("relaydeck_harness_pty_subscriber_drops_total",
              "Chunks dropped from full PTY subscriber queues.")
    # Workers
    r.gauge("relaydeck_workers",
            "Current count of workers by status.")
    r.counter("relaydeck_worker_restarts_total",
              "Worker restarts triggered by the supervisor.")
    r.counter("relaydeck_worker_crash_loops_total",
              "Workers that hit the crash-loop threshold.")


def record_message_state(state: str) -> None:
    """Increment the messages counter — called from messages.py on
    every state transition. Cheap; no-op when the registry has no
    subscriber (the counter still tracks but render isn't called)."""
    _REGISTRY.counter("relaydeck_messages_total").add(1.0, {"state": state})


def record_usage_event(provider: str, model: str, prompt: int, completion: int) -> None:
    c = _REGISTRY.counter("relaydeck_usage_tokens_total")
    if prompt:
        c.add(float(prompt), {"provider": provider, "model": model, "kind": "prompt"})
    if completion:
        c.add(float(completion), {"provider": provider, "model": model, "kind": "completion"})


def set_agents_gauge(counts_by_status: dict[str, int]) -> None:
    """Replace the agents gauge with a fresh snapshot of statuses.

    The full-replace approach (vs incremental add) is important
    because statuses can disappear — set running=0 explicitly when
    everything stopped, instead of leaving a stale `running=3`.
    """
    g = _REGISTRY.gauge("relaydeck_agents")
    # Zero out anything we might have set before that isn't in this snapshot.
    seen = set()
    for status, count in counts_by_status.items():
        g.set(float(count), {"status": status})
        seen.add(status)
    for (labels, _) in g.snapshot():
        d = dict(labels)
        if d.get("status") not in seen:
            g.set(0.0, d)


def record_bus_event(event_type: str) -> None:
    _REGISTRY.counter("relaydeck_bus_events_total").add(1.0, {"type": event_type})


def record_bus_error(handler: str) -> None:
    _REGISTRY.counter("relaydeck_bus_subscriber_errors_total").add(1.0, {"handler": handler})


def record_pty_overflow(agent_id: str, bytes_dropped: int) -> None:
    if bytes_dropped > 0:
        _REGISTRY.counter("relaydeck_harness_pty_overflow_bytes_total").add(
            float(bytes_dropped), {"agent_id": agent_id},
        )


def record_pty_subscriber_drop(agent_id: str, dropped: int = 1) -> None:
    if dropped > 0:
        _REGISTRY.counter("relaydeck_harness_pty_subscriber_drops_total").add(
            float(dropped), {"agent_id": agent_id},
        )


def record_worker_restart(plugin: str) -> None:
    _REGISTRY.counter("relaydeck_worker_restarts_total").add(1.0, {"plugin": plugin})


def record_worker_crash_loop(plugin: str) -> None:
    _REGISTRY.counter("relaydeck_worker_crash_loops_total").add(1.0, {"plugin": plugin})


def set_workers_gauge(counts_by_status: dict[str, int]) -> None:
    g = _REGISTRY.gauge("relaydeck_workers")
    seen = set()
    for status, count in counts_by_status.items():
        g.set(float(count), {"status": status})
        seen.add(status)
    for (labels, _) in g.snapshot():
        d = dict(labels)
        if d.get("status") not in seen:
            g.set(0.0, d)


# ── JSON logging formatter ──────────────────────────────────────────


# Keys we never include in logs unless the operator opts in via
# `RELAYDECK_LOG_PTY=1`. PTY byte streams are agent terminal output —
# they often contain credentials, prompts the user is mid-typing,
# file contents, and other private data. Default-private logging
# is a posture choice; diagnostic sessions can flip the env var.
_PTY_SENSITIVE_KEYS = frozenset({
    "pty_bytes",
    "pty_buffer",
    "raw_bytes",
    "raw_input",
    "pty_data",
    "screen_bytes",
})


def _pty_logging_allowed() -> bool:
    """True iff the operator has explicitly opted into PTY content
    in logs via `RELAYDECK_LOG_PTY=1`. Default is False (private)."""
    import os
    return os.environ.get("RELAYDECK_LOG_PTY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def configure_json_logging(stream: Any = None) -> None:
    """Switch the root logger to JSON-line output.

    Called by `relaydeck serve` when RELAYDECK_LOG_FORMAT=json. JSON lines are
    one-event-per-line, ordered keys, no embedded newlines —
    designed for ingestion by log shippers (vector, fluentbit,
    journalctl with json filtering).

    PTY byte streams are NEVER included by default. If a log
    statement passes `extra={"pty_bytes": b"..."}` (or any of the
    other `_PTY_SENSITIVE_KEYS`), the value is replaced with
    `"<redacted>"` unless `RELAYDECK_LOG_PTY=1` is set. Today no log
    statement in the codebase actually emits PTY content, but the
    filter prevents future regressions: a debug breadcrumb added
    in a hurry can't accidentally make agent terminal output
    public.
    """
    import json
    import logging
    import sys

    target = stream or sys.stderr
    allow_pty = _pty_logging_allowed()

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload: dict[str, Any] = {
                "ts": time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.gmtime(record.created),
                ) + f".{int((record.created % 1) * 1000):03d}Z",
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            # Attach any user-supplied `extra={...}` fields. Skip
            # internal LogRecord attributes so we don't dump the
            # entire record dict. Redact PTY-content keys unless
            # the operator has explicitly opted in.
            for k, v in record.__dict__.items():
                if k in _LOGRECORD_RESERVED:
                    continue
                if k in _PTY_SENSITIVE_KEYS and not allow_pty:
                    payload[k] = "<redacted>"
                    continue
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = repr(v)
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False)

    root = logging.getLogger()
    handler = logging.StreamHandler(target)
    handler.setFormatter(_JsonFormatter())
    # Replace any existing handlers so we don't double-log to stderr
    # in the human-readable format alongside JSON.
    root.handlers = [handler]
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


_LOGRECORD_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process",
    "taskName", "getMessage",
}
