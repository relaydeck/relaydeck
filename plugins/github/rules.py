"""
Rules engine for the github plugin.

Loads `github.yaml` from a workspace, matches inbound events against
its rule predicates, and renders action bodies with mustache-style
`{{ dotted.path }}` substitution against the event payload.

Pure functions — no I/O, no orchestrator coupling. The poller hands us
an event dict + a list of compiled rules and we yield the matched
(rule, rendered_action) pairs. That separation keeps `relaydeck github
rules test` honest: it runs the same matcher the worker does, just
against a fixture event instead of a live one.

The YAML shape is documented in the plugin docstring; the short
version is:

    repo: owner/name
    poll_interval_s: 30

    rules:
      - name: bug-to-triager
        when:
          event: IssuesEvent       # GitHub Event API type
          action: labeled          # webhook-style sub-action
          label: bug               # equals match on payload.label.name
        do:
          - agent.message:
              to: triager
              body: "{{ issue.title }}"

The `when` block keys are matched against a flattened view of the
event payload (see `_flatten_for_match`), so `label: bug` works
whether the label is at `payload.label.name` or `label.name` — we
walk both shapes and accept either.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """A compiled rule. `when` and `do` are kept as plain dicts/lists
    because matching is purely structural — we don't gain anything by
    forcing the user's free-form YAML into a typed schema."""

    name: str
    when: dict[str, Any]
    do: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RulesConfig:
    repo: str
    poll_interval_s: float
    rules: tuple[Rule, ...]


# ── Loading ─────────────────────────────────────────────────────────


def load_config(path: Path) -> RulesConfig | None:
    """Load and validate `github.yaml`. Returns None if the file is
    missing — the plugin treats absence as "this workspace has not
    opted into github routing" and silently does nothing.

    Malformed YAML or a missing `repo` key raises ValueError. We
    prefer a loud failure on misconfiguration over silent skipping
    so the operator sees the problem during `relaydeck github status`.
    """
    if not path.exists():
        return None
    try:
        from relaydeck.config import _load_yaml_or_toml
        data = _load_yaml_or_toml(path)
    except Exception as exc:
        raise ValueError(f"github.yaml is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("github.yaml top-level must be a mapping")
    repo = data.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise ValueError("github.yaml requires `repo: owner/name`")
    interval = data.get("poll_interval_s", 30.0)
    try:
        interval = float(interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("`poll_interval_s` must be a number") from exc
    if interval < 1.0:
        raise ValueError("`poll_interval_s` must be >= 1.0")

    rules_raw = data.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ValueError("`rules` must be a list")
    compiled: list[Rule] = []
    for i, entry in enumerate(rules_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"rule #{i}: must be a mapping")
        name = str(entry.get("name") or f"rule-{i}")
        when = entry.get("when") or {}
        do = entry.get("do") or []
        if not isinstance(when, dict):
            raise ValueError(f"rule {name!r}: `when` must be a mapping")
        if not isinstance(do, list):
            raise ValueError(f"rule {name!r}: `do` must be a list")
        compiled.append(Rule(name=name, when=dict(when), do=list(do)))
    return RulesConfig(repo=repo, poll_interval_s=interval, rules=tuple(compiled))


# ── Matching ────────────────────────────────────────────────────────


def matches(rule: Rule, event: dict[str, Any]) -> bool:
    """Return True if `event` satisfies every key in `rule.when`.

    `event` is the raw GitHub Events-API record: it has top-level
    `type` (e.g. "IssuesEvent") plus a `payload` dict with the
    event-specific shape. The matcher tries several lookup paths
    per `when` key:

      1. exact top-level (`event["type"] == when["type"]`)
      2. payload.<key>                       — common GitHub layout
      3. payload.<key>.name                  — label, repo, etc.
      4. payload.<key>.login                 — actors, reviewers
      5. payload.requested_reviewer.login    — explicit PR reviewer hit

    Special cases:
      - `event:` always matches against the top-level `type` field.
      - Values can be a scalar (exact-match) OR a list (OR-of-equals).
    """
    flat = _flatten_for_match(event)
    for key, expected in rule.when.items():
        actual = _lookup(flat, key, event)
        if not _scalar_match(actual, expected):
            return False
    return True


def _lookup(flat: dict[str, Any], key: str, event: dict[str, Any]) -> Any:
    """Resolve a `when` key against the flattened event view.

    Labels and actors carry their identifying string under `.name` or
    `.login` — match against either if the rule writer used the parent
    key directly (e.g. `label: bug`). That's why we unwrap dicts found
    at any of the lookup steps below."""
    if key == "event":
        return event.get("type")
    if key in flat:
        return _maybe_unwrap(flat[key])
    payload = event.get("payload") or {}
    if isinstance(payload, dict) and key in payload:
        return _maybe_unwrap(payload[key])
    return None


def _maybe_unwrap(value: Any) -> Any:
    """If `value` is a GitHub object dict with `name`/`login`, return
    that string so a `label: bug` rule matches a label dict's `.name`
    without forcing the writer to spell `label.name: bug`."""
    if isinstance(value, dict):
        return value.get("name") or value.get("login") or value
    return value


def _scalar_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_scalar_match(actual, e) for e in expected)
    return actual == expected


def _flatten_for_match(event: dict[str, Any]) -> dict[str, Any]:
    """Top-level + payload keys, with payload taking precedence on
    collision — `action` lives in `payload`, not at top level."""
    flat: dict[str, Any] = {}
    for k, v in event.items():
        if k == "payload":
            continue
        flat[k] = v
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            flat[k] = v
    return flat


# ── Template substitution ──────────────────────────────────────────


_TMPL_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]]+)\s*\}\}")


