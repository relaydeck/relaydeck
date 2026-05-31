// Skills lens — fleet inventory with deduplicated plugin skills + sidebar nav.
//
// Migrated to the @relaydeck/ui kit (build-less, light-DOM Lit). The public
// contract is unchanged: a default-export class with sections() +
// mount(container, api, ctx)/unmount(), driven by the host's section nav
// callbacks (ctx.onSectionChange/setSection/refreshNav). Rendering moved from
// innerHTML + manual addEventListener wiring to html``/render() with @click.
// The hand-rolled esc() is dropped entirely: every dynamic value now flows
// through Lit html`` interpolations, which auto-escape (matching the old
// esc()'s purpose). The bespoke <style id="skills-panel-css"> and all .sk-*
// layout classes are kept verbatim.

import { html, render } from '@relaydeck/ui';

const SOURCE_LABEL = {
  workspace: 'Workspace',
  'runtime-plugin': 'Plugin',
  'claude-project': 'Claude · project',
  'claude-user': 'Claude · user',
  'codex-user': 'Codex',
  'codex-system': 'Codex system',
};

const VENDOR = new Set(['claude-project', 'claude-user', 'codex-user', 'codex-system']);

const CSS = `
  .sk-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden;background:var(--bg-0)}
  .sk-head{padding:18px 22px 14px;border-bottom:1px solid var(--line-1);flex-shrink:0}
  .sk-head .eyebrow{font-family:var(--f-mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .sk-head h1{margin:0;font-family:var(--f-sans);font-size:28px;font-weight:600;letter-spacing:-.02em}
  .sk-head .row{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
  .sk-head .stats{display:flex;gap:8px;flex-wrap:wrap}
  .sk-head .stat{font-family:var(--f-mono);font-size:var(--t-xxs);background:var(--bg-2);border:1px solid var(--line-2);padding:3px 9px;border-radius:var(--r-1);color:var(--t-3)}
  .sk-head .stat b{color:var(--t-1);font-weight:600}
  .sk-head .stat.bad{border-color:var(--err);color:var(--err)}
  .sk-head .actions{display:flex;gap:6px;margin-left:auto}
  .sk-head .actions button{font:inherit;background:transparent;border:1px solid var(--line-2);color:var(--t-2);border-radius:var(--r-1);padding:4px 12px;font-size:var(--t-xs);font-family:var(--f-mono);cursor:pointer}
  .sk-head .actions button:hover{color:var(--t-1);border-color:var(--line-3)}
  .sk-head .actions button.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-text);font-weight:600}

  .sk-body{flex:1;min-height:0;overflow-y:auto;padding:18px 22px}
  .sk-body::-webkit-scrollbar{width:6px}
  .sk-body::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}
  .sk-grid{display:grid;gap:14px;grid-template-columns:minmax(280px,1fr) minmax(320px,1.2fr);align-items:start}
  @media(max-width:960px){.sk-grid{grid-template-columns:1fr}}

  .sk-card{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-3);overflow:hidden}
  .sk-card .hd{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line-1);gap:8px}
  .sk-card .hd .ttl{font-family:var(--f-mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--t-3)}
  .sk-card .hd .pill{font-family:var(--f-mono);font-size:9px;background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);padding:1px 7px;border-radius:3px}
  .sk-card .bd{padding:0;min-height:0}

  .sk-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line-1);cursor:pointer;font-family:var(--f-mono);font-size:var(--t-xs)}
  .sk-row:last-child{border-bottom:0}
  .sk-row:hover{background:var(--bg-2)}
  .sk-row.on{background:var(--acc-soft);border-left:2px solid var(--acc);padding-left:12px}
  .sk-row .dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
  .sk-row .dot.ok{background:var(--ok)}
  .sk-row .dot.bad{background:var(--err)}
  .sk-row .nm{color:var(--t-1);font-weight:500;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
  .sk-row .meta{color:var(--t-4);font-size:10px;white-space:nowrap}
  .sk-row .badge{font-size:9px;background:var(--bg-2);border:1px solid var(--line-2);padding:1px 6px;border-radius:3px;color:var(--t-3)}

  .sk-detail{padding:16px 18px;font-family:var(--f-mono)}
  .sk-detail h2{margin:0 0 4px;font-size:var(--t-lg);color:var(--t-1);font-family:var(--f-sans);font-weight:600}
  .sk-detail .sub{color:var(--t-4);font-size:var(--t-xs);margin-bottom:14px;word-break:break-all}
  .sk-kv{display:grid;grid-template-columns:110px 1fr;gap:4px 12px;font-size:var(--t-xs);margin-bottom:12px}
  .sk-kv .k{color:var(--t-4)}
  .sk-kv .v{color:var(--t-2);word-break:break-all}
  .sk-kv .v.bad{color:var(--err)}
  .sk-sec{font:600 var(--t-xxs) var(--f-mono);text-transform:uppercase;letter-spacing:.05em;color:var(--t-3);margin:14px 0 8px;border-top:1px solid var(--line-1);padding-top:10px}
  .sk-pre{background:var(--bg-0);border:1px solid var(--line-1);border-radius:4px;padding:10px 12px;font-size:var(--t-xs);color:var(--t-2);white-space:pre-wrap;max-height:280px;overflow:auto;line-height:1.5}
  .sk-empty{padding:48px 24px;text-align:center;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-sm)}
  .sk-empty code{background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc);font-size:var(--t-xs)}
  .sk-hint{margin:0 14px 10px;padding:10px 12px;background:var(--bg-0);border:1px dashed var(--line-2);border-radius:var(--r-2);font-size:var(--t-xxs);color:var(--t-3);line-height:1.55}
  .sk-ws-pick{display:flex;flex-wrap:wrap;gap:6px;padding:10px 14px;border-bottom:1px solid var(--line-1)}
  .sk-ws-pick button{font:inherit;background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-2);border-radius:var(--r-1);padding:3px 10px;font-size:var(--t-xxs);font-family:var(--f-mono);cursor:pointer}
  .sk-ws-pick button.on{background:var(--acc);border-color:var(--acc);color:var(--acc-text);font-weight:600}
  .sk-tools{padding:14px;display:grid;gap:12px}
  .sk-tools .lede{font-size:var(--t-sm);color:var(--t-2);line-height:1.55}
  .sk-tools .btnrow{display:flex;gap:8px;flex-wrap:wrap}
`;

