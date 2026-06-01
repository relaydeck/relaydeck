# playwright.md — dashboard browser-testing cheat sheet

How to drive + verify the relaydeck web dashboard (`localhost:8765`) with the
**Playwright MCP**. Keep this file updated: when you learn a new selector, a
verification trick, or a gotcha, add it here so the next session isn't
relearning it. Referenced from `AGENTS.md`.

## Screenshots — the key gotcha
- `browser_take_screenshot` **with** a `filename` saves to the MCP server's own
  filesystem (NOT readable from here) and returns only a link → you can't see it.
- `browser_take_screenshot` with **no `filename`** returns the PNG **inline** so
  it renders in the transcript. **Always omit `filename`** when you need to look.
- `type: "png"`. `fullPage: true` for the whole scroll height.

## Auth + serving
- Daemon must be up: `uv run relaydeck daemon start` (`relaydeck` isn't on PATH;
  always go through `uv`). Wait until `GET /api/agents` → 200.
- **Loopback auto-auth**: the dashboard bootstraps a token via
  `GET /api/auth/bootstrap` (returns `{token}` for loopback only). A fresh
  Playwright browser authenticates itself — no manual token paste needed.
- **Cache-bust**: `/static/*.{js,css}` are stamped `?v=<pid>`; **restart the
  daemon after editing JS/CSS** so the new pid busts the browser cache. `/` is
  served `no-store`. Verify served bytes with
  `curl -s "http://127.0.0.1:8765/static/<f>?v=$(cat ~/.relaydeck/daemon.pid)"`.
- **Update banner layout**: when `.banner-host` has content, `.app` must be
  `display:flex` (not a 3-row grid) and `.main` must stay tall (`flex:1`). If
  `.main` collapses to ~26px the workspace is broken — inject a fake banner via
  `browser_evaluate` and assert `document.querySelector('.main').getBoundingClientRect().height > 400`.
- **Agent detail Git tab**: `core:git` tile — `Status` / `Changes` sub-tabs;
  header chips (`worktree`/`main`, branch, `+N/-M`). API:
  `GET /api/workspaces/<ws>/git-detail`. Compact agent header: `.pane--agent`,
  `.stat-strip--compact`, `.subtabs--compact`.

## Verify objectively (no eyes needed)
- `browser_evaluate(() => {...})` — read computed styles / DOM state. Examples:
  - accent: `getComputedStyle(document.documentElement).getPropertyValue('--acc').trim()`
    → expect `#B7410E` (terracotta), NOT `#67e8f9` (old cyan).
  - `getComputedStyle(el).color / .backgroundColor / .fontFamily`.
  - `document.documentElement.getAttribute('data-accent')` → should be `null`
    (legacy accent system removed; theme engine owns accent).
- `browser_console_messages({level:'error'})` — JS errors. **The `favicon.ico`
  404 is benign noise — ignore it.** Anything else is real.
- `browser_snapshot` — accessibility tree (structure/labels, not visuals).

## Common selectors
- Header: `.hdr`, `.ws-pill` (workspace drawer trigger), `.plug-pill` (plugins
  popover), `.hdr [data-act="settings"]`, `[data-act="new-agent"]`,
  `[data-act="cmdk"]`, `[data-act="notifs"]`.
- Popovers/overlays: `.plug-pop` (ws/plugins drawer), `.cmdk` + `.cmdk-input`
  (command palette/search), `.kbd-cheatsheet` (`?`), `.settings-overlay`,
  `.drawer` (notifications), `.overlay-scrim`.
- Settings nav: `.settings-nav [data-s="<id>"]` — `general | shortcuts | plugins
  | integrations | auth | vault | about | danger`. Shortcuts toggles: `.sc-tog`.
  - **Appearance/theme lives under `general`** now (NOT a rail lens — the
    standalone Appearance lens was removed). Sub-tabs `.app-subtabs .seg-i[data-v=
    "theme"|"customize"]`; theme picker cards `.theme-card[data-theme=<name>]`;
    "Customize tokens" embeds the full `AppearanceLens.renderDetail`.
