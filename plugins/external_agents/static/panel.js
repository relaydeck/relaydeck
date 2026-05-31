// External Agents lens — contributed by the `external` plugin.
//
// Read-only control-plane view over Hermes / OpenClaw runtimes relaydeck does
// NOT run. Sections, top to bottom:
//   1. Header: title + counts (total / by kind / cli-available).
//   2. Add by path: paste a repo or config home → detect + register.
//   3. Detected on this machine: auto-scanned candidates not yet added.
//   4. Registered agents: one card each — kind/transport badges, root,
//      health probes, risk labels, detection signals; Health + Launch + Remove.
//
// All mutations round-trip through /api/plugins/external/* (the same surface
// the CLI uses). Live: subscribes to the SSE stream and reloads on
// external_agent.* (and workspace.* in case a scan root changed).
//
// MIGRATED to the @relaydeck/ui kit: still the framework-neutral default-export
// CLASS with mount(container, api, ctx)/unmount(), but the body now renders Lit
// templates into the container (no innerHTML / no querySelector wiring / no
// hand-rolled esc), and destructive Remove uses the kit confirm() dialog. The
// bespoke .ea-* CSS + class names are preserved verbatim, injected once via
// <style id="ea-panel-css">.
//
// Scroll discipline (learned the hard way on the telegram lens): the scroll
// container .ea-body is a plain block; never make a flex:1 scroll container
// display:grid or its rows collapse.

import { html, render, esc, confirm } from '@relaydeck/ui';

const _ago = (iso) => {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  const d = Math.max(0, (Date.now() - t) / 1000);
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
};

const HEALTH_OK = new Set(['ok', 'available', 'reachable', 'installed']);
const HEALTH_BAD = new Set(['missing', 'error', 'timeout', 'closed']);
const healthClass = v => HEALTH_OK.has(v) ? 'ok' : (HEALTH_BAD.has(v) ? 'bad' : 'warn');

const STYLE_ID = 'ea-panel-css';
const CSS = `
  .ea-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden;background:var(--bg-0)}
  .ea-head{padding:18px 22px 14px;border-bottom:1px solid var(--line-1);flex:0 0 auto}
  .ea-head .eyebrow{font-family:var(--f-mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .ea-head h1{margin:0;font-family:var(--f-sans);font-size:30px;font-weight:600;letter-spacing:-.025em;line-height:1.05}
  .ea-head .chips{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  .ea-chip{font-family:var(--f-mono);font-size:var(--t-xxs);background:var(--bg-2);border:1px solid var(--line-2);padding:2px 8px;border-radius:var(--r-1);color:var(--t-2)}
  .ea-chip b{color:var(--t-1)}

  .ea-body{flex:1;min-height:0;overflow-y:auto;padding:18px 22px;display:flex;flex-direction:column;gap:16px}
  .ea-body::-webkit-scrollbar{width:6px}
  .ea-body::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}

  .ea-section{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-3);overflow:hidden}
  .ea-section>.head{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line-1);gap:10px}
  .ea-section>.head .ttl{font-family:var(--f-mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-3)}
  .ea-section>.head .pill{background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);padding:1px 6px;border-radius:3px;font-size:9px}
  .ea-section>.body{padding:12px 14px}

  .ea-add{display:flex;gap:8px;align-items:center}
  .ea-add input{flex:1;font-family:var(--f-mono);font-size:var(--t-xs);background:var(--bg-2);border:1px solid var(--line-2);border-radius:var(--r-1);padding:6px 10px;color:var(--t-1)}
  .ea-add button{font:inherit;font-family:var(--f-mono);font-size:var(--t-xs);background:var(--acc);border:1px solid var(--acc);color:var(--acc-text);font-weight:600;border-radius:var(--r-1);padding:6px 14px;cursor:pointer}
  .ea-add button:disabled{opacity:.5;cursor:not-allowed}
  .ea-add .status{font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3);margin-top:8px;min-height:14px}
  .ea-add .status.ok{color:var(--ok)} .ea-add .status.err{color:var(--err)}
  .ea-hint{font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-4);margin-top:8px;line-height:1.6}
  .ea-hint code{background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)}

  .ea-cand{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line-1);font-family:var(--f-mono);font-size:var(--t-xs)}
  .ea-cand:last-child{border-bottom:0}
  .ea-cand .root{flex:1;min-width:0;color:var(--t-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ea-cand button{font:inherit;font-size:10px;background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);border-radius:3px;padding:2px 9px;cursor:pointer}

  .ea-card{border:1px solid var(--line-2);border-radius:var(--r-3);background:var(--bg-1);overflow:hidden}
  .ea-card>.top{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line-1)}
  .ea-card .name{font-family:var(--f-sans);font-size:var(--t-md);font-weight:600;color:var(--t-1)}
  .ea-card .badge{font-family:var(--f-mono);font-size:9px;text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;border-radius:3px;border:1px solid var(--line-2);color:var(--t-3)}
  .ea-card .badge.hermes{color:var(--acc);background:var(--acc-soft);border-color:var(--acc-line)}
  .ea-card .badge.openclaw{color:#f59e0b;background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.3)}
  .ea-card .sp{flex:1}
  .ea-card .acts button{font:inherit;font-family:var(--f-mono);font-size:10px;background:transparent;border:1px solid var(--line-2);color:var(--t-2);border-radius:3px;padding:3px 9px;cursor:pointer;margin-left:5px}
  .ea-card .acts button:hover{color:var(--t-1);border-color:var(--line-3)}
  .ea-card .acts button.danger:hover{color:var(--err);border-color:var(--err)}
  .ea-card>.meta{padding:10px 14px;display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-family:var(--f-mono);font-size:var(--t-xxs)}
  .ea-card>.meta .k{color:var(--t-4);text-transform:uppercase;letter-spacing:.05em}
  .ea-card>.meta .v{color:var(--t-2);min-width:0;overflow:hidden;text-overflow:ellipsis;word-break:break-all}
  .ea-card .health{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 12px}
  .ea-hb{font-family:var(--f-mono);font-size:10px;padding:2px 7px;border-radius:3px;border:1px solid var(--line-2);color:var(--t-3)}
  .ea-hb .v.ok{color:var(--ok)} .ea-hb .v.bad{color:var(--err)} .ea-hb .v.warn{color:var(--warn)}
  .ea-risk{display:flex;flex-wrap:wrap;gap:5px;padding:0 14px 12px}
  .ea-risk .r{font-family:var(--f-mono);font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:var(--t-4);background:var(--bg-2);border:1px solid var(--line-2);padding:2px 6px;border-radius:3px}
  .ea-risk .r.warn{color:var(--warn);border-color:rgba(251,191,36,.3)}

  .ea-empty{padding:28px;text-align:center;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs)}
  .ea-empty code{background:var(--bg-2);border:1px solid var(--line-2);padding:1px 5px;border-radius:3px;color:var(--acc)}
`;

