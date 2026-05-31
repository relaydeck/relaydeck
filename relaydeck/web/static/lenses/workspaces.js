// lenses/workspaces.js — Workspaces lens. Sidebar + detail with plugins
// matrix + mini-agents + scoped activity. Backed by host.state.workspaces,
// /api/workspace-plugins (catalog + harness gates) and /api/worktrees.
//
// MIGRATED to the @relaydeck/ui kit (build-less, light-DOM Lit). The lens
// extends RelayLens and authors sidebar()/detail() as Lit templates:
//   • shared collections (workspaces / agents / events) come from
//     this.host.state; the plugin catalog + worktree metadata come from the
//     live store via this.use() (auto-subscribes, auto-refreshes on
//     workspace.* / worktree.* events, auto-cleans on unmount);
//   • plugin toggles use <rd-toggle> (keeps the .set-toggle look + the
//     [data-pl-toggle] e2e hook);
//   • destructive actions use the kit confirm() dialog instead of
//     window.confirm();
//   • the live activity feed stays scoped + capped via this.onEvent(),
//     buffered in the lens and rendered into a static [data-activity] anchor
//     (a streamed list is procedural — a reactive repaint would thrash it).

import {
  RelayLens, html, nothing, ref,
  esc, icon, relTime, formatTs, confirm,
} from '@relaydeck/ui';
import { openAddWorkspaceModal, openNewWorktreeModal } from '../overlays.js';

const ACTIVITY_CAP = 80;

export class WorkspacesLens extends RelayLens {
  constructor(host, def) {
    super(host, def || { id: 'workspaces' });
    this.search = '';
    this.activeName = null;
    this._feed = [];          // capped, scoped activity buffer for activeName
    this._feedFor = null;     // workspace name the buffer is scoped to
    // Live: stream scoped events into the capped feed + repaint on plugin /
    // workspace churn. onEvent auto-cleans on unmount.
    this.onEvent((evt) => {
      const ws = (evt.workspace || evt.data?.workspace || '');
      if (this._feedFor && ws === this._feedFor) {
        this._feed.unshift({ ...evt, _streamed: true });
        while (this._feed.length > ACTIVITY_CAP) this._feed.pop();
        this._paintFeed();
      }
      if (/plugin|workspace|worktree/.test(evt?.type || '')) this.requestUpdate();
    });
  }

  // ── data ──────────────────────────────────────────────────────────
  _workspaces() {
    return this.host.state.workspaces || [];
  }

  // Canonical catalog of workspace-toggleable plugins + harness gates (same
  // source as the new-agent modal; /api/plugins omits the gates).
  _catalog() {
    return this.use('/api/workspace-plugins', []) || [];
  }

  // Worktree workspaces (branch + git status) keyed by name, for badges.
  _wtByName() {
    const rows = this.use('/api/worktrees', []) || [];
    return Object.fromEntries(rows.map((w) => [w.name, w]));
  }

  _gitInfo(w) { return (w && w.git) || null; }

  _resolveActive() {
    const ws = this._workspaces();
    if (this.activeName && ws.some((w) => w.name === this.activeName)) return;
    this.activeName = this.host.state.workspace
      || (ws.length ? ws[0].name : null);
  }

  // ── sidebar ───────────────────────────────────────────────────────
  sidebar() {
    this._resolveActive();
    const ws = this._workspaces();
    let rows = ws;
    if (this.search) {
      const q = this.search.toLowerCase();
      rows = rows.filter((w) =>
        w.name.toLowerCase().includes(q) || (w.path || '').toLowerCase().includes(q));
    }
    const wtByName = this._wtByName();
    return html`
      <div class="side-head">
        <div class="side-title">Workspaces <span class="count">${ws.length}</span></div>
        <div style="display:flex;gap:4px">
          <button class="btn icon sm" title="New worktree" data-act="new-wt"
            @click=${() => this._newWorktree()}>${icon('git', 12)}</button>
          <button class="btn icon sm" title="Add workspace" data-act="new"
            @click=${() => this._addWorkspace()}>${icon('plus', 12)}</button>
        </div>
      </div>
      <div class="side-search">${icon('search', 12)}
        <input placeholder="Search workspaces…" .value=${this.search}
          @input=${(e) => { this.search = e.target.value; this.requestUpdate(); }}>
      </div>
      <div class="side-list">
        ${rows.map((w) => this._sideRow(w, wtByName))}
      </div>`;
  }