- **Spawn modal** (`new_agent.js`, `.na-modal`): numbered sections `01-05`;
  harness cards `.na-card`; plugins `.na-pl` + rec/all/none `.na-mini[data-pl]`;
  live preamble preview `[data-preamble]` (auto-updates from `preview-prompt`);
  CLI mirror `[data-cli-eq]`. Open via `[data-act="new-agent"]`.
  **relaydeck-native / pi missing:** card badge `.na-tag-warn` ("pi not installed");
  section warn `.na-warn` inside `[data-type-section]` when `cli_installed` is
  false; `[data-act="spawn"]` stays disabled until pi is on the daemon PATH
  (live probe — no restart). Agent detail: `[data-native-pi-banner]` on
  `type:relaydeck` agents (`.na-warn`, hidden when pi present). Status API:
  `GET /api/plugins/relaydeck-native/status` → `{pi_installed, install_hint, …}`.
  **Native pi PTY chrome:** relaydeck-native passes `--no-skills --no-themes
  --no-prompt-templates --no-extensions` + isolated `PI_CODING_AGENT_DIR`
  with `quietStartup`. Expect `relaydeck-native` in pyte screen; NOT `pi v0.`
  logo or `[Skills]` dump. Verify:
  `GET /api/agents/<relaydeck-id>/screen?cols=120&rows=40` or e2e
  `test_spawn_relaydeck_native_renders_and_runs`.
- **Dashboard widget grid (native operator):** layout persists in
  `preferences.yaml` (`appearance.dashboard`). `POST /api/dashboard/command`
  `{op:"get"}` returns it under `appearance.dashboard` (+ `themes` hint).
  Native Context tile (`/api/plugins/relaydeck-native/<id>/context`) injects
  `clock @ (x,y) WxH` lines under Capabilities. E2e:
  `test_dashboard_get_api_includes_widget_grid`,
  `test_native_context_tile_shows_dashboard_layout`. **Two subtabs named
  Context:** core heatmap (`core:context`) vs native injection
  (`relaydeck-native:context`, card title **Injected context**). Playwright:
  `open_relaydeck_native_context_tab(page)` — second visible Context tab, or
  `+N` overflow row with `.sub` = `relaydeck-native`.
- **Workspaces lens** plugin grid: `.ws-plug-grid` → `.ws-plug-card` (`.wpc-name`
  `.wpc-desc`) + shared toggle `.idn-toggle`/`.idn-knob` (now global in panels.css).
  Catalog from `/api/workspace-plugins` (NOT `/api/plugins` — that omits gates).
- Rail: `.rail-btn` (lens switch). Sidebar: `.side`, `.side-list`.
- Home: `.home`, `.wgt` (widget), `.wgt-head .ttl`. Agent detail stat strip:
  `.stat-strip` / `.stat-cell`.
- **Identity tab** (`tiles/identity.js`, 2-col `.idn`): SPAWN COMPOSITION
  `.idn-comp` (rows `.idn-cmp` → `.idn-cmp-label`/`.idn-cmp-tok`/`.idn-cmp-plug`,
  total `.idn-total`); system_prompt viewer `.idn-sp` (`.idn-sp-code` lines, edit
  `[data-act="sp-edit"]`); identity yaml `.idn-yaml` (`.idn-kv`, edit
  `[data-act="id-edit"]`); plugins `.idn-plugins` (`.idn-plug` + `.idn-toggle`
  `[data-toggle="<name>"]` → PATCH /api/workspaces/{ws}); peers `.idn-peer`
  `[data-peer="<id>"]`. Data: `GET /api/agents/{id}/prompt-composition` (real
  components + chars + ~chars/4 est tokens + peers). Switch tabs by clicking the
  button whose text is exactly "Identity".
