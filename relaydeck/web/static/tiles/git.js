// tiles/git.js — workspace git checkout, worktree context, and file changes.

import {
  RelayElement, defineTile, LiveController, EventsController, html, nothing, chip, icon, button,
} from '@relaydeck/ui';
import { live } from '../data.js';

const CODE_CLASS = {
  M: 'git-st-mod', A: 'git-st-add', D: 'git-st-del', R: 'git-st-ren',
  C: 'git-st-ren', '?': 'git-st-untracked', U: 'git-st-conflict',
};

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

  _kindLabel(kind) {
    if (kind === 'worktree') return 'Linked worktree';
    if (kind === 'main') return 'Main checkout';
    return 'Not a git repo';
  }

  _statusView(d) {
    const g = d.git || {};
    if (!g.is_git) {
      return html`
        <div class="git-pane">
          <div class="git-callout">This workspace path is not a git repository. Use a plain folder or run <code>git init</code> locally — relaydeck re-checks on load.</div>
          <div class="git-meta mono">${d.path || '—'}</div>
        </div>`;
    }
    const ins = g.insertions || 0;
    const dele = g.deletions || 0;
    const sibs = g.sibling_workspaces || [];
    const gh = d.github || {};
    const agents = d.agents || [];
    const running = agents.filter((a) => a.status === 'running').length;
    return html`
      <div class="git-pane">
        ${d.parallel_hint ? html`
          <div class="git-callout warn">
            ${icon('git', 12)}
            ${d.parallel_reason === 'multi_agent'
              ? html`Multiple agents share this checkout — branch switches affect all of them. Use separate <b>worktree</b> workspaces for parallel branch work.`
              : html`Other workspaces use this repo on different branches — assign parallel work to <b>worktree</b> workspaces, not this main checkout.`}
            <button class="btn sm" style="margin-left:8px" @click=${() => this._hostOpenWorkspaces()}>Workspaces</button>
          </div>` : nothing}
        <div class="git-grid">
          <div class="git-card">
            <div class="git-card-k">Checkout</div>
            <div class="git-card-v">${this._kindLabel(g.kind)}</div>
            <div class="git-card-sub">${g.branch ? html`${icon('git', 10)} <b>${g.branch}</b>` : 'detached / unknown'}
              ${g.dirty ? chip('dirty', 'warn') : chip('clean', 'ok')}
              ${ins || dele ? html` · <span class="git-diff">+${ins}/-${dele}</span> · ${g.files_changed || 0} file(s)` : nothing}
              ${g.ahead ? html` · ↑${g.ahead}` : nothing}${g.behind ? html` · ↓${g.behind}` : nothing}
            </div>
          </div>
          <div class="git-card">
            <div class="git-card-k">GitHub workflow</div>
            <div class="git-card-v">${gh.configured ? 'Enabled' : 'Off'}</div>
            <div class="git-card-sub">${gh.configured
              ? (gh.repo ? html`Poller · <code>${gh.repo}</code>` : 'github.yaml present')
              : html`Add <code>workspaces/${d.workspace}/github.yaml</code> with <code>repo:</code>`}
            </div>
          </div>
          <div class="git-card">
            <div class="git-card-k">Agents here</div>
            <div class="git-card-v">${agents.length}</div>
            <div class="git-card-sub">${running} running · shared tree</div>
          </div>
        </div>
        ${g.repo_root ? html`<div class="git-meta">Repo root: <code>${g.repo_root}</code></div>` : nothing}
        ${sibs.length ? html`
          <div class="git-sibs">
            <div class="git-sibs-k">Sibling workspaces (same repo)</div>
            ${sibs.map((s) => html`
              <div class="git-sib-row">
                <span class="mono">@${s.workspace}</span>
                <span class="dim">${s.is_worktree ? 'worktree' : 'main'}</span>
                <span>${s.branch || '—'}</span>
                ${s.dirty ? chip('dirty', 'warn') : nothing}
              </div>`)}
          </div>` : nothing}
        <div class="git-foot">Harnesses get this at spawn. For parallel branches, use separate worktree workspaces per agent.</div>
      </div>`;
  }

  _changesView(d) {
    const lines = d.changes || [];
    if (!d.git?.is_git) {
      return html`<div class="git-pane"><div class="git-callout">No git checkout — nothing to list.</div></div>`;
    }
    if (!lines.length) {
      return html`<div class="git-pane"><div class="git-callout ok">Working tree clean (porcelain empty).</div></div>`;
    }
    return html`
      <div class="git-log-wrap">
        <div class="git-log-head">${lines.length} path(s) · porcelain</div>
        <div class="git-log">${lines.map((ln) => {
          const cls = CODE_CLASS[ln.code?.[0]] || CODE_CLASS[ln.code] || 'git-st-other';
          return html`<div class="git-log-line ${cls}"><span class="git-log-code">${ln.code}</span> ${ln.path}</div>`;
        })}</div>
      </div>`;
  }

  _hostOpenWorkspaces() {
    this.host?.setLens?.('workspaces');
  }

  render() {
    const d = this._detail.value;
    if (!this._detail.key || d === undefined) {
      return html`<div class="git-pane"><div class="git-loading">loading git…</div></div>`;
    }
    if (!d) {
      return html`<div class="git-pane"><div class="git-loading">git detail unavailable</div></div>`;
    }
    const nChanges = (d.changes || []).length;
    const ws = this.agent?.workspace || '';
    return html`
      <div class="git-tile">
        <div class="git-subtabs">
          <button class="git-sub ${this._view === 'status' ? 'on' : ''}" @click=${() => { this._view = 'status'; }}>Status</button>
          <button class="git-sub ${this._view === 'changes' ? 'on' : ''}" @click=${() => { this._view = 'changes'; }}>
            Changes${nChanges ? html` <span class="badge">${nChanges}</span>` : nothing}
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