  _sideRow(w, wtByName) {
    const agents = (this.host.state.agents || []).filter((a) => a.workspace === w.name);
    const active = agents.filter((a) => a.status === 'running').length;
    const git = this._gitInfo(w);
    const st = git && git.is_git ? git : null;
    const summary = `${(w.plugins || []).length} plug · ${agents.length} ag`;
    const branchChip = st && st.branch
      ? html`<span class="wt-branch" title="git ${git.kind || 'checkout'} on ${st.branch}">${icon('git', 9)} ${st.branch}${st.dirty ? html` <span class="wt-dot" title="uncommitted changes"></span>` : nothing}${st.files_changed ? html` <span title="${st.files_changed} file(s) changed vs HEAD"><span style="color:var(--ok)">+${st.insertions}</span>/<span style="color:var(--err)">−${st.deletions}</span></span>` : nothing}${(st.ahead || st.behind) ? html` <span style="color:var(--t-4)" title="${st.ahead} ahead, ${st.behind} behind upstream">↑${st.ahead} ↓${st.behind}</span>` : nothing}</span>`
      : nothing;
    return html`
      <div class="srow ${w.name === this.activeName ? 'sel' : ''}"
        style="grid-template-columns:28px 1fr auto"
        @click=${() => { this.activeName = w.name; this.requestUpdate(); }}>
        <div class="av" style="background:var(--bg-2)">${icon(git && git.is_worktree ? 'git' : 'workspace', 13)}<span class="ind ${active > 0 ? 'running' : 'idle'}"></span></div>
        <div class="info">
          <div class="name truncate">${w.name}</div>
          <div class="sub truncate">${summary}${branchChip !== nothing ? html` · ${branchChip}` : nothing}</div>
        </div>
        <div class="right">
          <span style="font-family:var(--f-mono);font-size:10px;color:${active > 0 ? 'var(--ok)' : 'var(--t-4)'}">${active} live</span>
        </div>
      </div>`;
  }

