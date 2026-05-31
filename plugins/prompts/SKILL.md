---
name: relaydeck-prompts
description: Ask a human for a decision with tap-able buttons instead of waiting for a typed reply.
---

# Asking a human for a decision

When you need a human to approve, choose, or confirm something, don't print
"reply YES to continue" and wait for free text. Raise an **interactive
prompt**: a question plus a set of buttons. The human taps one (in Telegram,
the dashboard, or wherever they're reached) and you get the structured
decision back — faster, unambiguous, and no typing.

## Block until they answer (recommended)

```
relaydeck prompts ask "Deploy v2.3 to production?" \
  -c approve:Approve:primary \
  -c reject:Reject:danger \
  -c hold:"Hold for review" \
  --wait
```

`--wait` blocks and prints the decision as JSON when a human picks:

```json
{"id": "prm_ab12…", "state": "answered", "choice": "approve", "answered_by": "telegram:ops @alice"}
```

Read `choice` (the stable id you defined) and branch on it. `state` is
`answered`, or `expired` if you passed `--expires` and nobody responded.

> Expiry is checked by a background sweep (every ~30s by default), so a
> prompt may live a little past its `--expires` deadline before flipping to
> `expired`. Don't rely on second-precise TTLs.

## Fire-and-continue (async)

Omit `--wait` to send the prompt and keep working. When a human answers, the
decision is delivered into your session as a relay line:

```
[relay from=prompt id=msg_…] ✅ Prompt prm_ab12… answered: Approve (choice=approve) by telegram:ops @alice
```

## Choices

Each `-c` / `--choice` is `id[:label[:style]]`:

- **id** — the stable token you branch on (`approve`). Required.
- **label** — what the human sees (`Approve & deploy`). Defaults to the id.
- **style** — `default` | `primary` | `danger` | `success`. Cosmetic hint
  (button color where the platform supports it).

## Where it goes — `--to` (provider-agnostic)

By default a prompt goes to the dashboard (`web`). Add `--to` to fan it out
to messaging platforms; the first human to answer anywhere wins, and the
others' buttons are retracted. Addresses are
`channel[:connection[:target[:thread]]]`:

```
--to web --to telegram:ops:-1001234567890 --to telegram:alerts:123456789
```

You don't need to know whether a channel supports buttons: if it does, you
get native buttons; if not (e.g. SMS), the platform shows a numbered list and
the human replies with a number or the choice word — same result either way.

## Other commands

- `relaydeck prompts list` — open prompts (add `--state all`).
- `relaydeck prompts show <id>` — full prompt JSON incl. delivery status.
- `relaydeck prompts answer <id> <choice>` — answer on a human's behalf.
