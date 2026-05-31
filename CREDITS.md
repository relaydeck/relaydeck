# Credits & licenses

relaydeck is [MIT-licensed](LICENSE). It stands on a lot of excellent open
source, and it integrates with tools and services owned by other people. This
page is our thank-you list and our attribution record. The same information is
surfaced in the dashboard under **Settings → Credits & Licenses**.

If you think something here is mis-attributed or missing, please open an issue —
we want to get this right.

## Trademarks & brand marks

All product names, logos, brand marks, and trademarks are the property of their
respective owners. Their appearance in relaydeck — in the harness catalog, the
provider list, or as icons in the dashboard — denotes **integration or
interoperability only**. It does not imply any affiliation with, sponsorship by,
or endorsement from those owners. relaydeck is an independent project.

Brand/provider icons are rendered from [Simple Icons](https://simpleicons.org)
(released into the public domain under CC0 1.0); the marks themselves remain the
property of the brands they represent.

## Fonts

Vendored locally (the dashboard renders fully offline) under
`relaydeck/web/static/fonts/` — see that directory's `LICENSE`.

| Font | By | License |
|------|----|---------|
| IBM Plex Sans, IBM Plex Mono | © IBM Corp. — <https://github.com/IBM/plex> | SIL Open Font License 1.1 |
| JetBrains Mono | The JetBrains Mono Project Authors — <https://github.com/JetBrains/JetBrainsMono> | SIL Open Font License 1.1 |

## Frontend libraries (vendored)

Vendored under `relaydeck/web/static/vendor/` so the daemon serves the dashboard
without any CDN. License texts ship alongside the files.

| Library | Version | By | License |
|---------|---------|----|---------|
| [Lit](https://lit.dev) | 3.3.3 | Google / the Lit authors | BSD-3-Clause |
| [xterm.js](https://xtermjs.org) (+ `addon-fit`, `addon-webgl`) | 5.5.0 | The xterm.js authors | MIT |
| [Heroicons](https://heroicons.com) | 2.1.5 | Tailwind Labs | MIT |
| [Simple Icons](https://simpleicons.org) | — | The Simple Icons contributors | CC0 1.0 |

Icons are vendored at build time by `scripts/gen_web_icons.py` into
`relaydeck/web/static/icon_data.js` (no runtime CDN calls).

## Python dependencies

The runtime and bundled plugins build on, among others:

| Package | License |
|---------|---------|
| FastAPI, Uvicorn, Starlette | BSD-3-Clause |
| Click | BSD-3-Clause |
| Rich, Textual | MIT |
| Pyte | LGPL-2.1 |
| Pydantic | MIT |
| PyYAML, tomli, tomli-w | MIT |
| cryptography | Apache-2.0 / BSD-3-Clause |
| openai (client) | Apache-2.0 |
| websockets | BSD-3-Clause |
| NumPy | BSD-3-Clause |
| sentence-transformers | Apache-2.0 |
| python-multipart | Apache-2.0 |
| python-telegram-bot | LGPL-3.0 |
| croniter | MIT |

See each project's distribution for the authoritative license text. The full,
resolved dependency tree (with versions) lives in `uv.lock`.

## Harnesses, agents & the ecosystem

relaydeck is **harness-native**: it wraps real CLI coding agents rather than
shipping its own model runtime. Sincere thanks to the projects that make that
possible, and to the wider community whose ideas shaped how we think about
fleets of agents.

- **pi** — the [pi coding agent](https://github.com/earendil-works/pi) by Mario
  Zechner is relaydeck's **reference harness** and powers relaydeck-native.
  Thank you — relaydeck would not be what it is without it. Our contribution
  process (the issue/PR gate and `lgtm`/`lgtmi` flow) is adapted, with thanks,
  from pi's.
- **Claude Code** (Anthropic), **Codex CLI** (OpenAI), **Cursor CLI** (Cursor),
  **OpenCode**, and **Antigravity** (Google) — first-class harnesses that
  relaydeck runs and observes. Thanks to each team.

We were also inspired, in part, by ideas from across the open agent ecosystem —
including Nous Research's
[**Hermes Agent**](https://github.com/NousResearch/hermes-agent) and
**OpenClaw**, which relaydeck can observe read-only alongside the fleet it
manages. Thanks to both teams.

## Providers & services

relaydeck integrates with model providers and services including Anthropic,
OpenAI, OpenRouter, Ollama, Google (Gemini), DeepSeek, Mistral AI, Hugging Face,
Meta (Llama), Telegram (Bot API), and GitHub. All trademarks belong to their
respective owners; see the trademark note above.
