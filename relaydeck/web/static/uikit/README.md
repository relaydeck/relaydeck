# @relaydeck/ui — dashboard UI kit & authoring guide

The relaydeck dashboard and its plugin UIs are built on **Lit** (vendored, 3.3.x,
build-less) plus a small shared kit, `@relaydeck/ui`. This is the one vocabulary
core lenses and third-party plugins share, so every surface looks and behaves
like one product.

No build step. No bundler. The daemon serves ES modules directly and injects an
importmap mapping the bare specifiers `lit` and `@relaydeck/ui` to the vendored
files (see `transports/api.py`). You just write:

```js
import { html, RelayElement, button, icon, openModal, live } from '@relaydeck/ui';
```

## Principles

- **Light DOM, always.** Components render into themselves (no shadow root) so
  the global stylesheets + CSS design tokens apply, text selection works, and
  the xterm terminal is never trapped behind a shadow boundary. `RelayElement`
  sets this up; never override `createRenderRoot` to return a shadow root.
- **No decorators.** relaydeck is build-less, so there's no TypeScript/Babel to
  strip `@property`. Declare reactive props with the static `properties` getter
  and register elements with `customElements.define`.
- **Keep the existing CSS classes.** Components render the dashboard's existing
  global classes (`.btn`, `.chip`, `.sbadge`, `.card`, `.subtab`, `.side-*`,
  `.pl-nav-*`, …). That keeps theming and the e2e selectors intact. Only the kit
  itself adds new `.rd-*` classes (styled in `uikit.css`).
- **Reuse the data layer.** `live` (the SSE-fed store) and `api` come from the
  kit — don't re-implement fetching or polling.

## What the kit exports

- **Lit:** `html`, `svg`, `css`, `nothing`, `render`, and the directives we use
  (`repeat`, `classMap`, `styleMap`, `when`, `ifDefined`, `ref`, `map`,
  `unsafeHTML`, `unsafeSVG`, …). `live` from Lit is exported as `liveDirective`
  (the store keeps the name `live`).
- **Bases:** `RelayElement` (component base), `RelayLens` (lens base),
  `defineTile` (tile/plugin mount adapter).
- **Controllers:** `LiveController`, `EventsController` (live-store / SSE wiring
  for `RelayElement`s, auto-unsubscribe on disconnect).
- **Helpers:** `esc`, `icon`, `brand`, `providerIcon`, `spark`, `fmtNum`,
  `fmtCost`, `relTime`, `formatTs`, `uptimeStr`, `visualStatus`.
- **Component helpers (functions → templates):** `button`, `iconBtn`, `chip`,
  `badge`, `dot`, `card`, `empty`, `sectionHead`, `statStrip`, `sideHead`,
  `sideSearch`, `sideFilter`, `subtabs`, `sidebarNav`.
- **Stateful elements:** `<rd-toggle>`, `<rd-settings-form>` (+ `openModal`,
  `confirm`).
- **Data layer:** `api`, `live`, `events`, `stamped`.

## Authoring a plugin UI

The dashboard host loads a plugin UI module and calls the **framework-neutral**
contract — this has NOT changed:

```js
export default class {
  async mount(container, api, ctx) { /* render into container */ }
  unmount() { /* teardown */ }
}
```

You're free to build inside `mount()` however you like, but the easy, cohesive
way is the kit. For an **agent-detail tile** or simple panel, author a
`RelayElement` and expose it with `defineTile`:

```js
import { RelayElement, defineTile, LiveController, html, chip, icon } from '@relaydeck/ui';

class MyTile extends RelayElement {
  static properties = { agent: { attribute: false } };
  constructor() { super(); this._data = new LiveController(this); }
  willUpdate(c) { if (c.has('agent') && this.agent?.id)
    this._data.setKey(`/api/plugins/mine/?agent=${encodeURIComponent(this.agent.id)}`); }
  render() { return html`${(this._data.value || []).map((x) => chip(x.name))}`; }
}
customElements.define('rd-tile-mine', MyTile);
export default defineTile('rd-tile-mine', (el, { ctx }) => { el.agent = ctx.agent; });
```

