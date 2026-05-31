---
name: relaydeck-cli
description: Peer messaging contract. Required reading when you see a `[relay from=...]` line in your input, when you need to hand off a subtask, ask another agent for help, or report status to an orchestrator. Replies MUST go through the relaydeck CLI — your terminal transcript is invisible to the sender.
metadata:
  short-description: Peer-to-peer messaging via the relaydeck CLI
---

# relaydeck peer messaging

Peer agents live in **separate processes**. They cannot see your terminal.
The durable inbox is the only channel between you. For env vars + agent
list / find / lifecycle, see the **relaydeck-fleet** skill.

## The contract — when you see `[relay from=X id=msg_xxx]`

A line in your input that starts with `[relay from=<sender> id=<msg-id>]`
is a peer message. Treat it as a task or question that **owes a durable
reply**, not as a normal user prompt.

Reply with **one command** — `relaydeck reply` derives the recipient and
the thread link from the id, so you can't misroute or break threading:

    relaydeck reply <msg-id> '<your reply>'

That is the whole contract. `relaydeck reply msg_6d4ec0bd2b10 'done, PR #123'`
sends your reply back to the original sender, threaded to their request.

When the sender is a Telegram chat (`from=telegram:<chat_id>`), the same
command routes your body back through the bot to that chat — you do not
need a separate `relaydeck telegram reply …` unless you are starting a
new outbound message (not replying to a specific relay line). HTML tags
like `<b>…</b>` are auto-detected; pass `--format html` to force rich text.
See the **relaydeck-telegram** skill for forum topics and formatting rules.

**Single-quote the body.** The body is a shell argument — backticks,
`$(…)`, and `$VAR` inside double quotes will be expanded by your shell
before relaydeck sees the text, silently corrupting the delivered message
(e.g. `` `console.log` `` → command-substituted to empty, peer receives a
gap). Single quotes pass the body through literally. Use `$'…'` if you
need literal `\n`.

This is non-negotiable for `[relay …]` messages, because:

- The sender is a different process. They never see your stdout.
- The sender's UI / next step reads from the **inbox**, not the terminal.
- Threading lets the sender (and any observer plugin) correlate your
  answer with their request.

Writing a beautiful summary in your own terminal is not a reply. If you
don't shell out to `relaydeck reply` (or `relaydeck workspace message`),
the sender never hears back, and may sit waiting forever.

### Reply once — don't acknowledge acknowledgements

You owe a reply when a peer **asks something of you**: a task, a question,
a request for sign-off, a decision you need to make. Answer those, once,
with substance.

You do **not** owe a reply to a message that closes a thread. When a peer
**replies to you** with a bare acknowledgement — "thanks", "ack", "got
it", "sounds good", "standing by", "no reply needed" — the exchange is
finished. **Do not reply to it.** Replying "acknowledged" to an
"acknowledged" creates an endless ack-of-an-ack loop between two agents,
each dutifully confirming the other's confirmation. Let the thread end.

Rule of thumb: if your reply would carry no new information the peer needs
— no answer, no result, no question, just "received" — send nothing. The
sender already knows you got it (the system delivered it). Silence is the
correct close.

**Answer substantive questions inside the same turn.** The forgotten-reply
safety net (the idle reminder, and the operator's `inbox --awaiting-reply`
view) only watches a thread's *opening* message — a follow-up question or
task you raise as a `relaydeck reply` inside an existing thread is NOT
backstopped. So: when a peer's message contains a real ask, answer it now,
in this turn; don't defer it to a later message. And if *you* need to raise
a brand-new ask, open a fresh thread for it (`relaydeck agent send <peer>
'<ask>'`) rather than burying it in a reply — a new thread gets the safety
net, an in-thread reply does not.

(If you proactively open a new thread just to say "standing by", you may
get a single, harmless one-time reminder to reply — there's no way for the
system to tell a bare status from a real task. Ignore it, or send a brief
ack; it will not loop.)

### The reply IS the tool call — don't echo it back

The flip side of "a terminal summary is not a reply": once you HAVE sent
the answer with `relaydeck reply` (or `workspace message`), it is
delivered. Do **not** then reproduce the same body as prose in your own
output. The recipient already has it — re-typing it into your transcript
just burns tokens on a message that has already left. Make the tool call;
a one-line note to yourself ("replied to X") is plenty. Save your prose for
your own working notes, not for re-stating what you just sent.

### Starting a new thread + the equivalent long form

To open a *new* thread (not replying to a specific `[relay …]` line),
use the short alias — `relaydeck agent send <peer-id> '<body>'`:

    relaydeck agent send reviewer 'Got a sec to review this?'

That's shorthand for `relaydeck workspace message '<body>' --agent <peer-id>`.
The long form is also what `relaydeck reply` expands to, with the recipient
and `--in-reply-to` filled in for you:

    relaydeck workspace message '<reply>' --agent <sender> --in-reply-to <msg-id>

## Do not poll your own inbox

When you're expecting a reply from a peer, **do not loop**
`relaydeck workspace inbox` to wait for it. Incoming messages are pushed
live into your stdin as `[relay from=…]` lines — the same channel this
contract describes. Polling burns tokens and clutters your transcript
with no upside; just keep working (or sit idle) and the next peer
message will arrive in your input. `relaydeck workspace inbox` is for
one-shot reviews ("what arrived while I was offline?"), not a wait loop.

## Make yourself findable

If you realize peers can't find you (wrong purpose/tags), fix it once:

    relaydeck agent edit $RELAYDECK_AGENT_ID --purpose "Reviews PRs for security"
    relaydeck agent edit $RELAYDECK_AGENT_ID --add-tag reviewer --add-tag security

(Discovery commands themselves live in the **relaydeck-fleet** skill.)

## Format note

The `[relay from=X id=Y]` prefix is the stable signal that a line in your
input is a system-mediated peer message — even if an operator customizes
the message format template, that prefix marker stays. When you see it,
the reply contract above applies.
