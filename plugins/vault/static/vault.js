// Vault panel — contributed by the vault plugin.
// Values never leave the daemon. We show key names + masked indicator only.
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit). The public contract is
// unchanged: a default-export CLASS with async mount(container, api)/unmount().
// We keep the bespoke .vault-* CSS (idempotent <style id="vault-panel-css">) and
// the same /api/vault/keys wiring, but render via html``/render(), swap the
// hand-rolled buttons for the kit button(), and replace window.confirm()/prompt()
// with the kit confirm()/openModal().

import { html, render, button, icon, confirm, openModal } from '@relaydeck/ui';

const PANEL_CSS = `
  .vault-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden}
  .vault-h{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line-1)}
  .vault-h h3{margin:0;font-size:var(--t-lg);font-weight:600}
  .vault-h .muted{font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3)}
  .vault-add{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;padding:14px 20px;border-bottom:1px solid var(--line-1);background:var(--bg-1)}
  .vault-add input{background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-1);padding:7px 10px;border-radius:4px;font:400 var(--t-sm) var(--f-mono);outline:none}
  .vault-add input:focus{border-color:var(--acc)}
  .vault-list{flex:1;overflow:auto;padding:8px 0}
  .vault-list::-webkit-scrollbar{width:6px}
  .vault-list::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}
  .vault-row{display:flex;align-items:center;gap:12px;padding:10px 20px;border-bottom:1px solid var(--line-1);font-family:var(--f-mono);font-size:var(--t-sm)}
  .vault-row:hover{background:var(--bg-1)}
  .vault-row .key{flex:1;color:var(--t-1);font-weight:500}
  .vault-row .mask{color:var(--t-3);letter-spacing:2px;flex:1}
  .vault-row .actions{display:flex;gap:6px}
  .vault-empty{padding:60px;text-align:center;color:var(--t-3);font-size:var(--t-sm)}
  .vault-empty code{display:inline-block;margin-top:8px}
  .vault-banner{padding:8px 20px;background:var(--info-bg);color:var(--info);font-size:var(--t-xs);font-family:var(--f-mono);border-bottom:1px solid var(--line-1)}
`;

export default class VaultPanel {
  constructor() {
    this.api = null;
    this.root = null;
    this.keys = [];
    this._key = '';
    this._val = '';
  }

  async mount(container, api) {
    this.api = api;
    this.root = container;
    if (!document.getElementById('vault-panel-css')) {
      const style = document.createElement('style');
      style.id = 'vault-panel-css';
      style.textContent = PANEL_CSS;
      document.head.appendChild(style);
    }
    this._paint();
    await this.refresh();
  }

  unmount() {}

  async refresh() {
    try {
      const r = await this.api.fetch('/api/vault/keys');
      const keys = await r.json();
      this.keys = keys || [];
    } catch (e) {
      console.warn('vault list failed', e);
    }
    this._paint();
  }

  _paint() {
    if (!this.root) return;
    render(this._tpl(), this.root);
  }

  _tpl() {
    return html`
      <div class="vault-wrap">
        <div class="vault-h">
          <h3>Vault</h3>
          <span class="muted">~/.relaydeck/vault.yaml · 0600</span>
        </div>
        <div class="vault-banner">
          values are held in the daemon — only key names cross the wire. use <code>\${vault:NAME}</code> in agent configs.
        </div>
        <div class="vault-add">
          <input id="v-key" placeholder="KEY (e.g. OPENROUTER_API_KEY)" autocomplete="off"
            .value=${this._key}
            @input=${(e) => { this._key = e.target.value; }}>
          <input id="v-val" type="password" placeholder="value" autocomplete="off"
            .value=${this._val}
            @input=${(e) => { this._val = e.target.value; }}
            @keydown=${(e) => { if (e.key === 'Enter') this._setSecret(); }}>
          ${button({ variant: 'primary', onClick: () => this._setSecret() }, 'Set')}
        </div>
        <div class="vault-list" id="v-list">${this._listTpl()}</div>
      </div>`;
  }

  _listTpl() {
    if (!this.keys.length) {
      return html`<div class="vault-empty">
        No secrets yet.<br><br>
        Add one above or via the CLI:<br>
        <code>relaydeck vault set OPENROUTER_API_KEY sk-...</code>
      </div>`;
    }
    return this.keys.map((k) => html`
      <div class="vault-row" data-key=${k}>
        <span class="key">${k}</span>
        <span class="mask">••••••••••••</span>
        <div class="actions">
          ${button({ variant: 'ghost', onClick: () => this._rotateSecret(k) }, '↻ Rotate')}
          ${button({ variant: 'danger', onClick: () => this._deleteSecret(k) }, '✕')}
        </div>
      </div>`);
  }

  async _setSecret() {
    const key = (this._key || '').trim();
    const val = this._val;
    if (!key) return;
    await this.api.fetch(`/api/vault/keys/${encodeURIComponent(key)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: val }),
    });
    this._key = '';
    this._val = '';
    await this.refresh();
  }

  async _deleteSecret(key) {
    const ok = await confirm({
      title: `Delete vault key “${key}”?`,
      body: 'This permanently removes the secret from the daemon vault.',
      danger: true, confirmLabel: 'Delete',
    });
    if (!ok) return;
    await this.api.fetch(`/api/vault/keys/${encodeURIComponent(key)}`, { method: 'DELETE' });
    await this.refresh();
  }

  _rotateSecret(key) {
    let next = '';
    const submit = async (close) => {
      const val = next;
      if (val == null || val === '') return;
      await this.api.fetch(`/api/vault/keys/${encodeURIComponent(key)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: val }),
      });
      close();
      await this.refresh();
    };
    openModal({
      heading: `Rotate ${key}`,
      size: 'sm',
      content: ({ close }) => html`
        <div class="rd-confirm-body" style="margin-bottom:10px">
          ${icon('shield', 13)} Set a new value for <b style="font-family:var(--f-mono)">${key}</b>.
          The old value is overwritten in the daemon vault.
        </div>
        <input class="vault-add" type="password" placeholder="new value" autocomplete="off"
          style="width:100%;box-sizing:border-box;background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-1);padding:7px 10px;border-radius:4px;font:400 var(--t-sm) var(--f-mono);outline:none"
          @input=${(e) => { next = e.target.value; }}
          @keydown=${(e) => { if (e.key === 'Enter') submit(close); }}>
        <div class="rd-modal-foot">
          ${button({ variant: 'ghost', size: 'sm', onClick: () => close() }, 'Cancel')}
          ${button({ variant: 'primary', size: 'sm', onClick: () => submit(close) }, 'Rotate')}
        </div>`,
    });
  }
}
