// tiles/git.js — workspace git checkout, worktrees, branches, file changes.

import {
  RelayElement, defineTile, LiveController, EventsController, html, nothing, chip, icon, button,
} from '@relaydeck/ui';
import { live } from '../data.js';

const CODE_CLASS = {
  M: 'git-st-mod', A: 'git-st-add', D: 'git-st-del', R: 'git-st-ren',
  C: 'git-st-ren', '?': 'git-st-untracked', U: 'git-st-conflict',
};

function fileChips(g) {
  if (!g?.is_git) return nothing;
  const parts = [];
  const ins = g.insertions || 0;
  const dele = g.deletions || 0;
  if (ins) parts.push(html`<span class="chip accent" title="Lines added vs HEAD">+${ins}</span>`);
  if (dele) parts.push(html`<span class="chip warn" title="Lines removed vs HEAD">-${dele}</span>`);
  if (g.untracked_files) parts.push(html`<span class="chip muted" title="Untracked files">${g.untracked_files} new</span>`);
  if (g.modified_files) parts.push(html`<span class="chip muted" title="Modified files">${g.modified_files} mod</span>`);
  if (g.added_files) parts.push(html`<span class="chip ok" title="Added files">${g.added_files} add</span>`);
  if (g.deleted_files) parts.push(html`<span class="chip err" title="Deleted files">${g.deleted_files} del</span>`);
  return parts.length ? html`${parts}` : html`<span class="chip ok">clean</span>`;
}

class GitTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    host: { attribute: false },
    _view: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this.host = null;
    this._view = 'status';
    this._detail = new LiveController(this);
    this._bus = new EventsController(this, {
      onEvent: (e) => {
        const t = e?.type || '';
        if (!/^(workspace\.|worktree\.)/.test(t)) return;
        const ws = this.agent?.workspace;
        if (ws) live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`);
      },
      rerender: false,
    });
  }

  willUpdate(changed) {
    const ws = this.agent?.workspace;
    if (changed.has('agent') && ws) {
      this._detail.setKey(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`);
    }
  }

  _wsRow(row) {
    const g = row.git || {};
    const agents = row.agents || [];
    const running = agents.filter((a) => a.status === 'running').map((a) => a.id);
    return html`
      <tr class=${row.current ? 'git-row-cur' : ''}>
        <td><span class="mono">@${row.workspace}</span>${row.current ? html` <span class="chip accent" style="font-size:9px">this</span>` : nothing}</td>
        <td>${g.is_worktree ? 'worktree' : (g.kind === 'main' ? 'main' : '—')}</td>
        <td>${g.branch || '—'}</td>
        <td class="git-chips-cell">${fileChips(g)}</td>
        <td>${g.ahead ? html`↑${g.ahead}` : ''}${g.behind ? html`${g.ahead ? ' ' : ''}↓${g.behind}` : ''}${!g.ahead && !g.behind ? '—' : nothing}</td>
        <td class="dim">${agents.length ? html`${agents.length} · ${running.join(', ') || 'stopped'}` : '—'}</td>
      </tr>`;
  }

  _wtRow(wt) {
    const br = (wt.branch || '').replace(/^refs\/heads\//, '') || '—';
    return html`
      <tr>
        <td class="mono dim" style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title=${wt.path || ''}>${wt.path || '—'}</td>
        <td>${wt.workspace ? html`<span class="mono">@${wt.workspace}</span>` : html`<span class="dim">—</span>`}</td>
        <td>${br}</td>
        <td class="mono dim" style="font-size:9px">${(wt.head || '').slice(0, 8)}</td>
      </tr>`;
  }

  _statusView(d) {
    const g = d.git || {};
    if (!g.is_git) {
      return html`
        <div class="git-pane">
          <div class="git-callout">Not a git repository at <code>${d.path || '—'}</code>.</div>
        </div>`;
    }
    const repoWs = d.repo_workspaces || [];
    const gitWts = d.git_worktrees || [];
    return html`
      <div class="git-pane">
        <div class="git-current">
          <div class="git-current-k">This workspace · @${d.workspace}</div>
          <div class="git-current-row">
            ${g.branch ? html`<span class="chip muted" style="text-transform:none">${icon('git', 10)} ${g.branch}</span>` : nothing}
            <span class="chip muted">${g.kind === 'worktree' ? 'worktree' : 'main checkout'}</span>
            ${fileChips(g)}
            ${g.ahead ? html`<span class="chip muted">↑${g.ahead}</span>` : nothing}
            ${g.behind ? html`<span class="chip muted">↓${g.behind}</span>` : nothing}
          </div>
          ${g.repo_root ? html`<div class="git-meta">Repo root: <code>${g.repo_root}</code></div>` : nothing}
        </div>

        ${repoWs.length ? html`
          <div class="git-section">
            <div class="git-section-k">Relaydeck workspaces on this repo</div>
            <table class="git-table">
              <thead><tr>
                <th>Workspace</th><th>Tree</th><th>Branch</th><th>Changes</th><th>Upstream</th><th>Agents</th>
              </tr></thead>
              <tbody>${repoWs.map((row) => this._wsRow(row))}</tbody>
            </table>
          </div>` : nothing}

        ${gitWts.length ? html`
          <div class="git-section">
            <div class="git-section-k">Git worktrees <span class="dim">(git worktree list)</span></div>
            <table class="git-table">
              <thead><tr>
                <th>Path</th><th>Workspace</th><th>Branch</th><th>HEAD</th>
              </tr></thead>
              <tbody>${gitWts.map((wt) => this._wtRow(wt))}</tbody>
            </table>
          </div>` : nothing}

        <div class="git-foot dim">Parallel agents on the same repo typically use separate worktree workspaces (one branch per tree).</div>
      </div>`;
  }

  _changesView(d) {
    const lines = d.changes || [];
    if (!d.git?.is_git) {
      return html`<div class="git-pane"><div class="git-callout">No git checkout.</div></div>`;
    }
    if (!lines.length) {
      return html`<div class="git-pane"><div class="git-callout ok">Working tree clean.</div></div>`;
    }
    return html`
      <div class="git-log-wrap">
        <div class="git-log-head">${lines.length} path(s) · git status --porcelain</div>
        <div class="git-log">${lines.map((ln) => {
          const cls = CODE_CLASS[ln.code?.[0]] || CODE_CLASS[ln.code] || 'git-st-other';
          return html`<div class="git-log-line ${cls}"><span class="git-log-code">${ln.code}</span> ${ln.path}</div>`;
        })}</div>
      </div>`;
  }

  render() {
    const d = this._detail.value;
    if (!this._detail.key || d === undefined) {
      return html`<div class="git-pane"><div class="git-loading">loading…</div></div>`;
    }
    if (!d) {
      return html`<div class="git-pane"><div class="git-loading">unavailable</div></div>`;
    }
    const nChanges = (d.changes || []).length;
    const ws = this.agent?.workspace || '';
    return html`
      <div class="git-tile">
        <div class="git-subtabs">
          <button class="git-sub ${this._view === 'status' ? 'on' : ''}" @click=${() => { this._view = 'status'; }}>Trees</button>
          <button class="git-sub ${this._view === 'changes' ? 'on' : ''}" @click=${() => { this._view = 'changes'; }}>
            Files${nChanges ? html` <span class="badge">${nChanges}</span>` : nothing}
          </button>
          <span class="sp"></span>
          <button class="btn sm" title="Refresh" @click=${() => live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`)}>${icon('restart', 11)}</button>
        </div>
        ${this._view === 'changes' ? this._changesView(d) : this._statusView(d)}
      </div>`;
  }
}

if (!customElements.get('rd-tile-git')) customElements.define('rd-tile-git', GitTile);

export default defineTile('rd-tile-git', (el, { ctx }) => {
  el.agent = ctx.agent;
  el.host = ctx.host;
});
