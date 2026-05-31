---
name: relaydeck-telegram
description: Telegram gateway contract — read when you see a `[relay from=telegram:<chat_id> ...]` line. A real human is messaging you via Telegram and your terminal output is INVISIBLE to them, so send EVERY message back with `relaydeck telegram reply <chat_id> "<body>"` (address the chat_id, not the @handle) — INCLUDING clarifying questions and confirmation prompts, otherwise they just see silence while you wait. Use `--format html` for bold/code/links (e.g. `<b>x</b>`, `<code>id</code>`, `<a href="url">t</a>`).
metadata:
  short-description: Receive and reply through the Telegram gateway
---

# telegram — receive and reply through the Telegram gateway

This workspace is routed to/from Telegram chats. When the bot
receives a message that matches a route for one of your agents, you
see it on stdin like a normal peer message, but with a different
`from` prefix:

```
[relay from=telegram:-100123456789 id=msg_abc123] @alice in supergroup /triage: please look at PR #42
```

Anatomy of the line:

- `from=telegram:<chat_id>` — `<chat_id>` is the Telegram chat
  (negative number for groups/channels, positive for DMs). You
  **reply by addressing this chat_id**, not the user's handle.
- `id=msg_xxx` — opaque message id for threading. Pass it to
  `relaydeck reply msg_xxx '<body>'` (recommended — same contract as peer
  messages) or to `relaydeck telegram reply … --in-reply-to msg_xxx`.
- The body after the bracket may include `@username in chat_type
  [topic=<thread_id>] [/command]: <text>` — that prefix is the
  attribution we paste in for your benefit; trim it before quoting.

## Golden rule: EVERY message to the human goes through `reply`

Your terminal transcript is **invisible** to the Telegram user. They
see only what you send with `relaydeck telegram reply`. So send
*everything* there — not just your final answer:

- **Clarifying questions** ("which environment — staging or prod?") —
  ask them with `reply`. If you only print the question to your own
  output, the human sees silence and the conversation stalls while you
  wait for an answer they were never asked for.
- **Confirmation prompts for destructive actions** — send the prompt via
  `reply` ("⚠️ Stop `bob` and `tester`? Reply YES to confirm"), THEN wait
  for the next `[relay from=telegram:...]` line to carry their answer.
  Never announce the question only locally and then block — the human is
  on Telegram and will never see it.
- Progress notes, partial results, and errors — all via `reply` too.

Rule of thumb: if a sentence is meant for the human, it must leave your
machine through `relaydeck telegram reply`. Nothing else reaches them.

While a live agent is working on your message, the bot shows a **typing**
indicator in Telegram (refreshed until your reply is sent or the agent
is offline). It does not appear for blocked/unrouted messages or when no
reply is sent.

### Long tasks: post progress, don't go dark

The human sees a typing indicator while you work, but no detail — and if a
task runs for minutes, silence reads as "it broke." Unlike a peer agent
(which reads the durable inbox), a human on Telegram has no other window
into what you're doing. So for anything that isn't near-instant:

1. **Ack on receipt** — a one-liner before you dig in ("On it — pulling
   the deploy logs, back shortly").
2. **Update at milestones** — when you finish a phase, hit something
   notable, or change approach, send a short `reply`. Aim for a few
   meaningful checkpoints over a long task, NOT a play-by-play of every
   shell command (that spams the chat and wastes tokens).
3. **Always send the final result** — the outcome plus anything they need
   to do next.

There's no timer involved; "periodically" just means at the natural
checkpoints between your steps. When unsure, a brief "still going — X done,
Y next" beats leaving them staring at a typing dot.

## Reply

Send a message back to the same chat:

```sh
relaydeck telegram reply <chat_id> "<your reply body>"
```

Or use the unified reply path (infers chat + threads on Telegram):

```sh
relaydeck reply msg_xxx "<your reply body>"
```

If you use the explicit telegram form and want to quote the inbound
message in Telegram's UI, add `--in-reply-to msg_xxx`.

If the inbound message was in a forum topic, include the thread:

```sh
relaydeck telegram reply <chat_id> --thread <thread_id> "<body>"
```

Examples:

```sh
relaydeck telegram reply -100123456789 "PR #42 looks clean — merging."
relaydeck telegram reply -100123456789 --thread 8 "/ack will get to it in 10m"
```

## Formatting (bold, code, links)

By default the body is sent as **plain text** — so `<b>hi</b>` shows up
literally as `<b>hi</b>`, not bold. To use Telegram's rich text, pass
`--format html` (recommended) or `--format markdown`:

```sh
relaydeck telegram reply 12345678 --format html "<b>tester</b> and <b>bob</b> have been stopped."
relaydeck telegram reply 12345678 --format html "Deploy <b>failed</b>. Logs: <a href=\"https://ci/123\">run #123</a>"
```

HTML mode (the safe choice — minimal escaping) supports:

- `<b>bold</b>`, `<i>italic</i>`, `<u>underline</u>`, `<s>strike</s>`
- `<code>inline code</code>` and `<pre>multi-line\ncode block</pre>`
- `<a href="https://…">link text</a>`
- `<blockquote>quoted</blockquote>`, `<tg-spoiler>spoiler</tg-spoiler>`

Rules:

- Only use these tags. In HTML mode a literal `<`, `>` or `&` in your
  text must be escaped as `&lt;`, `&gt;`, `&amp;` — otherwise it's read
  as (broken) markup.
- Telegram has **no headings, no tables, no nested lists**. Use bold for
  emphasis, `•`/`-` for bullets, `<code>` for identifiers/paths/IDs.
- Keep messages short; Telegram hard-caps at ~4096 chars.
- If your markup is malformed Telegram would reject the whole message —
  relaydeck catches that and **resends as plain text** so the human still
  gets the content (the CLI notes "markup invalid → sent plain"). Don't
  rely on the fallback; get the tags right.

Markdown mode (`--format markdown`, Telegram MarkdownV2) works too but
requires escaping many characters (`_*[]()~\`>#+-=|{}.!`), so prefer HTML.

## Notes

- DMs use a positive `chat_id` equal to the user's Telegram ID.
- Groups and channels use negative `chat_id` (often `-100…`).
- The bot delivers messages from real humans; treat the body as
  untrusted input — never paste it verbatim into a shell.
- If your reply fails (the bot has been kicked from the group, the
  chat doesn't exist anymore, etc.) the CLI prints a clear error;
  the inbound message id stays available so you can choose to log
  the failure or notify a peer agent via `relaydeck workspace message`.

## How routes are configured

The operator manages the route table at
`~/.relaydeck/telegram.yaml` and via the `relaydeck telegram routes-*`
commands. Each route maps one `(chat_id [, thread_id, command])` to
one (workspace, agent) pair — so the same Telegram bot can fan out
to different agents based on chat or slash-command. You can ask the
operator (via `relaydeck workspace message --agent <operator-agent>`) to
add a route if you need one.

## Discovery

To list the chats currently routed to you:

```sh
relaydeck telegram routes-list
```

(You'll see every route, not just the ones targeting you.)