- **Context tab** (`tiles/context.js`, `.ctx`): per-thread context usage —
  `.cs-row` (`.cs-label`, `.cs-cur` = current fill, `.cs-bar`). Data: `GET
  /api/agents/{id}/sessions` (current_context = latest turn's prompt_tokens;
  bars relative to busiest thread, no model limit assumed). NOT a heatmap.
- **Models lens** (`?lens=models`, `lenses/models.js` + `models.css`):
  sidebar tabs `.mdl-tab[data-tab="presets"|"providers"|"roles"]`; preset rows
  `.mdl-row--preset` (provider `.mdl-prov`, model leaf `.mdl-model`, stats
  `.mdl-row-foot`); detail resolve line `.mdl-resolve`; scrollable detail
  `.lens-body` (NOT `.dbody`); resolution pipeline
  `.mdl-pipe` → `.mdl-pipe-box` / `.mdl-pipe-arrow` (stacks vertically ≤960px,
  arrows hidden). Sub-filters `.mdl-subfilter button[data-f]`. Preset modal
  `.mp-form-modal`.
- **Usage heatmap** is a Home widget now (`.w-heatmap`, gallery key `heatmap`):
  `GET /api/usage-heatmap` (fleet) / `GET /api/agents/{id}/usage-heatmap`. Add via
  Home gallery or `window.__relaydeckHome._addWidget('heatmap')`.
- **Terminal — never touch**: `.xterm`, `.term-host` (xterm WebSocket PTY).
  Debug hooks (read-only, safe): `window.__relaydeckTerm` = the live TerminalTile
  (`.term.cols`/`.term.rows`, `._lastResize` = last "cols rows" sent, `.ws`,
  `.fit`). To see the width the **harness is actually drawing at** (vs. xterm),
  hit `/api/agents/<id>/screen?cols=400&rows=40` (pyte render, no wrap) and take
  `max(line length)`. If that ≠ xterm cols, the resize round-trip is broken.

## Deep links (jump straight to a view for testing)
`?workspace=<name>` (empty = all), `?lens=<id>` (agents|messages|models|workers|
workspaces|appearance|skills|github|telegram|external), `?agent=<id>`,
`?section=<id>` (plugin-lens sub-nav).

## Telegram lens (`?lens=telegram&section=connections`)
- Rail: `.rail-btn[title="Telegram"]` (namespaced id `telegram:telegram`; shorthand
  `?lens=telegram` resolves). Sidebar sections: `.pl-nav-item` (Connections,
  Conversations, Activity, Routes, Allowlists, Send, Settings).
- Connections card: `[data-act="add-conn"]` opens `.tg-row-form` with
  `[data-f="id"|"name"|"token"]`; submit `[data-act="save"]` inside the form
  (POST `/api/plugins/telegram/connections` — verifies via getMe, vaults token).
- Connection rows: `.tg-route` (`.acc` = connected, `token set` in sub-line).
- **Add route form** (`.tg-route-form`): chat `<select data-f="chat_pick">` groups
  Conversations / Allowlisted users / groups / Recent activity; `workspace` +
  `agent` are `<select>`s (agents filter by workspace); `thread_id` + `command`
  are selects with custom fallback inputs.
- Header actions: `[data-act="verify"]`, `[data-act="restart"]`. Setup card (no
  token yet): `.tg-setup [data-f="token"]` + `[data-act="save"]`.
- Live add-bot e2e: `tests/e2e/test_web_telegram_e2e.py` — needs
  `RELAYDECK_E2E_LIVE_DAEMON=1` (required when daemon already polls the bot).
  PTB (python-telegram-bot) ships with relaydeck core, so no extra install. Full wiring:
  `test_telegram_full_live_wiring` — real getUpdates inbound + real sendMessage
  outbound; operator sends staged DMs when prompted. If the test looks stuck,
  your :8765 daemon is probably stealing getUpdates — use LIVE_DAEMON=1.

