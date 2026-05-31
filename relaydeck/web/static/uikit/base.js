// uikit/base.js — the light-DOM Lit bases the whole relaydeck dashboard (and
// every plugin UI) is built on. Three pieces:
//
//   RelayElement  — base for all components/tiles/widgets (custom elements).
//   RelayLens     — base for dashboard lenses (the two-pane sidebar+detail
//                   controllers app.js mounts imperatively).
//   defineTile    — adapter that exposes a RelayElement as the legacy
//                   mount(container, api, ctx)/unmount() tile/plugin contract.
//
// WHY LIGHT DOM. We override createRenderRoot() to render into the element
// itself instead of a shadow root. The dashboard is a single themed surface:
// global stylesheets + CSS design tokens (styles.css/panels.css/theme tokens)
// must reach every component, text selection and xterm must work across the
// tree, and there is nothing we want to encapsulate. Shadow DOM would fight
// all of that, so we forgo style scoping (which we never wanted) for seamless
// theming. Components keep authoring with `html` exactly as normal.

import { LitElement, render, nothing } from 'lit';
import { live } from '../data.js';

export class RelayElement extends LitElement {
  // No shadow root — render into ourselves so global CSS + tokens apply.
  createRenderRoot() { return this; }
}

// RelayLens — base for dashboard lenses.
//
// A lens is NOT a single custom element: it owns TWO render targets, the rail
// sidebar (`.side`) and the detail pane (`.detail-host`), which app.js mounts
// into separately via the historical contract:
//
//   const lens = new SomeLens(host, def);   // app.js _mountActiveLens()
//   lens.renderSidebar(sideEl);
//   lens.renderDetail(detailEl, opts);      // opts only for the agents lens
//   lens.unmount();                          // on lens switch
//   lens.onAgentsChanged() / onWorkspacesChanged() / onAppearanceChanged();
//
// This base keeps that exact contract but lets subclasses author declaratively:
// implement `sidebar()` and `detail(opts)` returning Lit templates, read shared
// collections from `this.host.state` (kept fresh by app.js) or lens-private
// resources via `this.use('/api/…')` (auto-subscribes to the live store +
// re-renders + auto-cleans up), subscribe to the SSE bus via `this.onEvent(cb)`,
// and wire events inline with @click. No innerHTML, no querySelector, no manual
// addEventListener, no unsubscribe bookkeeping.
//
// Each target renders into the lens's own `display:contents` root element, so
// (a) switching lenses cleanly drops the previous lens's DOM with no lit-part
// contamination, and (b) the wrapper generates no box — the lens's own `.pane`
// remains a direct layout child of `.detail-host`, preserving the flex chain
// the terminal tile depends on for sizing.
export class RelayLens {
  constructor(host, def) {
    this.host = host;
    this.def = def || {};
    this._sideHost = null;     // app's `.side` container
    this._detailHost = null;   // app's `.detail-host` container
    this._sideRoot = null;     // our display:contents root inside _sideHost
    this._detailRoot = null;   // our display:contents root inside _detailHost
    this._opts = undefined;    // last renderDetail opts (agents lens: {agent, agents})
    this._subs = new Map();    // live-store key -> { value, unsub }
    this._cleanups = [];       // misc teardown (onEvent, timers, …)
    this._scheduled = false;
    this._sideScheduled = false;
    this._detailScheduled = false;
    this._dead = false;
  }

  // ── host contract ─────────────────────────────────────────────────
  renderSidebar(container) {
    this._sideHost = container;
    this._sideRoot = this._mountRoot(container, this._sideRoot);
    this._paintSidebar();
  }

  renderDetail(container, opts) {
    this._detailHost = container;
    if (opts !== undefined) this._opts = opts;
    this._detailRoot = this._mountRoot(container, this._detailRoot);
    this._paintDetail();
  }

  unmount() {
    this._dead = true;
    for (const s of this._subs.values()) { try { s.unsub?.(); } catch (_) {} }
    this._subs.clear();
    for (const fn of this._cleanups) { try { fn(); } catch (_) {} }
    this._cleanups = [];
    try { this.onUnmount?.(); } catch (_) {}
  }

  // app.js pushes these when the shared collections refresh (sidebar-only by
  // design — it never re-renders the detail on live collection churn so a
  // running terminal is never disturbed). Default: sidebar repaint only.
  // Subclasses that need the detail to react too (e.g. messages typing state)
  // override with requestUpdate().
  onAgentsChanged() { this.requestSidebarUpdate(); }
  onWorkspacesChanged() { this.requestSidebarUpdate(); }
  onAppearanceChanged() { this.requestSidebarUpdate(); }

