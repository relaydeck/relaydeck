// uikit/widgets.js — the relaydeck design system as composable Lit template
// helpers. Each returns a TemplateResult that renders the dashboard's existing
// global CSS classes (.btn/.chip/.sbadge/.sdot/.card/.subtab/.stat-cell/…), so
// components look identical to the hand-rolled originals and every e2e selector
// keeps matching — but authoring is now one shared, themed vocabulary instead
// of 18 plugins re-rolling buttons and cards.
//
// These are functions (not custom elements) on purpose: zero wrapper nodes, so
// they drop straight into flex/grid layouts. The genuinely stateful pieces
// (toggle, modal, settings-form) are custom elements in their own modules.

import { html, nothing } from 'lit';
import { classMap } from 'lit';
import { ifDefined } from 'lit';
import { icon } from './format.js';

// ── buttons ─────────────────────────────────────────────────────────
// button({variant:'primary'|'ghost'|'danger'|'icon', size:'sm', act, title,
//         type, disabled, onClick}, ...content)
export function button(opts = {}, ...content) {
  const { variant = '', size = '', type = 'button', disabled = false,
          title, act, onClick } = opts;
  const cls = { btn: true };
  for (const v of String(variant).split(/\s+/).filter(Boolean)) cls[v] = true;
  if (size) cls[size] = true;
  return html`<button
    class=${classMap(cls)}
    type=${type}
    ?disabled=${disabled}
    title=${ifDefined(title)}
    data-act=${ifDefined(act)}
    @click=${onClick}
  >${content}</button>`;
}

// iconBtn(name, {title, act, size:'sm', variant, onClick, glyph})
export function iconBtn(name, opts = {}) {
  const { glyph = 14, variant = 'icon', ...rest } = opts;
  return button({ variant, ...rest }, icon(name, glyph));
}

// ── chips / badges / dots ───────────────────────────────────────────
// chip(label, tone) — inline pill. tone: ''|accent|ok|warn|err|muted
export function chip(label, tone = '') {
  return html`<span class=${classMap({ chip: true, [tone]: !!tone })}>${label}</span>`;
}

// badge(label, status) — status pill (.sbadge.<status>)
export function badge(label, status = '') {
  return html`<span class=${classMap({ sbadge: true, [status]: !!status })}>${label}</span>`;
}

// dot(status) — status indicator dot (.sdot.<status>)
export function dot(status = '', title) {
  return html`<span class=${classMap({ sdot: true, [status]: !!status })} title=${ifDefined(title)}></span>`;
}

// ── cards / empty state / sections ──────────────────────────────────
// card({title, sub, head, foot, class}, ...body)
export function card(opts = {}, ...body) {
  const { title, sub, head, foot, class: extra = '' } = opts;
  return html`<div class="card ${extra}">
    ${head || title || sub ? html`<div class="card-head">
      ${title ? html`<div class="card-title">${title}</div>` : nothing}
      ${sub ? html`<div class="card-sub">${sub}</div>` : nothing}
      ${head ?? nothing}
    </div>` : nothing}
    ${body}
    ${foot ? html`<div class="card-foot">${foot}</div>` : nothing}
  </div>`;
}

// empty(title, sub?, ...extra) — the standard .pane-empty placeholder.
export function empty(title, sub, ...extra) {
  return html`<div class="pane-empty">
    <div class="ttl">${title}</div>
    ${sub ? html`<div class="sub">${sub}</div>` : nothing}
    ${extra}
  </div>`;
}

// sectionHead(label, iconName?, ...right) — a block header (icon + label).
export function sectionHead(label, iconName, ...right) {
  return html`<div class="rd-sec-h">
    <span>${iconName ? icon(iconName, 13) : nothing} ${label}</span>
    ${right.length ? html`<span class="rd-sec-r">${right}</span>` : nothing}
  </div>`;
}

// ── stat strip ──────────────────────────────────────────────────────
// statStrip([{k, v, tone?}]) — the metadata grid used in detail headers.
export function statStrip(cells = []) {
  return html`<div class="stat-strip">
    ${cells.map((c) => html`<div class="stat-cell">
      <div class="v ${c.tone || ''}">${c.v}</div>
      <div class="k">${c.k}</div>
    </div>`)}
  </div>`;
}

// ── sidebar chrome ──────────────────────────────────────────────────
// sideHead(title, {count, actions}) — the `.side-head` row.
export function sideHead(title, opts = {}) {
  const { count, actions } = opts;
  return html`<div class="side-head">
    <div class="side-title">${title} ${count != null ? html`<span class="count">${count}</span>` : nothing}</div>
    ${actions ?? nothing}
  </div>`;
}

// sideSearch(value, onInput, placeholder?) — `.side-search` input.
export function sideSearch(value, onInput, placeholder = 'Search…') {
  return html`<div class="side-search">${icon('search', 12)}
    <input .value=${value || ''} placeholder=${placeholder}
      @input=${(e) => onInput(e.target.value)}>
  </div>`;
}

// sideFilter(items, active, onPick) — `.side-filter` segmented buttons.
//   items: [{id, label}]
export function sideFilter(items, active, onPick) {
  return html`<div class="side-filter">
    ${items.map((f) => html`<button
      class=${classMap({ on: f.id === active })}
      data-f=${f.id}
      @click=${() => onPick(f.id)}>${f.label}</button>`)}
  </div>`;
}

// ── tabs ────────────────────────────────────────────────────────────
// subtabs(items, active, onPick) — `.subtabs` row. items: [{id,label,badge?}]
export function subtabs(items, active, onPick) {
  return html`<div class="subtabs">
    ${items.map((t) => html`<button
      class=${classMap({ subtab: true, on: t.id === active })}
      data-tab=${t.id}
      @click=${() => onPick(t.id)}>
      ${t.icon ? icon(t.icon, 13) : nothing}${t.label}
      ${t.badge != null && t.badge !== '' ? html`<span class="badge">${t.badge}</span>` : nothing}
    </button>`)}
  </div>`;
}

// ── sidebar nav (standardized plugin-lens nav) ──────────────────────
// sidebarNav(spec, active, onPick): the `.pl-nav` used by plugin lenses.
//   spec entries: {id,label,icon?,badge?} OR {group:'Title', items:[…]}
export function sidebarNav(spec, active, onPick) {
  const item = (it) => html`<button
    class=${classMap({ 'pl-nav-item': true, active: it.id === active })}
    data-pl-section=${it.id}
    title=${ifDefined(it.label || it.id)}
    @click=${() => onPick(it.id)}>
    ${it.icon ? html`<span class="pl-nav-ic">${icon(it.icon, 14)}</span>` : nothing}
    <span class="pl-nav-label">${it.label || it.id}</span>
    ${it.badge != null && it.badge !== '' ? html`<span class="pl-nav-badge">${it.badge}</span>` : nothing}
  </button>`;
  return html`<div class="pl-nav">
    ${(spec || []).map((s) => Array.isArray(s?.items)
      ? html`${s.group ? html`<div class="pl-nav-group">${s.group}</div>` : nothing}${s.items.filter((i) => i?.id).map(item)}`
      : (s?.id ? item(s) : nothing))}
  </div>`;
}