export default class SkillsPanel {
  constructor() {
    this.root = null;
    this.api = null;
    this.host = null;
    this._ctx = null;
    this._section = 'inventory';
    this.skills = [];
    this.selectedKey = null;
    this.detail = null;
    this.wsFilter = '';
    this.unsubEvent = null;
    this._msg = '';
  }

  sections() {
    const inv = this._groups(this._injectableSkills()).length;
    const wss = new Set(this.skills.map(s => s.workspace).filter(Boolean)).size;
    const vendor = this.skills.filter(s => VENDOR.has(s.source_type)).length;
    const bad = this.skills.filter(s => !s.valid).length;
    return [
      { group: 'Inventory', items: [
        { id: 'inventory', label: 'Fleet', icon: 'bolt', badge: inv || '' },
        { id: 'workspace', label: 'By workspace', icon: 'layers', badge: wss || '' },
        { id: 'vendor', label: 'Vendor', icon: 'eye', badge: vendor || '' },
      ] },
      { group: 'Tools', items: [
        { id: 'tools', label: 'Scan & validate', icon: 'cog', badge: bad || '' },
      ] },
    ];
  }

  async mount(container, api, ctx) {
    this.api = api;
    this.host = ctx?.host || null;
    this._ctx = ctx || null;
    this.root = container;
    if (ctx?.section) this._section = ctx.section;
    if (!document.getElementById('skills-panel-css')) {
      const s = document.createElement('style');
      s.id = 'skills-panel-css';
      s.textContent = CSS;
      document.head.appendChild(s);
    }
    render(html`<div class="sk-wrap"><div class="sk-body"><div class="sk-empty">loading…</div></div></div>`, this.root);
    await this._reload();
    this._subscribeLive();
    ctx?.onSectionChange?.((id) => {
      this._section = id;
      this._render();
    });
    this._render();
  }

  unmount() {
    if (this.unsubEvent) this.unsubEvent();
  }

  _injectableSkills() {
    return this.skills.filter(s => s.injectable && !VENDOR.has(s.source_type));
  }

  _groupKey(s) {
    if (s.source_type === 'runtime-plugin') return `rp:${s.owner_plugin || '?'}:${s.name}`;
    if (s.source_type === 'workspace') return `ws:${s.workspace}:${s.name}`;
    return `id:${s.id}`;
  }

  _groups(rows) {
    const map = new Map();
    for (const s of rows) {
      const k = this._groupKey(s);
      if (!map.has(k)) {
        map.set(k, { key: k, rep: s, items: [], workspaces: new Set() });
      }
      const g = map.get(k);
      g.items.push(s);
      if (s.workspace) g.workspaces.add(s.workspace);
    }
    return [...map.values()]
      .map(g => ({ ...g, workspaces: [...g.workspaces].sort() }))
      .sort((a, b) => a.rep.name.localeCompare(b.rep.name));
  }

  async _reload() {
    try {
      const r = await this.api.fetch('/api/plugins/skills/skills');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      this.skills = (await r.json()).skills || [];
      this._msg = '';
    } catch (e) {
      this._msg = `load failed: ${e.message || e}`;
    }
    this._ctx?.refreshNav?.();
    this._render();
  }