## Workers lens (`?lens=workers`)
- Sidebar sections: `.wk-side-section` (Configurable · N / System · N).
- Configurable rows: `.wk-side-row.cfg` (name in `.name`, trigger in `.sub`).
- System rows: `.wk-side-row.sys` (e.g. `db.maintenance`, `skills.scan`).
- New worker: `.side [data-new]` → modal `.ewm`; save `[data-act="save"]`.
  Trigger fields: `select[data-f="trig_kind"]` (interval/cron/on_event),
  `input[data-f="trig_value"]`. Default action is `model` — switch to
  `bus.emit` or `code` in e2e when no model role is configured.
- Detail (configurable): `.cwk-hdr`, `.cwk-pipeline`, `[data-act="run"|"pause"|"resume"]`.
- Detail (system): infra worker shows description + tick count + logs.
- E2E suite: `tests/e2e/test_web_workers_e2e.py` (headed:
  `RELAYDECK_E2E_HEADED=1 uv run pytest -m e2e tests/e2e/test_web_workers_e2e.py`).

## Settings + harness onboarding e2e (`test_web_settings_onboarding_e2e.py`)
Headed walk-through with delay so an operator can follow::

  RELAYDECK_E2E_HEADED=1 RELAYDECK_E2E_SLOWMO=600 RELAYDECK_E2E_PAUSE_MS=900 \\
    uv run pytest -m e2e tests/e2e/test_web_settings_onboarding_e2e.py -v

- Settings nav: `.settings-nav [data-s="<id>"]` — `general | shortcuts | plugins
  | integrations | auth | vault | about | danger`. General sub-tabs:
  `.app-subtabs .seg-i[data-v="theme"|"customize"]`; customize embeds
  `.ap-pane` / `[data-tokens]`.
- Global plugin toggle: `.set-toggle[data-name="<plugin>"]` in Settings →
  Installed. Disabling `messaging` must drop the Messages rail slot live
  (calls `host.refreshPluginManifest()` + SSE `system.plugin.unloaded`).
- Workspace plugin toggle: Workspaces lens → `.ws-plug-card` →
  `[data-pl-toggle]` (same `.idn-toggle` as Identity tile).
- New-agent harness cards: `.na-card[data-type="pi"|"claude-code"|…]`; spawn
  `[data-act="spawn"]`; type-specific section `[data-type-section]`; preamble
  preview `[data-preamble]`.

## Per-change loop
1. Edit JS/CSS. 2. `node --check <file>.js`. 3. Restart daemon (cache-bust).
4. `browser_navigate` → `browser_take_screenshot` (no filename) →
   `browser_console_messages({level:'error'})`. 5. Fix; repeat.

## Gotchas learned (append as you find more)
- **Body-appended overlays need the `.rd` class** to pick up the design-system
  component styles (`.seg`, `.chip`, `.btn`, `.block`). The app root is `.app.rd`,
  but `settings-overlay` / `cmdk` / `kbd-cheatsheet` are appended to `document.body`
  (outside `.rd`) — they each get `... rd` in their className. New overlay? add `rd`.
- **`themes.py THEME_TOKENS` defaults must mirror `styles.css :root`.** They're the
  theme *contract* (the "Customize tokens" editor placeholders + `/api/themes/contract`).
  If `:root` changes but the contract doesn't, the editor shows stale colors. Tests
  (`tests/test_themes.py`) pin builtin *names* (`base,cyan,amber,violet,green,mono`
  must exist) + that `base` resolves to `{}` + gruvbox/daylight resolved tokens — but
  NOT the contract default colors, so retuning defaults is safe; run the 39 tests.
- **Legacy `data-accent` is dead.** Accent is the theme engine + `:root`. Don't
  reintroduce `setAttribute('data-accent', …)` (it overrode the redesign accent).