// Risk labels we want to visually flag as "pay attention".
const WARN_RISK = new Set(['secrets-file-present', 'in-process-plugins', 'gateway-configured', 'remote-channel', 'cli-missing']);

export default class ExternalAgentsLens {
  constructor() { this.agents = []; this.candidates = []; this.workspaces = []; this._sub = null; }

  async mount(container, api, ctx) {
    this.api = api;
    this.ctx = ctx;
    this.root = container;
    if (!document.getElementById(STYLE_ID)) {
      const s = document.createElement('style');
      s.id = STYLE_ID; s.textContent = CSS; document.head.appendChild(s);
    }
    await this._reload();
    this._sub = api.events.subscribe((e) => {
      const t = (e && e.type) || '';
      if (t.startsWith('external_agent.') || t.startsWith('workspace.') ||
          t === 'system.plugin.loaded') {
        this._reload();
      }
    });
  }

  unmount() { this._sub?.(); this._sub = null; }

  async _reload() {
    try {
      const data = await this.api.getJSON('/api/plugins/external/agents');
      this.agents = (data && data.agents) || [];
      this.candidates = (data && data.candidates) || [];
    } catch (e) {
      this.agents = []; this.candidates = [];
      this._error = e.message;
    }
    // Workspaces back the "Launch in workspace" picker. Best-effort — a
    // failure just leaves the picker empty (the launch button disables).
    try {
      const ws = await this.api.getJSON('/api/workspaces');
      this.workspaces = (Array.isArray(ws) ? ws : (ws && ws.workspaces) || [])
        .map(w => (typeof w === 'string' ? w : w.name)).filter(Boolean);
    } catch (e) { /* keep prior list */ }
    this._render();
  }

  // ── render ────────────────────────────────────────────────────
  _render() {
    if (!this.root) return;
    render(html`
      <div class="ea-wrap">
        <div class="ea-head">${this._head()}</div>
        <div class="ea-body">
          ${this._addCard()}
          ${this.candidates.length ? this._candidatesCard() : ''}
          ${this._agentsCard()}
        </div>
      </div>`, this.root);
  }

