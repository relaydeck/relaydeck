// model_select.js — the one model picker used everywhere a model is
// chosen (new-agent modal, worker `model` action, …). Standardizes the
// UX and encourages presets for visibility, while still allowing any
// provider/model to be wired.
//
// Output is a single "spec" string: a preset name or `provider/model`.
// The package ships no built-in presets/aliases — the list is whatever
// the operator created. The backend `resolve_model` accepts all of those.
// Empty = default.
//
//   const sel = await mountModelSelect(host, containerEl, {
//     value: 'my-preset', allowDefault: true, onChange: (spec) => {...},
//   });
//   sel.getValue();   // current spec string
//
// MIGRATION NOTE. Internals are a light-DOM RelayElement (no innerHTML,
// no querySelector, no manual addEventListener) on the @relaydeck/ui kit,
// but the public contract is unchanged: mountModelSelect stays an async
// function returning {getValue, el}. getValue() returns the live spec
// string; onChange fires on EVERY input change (callers depend on the
// un-debounced cadence). The /api/models/resolve lookup keeps its
// sequence guard so a slow response can't clobber a newer one.

import { RelayElement, html, nothing, icon, providerIcon, esc } from '@relaydeck/ui';

const LBL = 'font-family:var(--f-mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-3)';
const INP = 'width:100%;font-family:var(--f-mono);font-size:var(--t-xs)';

class ModelSelect extends RelayElement {
  static properties = {
    // All driven imperatively from mountModelSelect; not reflected as
    // attributes (objects/functions) — request updates explicitly.
    presets: { attribute: false },
    allowDefault: { attribute: false },
    mode: { attribute: false },
    custom: { attribute: false },
    _resolved: { attribute: false },
  };

  constructor() {
    super();
    this.host = null;
    this.presets = [];
    this.presetNames = new Set();
    this.allowDefault = true;
    this.onChange = null;
    this.value = '';          // last preset name / spec used to seed selection
    this.mode = 'default';    // 'default' | 'preset' | 'custom'
    this.custom = '';
    this._resolved = { kind: 'idle' };  // resolved-target render state
    this._resolveSeq = 0;
  }

  // Seed initial mode/custom from the incoming value + known presets.
  seed(value, allowDefault) {
    this.allowDefault = allowDefault;
    this.value = value || '';
    if (!value) this.mode = allowDefault ? 'default' : 'custom';
    else if (this.presetNames.has(value)) this.mode = 'preset';
    else { this.mode = 'custom'; this.custom = value; }
  }

  setPresets(presets) {
    this.presets = presets || [];
    this.presetNames = new Set(this.presets.map((p) => p.name));
    this.requestUpdate();
  }

  // ── current spec ──────────────────────────────────────────────────
  // The single source of truth, derived from mode + custom input. Public
  // getValue() is bound to this.
  currentSpec() {
    if (this.mode === 'default') return '';
    if (this.mode === 'custom') return (this.custom || '').trim();
    if (this.mode === 'preset') return this.value || '';
    return '';
  }

  // ── events ────────────────────────────────────────────────────────
  _onMode(e) {
    const v = e.target.value;
    if (v === '__default__') { this.mode = 'default'; }
    else if (v === 'custom') { this.mode = 'custom'; }
    else if (v.startsWith('preset:')) { this.mode = 'preset'; this.value = v.slice(7); }
    this.requestUpdate();
    this._refreshResolved();
  }

  _onCustom(e) {
    this.custom = e.target.value;
    // No re-render needed (the input is uncontrolled-ish); just resolve.
    this._refreshResolved();
  }

  // Notify the caller (every change — never debounced) and re-resolve the
  // target against the backend with a sequence guard.
  async _refreshResolved() {
    const spec = this.currentSpec();
    if (this.onChange) this.onChange(spec);
    if (!spec) { this._resolved = { kind: 'idle' }; this.requestUpdate(); return; }
    const seq = ++this._resolveSeq;
    this._resolved = { kind: 'resolving' };
    this.requestUpdate();
    let r;
    try { r = await this.host.api.getJSON(`/api/models/resolve?spec=${encodeURIComponent(spec)}`); }
    catch (_) { if (seq === this._resolveSeq) { this._resolved = { kind: 'idle' }; this.requestUpdate(); } return; }
    if (seq !== this._resolveSeq) return;
    this._resolved = { kind: 'ok', r };
    this.requestUpdate();
  }

  async _savePreset(provider, model) {
    const name = prompt(`Save ${provider}/${model} as a preset named:`, model.split('/').pop());
    if (!name) return;
    try {
      await this.host.api.postJSON('/api/presets', { name: name.trim(), provider, model });
      const presets = await this.host.api.getJSON('/api/presets') || [];
      this.setPresets(presets);
      this.value = name.trim();
      this.mode = 'preset';
      this.requestUpdate();
      this._refreshResolved();
    } catch (e) { alert('Save preset failed: ' + e.message); }
  }