  // ── detail ────────────────────────────────────────────────────────
  detail() {
    this._resolveActive();
    const all = this._workspaces();
    const ws = all.find((w) => w.name === this.activeName) || all[0];
    if (!ws) {
      return html`
        <div class="pane-empty">
          <div class="ttl">No workspaces</div>
          <div class="sub">Register an existing directory on the daemon host.</div>
          <button class="btn primary" data-act="add-empty" style="margin-top:14px"
            @click=${() => this._addWorkspace()}>${icon('plus', 11)} Add workspace</button>
        </div>`;
    }
    this.activeName = ws.name;
    if (this._feedFor !== ws.name) this._seedFeed(ws.name);

    const agents = (this.host.state.agents || []).filter((a) => a.workspace === ws.name);
    const active = agents.filter((a) => a.status === 'running').length;
    const wsPlugins = new Set(ws.plugins || []);
    const catalog = this._catalog();
    const git = this._gitInfo(ws);
    const wst = git && git.is_git ? git : null;
    const isWorktree = git && git.is_worktree;

    // Real workspace metrics from the shared /api/agents collection.
    const lastActive = agents.map((a) => a.last_active_at || 0).reduce((m, t) => Math.max(m, t), 0);
    const tagsAcross = new Set();
    for (const a of agents) for (const t of (a.tags || [])) tagsAcross.add(t);

    return html`
      <div class="pane">
        <div class="dhdr">
          <div class="dhdr-top">
            <div class="dhdr-avatar ${active > 0 ? 'running' : ''}">${icon('workspace', 26)}<span class="ind ${active > 0 ? 'running' : 'idle'}"></span></div>
            <div class="dhdr-meta">
              <div class="dhdr-eyebrow">${isWorktree ? 'worktree' : (git && git.is_git ? 'git workspace' : 'workspace')}</div>
              <h1 class="dhdr-name truncate">${ws.name}</h1>
              <div class="dhdr-row" style="gap:6px">
                <div class="ws-path">
                  ${icon(isWorktree ? 'git' : 'workspace', 11)}
                  <span class="p">${ws.path || '—'}</span>
                  <span class="copy" data-act="copy" title="Copy"
                    @click=${() => this._copyPath(ws.path)}>${icon('copy', 11)}</span>
                </div>
                ${wst && wst.branch ? html`<span class="wt-branch">${icon('git', 9)} ${wst.branch}${wst.dirty ? html` <span class="wt-dot"></span>` : nothing}</span>` : nothing}
              </div>
              ${git && git.is_git === false ? html`<div style="margin-top:6px;font-size:var(--t-xs);color:var(--t-3);font-family:var(--f-mono)">Not a git repo — fine as a plain workspace. Run <code>git init</code> locally when you want worktrees; relaydeck re-checks on every load.</div>` : nothing}
              ${git && git.sibling_workspaces && git.sibling_workspaces.length ? html`<div style="margin-top:6px;font-size:var(--t-xs);color:var(--t-3);font-family:var(--f-mono)">Sibling workspaces on this repo: ${git.sibling_workspaces.map((s) => s.workspace).join(', ')}</div>` : nothing}
            </div>
            <div class="dhdr-actions">
              <button class="btn" data-act="add-agent"
                @click=${() => this.host.openNewAgent({ workspace: ws.name })}>${icon('plus', 11)} Add agent</button>
              <button class="btn" data-act="set-active" title="Set active workspace"
                @click=${() => this.host.setWorkspace(ws.name)}>Set active</button>
              ${isWorktree
                ? html`<button class="btn icon" data-act="remove-wt" title="Remove worktree (teardown + git worktree remove)"
                    @click=${() => this._removeWorktree(ws, wst)}>${icon('delete', 12)}</button>`
                : html`<button class="btn icon" data-act="remove-ws" title="Unregister workspace"
                    @click=${() => this._removeWorkspace(ws)}>${icon('delete', 12)}</button>`}
            </div>
          </div>
          <div class="stat-strip" style="--cols:${git && git.is_git ? 4 : 3}">
            ${git && git.is_git ? html`<div class="stat-cell">
              <div class="k">${isWorktree ? 'Worktree' : 'Git branch'}</div>
              <div class="v" style="font-size:var(--t-lg)">${(wst && wst.branch) || '—'}</div>
              <div class="sub">${wst && wst.dirty ? html`<span style="color:var(--warn)">dirty</span>` : html`<span style="color:var(--ok)">clean</span>`}${wst && wst.ahead ? html` · ↑${wst.ahead}` : nothing}${wst && wst.behind ? html` · ↓${wst.behind}` : nothing}</div>
            </div>` : nothing}
            <div class="stat-cell">
              <div class="k">Agents</div>
              <div class="v">${agents.length}</div>
              <div class="sub"><span style="color:var(--ok)">${active} running</span> · ${agents.length - active} stopped</div>
            </div>
            <div class="stat-cell">
              <div class="k">Plugins</div>
              <div class="v">${(ws.plugins || []).length}</div>
              <div class="sub">${(ws.plugins || []).slice(0, 3).join(' · ')}${(ws.plugins || []).length > 3 ? '…' : ''}</div>
            </div>
            <div class="stat-cell">
              <div class="k">Last activity</div>
              <div class="v">${lastActive ? relTime(lastActive * 1000) : '—'}</div>
              <div class="sub">${tagsAcross.size ? [...tagsAcross].slice(0, 3).join(' · ') : 'no tags across agents'}</div>
            </div>
          </div>
        </div>
        <div class="lens-body">
          <div class="ws-grid">
            <div class="wk-card">
              <div class="wk-card-head">
                <span class="ttl">Plugins · enabled <span class="pill">${(ws.plugins || []).length} / ${catalog.length}</span></span>
                <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-4)">agents inherit checked plugins</span>
              </div>
              <div class="wk-card-body">
                <div class="ws-plug-grid" data-matrix>
                  ${catalog.map((p) => this._plugCard(ws, p, wsPlugins.has(p.name)))}
                </div>
              </div>
            </div>
            <div style="display:flex;flex-direction:column;gap:14px;min-height:0">
              <div class="wk-card">
                <div class="wk-card-head"><span class="ttl">Agents · ${agents.length}</span></div>
                <div class="mini-agents" data-mini>
                  ${agents.length === 0
                    ? html`<div style="padding:16px;font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3);text-align:center">No agents in this workspace.</div>`
                    : agents.map((a) => this._miniAgent(a))}
                </div>
              </div>
              <div class="wk-card" style="flex:1;min-height:0">
                <div class="wk-card-head">
                  <span class="ttl">Activity · scoped to @${ws.name}</span>
                  <span style="font-family:var(--f-mono);font-size:10px;color:var(--t-3)">live</span>
                </div>
                <div class="wk-card-body" data-activity ${ref(this._feedAnchor)}></div>
              </div>
            </div>
          </div>
        </div>
      </div>`;
  }

