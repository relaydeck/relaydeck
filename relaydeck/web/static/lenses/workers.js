// lenses/workers.js — unified Workers lens (configurable + system) with a
// live LLM stream. Recreated from the Claude Design handoff (workers.css
// .cwk-*/.wk-side-* classes).
//
// Configurable workers (loop agents): countdown-ring header, 5-up stat
// strip, an action PIPELINE, and a live LLM STREAM that shows the actual
// prompt + response as bubbles, plus run history (duration bars) +
// activity. System workers (infra threads): simpler log + config view.
// Backed by /api/automations(+/runs,/invocations,/run,/pause,/resume),
// /api/agents/{id} (full config for the pipeline), /api/agents/{id}/events,
// and /api/workers.
//
// MIGRATION NOTE — @relaydeck/ui kit + terminal/countdown safety. The SIDEBAR
// is now a reactive RelayLens template (sidebar()): it reads the live store via
// this.use('/api/automations') + this.use('/api/workers') so loop ticks / new
// runs repaint the list without a full app render (lit-html diffs keep the
// search input focused). The DETAIL stays IMPERATIVE — renderDetail() is
// overridden and rebuilt only on an explicit selection / control action, never
// on a live push, exactly like lenses/agents.js. Per-detail data (runs /
// invocations / events / config) is fetched ONCE on selection into instance
// fields; the countdown ring runs on a local setInterval that touches ONLY the
// ring / chip / stat nodes imperatively — it never re-renders the lens and never
// refetches. Every timer / subscription is registered for teardown.

import {
  RelayLens, html, nothing, render, live,
  icon, button, sideHead, sideSearch, empty,
  esc, iconSVG, fmtNum, relTime,
} from '@relaydeck/ui';
import { openWorkerForm } from '../worker_form.js';

const STATUS_COLOR = {
  succeeded: 'var(--ok)', partial: 'var(--warn)', failed: 'var(--err)',
  running: 'var(--acc)',
};

function fmtDur(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return ms + 'ms';
  return (ms / 1000).toFixed(1) + 's';
}