  _head() {
    const n = this.agents.length;
    const hermes = this.agents.filter(a => a.kind === 'hermes').length;
    const openclaw = this.agents.filter(a => a.kind === 'openclaw').length;
    const cliOk = this.agents.filter(a => ((a.last_probe || {}).health || {}).cli &&
      HEALTH_OK.has((a.last_probe.health || {}).cli)).length;
    return html`
      <div class="eyebrow">control plane · read-only</div>
      <h1>External Agents</h1>
      <div class="chips">
        <span class="ea-chip"><b>${n}</b> registered</span>
        <span class="ea-chip">hermes <b>${hermes}</b></span>
        <span class="ea-chip">openclaw <b>${openclaw}</b></span>
        <span class="ea-chip">cli ok <b>${cliOk}</b></span>
        ${this.candidates.length ? html`<span class="ea-chip">detected <b>${this.candidates.length}</b></span>` : ''}
      </div>`;
  }

  // ── Add by path ───────────────────────────────────────────────
  _addCard() {
    let pathVal = '';
    const setStatus = (text, cls) => {
      const el = this.root?.querySelector('[data-ea-add-status]');
      if (el) { el.textContent = text; el.className = 'status' + (cls ? ' ' + cls : ''); }
    };
    const setBtn = (disabled) => {
      const el = this.root?.querySelector('[data-ea-add-btn]');
      if (el) el.disabled = disabled;
    };
    const submit = async () => {
      const path = pathVal.trim();
      if (!path) { setStatus('enter a path', 'err'); return; }
      setBtn(true); setStatus('detecting…', '');
      try {
        const res = await this.api.postJSON('/api/plugins/external/agents',
          { path, kind: 'auto', probe: true });
        setStatus(`✓ added ${res.id} (${res.kind})`, 'ok');
        await this._reload();
      } catch (e) {
        setStatus('✗ ' + (e.message || 'add failed'), 'err');
        setBtn(false);
      }
    };
    return html`
      <div class="ea-section">
        <div class="head"><span class="ttl">Add an external agent</span></div>
        <div class="body">
          <div class="ea-add">
            <input type="text" placeholder="/path/to/hermes-agent  or  ~/.openclaw"
              spellcheck="false" autocomplete="off"
              @input=${(e) => { pathVal = e.target.value; }}
              @keydown=${(e) => { if (e.key === 'Enter') submit(); }}>
            <button data-ea-add-btn @click=${submit}>Detect &amp; add</button>
          </div>
          <div class="status" data-ea-add-status></div>
          <div class="ea-hint">
            relaydeck detects Hermes / OpenClaw from the folder (no command is run, no
            onboarding). It never mutates the runtime or reads its secrets — at
            most it notes that an <code>.env</code> exists. CLI parity:
            <code>relaydeck external add &lt;path&gt;</code>.
          </div>
        </div>
      </div>`;
  }

  // ── Detected candidates ───────────────────────────────────────
  _candidatesCard() {
    return html`
      <div class="ea-section">
        <div class="head">
          <span class="ttl">Detected on this machine</span>
          <span class="pill">${this.candidates.length}</span>
        </div>
        <div class="body">
          ${this.candidates.map((c) => this._candRow(c))}
        </div>
      </div>`;
  }

  _candRow(c) {
    const add = async (ev) => {
      ev.target.disabled = true;
      try {
        await this.api.postJSON('/api/plugins/external/agents',
          { path: c.root, kind: c.kind, probe: true });
        await this._reload();
      } catch (e) { ev.target.textContent = '✗'; }
    };
    return html`
      <div class="ea-cand">
        <span class="badge ${c.kind}">${c.kind}</span>
        <span class="root" title=${esc(c.root)}>${c.root}</span>
        <span style="color:var(--t-4)">conf ${Number(c.confidence).toFixed(2)}</span>
        <button @click=${add}>+ add</button>
      </div>`;
  }

  // ── Registered agents ─────────────────────────────────────────
  _agentsCard() {
    return html`
      <div class="ea-section">
        <div class="head">
          <span class="ttl">Registered</span>
          <span class="pill">${this.agents.length}</span>
        </div>
        <div class="body" style=${this.agents.length ? 'display:flex;flex-direction:column;gap:12px' : ''}>
          ${this.agents.length
            ? this.agents.map((a) => this._agentCard(a))
            : html`<div class="ea-empty">No external agents yet. Add a Hermes
                or OpenClaw checkout / config home above, or with
                <code>relaydeck external add &lt;path&gt;</code>.</div>`}
        </div>
      </div>`;
  }

