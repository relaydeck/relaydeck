"""
Tests for the bundled github plugin.

Layered top-down:
  - pure rules tests (load_config, matches, render, evaluate)
  - action dispatch tests (mock subprocess + sender)
  - poller integration test (monkeypatched fetch_events + MockHost)
  - plugin lifecycle test (workspace add/remove starts and stops poller)

No real `gh` calls — every test that touches the poller stubs
`fetch_events`. No real subprocess for script/gh actions either; the
relevant tests monkeypatch `subprocess.run` to capture the cmd/env.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from relaydeck.automation import ActionContext, ActionError, dispatch
from plugins.github import poller
from plugins.github.plugin import GithubPlugin, _workspace_has_github
from plugins.github.poller import (
    Cursor,
    GithubPoller,
    PollResult,
    cursor_path,
    load_cursor,
    save_cursor,
)
from plugins.github.rules import (
    Rule,
    RulesConfig,
    evaluate,
    load_config,
    matches,
    render,
)
from relaydeck.testing import MockHost

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def issue_labeled_event() -> dict:
    """A realistic IssuesEvent for action=labeled, label=bug."""
    return {
        "id": "9001",
        "type": "IssuesEvent",
        "actor": {"login": "alice"},
        "repo": {"name": "relaydeck/relaydeck"},
        "created_at": "2026-05-18T10:00:00Z",
        "payload": {
            "action": "labeled",
            "issue": {
                "number": 42,
                "title": "Broken thing",
                "html_url": "https://github.com/relaydeck/relaydeck/issues/42",
            },
            "label": {"name": "bug", "color": "ff0000"},
        },
    }


@pytest.fixture
def pr_opened_event() -> dict:
    return {
        "id": "9002",
        "type": "PullRequestEvent",
        "actor": {"login": "bob"},
        "repo": {"name": "relaydeck/relaydeck"},
        "created_at": "2026-05-18T10:01:00Z",
        "payload": {
            "action": "opened",
            "pull_request": {
                "number": 7,
                "title": "Add github plugin",
                "html_url": "https://github.com/relaydeck/relaydeck/pull/7",
            },
        },
    }


# ── Rules: loading ──────────────────────────────────────────────────


def test_load_config_returns_none_for_missing_file(tmp_path):
    assert load_config(tmp_path / "missing.yaml") is None


def test_load_config_requires_repo(tmp_path):
    path = tmp_path / "github.yaml"
    path.write_text("rules: []\n")
    with pytest.raises(ValueError, match="repo"):
        load_config(path)


def test_load_config_validates_interval(tmp_path):
    path = tmp_path / "github.yaml"
    path.write_text("repo: relaydeck/relaydeck\npoll_interval_s: 0.5\n")
    with pytest.raises(ValueError, match=">= 1.0"):
        load_config(path)


def test_load_config_parses_rules(tmp_path):
    path = tmp_path / "github.yaml"
    path.write_text(
        """
repo: relaydeck/relaydeck
poll_interval_s: 15
rules:
  - name: pr-review
    when:
      event: PullRequestEvent
      action: opened
    do:
      - agent.message:
          to: reviewer
          body: "Review #{{ pull_request.number }}"