function fmtDT(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtClock(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtCountdown(secs) {
  if (secs <= 0) return 'now';
  if (secs >= 3600) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  if (secs >= 60) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${secs}s`;
}

function triggerLabel(t) {
  if (!t) return 'manual';
  if (t.kind === 'interval') return `every ${fmtCountdown(Math.round(t.value || 0))}`;
  if (t.kind === 'cron') return `cron ${(t.raw || '').split(':').slice(1).join(':')}`;
  if (t.kind === 'on_event') return `on ${t.value}`;
  if (t.kind === 'invalid') return `invalid: ${t.raw}`;
  return String(t.raw || t.kind);
}

export class WorkersLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'workers' });
    this.search = '';
    this.activeKey = null;   // "cfg:<id>" | "infra:<id>"
    // Live-store-fed collections (read in sidebar() via this.use()).
    this.configurable = [];
    this.infra = [];
    // Live: react to automation / worker churn. The store pushes into
    // this.use() (sidebar repaint); we additionally re-fetch the OPEN detail's
    // per-worker data so an active pane stays fresh — never on a countdown tick.
    this.onEvent((evt) => {
      if (/loop|automation|worker/.test(evt?.type || '')) this._refreshOpenDetail();
    });
  }

  _scopedInfra() { return this.host.scopedWorkers(this.infra); }

  // Pull the live-store collections into instance fields. Reading via use()
  // subscribes once (auto-released on unmount) and re-renders the sidebar on
  // every push; we mirror the values onto this.configurable/this.infra so the
  // imperative detail code keeps its plain-array view of the world.
  _syncCollections() {
    // No fallback → undefined until the live store's first push, so we can tell
    // "not loaded yet" from "loaded but empty" and render both sections at once
    // (the two subscriptions resolve independently otherwise).
    const autos = this.use('/api/automations');
    const workers = this.use('/api/workers');
    this._loaded = autos !== undefined && workers !== undefined;
    this.configurable = (autos || {}).automations || [];
    this.infra = workers || [];
    if (!this.activeKey) {
      if (this.configurable.length) this.activeKey = 'cfg:' + this.configurable[0].automation_id;
      else if (this.infra.length) this.activeKey = 'infra:' + (this.infra[0].id || this.infra[0].name);
      // Collections can arrive AFTER the detail first mounted (the reactive
      // sidebar path repaints only the sidebar). The detail pane is imperative,
      // so schedule a one-shot rebuild here when we auto-select a worker, else
      // it stays on the "No workers" empty state until the next event/click.
      // Cancelled if _renderActiveDetail runs synchronously in the meantime.
      if (this.activeKey && this._detailRoot && !this._autoDetailScheduled) {
        this._autoDetailScheduled = true;
        queueMicrotask(() => {
          if (this._autoDetailScheduled && !this._dead) { this._autoDetailScheduled = false; this._renderActiveDetail(); }
        });
      }
    }
  }

  // ── Sidebar (reactive lit-html via RelayLens) ───────────────────────
  sidebar() {
    this._syncCollections();
    const q = this.search.toLowerCase();
    let cfg = this.configurable;
    let infra = this._scopedInfra();
    if (q) {
      cfg = cfg.filter((a) => (a.automation_id || '').toLowerCase().includes(q));
      infra = infra.filter((w) => (w.id || w.name || '').toLowerCase().includes(q));
    }
    const total = this.configurable.length + this._scopedInfra().length;
    return html`
      ${sideHead(html`Workers`, {
        count: total,
        actions: html`<button class="btn icon sm" title="New worker" data-new
          @click=${() => this._newWorker()}>${icon('plus', 13)}</button>`,
      })}
      ${sideSearch(this.search, (v) => { this.search = v; this.requestUpdate(); }, 'Search workers…')}
      <div class="side-list">
        ${!this._loaded
          ? html`<div style="padding:20px 14px;color:var(--t-4);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">loading…</div>`
          : (cfg.length === 0 && infra.length === 0)
          ? html`<div style="padding:20px 14px;color:var(--t-4);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">No workers${
              q ? html` match "${esc(q)}"` : '.'}<br><br>Hit <b>+</b> to create one.</div>`
          : html`
            ${cfg.length ? html`
              <div class="wk-side-section">Configurable · ${cfg.length}</div>
              ${cfg.map((a) => this._cfgRow(a))}` : nothing}
            ${infra.length ? html`
              <div class="wk-side-section" style="margin-top:10px">System · ${infra.length}</div>
              ${infra.map((w) => this._infraRow(w))}` : nothing}`}
      </div>`;
  }

  _newWorker() {
    openWorkerForm(this.host, {
      onSaved: (id) => {
        this.activeKey = 'cfg:' + id;
        live.invalidate('/api/automations');   // show the new worker immediately
        this.host.render();
      },
    });
  }

  _select(key) {
    this.activeKey = key;
    this.requestUpdate();        // sidebar .sel highlight (reactive)
    this._renderActiveDetail();  // detail is imperative — rebuild explicitly
  }

  _cfgRow(a) {
    const key = 'cfg:' + a.automation_id;
    const running = a.agent_status === 'running';
    const actions = (a.action_kinds || []).join(', ') || 'no actions';
    return html`
      <div class="wk-side-row cfg ${key === this.activeKey ? 'sel' : ''}" @click=${() => this._select(key)}>
        <div class="av">${icon('clock', 13)}<span class="ind ${running ? 'running' : 'idle'}"></span></div>
        <div class="info">
          <div class="name truncate">${a.name || a.automation_id}</div>
          <div class="sub truncate">${triggerLabel(a.trigger)} · ${actions}</div>
        </div>
        <div class="right">
          ${running ? html`<span class="live-pill">live</span>` : html`<span class="muted">paused</span>`}
          <span class="runs">${fmtNum(a.runs || 0)} runs</span>
        </div>
      </div>`;
  }

  _infraRow(w) {
    const id = w.id || w.name;
    const key = 'infra:' + id;
    const last = w.last_tick_at ? relTime(w.last_tick_at * 1000) : '—';
    const ticks = w.tick_count ?? w.ticks ?? 0;
    return html`
      <div class="wk-side-row sys ${key === this.activeKey ? 'sel' : ''}" @click=${() => this._select(key)}>
        <div class="av">${icon('cpu', 13)}<span class="ind ${w.status === 'running' ? 'running' : 'idle'}"></span></div>
        <div class="info">
          <div class="name truncate">${w.name || id}</div>
          <div class="sub truncate">${w.plugin || '—'}</div>
        </div>
        <div class="right">
          <span class="muted">${last}</span>
          <span class="runs">${fmtNum(ticks)} ticks</span>
        </div>
      </div>`;
  }

  // ── Detail pane (override — imperative, see migration note) ──────────
  // app.js calls renderDetail(container); the RelayLens base would paint a
  // detail() template, but Workers hosts a countdown ring + imperatively-mounted
  // sub-regions, so we own the detail DOM directly (parity with agents.js).
  renderDetail(container) {
    this._detailHost = container;
    this._detailRoot = this._mountRoot(container, this._detailRoot);
    this._renderActiveDetail();
  }

  async _renderActiveDetail() {
    const root = this._detailRoot;
    if (!root) return;
    this._clearCountdown();
    this._syncCollections();
    this._autoDetailScheduled = false;   // rendering now — cancel any pending one-shot
    if (!this.activeKey) {
      render(empty('No workers',
        html`A worker runs attached actions (model / code / script) when a trigger fires. Hit <b>+</b> to create one.`),
        root);
      return;
    }
    if (this.activeKey.startsWith('cfg:')) {
      await this._renderConfigurable(root, this.activeKey.slice(4));
    } else {
      this._renderInfra(root, this.activeKey.slice(6));
    }
  }

  // Live refresh of the OPEN detail only — re-fetches its per-worker data and
  // rebuilds the pane imperatively. Never invoked from the countdown tick.
  _refreshOpenDetail() {
    if (this._dead) return;
    if (this._detailRoot) this._renderActiveDetail();
  }

  // ── Configurable worker detail ─────────────────────────────────────
  async _renderConfigurable(container, id) {
    const a = this.configurable.find((x) => x.automation_id === id) || this.configurable[0];
    if (!a) {
      render(empty('Worker gone'), container);
      return;
    }
    id = a.automation_id;

    // Per-detail data: fetched ONCE here (on selection / explicit refresh),
    // loaded into locals, then rendered imperatively. Not refetched per tick.
    let runs = [], invos = [], invoRollup = {}, activity = [], cfg = {};
    try {
      const [rr, ri, re, rc] = await Promise.all([
        this.host.api.fetch(`/api/automations/${encodeURIComponent(id)}/runs?limit=50`),
        this.host.api.fetch(`/api/automations/${encodeURIComponent(id)}/invocations?limit=25`),
        this.host.api.fetch(`/api/agents/${encodeURIComponent(id)}/events`),
        this.host.api.fetch(`/api/agents/${encodeURIComponent(id)}`),
      ]);
      if (rr.ok) runs = (await rr.json()).runs || [];
      if (ri.ok) { const j = await ri.json(); invos = j.invocations || []; invoRollup = j.rollup || {}; }
      if (re.ok) { const j = await re.json(); activity = (Array.isArray(j) ? j : (j.events || [])).slice().reverse(); }
      if (rc.ok) cfg = (await rc.json()) || {};
    } catch (_) {}

    const actions = (cfg.config && Array.isArray(cfg.config.actions)) ? cfg.config.actions : [];
    const running = a.agent_status === 'running';
    const lastRel = a.last_started_at ? relTime(a.last_started_at * 1000) : '—';
    const totalTok = invoRollup.total_tokens || 0;
    const avgMs = invoRollup.avg_latency_ms || 0;
    const emitEvent = this._modelEmit(actions);

    // Countdown ring geometry.
    const period = (a.trigger && a.trigger.kind === 'interval') ? Math.max(1, a.trigger.value || 0) : 0;
    const secsLeft = a.next_fire_at ? Math.max(0, Math.round(a.next_fire_at - Date.now() / 1000)) : null;
    const ringFrac = (period && secsLeft != null) ? Math.min(1, Math.max(0, 1 - secsLeft / period)) : 0;

    const statusStyle = a.last_status && a.last_status !== 'succeeded'
      ? `color:${STATUS_COLOR[a.last_status] || 'var(--t-1)'}` : '';

    // Header controls (lit templates so @click wires inline — no querySelector).
    const controls = a.is_agent
      ? html`
        ${running
          ? html`
            ${button({ act: 'run', onClick: () => this._control(id, 'run', 'run', 'Run now') }, icon('restart', 11), ' Run now')}
            ${button({ act: 'pause', onClick: () => this._control(id, 'pause', 'pause', 'Pause') }, icon('stop', 11), ' Pause')}`
          : button({ act: 'resume', onClick: () => this._control(id, 'resume', 'resume', 'Resume') }, icon('play', 11), ' Resume')}
        ${button({ variant: 'primary', act: 'edit', onClick: () => this._editWorker(a, id) }, icon('edit', 11), ' Edit')}
        ${button({ variant: 'icon', act: 'delete', title: 'Delete worker', onClick: () => this._deleteWorker(a, id) }, icon('x', 12))}`
      : html`<span class="chip muted" title="No live agent backs this — history is kept.">history only</span>`;

    // The detail pane is rebuilt fresh on each explicit render (parity with the
    // old innerHTML rebuild); the [data-*] anchor nodes below are populated
    // imperatively, and the countdown ring is updated by a local timer only.
    const pane = document.createElement('div');
    pane.className = 'pane';
    render(html`
      <div class="cwk-hdr">
        <div class="cwk-hdr-left">
          <div class="cwk-clock">
            <svg viewBox="0 0 40 40" width="40" height="40" aria-hidden="true">
              <circle cx="20" cy="20" r="18" fill="none" stroke="var(--line-2)" stroke-width="2"></circle>
              <circle cx="20" cy="20" r="18" fill="none" stroke="var(--violet)" stroke-width="2"
                stroke-dasharray="${(ringFrac * 113.1).toFixed(1)} 113.1" stroke-linecap="round"
                transform="rotate(-90 20 20)" data-ring style="transition:stroke-dasharray 1s linear"></circle>
              <text x="20" y="22.5" text-anchor="middle" font-family="var(--f-mono)" font-size="9"
                fill="var(--t-1)" font-weight="500" data-ring-txt>${secsLeft != null ? secsLeft + 's' : '—'}</text>
            </svg>
          </div>
          <div class="cwk-hdr-meta">
            <div class="cwk-eyebrow">worker · configurable</div>
            <h1 class="cwk-name truncate">${a.name || id}</h1>
            <div class="cwk-chips">
              ${a.is_agent ? html`<span class="chip ${running ? 'ok' : 'muted'}">● ${running ? 'live' : 'paused'}</span>` : nothing}
              <span class="chip muted">trigger: ${triggerLabel(a.trigger)}</span>
              ${a.next_fire_at ? html`<span class="chip muted" data-countdown>next …</span>` : nothing}
              <span class="chip muted">${fmtNum(a.runs || 0)} runs</span>
              <span class="chip muted">last <span style="color:var(--ok)">${lastRel}</span></span>
              ${a.workspace ? html`<span class="chip accent" style="text-transform:none">@${a.workspace}</span>` : nothing}
            </div>
          </div>
        </div>
        <div class="cwk-hdr-actions">${controls}</div>
      </div>

      <div class="cwk-body">
        <div class="cwk-strip">
          <div class="cwk-stat"><div class="k">Runs</div><div class="v">${fmtNum(a.runs || 0)}</div><div class="sub">last ${lastRel}</div></div>
          <div class="cwk-stat"><div class="k">Last status</div><div class="v ${a.last_status === 'succeeded' ? 'ok' : ''}" style=${statusStyle}>${a.last_status || '—'}</div><div class="sub">${(a.last_error_count || 0) > 0 ? html`<span style="color:var(--err)">${a.last_error_count} err</span>` : 'clean'}</div></div>
          <div class="cwk-stat"><div class="k">Last duration</div><div class="v acc">${fmtDur(a.last_duration_ms)}</div><div class="sub">wall-clock</div></div>
          <div class="cwk-stat"><div class="k">LLM calls</div><div class="v">${fmtNum(invoRollup.count || 0)}</div><div class="sub">${invoRollup.count ? `${fmtNum(totalTok)} tok · ~${avgMs}ms` : 'no calls'}</div></div>
          <div class="cwk-stat"><div class="k">Next run</div><div class="v ${a.next_fire_at ? 'pulse' : ''}" data-countdown-v>${a.next_fire_at && secsLeft != null ? secsLeft + 's' : '—'}</div><div class="sub">${a.trigger ? triggerLabel(a.trigger) : 'not scheduled'}</div></div>
        </div>

        <div class="cwk-pipeline">
          <div class="cwk-pipeline-head">
            <span class="ttl">Pipeline</span>
            <span class="cwk-pipeline-sub">${actions.length} action${actions.length === 1 ? '' : 's'} · runs top-to-bottom on every tick</span>
          </div>
          <div class="cwk-pipeline-flow" data-pipeline></div>
        </div>

        <div class="cwk-main-grid">
          <div class="cwk-card live-stream">
            <div class="cwk-card-head">
              <span class="ttl">LLM Stream <span class="pill">tail -f</span> <span class="count-tag">${invos.length}</span></span>
              <div class="cwk-card-actions">
                <span class="cwk-live-dot"></span>
                <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-2);letter-spacing:.06em">streaming</span>
              </div>
            </div>
            <div class="cwk-card-body" data-invos></div>
          </div>
          <div class="cwk-side">
            <div class="cwk-card">
              <div class="cwk-card-head"><span class="ttl">Run history <span class="count-tag">${runs.length}</span></span></div>
              <div class="cwk-card-body" data-runs></div>
            </div>
            <div class="cwk-card">
              <div class="cwk-card-head"><span class="ttl">Activity <span class="count-tag">${activity.length}</span></span></div>
              <div class="cwk-card-body activity" data-activity></div>
            </div>
          </div>
        </div>
      </div>
    `, pane);
    container.replaceChildren(pane);

    // Imperative population of the procedural sub-regions (UNCHANGED logic).
    this._renderPipeline(pane.querySelector('[data-pipeline]'), actions);
    this._renderInvocations(pane.querySelector('[data-invos]'), invos, emitEvent);
    this._renderRuns(pane.querySelector('[data-runs]'), runs);
    this._renderActivity(pane.querySelector('[data-activity]'), activity);

    // Live countdown — local timer, imperative ring/chip/stat updates only.
    if (a.next_fire_at) this._startCountdown(pane, a, period);
  }

  _editWorker(a, id) {
    openWorkerForm(this.host, { worker: a, onSaved: async () => { await this._refreshOpenDetail(); } });
  }

  async _deleteWorker(a, id) {
    if (!confirm(`Delete worker "${a.name || id}"? Its run history is kept, but the worker definition is removed.`)) return;
    try { await this.host.api.fetch(`/api/agents/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
    catch (e) { alert('Delete failed: ' + e.message); return; }
    this.activeKey = null;
    // agent.deleted only invalidates /api/agents — refresh the worker list the
    // sidebar reads via use('/api/automations') so it drops the row now, not
    // after the 12s heartbeat.
    live.invalidate('/api/automations');
    this.host.render();
  }

  _modelEmit(actions) {
    for (const act of actions) {
      if (act && typeof act === 'object') {
        const k = Object.keys(act)[0];
        if (k === 'model' && act.model && act.model.emit) return act.model.emit;
      }
    }
    return null;
  }

  _renderPipeline(el, actions) {
    if (!el) return;
    if (!actions.length) {
      el.innerHTML = '<div style="color:var(--t-4);font-family:var(--f-mono);font-size:var(--t-xs);padding:4px 0">No actions attached — Edit the worker to add some.</div>';
      return;
    }
    actions.forEach((act, i) => {
      el.appendChild(this._actionNode(act, i + 1));
      if (i < actions.length - 1) {
        const arrow = document.createElement('div');
        arrow.className = 'cwk-pipe-arrow';
        arrow.innerHTML = iconSVG('arrow_r', 14);
        el.appendChild(arrow);
      }
    });
  }

  _actionNode(act, ix) {
    const node = document.createElement('div');
    const kind = (act && typeof act === 'object') ? Object.keys(act)[0] : '?';
    const p = (act && act[kind]) || {};
    if (kind === 'code') {
      const lines = String(p.body || '').split('\n');
      const body = lines.slice(0, 3).join('\n') + (lines.length > 3 ? '\n…' : '');
      node.className = 'cwk-act';
      node.innerHTML = `
        <div class="cwk-act-head"><span class="ix">${ix}</span><span class="ty">code</span><span class="lg">${esc(p.lang || 'python')}</span></div>
        <pre class="cwk-act-code">${esc(body)}</pre>`;
    } else if (kind === 'bus.emit') {
      node.className = 'cwk-act';
      node.innerHTML = `
        <div class="cwk-act-head"><span class="ix">${ix}</span><span class="ty">bus.emit</span><span class="lg">event</span></div>
        <div class="cwk-act-event">${iconSVG('send', 11)}<span class="ev">${esc(p.type || '—')}</span></div>
        ${p.data ? `<div class="cwk-act-data">${esc(typeof p.data === 'string' ? p.data : JSON.stringify(p.data))}</div>` : ''}`;
    } else if (kind === 'model') {
      node.className = 'cwk-act model';
      node.innerHTML = `
        <div class="cwk-act-head"><span class="ix">${ix}</span><span class="ty">model</span><span class="lg">${esc(p.model || 'role:fast')}</span></div>
        <div class="cwk-act-prompt">"${esc(String(p.prompt || '').slice(0, 160))}"</div>
        <div class="cwk-act-foot"><span>max ${esc(p.max_tokens ?? 256)} tok</span>${p.emit ? `<span>→ emits <span style="color:var(--violet)">${esc(p.emit)}</span></span>` : ''}</div>`;
    } else {
      node.className = 'cwk-act';
      node.innerHTML = `<div class="cwk-act-head"><span class="ix">${ix}</span><span class="ty">${esc(kind)}</span></div>
        <div class="cwk-act-data">${esc(JSON.stringify(p).slice(0, 120))}</div>`;
    }
    return node;
  }

  _renderInvocations(el, invos, emitEvent) {
    if (!el) return;
    if (!invos.length) {
      el.innerHTML = '<div style="padding:20px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">No model calls yet.<br>Attach a <code>model</code> action and they stream here.</div>';
      return;
    }
    invos.forEach((v, idx) => {
      const inv = document.createElement('div');
      inv.className = 'cwk-inv' + (idx === 0 ? ' fresh' : '');
      const okSym = v.ok ? '✓' : '✗';
      const tok = v.tokens_known ? `${v.total_tokens} tok` : 'tok n/a';
      const head = `
        <div class="cwk-inv-head">
          <span class="t">${esc(fmtClock(v.ts))}</span>
          <span class="st ${v.ok ? 'ok' : 'err'}">${okSym}</span>
          <span class="model">${esc(v.model || '?')}</span>
          <span class="prov">${esc(v.provider || '')}</span>
          <span class="dot">·</span>
          <span class="tok">${esc(tok)}</span>
          <span class="ms">${esc(v.latency_ms != null ? v.latency_ms + 'ms' : '')}</span>
          ${idx === 0 ? '<span class="just-now">just now</span>' : ''}
        </div>`;
      const respText = v.ok ? (v.response || '') : (v.error || 'error');
      const respClass = v.ok ? 'assistant' : 'assistant';
      const body = `
        <div class="cwk-inv-body">
          ${v.prompt ? `
          <div class="cwk-bubble user">
            <div class="role">${iconSVG('send', 10)} prompt</div>
            <div class="text">${esc(v.prompt)}</div>
          </div>` : ''}
          <div class="cwk-bubble ${respClass}" ${v.ok ? '' : 'style="border-left-color:var(--err);background:rgba(239,68,68,.06);border-color:rgba(239,68,68,.25)"'}>
            <div class="role" ${v.ok ? '' : 'style="color:var(--err)"'}>${iconSVG('agent', 10)} ${v.ok ? 'response' : 'error'}<span class="role-meta">${v.tokens_known ? v.total_tokens + ' tokens · ' : ''}${v.latency_ms != null ? v.latency_ms + 'ms' : ''}</span></div>
            <div class="text">${esc(respText)}</div>
            ${(v.ok && (emitEvent || v.response)) ? `<div class="cwk-bubble-foot">${emitEvent ? `<span>↳ emitted as <code>${esc(emitEvent)}</code></span>` : '<span></span>'}<button class="btn ghost sm" data-copy>${iconSVG('copy', 10)} Copy</button></div>` : ''}
          </div>
        </div>`;
      inv.innerHTML = head + body;
      const copyBtn = inv.querySelector('[data-copy]');
      if (copyBtn) copyBtn.addEventListener('click', () => {
        navigator.clipboard?.writeText(v.response || '');
        copyBtn.innerHTML = iconSVG('copy', 10) + ' Copied';
        setTimeout(() => { copyBtn.innerHTML = iconSVG('copy', 10) + ' Copy'; }, 1200);
      });
      el.appendChild(inv);
    });
  }

  _renderRuns(el, runs) {
    if (!el) return;
    if (!runs.length) {
      el.innerHTML = '<div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">No runs yet.</div>';
      return;
    }
    const maxDur = Math.max(1, ...runs.map((r) => r.duration_ms || 0));
    for (const r of runs) {
      const row = document.createElement('div');
      row.className = 'cwk-run-row';
      const st = r.status === 'succeeded' ? 'ok' : (r.status === 'partial' ? '' : 'err');
      row.innerHTML = `
        <span class="t">${esc(fmtDT(r.started_at))}</span>
        <span class="st ${st}" ${r.status === 'partial' ? 'style="color:var(--warn)"' : ''}>${esc(r.status || '—')}</span>
        <div class="cwk-run-bar"><i style="width:${Math.round(((r.duration_ms || 0) / maxDur) * 100)}%"></i></div>
        <span class="ms">${esc(fmtDur(r.duration_ms))}</span>`;
      el.appendChild(row);
    }
  }

  _renderActivity(el, activity) {
    if (!el) return;
    if (!activity.length) {
      el.innerHTML = '<div style="padding:14px;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-xs);text-align:center">No recent activity.</div>';
      return;
    }
    for (const ev of activity) {
      const t = ev.type || '';
      const row = document.createElement('div');
      row.className = 'cwk-act-row';
      const cls = t.includes('tick') ? 'tick' : (t.includes('error') || t.includes('fail') ? 'err' : 'act');
      row.innerHTML = `
        <span class="t">${esc(fmtClock(ev.ts))}</span>
        <span class="ty ${cls}" ${cls === 'err' ? 'style="color:var(--err)"' : ''}>${esc(t)}</span>
        <span class="dt">${esc(this._activityDetail(ev))}</span>`;
      el.appendChild(row);
    }
  }

  _activityDetail(ev) {
    let payload = ev.payload;
    if (typeof payload === 'string') { try { payload = JSON.parse(payload); } catch (_) { payload = {}; } }
    payload = payload || {};
    if (ev.type === 'loop.tick') {
      const trig = payload.trigger || (payload.event_type ? `event:${payload.event_type}` : 'interval');
      return `trigger=${trig}`;
    }
    if (ev.type === 'loop.action') {
      const r = payload.result || {};
      const bits = [r.kind];
      if (r.model) bits.push(r.model);
      if (r.type) bits.push(r.type);
      if (r.text_len != null) bits.push(`${r.text_len} chars`);
      if (r.lang) bits.push(r.lang);
      return bits.filter(Boolean).join(' · ');
    }
    if (ev.type === 'loop.action_error') return String(payload.error || '').slice(0, 60);
    return '';
  }

  async _control(id, act, path, verb) {
    try {
      const r = await this.host.api.fetch(
        `/api/automations/${encodeURIComponent(id)}/${path}`, { method: 'POST' });
      if (!r.ok) {
        let msg = `${verb} failed (${r.status})`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        alert(msg);
      }
    } catch (e) { alert(`${verb} failed: ${e.message}`); }
    setTimeout(() => {
      this._refreshOpenDetail();
      // Reflect the new live/paused state in the sidebar pill (agent.status_changed
      // doesn't invalidate /api/automations).
      live.invalidate('/api/automations');
    }, act === 'run' ? 700 : 150);
  }

  // ── Countdown (ring + chips + stat) ────────────────────────────────
  // Local 1s clock — imperatively updates ONLY the ring / countdown nodes;
  // never re-renders the lens, never refetches. Cleared via addCleanup.
  _startCountdown(container, a, period) {
    this._clearCountdown();
    const ring = container.querySelector('[data-ring]');
    const ringTxt = container.querySelector('[data-ring-txt]');
    const tick = () => {
      const secs = Math.round(a.next_fire_at - Date.now() / 1000);
      const shown = Math.max(0, secs);
      container.querySelectorAll('[data-countdown]').forEach((el) => { el.textContent = secs > 0 ? `next in ${fmtCountdown(secs)}` : 'firing…'; });
      container.querySelectorAll('[data-countdown-v]').forEach((el) => { el.textContent = secs > 0 ? `${shown}s` : 'now'; });
      if (ringTxt) ringTxt.textContent = `${shown}s`;
      if (ring && period) {
        const frac = Math.min(1, Math.max(0, 1 - shown / period));
        ring.setAttribute('stroke-dasharray', `${(frac * 113.1).toFixed(1)} 113.1`);
      }
      if (secs <= -2 && !this._countdownReloading) {
        this._countdownReloading = true;
        this._clearCountdown();
        setTimeout(() => { this._countdownReloading = false; this._refreshOpenDetail(); }, 1500);
      }
    };
    tick();
    this._countdownTimer = setInterval(tick, 1000);
    this.addCleanup(() => this._clearCountdown());
  }

  _clearCountdown() {
    if (this._countdownTimer) { clearInterval(this._countdownTimer); this._countdownTimer = null; }
  }

  // Render the worker's real runtime config dict (session_dir, repo,
  // watch_patterns, …) as kv-rows — the honest "what is this worker
  // pointed at" surface, straight from the worker snapshot.
  _configRows(cfg) {
    const keys = Object.keys(cfg || {});
    if (!keys.length) return '';
    let html = '<div class="kv-row kv-sub"><span class="k">runtime config</span><span class="v"></span></div>';
    for (const k of keys) {
      let v = cfg[k];
      if (v == null) v = '—';
      else if (typeof v === 'object') v = JSON.stringify(v);
      else v = String(v);
      html += `<div class="kv-row"><span class="k">${esc(k)}</span><span class="v" title="${esc(v)}">${esc(v)}</span></div>`;
    }
    return html;
  }

  // ── System (infra) worker detail ───────────────────────────────────
  _renderInfra(container, id) {
    const worker = this.infra.find((w) => (w.id || w.name) === id) || this.infra[0];
    if (!worker) {
      render(empty('No worker'), container);
      return;
    }
    id = worker.id || worker.name;
    const ticks = worker.tick_count ?? worker.ticks ?? 0;
    const intervalS = worker.interval_s ?? null;
    const startedRel = worker.started_at ? relTime(worker.started_at * 1000) : '—';
    const lastTickRel = worker.last_tick_at ? relTime(worker.last_tick_at * 1000) : '—';
    const desc = worker.description || '';
    const agentId = worker.agent_id || null;
    const cfg = worker.config && typeof worker.config === 'object' ? worker.config : {};
    const cadence = intervalS === 0 ? 'self-paced (drives its own loop)'
      : intervalS != null ? `every ${intervalS}s` : '—';

    const pane = document.createElement('div');
    pane.className = 'pane';
    render(html`
      <div class="cwk-hdr">
        <div class="cwk-hdr-left">
          <div class="dhdr-avatar" style="background:var(--bg-2);border-color:var(--line-3);color:var(--t-2);width:48px;height:48px">${icon('cpu', 22)}</div>
          <div class="cwk-hdr-meta">
            <div class="cwk-eyebrow">worker · system · ${worker.plugin || ''}</div>
            <h1 class="cwk-name truncate" style="font-size:28px">${worker.name || id}</h1>
            <div class="cwk-chips">
              <span class="sbadge ${worker.status === 'running' ? 'running' : 'idle'}">${worker.status || 'idle'}</span>
              <span class="chip muted">last tick <span style="color:var(--ok)">${lastTickRel}</span></span>
              <span class="chip muted">${fmtNum(ticks)} ticks</span>
              <span class="chip muted">plugin: ${worker.plugin || '—'}</span>
              ${agentId ? html`<span class="chip muted">agent: ${agentId}</span>` : nothing}
            </div>
            ${desc ? html`<p class="cwk-desc">${desc}</p>` : nothing}
          </div>
        </div>
        <div class="cwk-hdr-actions">
          ${button({ act: 'restart', onClick: () => this._infraControl(id, 'restart', 'Restart') }, icon('restart', 11), ' Restart')}
          ${button({ act: 'stop', onClick: () => this._infraControl(id, 'stop', 'Stop') }, icon('stop', 11), ' Stop')}
        </div>
      </div>
      <div class="cwk-body">
        <div class="cwk-strip">
          <div class="cwk-stat"><div class="k">Ticks</div><div class="v ok">${fmtNum(ticks)}</div><div class="sub">${startedRel} since start</div></div>
          <div class="cwk-stat"><div class="k">Cadence</div><div class="v acc" style=${intervalS === 0 ? 'font-size:14px' : ''}>${intervalS === 0 ? 'self-paced' : (intervalS != null ? intervalS + 's' : '—')}</div><div class="sub">restart: ${worker.restart_policy || '—'}</div></div>
          <div class="cwk-stat"><div class="k">Restarts</div><div class="v">${worker.restart_count ?? 0}</div><div class="sub">${worker.last_error ? html`<span style="color:var(--err)">recent error</span>` : 'no errors'}</div></div>
          <div class="cwk-stat"><div class="k">Alive</div><div class="v ${worker.alive ? 'ok' : ''}" style=${worker.alive ? '' : 'color:var(--err)'}>${worker.alive ? 'yes' : 'no'}</div><div class="sub">last ${lastTickRel}</div></div>
          <div class="cwk-stat"><div class="k">Plugin</div><div class="v" style="font-size:14px">${worker.plugin || '—'}</div><div class="sub">owner</div></div>
        </div>
        <div class="cwk-main-grid sys">
          <div class="cwk-card">
            <div class="cwk-card-head"><span class="ttl">Log <span class="pill">tail -f</span></span><div class="cwk-card-actions"><span class="cwk-live-dot"></span></div></div>
            <div class="cwk-card-body" data-logs></div>
          </div>
          <div class="cwk-card">
            <div class="cwk-card-head"><span class="ttl">What it does</span></div>
            <div class="cwk-card-body">
              <p class="cwk-cfg-desc">${desc ? desc : 'No description provided by the owning plugin.'}</p>
              <div class="kv-table">
                <div class="kv-row"><span class="k">id</span><span class="v" style="font-size:10px;color:var(--t-3)">${id}</span></div>
                <div class="kv-row"><span class="k">plugin</span><span class="v">${worker.plugin || '—'}</span></div>
                ${agentId ? html`<div class="kv-row"><span class="k">agent</span><span class="v">${agentId}</span></div>` : nothing}
                <div class="kv-row"><span class="k">cadence</span><span class="v">${cadence}</span></div>
                <div class="kv-row"><span class="k">restart policy</span><span class="v">${worker.restart_policy || '—'}</span></div>
                <div class="kv-row"><span class="k">last error</span><span class="v ${worker.last_error ? '' : 'dim'}" style=${worker.last_error ? 'color:var(--err)' : ''}>${worker.last_error || 'none'}</span></div>
                <div class="kv-row"><span class="k">started</span><span class="v">${worker.started_at ? new Date(worker.started_at * 1000).toLocaleString() : '—'}</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `, pane);
    container.replaceChildren(pane);

    // runtime config kv-rows (raw html string) appended to the kv-table.
    const kvTable = pane.querySelector('.kv-table');
    const rows = this._configRows(cfg);
    if (kvTable && rows) kvTable.insertAdjacentHTML('beforeend', rows);

    // logs (fetched once, rendered imperatively).
    (async () => {
      let logs = [];
      try {
        const r = await this.host.api.fetch(`/api/workers/${encodeURIComponent(id)}/logs?limit=40`);
        if (r.ok) logs = await r.json();
      } catch (_) {}
      const logsEl = pane.querySelector('[data-logs]');
      if (!logsEl || !logsEl.isConnected) return;
      if (!logs.length) {
        logsEl.innerHTML = `<div class="cwk-log-empty">
          <div class="hd">No log lines yet</div>
          <div>This worker writes a line only when it has something to report
          (work done, or an error). It has completed
          <b style="color:var(--ok)">${esc(fmtNum(ticks))}</b> quiet tick${ticks === 1 ? '' : 's'}${worker.started_at ? ` since ${esc(startedRel)}` : ''} — a silent log is the healthy steady state.</div>
        </div>`;
        return;
      }
      for (const l of logs.reverse()) {
        const g = (l.level || 'info').toLowerCase();
        const sym = g === 'error' ? '✗' : (g === 'warn' || g === 'warning') ? '⚠' : g === 'debug' ? '·' : g === 'info' ? '✓' : 'i';
        const cls = g === 'error' ? 'err' : (g === 'warn' || g === 'warning') ? 'warn' : g === 'debug' ? 'info' : 'ok';
        const text = (l.msg ?? l.message ?? l.line ?? '').toString();
        const lvlTag = (g === 'info' || g === 'debug') ? '' : g;
        const row = document.createElement('div');
        row.className = 'log-row';
        const ts = l.ts ? new Date(typeof l.ts === 'number' ? l.ts * 1000 : Date.parse(l.ts)).toTimeString().slice(0, 8) : '';
        row.innerHTML = `<span class="t">${esc(ts)}</span><span class="g ${cls}">${sym}</span><span class="msg" title="${esc(text)}">${text ? esc(text) : '<span class="dim">tick — nothing to report</span>'}</span><span class="dur">${esc(lvlTag)}</span>`;
        logsEl.appendChild(row);
      }
    })();
  }

  async _infraControl(id, path, verb) {
    try { await this.host.api.fetch(`/api/workers/${encodeURIComponent(id)}/${path}`, { method: 'POST' }); }
    catch (e) { alert(`${verb} failed: ` + e.message); }
  }

  unmount() {
    this._clearCountdown();
    super.unmount();
  }
}