  _plugCard(ws, p, on) {
    const cat = (p.category || '').replace('harness-gate', 'gate');
    return html`
      <div class="ws-plug-card ${on ? 'on' : ''}">
        <div class="wpc-head">
          <span class="wpc-title"><span class="wpc-name">${p.name}</span><span class="wpc-cat">· ${cat}</span></span>
          <button class="idn-toggle ${on ? 'on' : ''}" role="switch" aria-checked=${on} data-pl-toggle
            @click=${() => this._togglePlugin(ws.name, p.name, !on)}><span class="idn-knob"></span></button>
        </div>
        <div class="wpc-desc">${p.description || ''}</div>
      </div>`;
  }

  _miniAgent(a) {
    const lastA = a.last_active_at ? relTime(a.last_active_at * 1000) : '—';
    return html`
      <div class="mini-agent-row" @click=${() => this.host.setLens('agents', a.id)}>
        <div class="av ${a.status === 'running' ? 'running' : ''}">${(a.id[0] || '').toUpperCase()}</div>
        <div class="name">${a.id}</div>
        <div class="toks">${lastA}</div>
      </div>`;
  }

  // ── scoped activity feed (imperative — a streamed, capped list) ─────
  // The feed buffer survives reactive repaints; the [data-activity] anchor is
  // re-seeded from it whenever lit-html re-creates the node (ref callback).
  _feedAnchor = (el) => { this._feedEl = el || null; if (el) this._paintFeed(); };

  _seedFeed(name) {
    this._feedFor = name;
    this._feed = (this.host.state.events || [])
      .filter((e) => (e.workspace || e.data?.workspace || '') === name)
      .slice(0, 20);
    // Repaint immediately: on a workspace switch lit-html reuses the same
    // [data-activity] node (ref callback won't re-fire), so reseeding alone
    // would leave the previous workspace's rows until the next scoped event.
    this._paintFeed();
  }

  _paintFeed() {
    const el = this._feedEl;
    if (!el) return;
    el.innerHTML = '';
    if (!this._feed.length) {
      el.innerHTML = '<div style="padding:16px;font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3);text-align:center">No recent activity.</div>';
      return;
    }
    for (const e of this._feed) el.appendChild(this._eventRow(e));
  }