  _render() {
    if (!this.root) return;
    const inv = this._groups(this._injectableSkills());
    const bad = this.skills.filter(s => !s.valid).length;
    render(html`
      <div class="sk-wrap">
        <div class="sk-head">
          <div class="eyebrow">Cognitive layer</div>
          <h1>Skills</h1>
          <div class="row">
            <div class="stats">
              <span class="stat"><b>${inv.length}</b> unique injectable</span>
              <span class="stat"><b>${this.skills.length}</b> deployed copies</span>
              ${bad ? html`<span class="stat bad"><b>${bad}</b> invalid</span>` : ''}
            </div>
            <div class="actions">
              <button type="button" class="primary" @click=${() => this._rescan()}>Rescan</button>
            </div>
          </div>
          ${this._msg ? html`<div class="sk-hint" style="margin-top:10px">${this._msg}</div>` : ''}
        </div>
        <div class="sk-body">${this._sectionBody()}</div>
      </div>`, this.root);
  }

  _sectionBody() {
    if (this._section === 'workspace') return this._tmplWorkspace();
    if (this._section === 'vendor') return this._tmplVendor();
    if (this._section === 'tools') return this._tmplTools();
    return this._tmplInventory();
  }

  _tmplInventory() {
    const groups = this._groups(this._injectableSkills());
    if (!groups.length) {
      return html`<div class="sk-empty">No injectable skills yet.<br><br>
        Enable <code>messaging</code> on a workspace for <code>relaydeck-cli</code>,
        or add a <code>SKILL.md</code> under <code>workspaces/&lt;ws&gt;/skills/</code>.</div>`;
    }
    return html`<div class="sk-grid">
      <div class="sk-card"><div class="hd"><span class="ttl">Fleet inventory</span><span class="pill">deduped</span></div>
        <div class="bd">${groups.map(g => this._rowTmpl(g))}</div></div>
      <div class="sk-card"><div class="hd"><span class="ttl">Detail</span></div>
        <div class="bd">${this._detailTmpl()}</div></div></div>`;
  }

  _tmplWorkspace() {
    const wss = [...new Set(this.skills.map(s => s.workspace).filter(Boolean))].sort();
    const cur = this.wsFilter || wss[0] || '';
    const rows = cur
      ? this.skills.filter(s => s.workspace === cur && s.injectable)
      : [];
    const groups = this._groups(rows);
    const picks = wss.map(w => html`<button type="button" data-ws=${w}
      class=${w === cur ? 'on' : ''}
      @click=${() => { this.wsFilter = w; this.selectedKey = null; this.detail = null; this._render(); }}>${w}</button>`);
    const list = groups.length
      ? groups.map(g => this._rowTmpl(g, false))
      : html`<div class="sk-empty">No skills in this workspace.</div>`;
    return html`<div class="sk-grid">
      <div class="sk-card"><div class="hd"><span class="ttl">Workspace</span></div>
        <div class="sk-ws-pick">${picks.length ? picks : html`<span class="sk-empty" style="padding:8px">No workspaces</span>`}</div>
        <div class="bd">${list}</div></div>
      <div class="sk-card"><div class="hd"><span class="ttl">Detail</span></div>
        <div class="bd">${this._detailTmpl()}</div></div></div>`;
  }

  _tmplVendor() {
    const rows = this.skills.filter(s => VENDOR.has(s.source_type));
    const groups = this._groups(rows);
    if (!groups.length) {
      return html`<div class="sk-empty">No Claude Code or Codex skills discovered.<br>
        These are read-only — relaydeck shows what those harnesses load natively.</div>`;
    }
    return html`<div class="sk-grid">
      <div class="sk-card"><div class="hd"><span class="ttl">Vendor inventory</span><span class="pill">read-only</span></div>
        <div class="bd">${groups.map(g => this._rowTmpl(g))}</div></div>
      <div class="sk-card"><div class="hd"><span class="ttl">Detail</span></div>
        <div class="bd">${this._detailTmpl()}</div></div></div>`;
  }

  _tmplTools() {
    const bad = this.skills.filter(s => !s.valid);
    return html`<div class="sk-card" style="max-width:640px"><div class="hd"><span class="ttl">Maintenance</span></div>
      <div class="sk-tools">
        <div class="lede">Rescan walks every workspace and refreshes the cache mirror.
          Validate checks every <code>SKILL.md</code> on disk — invalid skills are skipped at injection time.</div>
        <div class="btnrow">
          <button type="button" @click=${() => this._validate()}>Validate all</button>
          <button type="button" class="primary" @click=${() => this._rescan()}>Rescan fleet</button>
        </div>
        ${bad.length ? html`<div class="sk-sec">Invalid (${bad.length})</div>
          ${bad.map(s => html`<div class="sk-row" style="cursor:default"><span class="dot bad"></span>
            <span class="nm">${s.name}</span><span class="meta">${s.workspace || s.source_type}</span></div>`)}` : ''}
      </div></div>`;
  }

