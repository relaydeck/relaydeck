// context.js — Context tile for Relaydeck-native agents.
// Shows EXACTLY what Relaydeck injects into a session: each prompt layer
// (contract / soul / workspace / capabilities / skills / policy / history),
// flagged protected vs editable. This is the transparency surface the
// issue calls out as the key differentiator — what you see is what the
// model saw.
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit). The shell is
// rendered once with the kit's html/render into the host container; the
// pi-missing banner ([data-pi-banner]) is a STATIC anchor node we hand to
// ./pi_install.js — pi_install owns that node's innerHTML/display, so the
// reactive layer never touches it (same rule the terminal tile follows). The
// chips + layered <details> list are re-rendered into their anchors on load.

import { html, render, chip, button, esc } from '@relaydeck/ui';

export default class RelaydeckContextTile {
  async mount(container, api, ctx) {
    this.api = api;
    this.agent = ctx.agent;
    this.container = container;

    // One-time shell. [data-pi-banner] and [data-body] are stable anchors that
    // pi_install.js / _renderLayers own imperatively after this paint.
    render(html`
      <div class="card" style="flex:1;overflow:auto;padding:0;min-height:0">
        <div style="padding:10px 14px;border-bottom:1px solid var(--line-1);display:flex;justify-content:space-between;align-items:center">
          <span class="card-title">Injected context</span>
          <span style="display:flex;gap:8px;align-items:center">
            <span data-engine></span>
            <span data-model></span>
            ${button({ variant: 'ghost', size: 'sm', onClick: () => this._load() }, 'refresh')}
          </span>
        </div>
        <div data-pi-banner style="display:none;margin:10px 14px 0"></div>
        <div data-body style="padding:10px 14px;display:flex;flex-direction:column;gap:8px"></div>
      </div>`, container);

    this._renderBody(html`loading…`);
    await this._load();
  }

  _renderBody(tpl) {
    const body = this.container?.querySelector('[data-body]');
    if (body) render(tpl, body);
  }

  _renderChip(sel, tpl) {
    const node = this.container?.querySelector(sel);
    if (node) render(tpl ?? html``, node);
  }

  _ensurePiWidget(piMissing) {
    const banner = this.container?.querySelector('[data-pi-banner]');
    if (!banner) return;
    if (piMissing && !this._piHandle) {
      banner.style.display = 'block';
      import('./pi_install.js').then((mod) => {
        if (!this.container?.isConnected) return;
        this._piHandle = mod.mountPiInstall({
          container: banner,
          api: this.api,
          compact: true,
          onReady: () => { this._load(); /* refresh the layered view */ },
        });
      }).catch(() => {});
    } else if (!piMissing) {
      try { this._piHandle?.unmount?.(); } catch (_) {}
      this._piHandle = null;
      banner.style.display = 'none';
      // Clear via lit (not innerHTML='') so pi_install's cached ChildPart on
      // this node stays consistent and a later re-mount reconciles correctly.
      render(html``, banner);
    }
  }

  async _load() {
    try {
      const r = await this.api.getJSON(`/api/plugins/relaydeck-native/${encodeURIComponent(this.agent.id)}/context`);
      this._renderChip('[data-model]', chip('model ' + (r.model || '?'), 'accent'));
      this._ensurePiWidget(r.pi_installed === false);
      if (r.pi_installed === false) {
        this._renderChip('[data-engine]', chip('pi missing', 'err'));
      } else {
        this._renderChip('[data-engine]', chip(r.pi_version ? `pi ${r.pi_version}` : 'pi ok', 'ok'));
      }
      const layers = (r && r.layers) || [];
      if (!layers.length) {
        this._renderBody(html`<div style="color:var(--t-4)">no context</div>`);
        return;
      }
      this._renderBody(html`${layers.map((l) => html`
        <details ?open=${l.id !== 'contract'} style="border:1px solid var(--line-2);border-radius:var(--r-2)">
          <summary style="cursor:pointer;padding:6px 10px;display:flex;gap:8px;align-items:center;font-family:var(--f-mono);font-size:var(--t-xs)">
            ${l.title}
            ${l.editable ? chip('editable', 'ok') : chip('protected', 'muted')}
          </summary>
          <pre style="margin:0;padding:8px 12px;border-top:1px solid var(--line-1);white-space:pre-wrap;word-break:break-word;font-family:var(--f-mono);font-size:11px;color:var(--t-2)">${l.body}</pre>
        </details>`)}`);
    } catch (e) {
      this._renderBody(html`<div style="color:var(--err);font-family:var(--f-mono);font-size:var(--t-xs)">failed: ${esc(e.message)}</div>`);
    }
  }

  unmount() {
    try { this._piHandle?.unmount?.(); } catch (_) {}
    this._piHandle = null;
  }
}