- **Empty `.chip`/`.chip.muted` renders as a stray box.** A chip whose text is set
  to `''` still shows its bg/border/padding. Toggle `style.display='none'` when
  empty (see models.js recent-note/used-note). Watch for this in any "count" chip.
- **Don't reuse design-system primitive class names (`.seg`/`.chip`/`.btn`/
  `.block`) as plain layout hooks.** The Home Usage widget's stat cells were
  `class="seg"` and silently inherited the `.seg` *segmented-control* primitive's
  beige fill + border (`bg rgb(230,225,209)`), so cost/agents/evt-s looked like
  boxed buttons. Renamed to `.ucell` (clean dividered columns, transparent bg).
  Verify a "plain" cell isn't boxed: `getComputedStyle($cell).backgroundColor`
  should be `rgba(0,0,0,0)`. Pick a widget-specific class, not a primitive.
- **Sidebar sub-tab rows must fit `--side-w` (~280px regular).** 3 `flex:1` tabs
  with icon+label+count overflow; drop the icon, font 10px, tight padding, count
  font 9px (models.js `.mdl-tab`). Verify each tab's `right` < sidebar right edge.
- **Lens sub-tabs should auto-select the first item on switch.** Switching the
  Models Presets/Providers/Defaults tabs must set activeProvider/Role/Name to
  `[0]` or the detail shows a bare "No X" empty state. Detail renderers also
  fall back to `list[0]` (mirror presets/roles).
- **Don't `text-transform:uppercase` value spans that hold URLs/keys.** The
  Provider Config base URL was uppercased + clipped; set `.k b { text-transform:
  none; word-break:break-all }` + `flex-wrap:wrap` on the row.
- **`.side-search` icon must overlay the input, not sit above it.** Every lens
  emits a bare `<svg>` (no `.ic` wrapper) as the first child of `.side-search`,
  so a CSS rule must absolute-position the bare svg: `.side-search .ic,
  .side-search > svg { position:absolute; left:8px; top:50%;
  transform:translateY(-50%); pointer-events:none }` (input already reserves
  `padding-left:26px`). Without this the magnifier renders on its own line above
  the input (looked like "search is broken" on Models). Verify svg vertical
  mid == input vertical mid.
- **Rail icons must be unique — plugin lenses collide with core lenses.** Core
  (app.js): agents=agent, messages=message, models=diamond, workers=cpu,
  workspaces=workspace. Plugin lens icons live in `plugin.toml [plugin.ui] tabs`.
  Skills shipped `icon="diamond"` (==Models) and External `icon="cpu"`
  (==Workers) → dup rail icons. Now Skills=`bolt`, External=`eye`. When adding a
  plugin lens, pick an icon NOT in that core set. Icon must be in `_KNOWN_ICONS`
  (app.js) AND defined in primitives.js or it falls back to `star`. Verify: no
  two `.rail-btn svg path[d]` are equal. plugin.toml changes need a daemon
  restart (UI manifest is built at startup).
- **Selected theme-card check overlapped the BUILT-IN badge.** `.theme-card .ck`
  was `position:absolute; right:8px` landing on `.bi` (also right-edge). Make the
  check a flex child (`flex:0 0 auto`) so it flows after the badge.
- **Workspace plugin-card header: split name + category.** Long names
  (forbidden-tools) ellipsized the whole `wpc-name` and ate the `· GATE` tag. Now
  `.wpc-title` (flex) holds `.wpc-name` (ellipsis) + `.wpc-cat` (flex-shrink:0,
  always shown); title letter-spacing dropped to .03em so the full name fits.
- **Plugin lens ids are namespaced** (`skills:skills`, `github:github`,
  `telegram:telegram`, `external:external`). `app.js _resolveLensId` now maps a
  shorthand `?lens=skills` → `skills:skills` (exact id → `<id>:<id>` → suffix
  `:<id>` → prefix `<id>:`), resolved in `_mountActiveLens` so it self-heals once
  the plugin lens loads. So both `?lens=skills` and `?lens=skills:skills` work.
