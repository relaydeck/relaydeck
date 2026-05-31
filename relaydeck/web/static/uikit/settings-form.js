// uikit/settings-form.js — <rd-settings-form>, the one schema-driven settings
// form for the whole ecosystem.
//
// relaydeck's typed plugin-settings descriptors (plugin_settings.py) come back
// from /api/plugins/<name>/settings as {schema, values, sources}. The Plugins
// lens, the Settings overlay, and plugin authors all rendered this by hand
// before. This element renders it once, for every supported field type —
// text / textarea / number / bool / enum / secret_ref / preset_ref — tracks
// edits, and emits a `save` event with the collected values.
//
//   <rd-settings-form
//     .schema=${schema} .values=${values} .sources=${sources}
//     @save=${(e) => api.postJSON(`/api/plugins/${name}/settings`, e.detail)}>
//   </rd-settings-form>
//
// `save` detail mirrors the historical contract: every key is included (bool →
// boolean; an empty number is omitted; everything else → string).

import { RelayElement } from './base.js';
import { html, nothing } from 'lit';
import { classMap } from 'lit';
import { ifDefined } from 'lit';
import { esc } from './format.js';
import './toggle.js';

export class RdSettingsForm extends RelayElement {
  static properties = {
    schema: { attribute: false },
    values: { attribute: false },
    sources: { attribute: false },
    saveLabel: { type: String },
    _draft: { state: true },
    _msg: { state: true },
    _busy: { state: true },
  };

  constructor() {
    super();
    this.schema = [];
    this.values = {};
    this.sources = {};
    this.saveLabel = 'Save settings';
    this._draft = {};
    this._msg = null;
    this._busy = false;
  }

  willUpdate(changed) {
    // (Re)seed the working draft whenever the inputs change identity.
    if (changed.has('schema') || changed.has('values')) {
      const d = {};
      for (const f of (this.schema || [])) {
        const v = (this.values || {})[f.key];
        let val = v == null ? (f.default == null ? '' : f.default) : v;
        // An enum whose stored value isn't one of the current options would
        // visually fall back to the first <option>; reflect that in the draft
        // so save() emits the displayed value, not a stale/empty one.
        if (f.type === 'enum') {
          const opts = f.options || [];
          if (opts.length && !opts.some((o) => String(o) === String(val))) val = opts[0];
        }
        d[f.key] = val;
      }
      this._draft = d;
    }
  }

  _set(key, val) { this._draft = { ...this._draft, [key]: val }; }

  _collect() {
    const out = {};
    for (const f of (this.schema || [])) {
      const v = this._draft[f.key];
      if (f.type === 'bool') out[f.key] = v === true || v === 'true' || v === 1 || v === '1';
      else if (f.type === 'number') { const n = String(v ?? '').trim(); if (n !== '') out[f.key] = n; }
      else out[f.key] = v ?? '';
    }
    return out;
  }

  _save = () => {
    if (this._busy) return;   // block a rapid double-submit (in-flight)
    this._busy = true; this._msg = { text: 'saving…', cls: '' };
    this.dispatchEvent(new CustomEvent('save', {
      detail: this._collect(),
      bubbles: true, composed: true,
    }));
    // The host normally re-enables via setStatus() once the POST resolves;
    // a safety timeout frees the button if a host never reports back.
    clearTimeout(this._busyTimer);
    this._busyTimer = setTimeout(() => { this._busy = false; }, 8000);
  };

  /** Hosts that await the POST can call form.setStatus('✓ saved','ok'). */
  setStatus(text, cls = '') {
    clearTimeout(this._busyTimer);
    this._busy = false;
    this._msg = text ? { text, cls } : null;
  }

  disconnectedCallback() { super.disconnectedCallback(); clearTimeout(this._busyTimer); }

  _field(f) {
    const v = this._draft[f.key];
    const src = (this.sources || {})[f.key];
    const srcTag = src && src !== 'default'
      ? html`<span class="rd-src ${src}">from ${src}</span>` : nothing;
    const isBool = f.type === 'bool';
    const ctrl = this._control(f, v);
    return html`<div class=${classMap({ 'rd-field': true, inline: isBool })}>
      <div class="rd-field-head">
        <label>${f.label || f.key} ${srcTag}</label>
        ${isBool ? ctrl : nothing}
      </div>
      ${f.description ? html`<div class="rd-field-desc">${f.description}</div>` : nothing}
      ${isBool ? nothing : html`<div class="rd-field-ctrl">${ctrl}</div>`}
    </div>`;
  }

  _control(f, v) {
    if (f.type === 'bool') {
      const on = v === true || v === 'true' || v === 1 || v === '1';
      return html`<rd-toggle ?on=${on}
        @change=${(e) => this._set(f.key, e.detail.on)}></rd-toggle>`;
    }
    if (f.type === 'enum') {
      return html`<select @change=${(e) => this._set(f.key, e.target.value)}>
        ${(f.options || []).map((o) => html`<option ?selected=${String(o) === String(v)}>${o}</option>`)}
      </select>`;
    }
    if (f.type === 'textarea') {
      return html`<textarea rows="3" .value=${String(v ?? '')}
        @input=${(e) => this._set(f.key, e.target.value)}></textarea>`;
    }
    const itype = f.type === 'number' ? 'number' : (f.secret ? 'password' : 'text');
    const ph = f.type === 'secret_ref' ? 'vault key name'
      : (f.type === 'preset_ref' ? 'preset name' : '');
    return html`<input type=${itype} .value=${String(v ?? '')} placeholder=${ph}
      @input=${(e) => this._set(f.key, e.target.value)}>`;
  }

  render() {
    const schema = this.schema || [];
    if (!schema.length) return html`<div class="rd-muted">No configurable settings.</div>`;
    return html`
      <div class="rd-fields">${schema.map((f) => this._field(f))}</div>
      <div class="rd-form-foot">
        ${this._msg ? html`<span class="rd-form-msg ${this._msg.cls}">${this._msg.text}</span>` : nothing}
        <button class="btn primary sm" ?disabled=${this._busy} @click=${this._save}>${this.saveLabel}</button>
      </div>`;
  }
}

if (!customElements.get('rd-settings-form')) customElements.define('rd-settings-form', RdSettingsForm);

// Note: `esc` is intentionally imported (re-exported here for callers that
// build labels) but Lit auto-escapes interpolations, so templates above pass
// raw strings safely.
export { esc };