  _eventRow(e) {
    const row = document.createElement('div');
    row.className = 'log-row' + (e._streamed ? ' streamed' : '');
    row.style.gridTemplateColumns = '60px 14px 1fr';
    const g = (e.type || '').includes('error') ? { sym: '✗', cls: 'err' }
      : (e.type || '').includes('message') ? { sym: '✉', cls: 'info' }
      : (e.type || '').includes('start') || (e.type || '').includes('complete') ? { sym: '✓', cls: 'ok' }
      : { sym: '●', cls: 'info' };
    row.innerHTML = `
      <span class="t">${esc(formatTs(e.ts || e.created_at) || '')}</span>
      <span class="g ${g.cls}">${g.sym}</span>
      <span class="msg">${esc(e.agent_id ? e.agent_id + ' · ' : '')}<span class="dim">${esc(e.type || '')}</span></span>`;
    return row;
  }

  // ── actions ───────────────────────────────────────────────────────
  _addWorkspace() {
    openAddWorkspaceModal(this.host, (name, opts) => {
      this.activeName = name;
      // Reload the cached list first so the new ws shows in the sidebar +
      // detail (setWorkspace only switches active, doesn't reload the list).
      this.host.reloadWorkspaces().then(() => {
        if (opts && opts.setActive === false) this.requestUpdate();
        else this.host.setWorkspace(name);
      });
    });
  }

  _newWorktree() {
    openNewWorktreeModal(this.host, async (name) => {
      await this.host.reloadWorkspaces();
      if (name) this.activeName = name;
      this.requestUpdate();
    });
  }

  async _copyPath(path) {
    try { await navigator.clipboard.writeText(path || ''); } catch (_) {}
  }

  async _togglePlugin(wsName, pluginName, enable) {
    try {
      const cur = (this._workspaces().find((w) => w.name === wsName)?.plugins || []).slice();
      const next = enable ? [...new Set([...cur, pluginName])] : cur.filter((x) => x !== pluginName);
      await this.host.api.fetch(`/api/workspaces/${encodeURIComponent(wsName)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugins: next }),
      });
      await this.host.reloadWorkspaces();
      this.requestUpdate();
    } catch (e) {
      alert('Toggle failed: ' + e.message);
      try { await this.host.reloadWorkspaces(); } catch (_) {}
      this.requestUpdate();
    }
  }

  async _removeWorkspace(ws) {
    const ok = await confirm({
      title: `Unregister workspace "${ws.name}"?`,
      body: 'The directory on disk is left untouched; only the relaydeck registration is removed.',
      danger: true, confirmLabel: 'Unregister',
    });
    if (!ok) return;
    try {
      const r = await this.host.api.fetch(`/api/workspaces/${encodeURIComponent(ws.name)}`, { method: 'DELETE' });
      if (!r.ok) {
        let msg = `Remove failed (${r.status})`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        alert(msg);  // e.g. 409 when agents still reference this workspace
        return;
      }
    } catch (e) { alert('Remove failed: ' + e.message); return; }
    this.activeName = null;
    await this.host.reloadWorkspaces();
    this.requestUpdate();
  }

  async _removeWorktree(ws, wst) {
    const dirty = wst && wst.dirty;
    const ok = await confirm({
      title: `Remove worktree "${ws.name}"${wst && wst.branch ? ` (${wst.branch})` : ''}?`,
      body: html`This runs the teardown hook, removes the git working tree on disk, and unregisters the workspace.${
        dirty ? html`<br><br><b style="color:var(--warn)">⚠ It has UNCOMMITTED changes — they will be lost (force).</b>` : nothing}`,
      danger: true, confirmLabel: 'Remove',
    });
    if (!ok) return;
    try {
      const r = await this.host.api.fetch(
        `/api/worktrees/${encodeURIComponent(ws.name)}?force=${dirty ? 'true' : 'false'}`,
        { method: 'DELETE' });
      if (!r.ok) {
        let msg = `Remove failed (${r.status})`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        alert(msg);  // 409 when agents still registered
        return;
      }
    } catch (e) { alert('Remove failed: ' + e.message); return; }
    this.activeName = null;
    await this.host.reloadWorkspaces();
    this.requestUpdate();
  }

  onWorkspacesChanged() { this.requestUpdate(); }
  onAgentsChanged() { this.requestUpdate(); }
}