  _rowTmpl(g, showWs = true) {
    const ws = g.workspaces;
    let meta = '';
    if (g.rep.source_type === 'runtime-plugin' && ws.length > 1) {
      meta = html`<span class="badge">${ws.length} workspaces</span>`;
    } else if (showWs && ws.length === 1) {
      meta = html`<span class="meta">${ws[0]}</span>`;
    } else if (g.rep.source_type === 'runtime-plugin') {
      meta = html`<span class="meta">${g.rep.owner_plugin || 'plugin'}</span>`;
    }
    return html`<div class="sk-row ${g.key === this.selectedKey ? 'on' : ''}" data-key=${g.key}
      @click=${() => this._select(g.key)}>
      <span class="dot ${g.rep.valid ? 'ok' : 'bad'}"></span>
      <span class="nm">${g.rep.name}</span>${meta}</div>`;
  }

  _detailTmpl() {
    if (!this.selectedKey) return html`<div class="sk-detail sk-empty">Select a skill.</div>`;
    const g = this._groups(this.skills).find(x => x.key === this.selectedKey);
    if (!g) return html`<div class="sk-detail sk-empty">Not found.</div>`;
    const d = this.detail || g.rep;
    const ro = !d.injectable ? html`<span class="badge">read-only</span>` : '';
    const kv = (k, v, cls = '') => html`<div class="k">${k}</div><div class="v ${cls}">${v}</div>`;
    const rows = [
      kv('source', SOURCE_LABEL[d.source_type] || d.source_type),
      kv('owner', d.owner_plugin || '—'),
      kv('valid', d.valid ? 'yes' : 'NO', d.valid ? '' : 'bad'),
    ];
    if (g.workspaces.length) {
      rows.push(kv('workspaces', g.workspaces.length > 1 ? `${g.workspaces.length}: ${g.workspaces.join(', ')}` : g.workspaces[0]));
    }
    if (g.items.length > 1) {
      rows.push(kv('deployments', `${g.items.length} copies (one per workspace)`));
    }
    rows.push(kv('path', d.path || '—'));
    return html`<div class="sk-detail">
      <h2>${d.name} ${ro}</h2>
      <div class="sub">${d.description || '—'}</div>
      <div class="sk-kv">${rows}</div>
      ${(d.errors || []).length ? html`<div class="sk-sec">Errors</div>
        ${d.errors.map(e => html`<div class="sk-pre bad">${e}</div>`)}` : ''}
      ${d.body_preview ? html`<div class="sk-sec">SKILL.md</div><div class="sk-pre">${d.body_preview}</div>`
        : d.injectable ? html`<div class="sk-sec">Injection</div><div class="lede" style="font-size:var(--t-xs);color:var(--t-3)">
            Restart agents after edits — pi uses <code>--skill</code>, codex paths, claude inlines runtime skills.</div>` : ''}
    </div>`;
  }

  async _select(key) {
    this.selectedKey = key;
    const g = this._groups(this.skills).find(x => x.key === key);
    if (!g) return;
    this.detail = g.rep;
    this._render();
    try {
      const r = await this.api.fetch(`/api/plugins/skills/skills/${encodeURIComponent(g.rep.id)}`);
      const data = await r.json();
      if (!data.error) { this.detail = data; this._render(); }
    } catch (_) {}
  }

  async _rescan() {
    this._msg = 'rescanning…';
    this._render();
    try {
      await this.api.fetch('/api/plugins/skills/rescan', { method: 'POST' });
      this._msg = '';
    } catch (e) {
      this._msg = String(e.message || e);
    }
    await this._reload();
  }

  async _validate() {
    try {
      const r = await this.api.fetch('/api/plugins/skills/validate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const res = await r.json();
      this._msg = res.ok ? `all ${res.total} valid` : `${res.invalid.length} invalid`;
      await this._reload();
      if (!res.ok) { this._section = 'tools'; this._ctx?.setSection?.('tools'); }
    } catch (e) {
      this._msg = String(e.message || e);
      this._render();
    }
  }

  _subscribeLive() {
    if (!this.api || typeof this.api.onEvent !== 'function') return;
    this.unsubEvent = this.api.onEvent(ev => {
      const t = ev && (ev.type || ev.event);
      if (typeof t === 'string' && (t.startsWith('skills.') || t === 'workspace.updated')) {
        this._reload();
      }
    });
  }
}