  // ── reactivity helpers ────────────────────────────────────────────
  // Read a live-store resource by path. First call subscribes (re-rendering on
  // every push) and returns the cached value (or `fallback` until the first
  // fetch resolves). Subsequent calls return the latest value without
  // re-subscribing. Subscriptions are released on unmount.
  use(key, fallback) {
    let s = this._subs.get(key);
    if (!s) {
      s = { value: live.peek(key), unsub: null };
      this._subs.set(key, s);
      s.unsub = live.subscribe(key, (data) => {
        s.value = data;
        if (!this._dead) this.requestUpdate();
      });
    }
    return s.value == null ? fallback : s.value;
  }

  /** Drop a `use()` subscription (e.g. when a selected id changes and the old
   *  per-id resource is no longer needed). */
  drop(key) {
    const s = this._subs.get(key);
    if (s) { try { s.unsub?.(); } catch (_) {} this._subs.delete(key); }
  }

  /** Subscribe to the raw SSE event bus through the host; auto-cleans on
   *  unmount. Some lenses react to events that aren't a single resource. */
  onEvent(cb) {
    if (!this.host?.subscribeEvents) return () => {};
    const off = this.host.subscribeEvents(cb);
    this._cleanups.push(off);
    return off;
  }

  /** Track an arbitrary teardown fn (timer clears, observers) for unmount. */
  addCleanup(fn) { if (typeof fn === 'function') this._cleanups.push(fn); }

  /** Re-render both targets on the next microtask (coalesced). */
  requestUpdate() {
    if (this._scheduled || this._dead) return;
    this._scheduled = true;
    queueMicrotask(() => {
      this._scheduled = false;
      if (this._dead) return;
      this._paintSidebar();
      this._paintDetail();
    });
  }

  /** Re-render ONLY the sidebar (coalesced). Use when a live update must not
   *  disturb the detail pane — e.g. it holds a focused input, a mounted tile,
   *  or the terminal. (A plain requestUpdate() repaints both panes.) */
  requestSidebarUpdate() {
    if (this._sideScheduled || this._dead) return;
    this._sideScheduled = true;
    queueMicrotask(() => { this._sideScheduled = false; if (!this._dead) this._paintSidebar(); });
  }

  /** Re-render ONLY the detail pane (coalesced). */
  requestDetailUpdate() {
    if (this._detailScheduled || this._dead) return;
    this._detailScheduled = true;
    queueMicrotask(() => { this._detailScheduled = false; if (!this._dead) this._paintDetail(); });
  }

  // ── internals ─────────────────────────────────────────────────────
  _mountRoot(container, existing) {
    if (!container) return existing;
    let root = existing;
    if (!root) {
      root = document.createElement('div');
      // display:contents → no layout box; the lens's own `.pane`/`.side-*`
      // children act as direct children of the host container.
      root.style.display = 'contents';
    }
    if (root.parentNode !== container) container.replaceChildren(root);
    return root;
  }

  _paintSidebar() {
    if (!this._sideRoot || typeof this.sidebar !== 'function') return;
    try { render(this.sidebar() ?? nothing, this._sideRoot); }
    catch (e) { console.error(`[${this.def.id || 'lens'}] sidebar render`, e); }
  }

  _paintDetail() {
    if (!this._detailRoot || typeof this.detail !== 'function') return;
    try { render(this.detail(this._opts) ?? nothing, this._detailRoot); }
    catch (e) { console.error(`[${this.def.id || 'lens'}] detail render`, e); }
  }
}

// defineTile — bridge a RelayElement to the legacy tile/plugin mount contract.
//
// Agent-detail tiles and plugin UI modules are mounted by tile_system.js (and
// the plugin lens / home grid) via `mount(container, api, ctx)` + `unmount()`,
// returning either a cleanup fn or an object with unmount(). This adapter lets
// you author the tile as a normal light-DOM Lit element and expose it through
// that contract with one line:
//
//   class MyTile extends RelayElement { static properties = {...}; render(){...} }
//   customElements.define('rd-tile-mine', MyTile);
//   export default defineTile('rd-tile-mine', (el, { api, ctx }) => {
//     el.agent = ctx.agent; el.api = api; el.mode = ctx.mode;
//   });
//
// The element is created, configured by `assign`, and appended to the
// container. unmount() removes it → disconnectedCallback fires → its
// LiveController/EventsController subscriptions release automatically.
export function defineTile(tagName, assign) {
  return class TileAdapter {
    async mount(container, api, ctx) {
      this._el = document.createElement(tagName);
      try { assign?.(this._el, { api, ctx, container }); } catch (e) { console.error(tagName, 'assign', e); }
      container.replaceChildren(this._el);
      // Let the element's first update settle before resolving (parity with
      // tiles that await initial data in mount()).
      if (this._el.updateComplete) { try { await this._el.updateComplete; } catch (_) {} }
      return () => this.unmount();
    }
    unmount() {
      try { this._el?.remove(); } catch (_) {}
      this._el = null;
    }
  };
}
