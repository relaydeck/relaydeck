// @relaydeck/ui — the relaydeck dashboard UI kit.
//
// One import surface for core dashboard code AND third-party plugin UIs. The
// daemon maps the bare specifier `@relaydeck/ui` → this file in the page
// importmap (transports/api.py), so everyone writes:
//
//   import { html, RelayElement, RelayLens, button, card, icon, openModal,
//            LiveController, live, api } from '@relaydeck/ui';
//
// What's here:
//   • the Lit API you need (html/css/render + the directives we use), so a
//     plugin needs only this one import, not a separate `lit` import;
//   • the relaydeck bases + reactive controllers (light DOM, live-store wiring);
//   • themed component helpers + stateful elements (<rd-toggle>,
//     <rd-settings-form>, modal/confirm);
//   • escaping, icons, brand marks, and formatters;
//   • the data layer (api/live/events/stamped) for convenience.
//
// Plugins keep the framework-neutral mount(container, api, ctx) boundary — this
// kit is what they build WITH, not a renderer they must adopt. See AGENTS.md
// "Plugin UI" for the authoring guide and the CSS design-token contract.

// ── Lit (re-exported from the vendored bundle) ──────────────────────
export {
  LitElement, html, svg, css, nothing, noChange, render,
  // directives we actually use across the dashboard
  repeat, classMap, styleMap, when, ifDefined, ref, createRef,
  live as liveDirective, map, until, guard, cache, keyed,
  unsafeHTML, unsafeSVG,
} from 'lit';

// ── relaydeck bases + reactive controllers ──────────────────────────
export { RelayElement, RelayLens, defineTile } from './base.js';
export { LiveController, EventsController } from './reactive.js';

// ── escaping / icons / formatters ───────────────────────────────────
export {
  esc, iconSVG, brandSVG, providerMark, brandHex, hasBrand, providerSlug,
  knownIconNames, sparklineSVG, areaChartSVG, fmtNum, fmtCost, relTime,
  formatTs, uptimeStr, toMs, visualStatus, BRAND_SVG,
  icon, brand, providerIcon, spark, areaChart,
} from './format.js';

// ── component helpers (template functions) ──────────────────────────
export {
  button, iconBtn, chip, badge, dot, card, empty, sectionHead,
  statStrip, sideHead, sideSearch, sideFilter, subtabs, sidebarNav,
} from './widgets.js';

// ── stateful elements + overlays ────────────────────────────────────
export { RdToggle } from './toggle.js';
export { RdSettingsForm } from './settings-form.js';
export { openModal, confirm } from './modal.js';

// ── data layer (the same instances core uses) ───────────────────────
export { api, live, events, stamped, BUILD_V } from '../data.js';