For **settings**, don't hand-roll a form — feed the typed schema to
`<rd-settings-form>` and handle `@save`. For dialogs, use `openModal`/`confirm`
instead of `window.confirm`. For escaping, import `esc` (Lit auto-escapes
interpolations anyway). The point: never re-roll buttons, cards, badges,
toggles, forms, or escaping — import them.

## CSS design-token contract

Style with these tokens (do not hard-code colors); they retheme live and
respond to `[data-density]` / `[data-glow]` on `<html>`:

| Group | Tokens |
| --- | --- |
| Surfaces | `--bg-0` (canvas) … `--bg-4`, `--bg-term` |
| Hairlines | `--line-1` … `--line-4` |
| Ink/text | `--t-1` (primary) … `--t-4` (faint) |
| Accent | `--acc`, `--acc-d`, `--acc-soft`, `--acc-line`, `--acc-glow`, `--acc-text` |
| Status | `--ok`/`--ok-soft`, `--warn`/`--warn-soft`, `--err`/`--err-soft`, `--info`/`--info-soft` |
| Type | `--f-sans`, `--f-mono`; sizes `--t-xxs`(10) `--t-xs`(11) `--t-sm`(12) `--t-md`(13) `--t-lg`(15) `--t-xl`(18) `--t-2xl` `--t-3xl` `--t-display` |
| Radius | `--r-0`(2) … `--r-4`(12) |
| Density | `--row-h`, `--pad-x`, `--pad-y`, `--tab-h`, `--hdr-h`, `--side-w` |

## Migrating a core surface to Lit

Three reference migrations show the patterns end to end:

- **`tiles/config.js`** — a read-only tile: `RelayElement` + `LiveController` +
  `defineTile`.
- **`lenses/plugins.js`** — a full two-pane lens: `RelayLens` with
  `sidebar()`/`detail()` templates, `this.use('/api/…')` for live data,
  `this.onEvent()` for the SSE bus, and `<rd-settings-form>`/`<rd-toggle>`/
  `confirm()`.
- **`lenses/agents.js`** — the lens that hosts the xterm terminal: structural
  skeleton + sidebar in lit-html, but stat-ticking, sub-tab popovers, and the
  tile body stay imperative against static anchor nodes (`[data-body]`) that the
  reactive layer never touches. **Terminal safety rule:** never reactively
  re-render the node a tile (especially the terminal) is mounted into.

Rules for a migration:

1. **Preserve the public contract.** Keep every exported name and signature
   exactly (`export class XLens`, `export function openY(host)`,
   `export default class` for tiles). app.js and siblings call these.
2. **Preserve DOM classes + `data-*` attributes** the rest of the app and the
   e2e suite rely on (`.rail`, `.side`, `.srow`, `.subtab`, `.detail-host`,
   `.hdr [data-act=…]`, `.na-card[data-type]`, `[data-f]`, `.theme-card[data-theme]`,
   `.set-toggle[data-name]`, `.lens-body`, …). Light DOM makes this free.
3. **Lenses** → extend `RelayLens`; implement `sidebar()` + `detail(opts)`;
   read shared collections from `this.host.state`, lens-private data via
   `this.use(key)`; events via `@click`.
4. **Tiles** → `RelayElement` + `defineTile`. **Never migrate `tiles/terminal.js`.**
5. **Modals/overlays** → `openModal`/`confirm`, or render a Lit template into the
   existing `.overlay-scrim` and keep the function's exported signature.
6. **No live subscriptions left dangling** — controllers and `RelayLens` clean up
   on disconnect/unmount automatically; if you add a timer or document listener,
   register teardown (`this.addCleanup(fn)` on a lens).
7. **Sidebar-only live refresh** — `RelayLens.requestUpdate()` repaints *both*
   panes. When a live-store push must not disturb the detail (focused inputs,
   mounted tiles, terminal hosts), call `requestSidebarUpdate()` instead.
   `requestDetailUpdate()` exists for the inverse. app.js's
   `onAgentsChanged`/`onWorkspacesChanged` hooks default to sidebar-only; override
   with `requestUpdate()` when the detail must react (e.g. messages typing state).
   Controlled inputs in a reactive detail need Lit's `liveDirective` on `.value`.