- **Lens detail must scroll.** Core lenses: `.lens-body` (Models, Workspaces),
  `.cwk-body` (Workers), `.msg-list` (Messages), `.home-scroll` (Agents fleet).
  Agents tile host: `.dbody` scrolls; terminal adds `.fullbleed` →
  `overflow:hidden`. Never put long lens content in bare `.dbody`. Plugin wraps:
  `flex:1; min-height:0` on `.tg-wrap`/`.gh-wrap`/`.sk-wrap`/`.ea-wrap`; scroll
  on plain `*-body` blocks (not `display:grid`). Audit:
  `tests/e2e/test_web_lens_scroll_e2e.py`.
- **Don't rebuild a list on `mouseenter` — it eats clicks.** The cmdk palette
  re-rendered the whole `.cmdk-list` on hover; replacing the node under the
  cursor re-fires `mouseenter` (infinite re-render) AND detaches the node
  mid-click so the `click` never lands (Playwright: "element was detached from
  the DOM, retrying" ×N). Fix: a `setActive(n)` that just toggles `.active`
  classes in place; only rebuild on input change. Same trap for any
  hover-highlight list. Test: open `⌘K`, type, **hover** then **click** a result
  — must navigate (URL changes), not time out.
- **There must be a way out of agent detail → home.** `host.goHome()` clears the
  focused agent; wired to (a) the `.dhdr-back` "⌂ fleet" breadcrumb in the agent
  detail eyebrow, and (b) re-clicking the **Agents** rail icon / palette "Go to
  Agents" while an agent is selected (`setLens('agents')` with no selectionId +
  `wasLens==='agents'` clears `activeAgentId`). Verify: URL drops `&agent=…` and
  `document.querySelector('.home')` exists.
- **`?section=` must not leak across lens switches.** `_syncUrl` now guards the
  section param on `this._currentLens?.def?.id === this.state.lens` (the section
  belongs to the *mounted* lens), and `PluginLens.renderSidebar` re-calls
  `_syncUrl` once it resolves its active section (async, after the early
  switch-time sync). Verify: Telegram URL has `&section=connections`; switching
  to External drops it.
- **No fake/seeded sparkline data.** The old `walk()` seeded-random generator
  (was in `primitives.js`) is removed. Home Usage/Agents sparklines wire to the
  real `/api/agents/usage-rollup` (`{agent_id:{tokens,spark[24 hourly]}}`),
  summed per-bucket for the fleet; render the spark only when `spark.some(v>0)`,
  else an honest empty state. Worker widget uses real snapshot fields
  (`tick_count`/`last_tick_at`/`interval_s`/`alive`), not fabricated bars.
- **Home live widgets: register re-render closures UNCONDITIONALLY.** Bug found:
  `home.js _buildShell` registered the live re-render only `if (body.isConnected
  !== false)`, but `_renderGrid` builds each shell **detached** then appends — so
  `body.isConnected` was always `false` at registration → `_liveRerenders` stayed
  empty → **no home widget was ever real-time**. The closure already self-guards
  (`if (!body.isConnected) return;`) at call time, so drop the precondition.
  Verify: `window.__relaydeckHome._liveRerenders.length` must be > 0 after mount.
- **Home dashboard only renders when no agent is selected.** Deep-linking
  `?agent=<id>` shows agent detail, not `.home`. To verify home: clear the agent
  param, click `.dhdr-back` ("⌂ fleet"), or navigate to `/?lens=agents` with no
  `agent` query param.
- **Home widget gallery closes on Escape** (and scrim click / × button). Without
  the key handler the drawer stays open and intercepts clicks on widgets behind it.