def render(value: Any, event: dict[str, Any]) -> Any:
    """Recursively substitute `{{ dotted.path }}` placeholders.

    - Scalars: passes through unless it's a string with `{{ }}`.
    - Strings: each `{{ path }}` resolved against the event's
      `payload` (preferred) then top-level event keys.
    - Lists / dicts: rendered element-wise.

    Missing paths render as the literal placeholder unchanged AND log a
    warning, rather than blowing up — a rule that references a field
    not present on a given event type shouldn't crash the worker.
    """
    if isinstance(value, str):
        return _render_str(value, event)
    if isinstance(value, list):
        return [render(v, event) for v in value]
    if isinstance(value, dict):
        return {k: render(v, event) for k, v in value.items()}
    return value


def _render_str(s: str, event: dict[str, Any]) -> str:
    def repl(match: re.Match) -> str:
        path = match.group(1)
        resolved = _resolve_path(path, event)
        if resolved is None:
            logger.warning("github: template path %r not found in event", path)
            return match.group(0)
        return str(resolved)
    return _TMPL_RE.sub(repl, s)


def _resolve_path(path: str, event: dict[str, Any]) -> Any:
    """Resolve `a.b.c` against payload first, then the event root.

    The order matters: most rules want to talk about the payload
    (`issue.title`, `pull_request.number`). Falling back to root
    lets you reach `type` or `actor.login` directly without
    having to prefix every reference."""
    parts = _split_path(path)
    payload = event.get("payload")
    if isinstance(payload, dict):
        out = _walk(payload, parts)
        if out is not None:
            return out
    return _walk(event, parts)


def _split_path(path: str) -> list[str | int]:
    """Split `a.b[0].c` into `['a', 'b', 0, 'c']`."""
    parts: list[str | int] = []
    for chunk in path.split("."):
        # Strip [n] indices.
        while "[" in chunk:
            head, _, rest = chunk.partition("[")
            if head:
                parts.append(head)
            idx_str, _, chunk = rest.partition("]")
            try:
                parts.append(int(idx_str))
            except ValueError:
                # Malformed — treat the chunk as a string key.
                parts.append("[" + idx_str + "]" + chunk)
                chunk = ""
                break
        if chunk:
            parts.append(chunk)
    return parts


def _walk(obj: Any, parts: Iterable[str | int]) -> Any:
    for p in parts:
        if obj is None:
            return None
        if isinstance(p, int):
            if isinstance(obj, list) and -len(obj) <= p < len(obj):
                obj = obj[p]
            else:
                return None
        else:
            if isinstance(obj, dict) and p in obj:
                obj = obj[p]
            else:
                return None
    return obj


# ── Convenience for the worker + CLI ───────────────────────────────


def evaluate(config: RulesConfig, event: dict[str, Any]) -> list[tuple[Rule, list[dict[str, Any]]]]:
    """Walk all rules in order; return matches with their `do` lists
    rendered against the event. Used by both the live poller and
    `relaydeck github rules test` so they share one code path."""
    out: list[tuple[Rule, list[dict[str, Any]]]] = []
    for rule in config.rules:
        if not matches(rule, event):
            continue
        rendered = [render(action, event) for action in rule.do]
        out.append((rule, rendered))
    return out