  // ── render ────────────────────────────────────────────────────────
  _modeValue() {
    if (this.mode === 'default') return '__default__';
    if (this.mode === 'preset') return 'preset:' + this.value;
    return 'custom';
  }

  _resolvedTpl() {
    const st = this._resolved;
    if (st.kind === 'idle') return html`<span style="color:var(--t-4)">uses the agent/worker default model</span>`;
    if (st.kind === 'resolving') return html`<span style="color:var(--t-4)">resolving…</span>`;
    const r = st.r || {};
    // Offer "save as preset" for a non-preset custom combo so it becomes
    // a named, shareable, visible preset.
    const canSave = r.source === 'provider/model' && !r.preset;
    // Row 1: resolved target + (right-aligned) save button. min-width:0 +
    // word-break let a long provider/model truncate gracefully in the
    // narrow modal column instead of forcing the row wider.
    const targetRow = html`
      <div style="display:flex;align-items:center;gap:6px;width:100%">
        <span style="flex:1;min-width:0;word-break:break-all;display:inline-flex;align-items:center;gap:5px">→ ${providerIcon(r.provider, 12)}<span style="color:var(--acc)">${r.provider || '?'}/${r.model || '?'}</span></span>
        ${canSave ? html`<button class="btn ghost sm" @click=${() => this._savePreset(r.provider, r.model)}>${icon('plus', 10)} Save as preset</button>` : nothing}
      </div>`;
    // Capability badges + price from the models.dev metadata index (when
    // known). Pure enrichment — empty/absent when models.dev hasn't loaded.
    // They live on their OWN wrapping row so they never collide with the
    // target text or the price in a constrained column.
    const caps = Array.isArray(r.capabilities) ? r.capabilities : [];
    const badges = caps.map((cap) =>
      html`<span title=${cap} style="font-size:9px;padding:1px 5px;border-radius:8px;background:var(--bg-2);color:var(--t-3);text-transform:uppercase;letter-spacing:.04em;white-space:nowrap">${cap}</span>`);
    let priceTpl = nothing;
    if (r.price && (r.price.input != null || r.price.output != null)) {
      const fmt = (v) => (v === 0 ? 'free' : `$${v}`);
      priceTpl = html`<span title="USD per 1M tokens (input/output)" style="font-size:9px;color:var(--t-4);white-space:nowrap">${fmt(r.price.input)}/${fmt(r.price.output)} per 1M</span>`;
    }
    const metaRow = (badges.length || priceTpl !== nothing)
      ? html`<div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px">${badges}${priceTpl}</div>`
      : nothing;
    const warnRow = r.warning
      ? html`<div style="color:var(--warn);word-break:break-word" title=${r.warning}>⚠ ${r.warning}</div>`
      : nothing;
    return html`${targetRow}${metaRow}${warnRow}`;
  }

  render() {
    const sel = this._modeValue();
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <select data-ms-mode style=${INP} @change=${(e) => this._onMode(e)}>
          ${this.allowDefault ? html`<option value="__default__" ?selected=${sel === '__default__'}>— default model —</option>` : nothing}
          ${this.presets.length ? html`
            <optgroup label="Presets (recommended)">
              ${this.presets.map((p) => html`<option value=${'preset:' + p.name} ?selected=${sel === 'preset:' + p.name}>${p.name} — ${p.provider}/${p.model}</option>`)}
            </optgroup>` : nothing}
          <option value="custom" ?selected=${sel === 'custom'}>Custom (provider/model)…</option>
        </select>
        <input data-ms-custom placeholder="provider/model — e.g. ollama/gemma3:1b"
          .value=${this.custom}
          @input=${(e) => this._onCustom(e)}
          style="${INP};display:${this.mode === 'custom' ? 'block' : 'none'}">
        <div data-ms-resolved style="font-family:var(--f-mono);font-size:10px;color:var(--t-4);min-height:14px;display:flex;flex-direction:column;gap:3px">${this._resolvedTpl()}</div>
      </div>`;
  }
}

if (!customElements.get('rd-model-select')) customElements.define('rd-model-select', ModelSelect);

export async function mountModelSelect(host, container, { value = '', allowDefault = true, onChange } = {}) {
  let presets = [];
  try { presets = await host.api.getJSON('/api/presets') || []; } catch (_) {}

  const el = document.createElement('rd-model-select');
  el.host = host;
  el.onChange = onChange;
  el.setPresets(presets);
  el.seed(value, allowDefault);
  container.replaceChildren(el);

  // Let the first render settle, then kick the initial resolve (fires
  // onChange + populates the resolved row) — parity with the original.
  if (el.updateComplete) { try { await el.updateComplete; } catch (_) {} }
  el._refreshResolved();

  // `el` is the <rd-model-select> element (remove it to tear the picker down);
  // `container` is the caller-provided host node (returned for back-compat with
  // callers that expected the old return shape, where `el` was the container).
  return { getValue: () => el.currentSpec(), el, container };
}