- **Home widget gallery must use `var(--bg-1)`** — it shipped with hardcoded
  `#0a0d14` (old dark shell) so the drawer looked like a dark theme panel on the
  paper dashboard. Match `.plug-pop` / `.cmdk`: `background:var(--bg-1)` and a
  light paper shadow (`rgba(0,0,0,.12)`), not `box-shadow:…rgba(0,0,0,.6)`.
  Verify: `getComputedStyle(.gallery).backgroundColor === getComputedStyle(.wgt).backgroundColor`.
- **Live activity feed needs an empty state** — when `host.state.events` is empty
  the feed widget must show `No events yet…`, not a blank box.
- **Inspect the live Home instance** via `window.__relaydeckHome` (exposed in
  `mount()`): `._usageRollup`, `._fleetSpark()`, `.host.scopedAgents()`,
  `._liveRerenders.length`. Great for debugging widget data without screenshots.
- **Authenticated fetch in `browser_evaluate`**: a bare `fetch('/api/...')`
  returns `{detail: "..."}` (401). Use the app's own client:
  `(await import('/static/data.js?v=<pid>')).api.getJSON('/api/...')`.

## Codified browser E2E (`tests/e2e/test_web_*_e2e.py`)
The interactive MCP flow is also a repeatable **Playwright (Python) e2e** under
`pytest -m e2e`. Setup: `uv sync --group e2e` + `uv run playwright install
chromium`. The `live_daemon` fixture spawns `relaydeck serve` on an ephemeral
port with an isolated `$HOME` (no pollution, dashboard auto-auths over loopback).
**relaydeck-native pi detection:** `test_web_relaydeck_native_e2e.py` uses
`live_daemon_no_pi` (daemon PATH with pi's directory stripped) to assert the
new-agent modal + agent-detail banner warn and block spawn — proves live
`shutil.which` probes without a daemon restart.
**Watch it run (headed):** headless by default for batch runs. Set
``RELAYDECK_E2E_HEADED=1``, ``RELAYDECK_E2E_SLOWMO=400`` (ms), or target a
single test (``file.py::test_name``) to open a real window. CI sets
``RELAYDECK_E2E_HEADLESS=1``. The pytest-playwright ``--headed`` flag does NOT
apply (we use our own fixture). Run ONE test to avoid many windows:
``RELAYDECK_E2E_HEADED=1 RELAYDECK_E2E_SLOWMO=400 uv run pytest -m e2e
"tests/e2e/test_harness_integration_e2e.py::test_identity_tile_shows_spawn_composition"``.
For step-through debugging, ``PWDEBUG=1`` opens the Playwright Inspector.
**Env-var migration:** the old opt-in ``RELAYDECK_E2E_HEADED=1`` still works;
``RELAYDECK_E2E_HEADLESS=1`` (inverted) is the CI/unattended override.
**Headed ≠ headless browser build.** Headless uses `chromium-headless-shell`;
headed uses full "Chrome for Testing". They differ: full Chrome **requests
`/favicon.ico`** (headless-shell doesn't) and logs a GENERIC
`Failed to load resource: … 404` console error whose *text* has no "favicon"
substring. Console-error assertions must filter on the message's
`location.url` (the favicon path), not just `.text` — see `_webutil.errors()`.
So always smoke a console-error test headed too; a headless-only pass can hide
the favicon noise.

Learnings baked in (re-apply when extending it):
- **Add-workspace modal is single-column + path-first** (streamlined). The
  initial directory listing no longer clobbers the path input (initial
  `browse('', {syncInput:false})`), so the path field starts EMPTY — set it via
  the native value setter + `input` event on `.addws-modal [data-path]`. A path
  that doesn't resolve is NOT a dead-end: `[data-status]` shows "folder doesn't
  exist — it'll be created on add" (`.addws-will-create`), the confirm button
  flips to "Create & add", and submit POSTs `create_dir:true` so the daemon
  mkdir-p's it. The 404 from `/api/fs/browse` on a not-yet-created path is
  EXPECTED console noise, not a bug. Name auto-derives from the path (override
  via `input[placeholder="workspace-name"]`). The folder browser + plugins are
  behind collapsed disclosures: `.addws-disc [data-disc-toggle="browse"]` and
  `[data-disc-toggle="adv"]` (plugins) — click to expand (`[data-disc-body=
  "<key>"]`, `hidden` toggles; header gets `.open`). Submit is `.addws-modal
  [data-act="confirm"]` (text match gets intercepted by the `.addws-count`
  footer mirror). The **Recommended** preset (`button[data-preset=
  "recommended"]`, inside Advanced) filters picks against the loaded catalog, so
  wait for `.addws-plugin` rows before clicking it.
- **Set a controlled `<input>`** via the native value setter + an `input` event
  (`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set`),
  not `.value =` — the framework listens for `input`.
- **Harness "Spawn" auto-starts now** — assert the agent reaches `running`
  (status via `/api/agents/{id}`), the terminal mounts (`.xterm`/`.term-host`),
  and the PTY WS connects (`window.__relaydeckTerm.ws.readyState === 1`). That's
  the platform contract for ALL harnesses. Do NOT assert harness *content* under
  the isolated `$HOME`: pi/claude/codex paint a banner, but **opencode stays
  blank without a provider configured** — content needs the CLI's own auth,
  which the harness smoke tests (real `$HOME`/keys) cover.
- **E2E Playwright headed/headless env vars:** batch ``pytest -m e2e`` is
  **headless by default** (no N-window footgun). Headed when
  ``RELAYDECK_E2E_HEADED=1``, ``RELAYDECK_E2E_SLOWMO>0``, or a single
  ``file.py::test`` target. CI sets ``RELAYDECK_E2E_HEADLESS=1``. The old
  ``RELAYDECK_E2E_HEADED=1`` opt-in still works; ``HEADLESS=1`` is the new
  explicit force-headless override (inverted polarity from the brief headed-by-
  default experiment on this branch).
- **Resilient polling**: a harness flooding the PTY can briefly slow the single
  daemon's pyte screen render — wrap `/screen` + `/agents/{id}` polls in
  try/except and retry to the deadline; cache the loopback bootstrap token.

## Redesign quick-asserts (Studio paper theme)
- `--acc` = `#B7410E`, `--bg-0` = `#F2EFE6`, body font includes `IBM Plex Sans`.
- No heavy black shadows on overlays (paper uses `rgba(0,0,0,.12)`-ish).
- `.plug-pop` background = `var(--bg-1)` (was a hardcoded dark `#0a0d14` bug).

## Notifications bell / HITL (verifying the live bell)
- The header bell is `getByRole('button', {name: 'Notifications'})` (an SVG
  icon, NOT a 🔔 emoji — text/`/notif/i`-class selectors miss it). Click it to
  open the right-side **Inbox** drawer (filters: All · Awaiting · Errors · Info).
- **Bridged plugin-bus events are NOT observable via a bare
  `fetch('/api/events')` reader inside `browser_evaluate`** — that stream only
  showed heartbeats even for known-bridged `agent.status_changed`. Verify
  notification delivery through the **app's own bell drawer** instead (the app's
  real SSE connection consumes them). Don't waste time debugging the bare-fetch
  path; it's a harness artifact, not a bridge bug.
- HITL escalations render as "`<agent>` needs you" with the escalation message
  as the body. Fire one for verification: `POST /api/plugins/hitl/escalate`
  `{agent_id, message, kind}` — but the `(agent_id, kind)` pair has a 300s
  **cooldown**, so use a *novel* `kind` each run or the escalation is suppressed
  (returns `{"escalated": false, "reason": "cooldown"}`).
- New harnesses appear in the **New Agent** modal automatically (data-driven
  from the catalog); an uninstalled CLI shows a "`<cli> not installed`" badge.

Note : If running on dev machine dont use headless mode , show the user browser with a delay