"""
    )
    config = load_config(path)
    assert config is not None
    assert config.repo == "relaydeck/relaydeck"
    assert config.poll_interval_s == 15
    assert len(config.rules) == 1
    rule = config.rules[0]
    assert rule.name == "pr-review"
    assert rule.when == {"event": "PullRequestEvent", "action": "opened"}
    assert rule.do == [
        {"agent.message": {"to": "reviewer", "body": "Review #{{ pull_request.number }}"}}
    ]


# ── Rules: matching ────────────────────────────────────────────────


def test_matches_event_type(issue_labeled_event):
    assert matches(Rule(name="r", when={"event": "IssuesEvent"}, do=[]), issue_labeled_event)
    assert not matches(Rule(name="r", when={"event": "PushEvent"}, do=[]), issue_labeled_event)


def test_matches_payload_action(issue_labeled_event):
    assert matches(
        Rule(name="r", when={"event": "IssuesEvent", "action": "labeled"}, do=[]),
        issue_labeled_event,
    )
    assert not matches(
        Rule(name="r", when={"event": "IssuesEvent", "action": "opened"}, do=[]),
        issue_labeled_event,
    )


def test_matches_resolves_label_name_via_nested_dict(issue_labeled_event):
    assert matches(Rule(name="r", when={"label": "bug"}, do=[]), issue_labeled_event)


def test_matches_actor_via_nested_login(issue_labeled_event):
    assert matches(Rule(name="r", when={"actor": "alice"}, do=[]), issue_labeled_event)


def test_matches_supports_list_or_match(issue_labeled_event):
    assert matches(Rule(name="r", when={"label": ["bug", "P0"]}, do=[]), issue_labeled_event)
    assert not matches(Rule(name="r", when={"label": ["enhancement"]}, do=[]), issue_labeled_event)


def test_matches_missing_field_fails(issue_labeled_event):
    assert not matches(
        Rule(name="r", when={"event": "IssuesEvent", "unknown_field": "x"}, do=[]),
        issue_labeled_event,
    )


# ── Rules: rendering ───────────────────────────────────────────────


def test_render_substitutes_dotted_path(pr_opened_event):
    out = render("Review #{{ pull_request.number }} — {{ pull_request.title }}", pr_opened_event)
    assert out == "Review #7 — Add github plugin"


def test_render_walks_nested_dict_and_list(pr_opened_event):
    out = render(
        {"to": "reviewer", "body": "PR by {{ actor.login }}: {{ pull_request.html_url }}"},
        pr_opened_event,
    )
    assert out["body"] == "PR by bob: https://github.com/relaydeck/relaydeck/pull/7"


def test_render_missing_path_passes_through(pr_opened_event, caplog):
    out = render("hello {{ does.not.exist }}", pr_opened_event)
    assert out == "hello {{ does.not.exist }}"


def test_evaluate_returns_only_matching_rules(issue_labeled_event):
    config = RulesConfig(
        repo="relaydeck/relaydeck",
        poll_interval_s=30.0,
        rules=(
            Rule(name="match", when={"label": "bug"}, do=[{"bus.emit": {"type": "x", "data": {}}}]),
            Rule(name="skip", when={"label": "feature"}, do=[]),
        ),
    )
    result = evaluate(config, issue_labeled_event)
    assert [r.name for r, _ in result] == ["match"]


# ── Actions: agent.message ─────────────────────────────────────────


def test_action_agent_message_sends_via_orchestrator(issue_labeled_event):
    sent: list[dict] = []

    def fake_send(*, to, body, from_, in_reply_to):  # signature matches host.agents.send_message
        sent.append({"to": to, "body": body, "from_": from_, "in_reply_to": in_reply_to})

        class _R:
            ids = ("msg_1",)

        return _R()

    ctx = ActionContext(
        send_message=fake_send,
        emit_event=lambda *a, **kw: None,
        gh_binary="gh",
        workspace_path=None,
        event=issue_labeled_event,
    )
    summary = dispatch(
        {"agent.message": {"to": "triager", "body": "New bug"}},
        ctx,
    )
    assert summary["kind"] == "agent.message"
    assert summary["msg_id"] == "msg_1"
    assert sent == [{"to": "triager", "body": "New bug", "from_": "github", "in_reply_to": None}]


def test_action_agent_message_requires_to_and_body(issue_labeled_event):
    ctx = ActionContext(
        send_message=lambda **kw: None,
        emit_event=None,
        gh_binary="gh",
        workspace_path=None,
        event=issue_labeled_event,
    )
    with pytest.raises(ActionError, match="to:"):
        dispatch({"agent.message": {"body": "x"}}, ctx)
    with pytest.raises(ActionError, match="body:"):
        dispatch({"agent.message": {"to": "t"}}, ctx)


# ── Actions: bus.emit ─────────────────────────────────────────────


def test_action_bus_emit_calls_emit(issue_labeled_event):
    emitted: list[tuple[str, dict]] = []
    ctx = ActionContext(
        send_message=None,
        emit_event=lambda t, d: emitted.append((t, d)),
        gh_binary="gh",
        workspace_path=None,
        event=issue_labeled_event,
    )
    dispatch({"bus.emit": {"type": "custom", "data": {"k": "v"}}}, ctx)
    assert emitted == [("custom", {"k": "v"})]


# ── Actions: script ───────────────────────────────────────────────


def test_action_script_runs_with_event_on_stdin(tmp_path, issue_labeled_event):
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/sh\ncat > $0.received\n")
    script.chmod(0o755)
    ctx = ActionContext(
        send_message=None,
        emit_event=None,
        gh_binary="gh",
        workspace_path=tmp_path,
        event=issue_labeled_event,
    )
    summary = dispatch({"script": {"path": "ok.sh"}}, ctx)
    assert summary["returncode"] == 0
    received = (tmp_path / "ok.sh.received").read_text()
    assert json.loads(received) == issue_labeled_event


def test_action_script_missing_path_raises(tmp_path, issue_labeled_event):
    ctx = ActionContext(
        send_message=None,
        emit_event=None,
        gh_binary="gh",
        workspace_path=tmp_path,
        event=issue_labeled_event,
    )
    with pytest.raises(ActionError, match="script not found"):
        dispatch({"script": {"path": "does-not-exist.sh"}}, ctx)


# ── Actions: gh ────────────────────────────────────────────────────


def test_action_gh_runs_with_args(monkeypatch, tmp_path, issue_labeled_event):
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)

        class _P:
            returncode = 0
            stdout = b""
            stderr = b""

        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = ActionContext(
        send_message=None,
        emit_event=None,
        gh_binary="gh-stub",
        workspace_path=tmp_path,
        event=issue_labeled_event,
    )
    summary = dispatch(
        {"gh": {"args": ["pr", "comment", "42", "--body", "ack"]}},
        ctx,
    )
    assert summary["returncode"] == 0
    assert captured == [["gh-stub", "pr", "comment", "42", "--body", "ack"]]


# ── Actions: model ─────────────────────────────────────────────────


def test_action_model_emits_and_sends(issue_labeled_event):
    calls = {}
    emitted: list[tuple[str, dict]] = []
    sent: list[dict] = []

    def fake_complete(prompt, *, model, max_tokens):
        calls["prompt"] = prompt
        calls["model"] = model
        calls["max_tokens"] = max_tokens
        return "urgent"

    def fake_send(**kw):
        sent.append(kw)

        class _R:
            ids = ("m1",)

        return _R()

    ctx = ActionContext(
        send_message=fake_send,
        emit_event=lambda t, d: emitted.append((t, d)),
        gh_binary="gh",
        workspace_path=None,
        event=issue_labeled_event,
        model_complete=fake_complete,
    )
    summary = dispatch(
        {"model": {"prompt": "is it urgent?", "model": "local-fast",
                   "max_tokens": 32, "emit": "loop.model.result", "to": "triager"}},
        ctx,
    )
    assert summary["kind"] == "model"
    assert summary["model"] == "local-fast"
    assert summary["emitted"] == "loop.model.result"
    assert summary["sent_to"] == "triager"
    assert calls["max_tokens"] == 32
    assert emitted[0][0] == "loop.model.result"
    assert emitted[0][1]["text"] == "urgent"
    assert sent[0]["to"] == "triager"
    assert sent[0]["body"] == "urgent"


def test_action_model_include_event_appends_json(issue_labeled_event):
    captured = {}

    def fake_complete(prompt, *, model, max_tokens):
        captured["prompt"] = prompt
        return "ok"

    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event, model_complete=fake_complete,
    )
    dispatch({"model": {"prompt": "summarize", "include_event": True}}, ctx)
    assert "Event:" in captured["prompt"]
    # The event JSON is appended, so a known field shows up in the prompt.
    assert "action" in captured["prompt"]


def test_action_model_requires_prompt(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event, model_complete=lambda *a, **k: "x",
    )
    with pytest.raises(ActionError, match="prompt"):
        dispatch({"model": {}}, ctx)


def test_action_model_unavailable_without_gateway(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event,  # model_complete defaults to None
    )
    with pytest.raises(ActionError, match="model gateway not wired"):
        dispatch({"model": {"prompt": "hi"}}, ctx)


def test_action_model_completion_error_is_wrapped(issue_labeled_event):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event, model_complete=boom,
    )
    with pytest.raises(ActionError, match="model completion failed"):
        dispatch({"model": {"prompt": "hi"}}, ctx)


def test_action_model_read_files_appends_workspace_content(tmp_path, issue_labeled_event):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "inbox").mkdir()
    (ws / "inbox" / "today.md").write_text("Pay rent Friday\n")
    captured = {}

    def fake_complete(prompt, *, model, max_tokens):
        captured["prompt"] = prompt
        return "ok"

    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh",
        workspace_path=ws, event=issue_labeled_event, model_complete=fake_complete,
    )
    dispatch({"model": {"prompt": "summarize", "read_files": ["inbox/today.md"]}}, ctx)
    assert "--- workspace files ---" in captured["prompt"]
    assert "Pay rent Friday" in captured["prompt"]
    assert "## inbox/today.md" in captured["prompt"]


def test_action_model_read_files_rejects_path_escape(tmp_path, issue_labeled_event):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh",
        workspace_path=ws, event=issue_labeled_event,
        model_complete=lambda *a, **k: "x",
    )
    with pytest.raises(ActionError, match="invalid path"):
        dispatch({"model": {"prompt": "hi", "read_files": ["../secret.txt"]}}, ctx)


def test_action_model_empty_text_with_to_raises(issue_labeled_event):
    sent = []

    ctx = ActionContext(
        send_message=lambda **kw: sent.append(kw),
        emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event, model_complete=lambda *a, **k: "",
    )
    with pytest.raises(ActionError, match="empty text"):
        dispatch({"model": {"prompt": "hi", "to": "agent-a"}}, ctx)
    assert not sent


# ── Actions: code (inline) ─────────────────────────────────────────


def test_action_code_runs_python_with_event_on_stdin(issue_labeled_event):
    emitted: list[tuple[str, dict]] = []
    ctx = ActionContext(
        send_message=None,
        emit_event=lambda t, d: emitted.append((t, d)),
        gh_binary="gh",
        workspace_path=None,
        event=issue_labeled_event,
    )
    body = (
        "import json, sys\n"
        "ev = json.load(sys.stdin)\n"
        "print('action=' + str(ev.get('action')))\n"
    )
    summary = dispatch(
        {"code": {"lang": "python", "body": body, "emit": "loop.code.result"}},
        ctx,
    )
    assert summary["kind"] == "code"
    assert summary["returncode"] == 0
    assert summary["emitted"] == "loop.code.result"
    # stdout was emitted back onto the bus and reflects the event.
    assert emitted[0][0] == "loop.code.result"
    assert "action=" in emitted[0][1]["stdout"]


def test_action_code_sh(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event,
    )
    summary = dispatch({"code": {"lang": "sh", "body": "echo hi"}}, ctx)
    assert summary["kind"] == "code"
    assert summary["returncode"] == 0


def test_action_code_requires_body(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event,
    )
    with pytest.raises(ActionError, match="body"):
        dispatch({"code": {"lang": "python"}}, ctx)


def test_action_code_rejects_unknown_lang(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event,
    )
    with pytest.raises(ActionError, match="lang"):
        dispatch({"code": {"lang": "ruby", "body": "puts 1"}}, ctx)


def test_action_code_nonzero_returncode_surfaced(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event,
    )
    summary = dispatch({"code": {"lang": "sh", "body": "exit 3"}}, ctx)
    assert summary["returncode"] == 3


# ── Unknown action ────────────────────────────────────────────────


def test_dispatch_unknown_action_raises(issue_labeled_event):
    ctx = ActionContext(
        send_message=None, emit_event=None, gh_binary="gh", workspace_path=None,
        event=issue_labeled_event,
    )
    with pytest.raises(ActionError, match="unknown action"):
        dispatch({"NOT_AN_ACTION": {}}, ctx)


# ── Cursor persistence ────────────────────────────────────────────


def test_cursor_round_trip(tmp_path):
    path = tmp_path / "cursor.json"
    save_cursor(path, Cursor(last_event_id="9001", last_poll_ts="t", last_error=None))
    loaded = load_cursor(path)
    assert loaded.last_event_id == "9001"
    assert loaded.last_poll_ts == "t"


def test_cursor_missing_returns_empty(tmp_path):
    assert load_cursor(tmp_path / "missing.json").last_event_id is None


# ── Poller: dedup + restart ───────────────────────────────────────


def test_poller_first_tick_bookmarks_without_firing(monkeypatch, tmp_path, issue_labeled_event):
    """On first run with no cursor we must NOT replay 90 days of
    history through the action loop. The first tick records the
    latest event id AND DOES fire no rules — so the cursor advances
    to 9001 (the fetched event's id) and no `should-not-fire` event
    appears on the bus."""
    ws = "demo"
    cfg_home = tmp_path / "cfg"
    (cfg_home / "workspaces" / ws / "runtime" / "github").mkdir(parents=True)
    (cfg_home / "workspaces" / ws).mkdir(exist_ok=True)
    (cfg_home / "workspaces" / ws / "github.yaml").write_text(
        """
repo: relaydeck/relaydeck
rules:
  - name: bug
    when: { event: IssuesEvent, label: bug }
    do:
      - bus.emit: { type: should-not-fire, data: {} }
"""
    )

    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[issue_labeled_event], error=None),
    )

    emitted = []

    class _Worker:
        def log(self, *a, **kw):
            pass

    p = GithubPoller(
        workspace=ws,
        config_home=cfg_home,
        workspace_path=None,
        bus=None,
        send_message=None,
        emit_event=lambda t, d: emitted.append((t, d)),
        gh_binary="gh",
        default_interval_s=30.0,
    )
    p._tick(_Worker())
    # No rules should have fired on first poll.
    assert not any(t == "should-not-fire" for t, _ in emitted)
    # But the cursor MUST have advanced to the latest fetched event id —
    # otherwise we'd stay in bootstrap mode forever.
    cursor = load_cursor(cursor_path(cfg_home, ws))
    assert cursor.last_event_id == "9001"
    assert cursor.last_error is None


def test_poller_subsequent_tick_fires_new_events(
    monkeypatch, tmp_path, issue_labeled_event
):
    """With a cursor already in place, a fresh event with a higher id
    fires the matching rule."""
    ws = "demo"
    cfg_home = tmp_path / "cfg"
    (cfg_home / "workspaces" / ws / "runtime" / "github").mkdir(parents=True)
    (cfg_home / "workspaces" / ws / "github.yaml").write_text(
        """
repo: relaydeck/relaydeck
rules:
  - name: bug
    when: { event: IssuesEvent, label: bug }
    do:
      - bus.emit: { type: bug-seen, data: { issue_id: "{{ issue.number }}" } }
"""
    )
    save_cursor(cursor_path(cfg_home, ws), Cursor(last_event_id="9000"))

    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[issue_labeled_event], error=None),
    )

    emitted = []

    class _Worker:
        def log(self, *a, **kw):
            pass

    p = GithubPoller(
        workspace=ws,
        config_home=cfg_home,
        workspace_path=tmp_path,
        bus=None,
        send_message=None,
        emit_event=lambda t, d: emitted.append((t, d)),
        gh_binary="gh",
        default_interval_s=30.0,
    )
    p._tick(_Worker())

    types = [t for t, _ in emitted]
    assert "bug-seen" in types
    bug_seen = next(d for t, d in emitted if t == "bug-seen")
    assert bug_seen["issue_id"] == "42"
    # cursor advanced
    assert load_cursor(cursor_path(cfg_home, ws)).last_event_id == "9001"


def test_poller_records_poll_error_without_masking_as_empty(
    monkeypatch, tmp_path, issue_labeled_event
):
    """A broken auth / network blip / non-zero gh exit must NOT look
    like a successful empty poll. The cursor's last_error captures
    the failure, last_event_id is preserved, and the next `relaydeck
    github status` call surfaces it."""
    ws = "demo"
    cfg_home = tmp_path / "cfg"
    (cfg_home / "workspaces" / ws / "runtime" / "github").mkdir(parents=True)
    (cfg_home / "workspaces" / ws / "github.yaml").write_text(
        "repo: relaydeck/relaydeck\nrules: []\n"
    )
    save_cursor(cursor_path(cfg_home, ws), Cursor(last_event_id="9000"))

    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[], error="gh api failed (rc=4): bad credentials"),
    )

    class _Worker:
        warnings: list[str] = []

        def log(self, msg, level="info"):
            if level == "warn":
                _Worker.warnings.append(msg)

    p = GithubPoller(
        workspace=ws,
        config_home=cfg_home,
        workspace_path=tmp_path,
        bus=None,
        send_message=None,
        emit_event=lambda *a, **kw: None,
        gh_binary="gh",
        default_interval_s=30.0,
    )
    p._tick(_Worker())
    cursor = load_cursor(cursor_path(cfg_home, ws))
    assert cursor.last_event_id == "9000"  # preserved, not wiped
    assert "bad credentials" in (cursor.last_error or "")


def test_poller_clears_last_error_on_successful_empty_poll(
    monkeypatch, tmp_path
):
    """After a failure recovers, the next successful poll (even with
    zero new events) clears last_error so `relaydeck github status` stops
    showing a stale alarm."""
    ws = "demo"
    cfg_home = tmp_path / "cfg"
    (cfg_home / "workspaces" / ws / "runtime" / "github").mkdir(parents=True)
    (cfg_home / "workspaces" / ws / "github.yaml").write_text(
        "repo: relaydeck/relaydeck\nrules: []\n"
    )
    save_cursor(
        cursor_path(cfg_home, ws),
        Cursor(last_event_id="9000", last_error="previous failure"),
    )

    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[], error=None),
    )

    class _Worker:
        def log(self, *a, **kw):
            pass

    p = GithubPoller(
        workspace=ws,
        config_home=cfg_home,
        workspace_path=tmp_path,
        bus=None,
        send_message=None,
        emit_event=lambda *a, **kw: None,
        gh_binary="gh",
        default_interval_s=30.0,
    )
    p._tick(_Worker())
    cursor = load_cursor(cursor_path(cfg_home, ws))
    assert cursor.last_error is None
    assert cursor.last_event_id == "9000"


def test_poller_skips_when_no_config(monkeypatch, tmp_path):
    """A workspace with `github` in agent.toml but no github.yaml
    should not crash — the poller no-ops until config appears."""
    ws = "demo"
    cfg_home = tmp_path / "cfg"
    (cfg_home / "workspaces" / ws).mkdir(parents=True)

    monkeypatch.setattr(
        poller, "fetch_events", lambda *a, **kw: pytest.fail("fetch should not run")
    )

    class _Worker:
        def log(self, *a, **kw):
            pass

    p = GithubPoller(
        workspace=ws,
        config_home=cfg_home,
        workspace_path=tmp_path,
        bus=None,
        send_message=None,
        emit_event=lambda *a, **kw: None,
        gh_binary="gh",
        default_interval_s=30.0,
    )
    p._tick(_Worker())  # must not raise


# ── Plugin lifecycle: workspace.added / workspace.removed ─────────


def test_workspace_github_active_with_yaml(tmp_path):
    ws_dir = tmp_path / "workspaces" / "demo"
    ws_dir.mkdir(parents=True)
    (ws_dir / "github.yaml").write_text("repo: relaydeck/relaydeck\nrules: []\n")
    assert _workspace_has_github(tmp_path, "demo") is True


def test_workspace_without_github_yaml_returns_false(tmp_path):
    ws_dir = tmp_path / "workspaces" / "demo"
    ws_dir.mkdir(parents=True)
    (ws_dir / "agent.toml").write_text("[workspace]\nplugins = ['messaging']\n")
    assert _workspace_has_github(tmp_path, "demo") is False


def test_plugin_lifecycle_starts_and_stops_poller_with_workspace(monkeypatch, tmp_path):
    """workspace.added → start poller when github.yaml exists; workspace.removed → stop."""
    config_home = tmp_path / "cfg"
    (config_home / "workspaces" / "demo").mkdir(parents=True)
    (config_home / "workspaces" / "demo" / "github.yaml").write_text(
        "repo: relaydeck/relaydeck\nrules: []\n"
    )

    host = MockHost(
        name="github",
        workspace="demo",
        workspace_path=tmp_path / "repo",
        config_home=config_home,
        declared_capabilities={
            "events.subscribe", "events.emit", "workers.spawn",
            "agents.list", "agents.send", "cli.register", "api.register",
        },
    )

    # Patch fetch_events so even if the worker ticks during the test,
    # we don't hit the real gh binary.
    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[], error=None),
    )

    plugin = GithubPlugin()
    plugin.on_load(host)

    # No workspaces yet — drive workspace.added directly.
    from relaydeck.plugin import Event
    host.events._bus.emit(Event(
        type="workspace.added",
        data={"name": "demo", "path": str(tmp_path / "repo")},
        source_plugin="test",
    ))
    assert "demo" in plugin._pollers

    host.events._bus.emit(Event(
        type="workspace.removed",
        data={"name": "demo"},
        source_plugin="test",
    ))
    assert "demo" not in plugin._pollers

    plugin.on_unload()


def test_file_changed_reconciles_github_poller(monkeypatch, tmp_path):
    """workspace.file.changed on github.yaml stop+starts the poller."""
    config_home = tmp_path / "cfg"
    repo = tmp_path / "repo"
    repo.mkdir()
    ws_dir = config_home / "workspaces" / "demo"
    ws_dir.mkdir(parents=True)
    yaml_path = ws_dir / "github.yaml"
    yaml_path.write_text("repo: relaydeck/relaydeck\nrules: []\n")
    (config_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{repo}"\n'
    )

    host = MockHost(
        name="github",
        workspace="demo",
        workspace_path=repo,
        config_home=config_home,
        declared_capabilities={
            "events.subscribe", "events.emit", "workers.spawn",
            "agents.list", "agents.send", "cli.register", "api.register",
        },
    )
    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[], error=None),
    )

    plugin = GithubPlugin()
    plugin.on_load(host)
    assert "demo" in plugin._pollers
    first = plugin._pollers["demo"]

    from relaydeck.plugin import Event
    host.events._bus.emit(Event(
        type="workspace.file.changed",
        data={"path": str(yaml_path), "relative_path": "github.yaml", "root": str(repo)},
        source_plugin="test",
    ))

    assert "demo" in plugin._pollers
    assert plugin._pollers["demo"] is not first
    plugin.on_unload()


def test_file_changed_reconciles_via_repo_root_fallback(monkeypatch, tmp_path):
    """When path has no workspaces/ segment, resolve workspace from event root."""
    config_home = tmp_path / "cfg"
    repo = tmp_path / "repo"
    repo.mkdir()
    ws_dir = config_home / "workspaces" / "demo"
    ws_dir.mkdir(parents=True)
    (ws_dir / "github.yaml").write_text("repo: relaydeck/relaydeck\nrules: []\n")
    (config_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{repo}"\n'
    )
    repo_yaml = repo / "github.yaml"
    repo_yaml.write_text("# repo-side path watched by file-watcher\n")

    host = MockHost(
        name="github",
        workspace="demo",
        workspace_path=repo,
        config_home=config_home,
        declared_capabilities={
            "events.subscribe", "events.emit", "workers.spawn",
            "agents.list", "agents.send", "cli.register", "api.register",
        },
    )
    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[], error=None),
    )

    plugin = GithubPlugin()
    plugin.on_load(host)
    assert "demo" in plugin._pollers
    first = plugin._pollers["demo"]

    from relaydeck.plugin import Event
    host.events._bus.emit(Event(
        type="workspace.file.changed",
        data={
            "path": str(repo_yaml),
            "relative_path": "github.yaml",
            "root": str(repo),
        },
        source_plugin="test",
    ))

    assert "demo" in plugin._pollers
    assert plugin._pollers["demo"] is not first
    plugin.on_unload()


def test_file_changed_stops_poller_when_github_yaml_removed(monkeypatch, tmp_path):
    """Deleting github.yaml and firing reconcile stops the poller."""
    config_home = tmp_path / "cfg"
    repo = tmp_path / "repo"
    repo.mkdir()
    ws_dir = config_home / "workspaces" / "demo"
    ws_dir.mkdir(parents=True)
    yaml_path = ws_dir / "github.yaml"
    yaml_path.write_text("repo: relaydeck/relaydeck\nrules: []\n")
    (config_home / "config.toml").write_text(
        f'[[workspace]]\nname = "demo"\npath = "{repo}"\n'
    )

    host = MockHost(
        name="github",
        workspace="demo",
        workspace_path=repo,
        config_home=config_home,
        declared_capabilities={
            "events.subscribe", "events.emit", "workers.spawn",
            "agents.list", "agents.send", "cli.register", "api.register",
        },
    )
    monkeypatch.setattr(
        poller, "fetch_events",
        lambda *a, **kw: PollResult(events=[], error=None),
    )

    plugin = GithubPlugin()
    plugin.on_load(host)
    assert "demo" in plugin._pollers

    yaml_path.unlink()
    from relaydeck.plugin import Event
    host.events._bus.emit(Event(
        type="workspace.file.changed",
        data={"path": str(yaml_path), "relative_path": "github.yaml", "root": str(repo)},
        source_plugin="test",
    ))

    assert "demo" not in plugin._pollers
    plugin.on_unload()