  _agentCard(a) {
    const probe = a.last_probe || {};
    const health = probe.health || {};
    const risk = probe.risk || [];
    const hb = Object.entries(health).map(([k, v]) =>
      html`<span class="ea-hb">${k} <span class="v ${healthClass(v)}">${v}</span></span>`);
    const risks = risk.map(r =>
      html`<span class="r ${WARN_RISK.has(r) ? 'warn' : ''}">${r}</span>`);

    const onHealth = async (ev) => {
      ev.target.textContent = '…'; ev.target.disabled = true;
      try {
        await this.api.postJSON(`/api/plugins/external/agents/${encodeURIComponent(a.id)}/probe`, {});
        await this._reload();
      } catch (e) { ev.target.textContent = 'Health'; ev.target.disabled = false; }
    };

    const onRemove = async () => {
      const ok = await confirm({
        title: `Remove external agent "${a.id}"?`,
        body: `This only unregisters it from relaydeck — the ${a.kind} runtime is untouched.`,
        danger: true, confirmLabel: 'Remove',
      });
      if (!ok) return;
      try {
        await this.api.deleteJSON(`/api/plugins/external/agents/${encodeURIComponent(a.id)}`);
        await this._reload();
      } catch (e) { alert('Remove failed: ' + e.message); }
    };

    // Launch picker state is read straight off the DOM at click time so the
    // template stays declarative and reloads don't clobber a chosen workspace.
    let selectedWs = this.workspaces[0] || '';
    const onLaunch = async (ev) => {
      const btn = ev.target;
      const card = btn.closest('.ea-card');
      const sel = card?.querySelector('[data-f="ws"]');
      const status = card?.querySelector('[data-f="status"]');
      const ws = sel ? sel.value : selectedWs;
      if (!ws) return;
      btn.disabled = true;
      if (status) { status.textContent = 'launching…'; status.style.color = 'var(--t-3)'; }
      try {
        const r = await this.api.postJSON(
          `/api/plugins/external/agents/${encodeURIComponent(a.id)}/spawn`,
          { workspace: ws, start: true });
        if (status) {
          render(html`✓ <a href="?lens=agents&agent=${encodeURIComponent(r.agent_id)}"
            style="color:var(--acc)">open ${r.agent_id}</a>`, status);
        }
      } catch (e) {
        if (status) { status.textContent = 'failed: ' + e.message; status.style.color = 'var(--err)'; }
        btn.disabled = false;
      }
    };

    return html`
      <div class="ea-card">
        <div class="top">
          <span class="badge ${a.kind}">${a.kind}</span>
          <span class="name">${a.name || a.id}</span>
          <span class="badge">${a.transport}</span>
          <span class="sp"></span>
          <span class="acts">
            <button @click=${onHealth}>Health</button>
            <button class="danger" @click=${onRemove}>Remove</button>
          </span>
        </div>
        <div class="meta">
          <span class="k">root</span><span class="v" title=${esc(a.root)}>${a.root}</span>
          ${a.config_home ? html`<span class="k">config</span><span class="v">${a.config_home}</span>` : ''}
          ${a.workspace ? html`<span class="k">workspace</span><span class="v">${a.workspace}</span>` : ''}
          <span class="k">checked</span><span class="v">${probe.checked_at ? _ago(probe.checked_at) : 'never — click Health'}</span>
        </div>
        ${hb.length ? html`<div class="health">${hb}</div>` : ''}
        ${risks.length ? html`<div class="ea-risk">${risks}</div>` : ''}
        <div class="ea-launch" style="display:flex;align-items:center;gap:8px;margin-top:10px;border-top:1px solid var(--line-1);padding-top:10px;flex-wrap:wrap">
          <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-3)">Launch ${a.kind} chat in</span>
          <select data-f="ws" @change=${(e) => { selectedWs = e.target.value; }}
            style="font:inherit;font-family:var(--f-mono);font-size:10px;background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-1);border-radius:3px;padding:3px 6px">
            ${this.workspaces.length
              ? this.workspaces.map(w => html`<option>${w}</option>`)
              : html`<option value="">no workspaces</option>`}
          </select>
          <button data-act="launch" ?disabled=${!this.workspaces.length} @click=${onLaunch}
            style="font:inherit;font-family:var(--f-mono);font-size:10px;background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);border-radius:3px;padding:3px 10px;cursor:pointer">Launch</button>
          <span data-f="status" style="font-family:var(--f-mono);font-size:10px;color:var(--t-3)"></span>
        </div>
      </div>`;
  }
}
