// tiles/git.js — workspace git checkout, worktrees, GitHub-style file diffs.

import {
  RelayElement, defineTile, LiveController, EventsController, html, nothing, chip, icon, button,
} from '@relaydeck/ui';
import { live } from '../data.js';

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

const ST_LABEL = { added: 'A', modified: 'M', deleted: 'D', renamed: 'R', untracked: 'U' };

class GitTile extends RelayElement {
  static properties = {
    agent: { attribute: false },
    host: { attribute: false },
    _view: { state: true },
    _selectedFile: { state: true },
  };

  constructor() {
    super();
    this.agent = {};
    this.host = null;
    this._view = 'status';
    this._selectedFile = null;
    this._detail = new LiveController(this);
    this._fileDiff = new LiveController(this);
    this._bus = new EventsController(this, {
      onEvent: (e) => {
        const t = e?.type || '';
        if (!/^(workspace\.|worktree\.)/.test(t)) return;
        const ws = this.agent?.workspace;
        if (ws) {
          live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`);
          live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-diff`);
        }
      },
      rerender: false,
    });
  }

  willUpdate(changed) {
    const ws = this.agent?.workspace;
    if (changed.has('agent') && ws) {
      this._detail.setKey(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`);
      this._selectedFile = null;
    }
    const files = this._detail.value?.diff_files;
    if (files?.length && this._view === 'changes') {
      const paths = new Set(files.map((f) => f.path));
      if (!this._selectedFile || !paths.has(this._selectedFile)) {
        this._selectedFile = files[0].path;
      }
    }
    if (changed.has('_selectedFile') || changed.has('_view') || changed.has('agent')) {
      this._syncFileDiff();
    }
  }

  _syncFileDiff() {
    const ws = this.agent?.workspace;
    if (!ws || this._view !== 'changes' || !this._selectedFile) {
      this._fileDiff.setKey(null);
      return;
    }
    const q = `/api/workspaces/${encodeURIComponent(ws)}/git-diff?path=${encodeURIComponent(this._selectedFile)}`;
    this._fileDiff.setKey(q);
  }

  _pickFile(path) {
    this._selectedFile = path;
    this._syncFileDiff();
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
      return html`<div class="git-pane"><div class="git-callout">Not a git repository at <code>${d.path || '—'}</code>.</div></div>`;
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
          </div>
          ${g.repo_root ? html`<div class="git-meta">Repo root: <code>${g.repo_root}</code></div>` : nothing}
        </div>
        ${repoWs.length ? html`
          <div class="git-section">
            <div class="git-section-k">Relaydeck workspaces on this repo</div>
            <table class="git-table">
              <thead><tr><th>Workspace</th><th>Tree</th><th>Branch</th><th>Changes</th><th>Upstream</th><th>Agents</th></tr></thead>
              <tbody>${repoWs.map((row) => this._wsRow(row))}</tbody>
            </table>
          </div>` : nothing}
        ${gitWts.length ? html`
          <div class="git-section">
            <div class="git-section-k">Git worktrees</div>
            <table class="git-table">
              <thead><tr><th>Path</th><th>Workspace</th><th>Branch</th><th>HEAD</th></tr></thead>
              <tbody>${gitWts.map((wt) => this._wtRow(wt))}</tbody>
            </table>
          </div>` : nothing}
      </div>`;
  }

  _diffLine(line) {
    const k = line.kind || 'ctx';
    return html`<div class="gd-line gd-${k}"><span class="gd-gutter"></span><span class="gd-text">${line.text ?? ''}</span></div>`;
  }

  _diffHunk(hunk) {
    return html`
      <div class="gd-hunk">
        <div class="gd-hunk-hdr">${hunk.header || ''}</div>
        ${(hunk.lines || []).map((ln) => this._diffLine(ln))}
      </div>`;
  }

  _diffPane() {
    const diff = this._fileDiff.value;
    if (!this._selectedFile) {
      return html`<div class="gd-empty">Select a file to view its diff.</div>`;
    }
    if (this._fileDiff.key && diff === undefined) {
      return html`<div class="gd-empty">Loading diff…</div>`;
    }
    if (!diff) {
      return html`<div class="gd-empty">Diff unavailable.</div>`;
    }
    if (diff.binary) {
      return html`<div class="gd-empty">Binary file — no text diff.</div>`;
    }
    if (diff.empty || !(diff.hunks || []).length) {
      return html`<div class="gd-empty">No textual diff for this path.</div>`;
    }
    return html`
      <div class="gd-view">
        <div class="gd-file-hdr">
          <span class="mono">${diff.path}</span>
          ${diff.truncated ? html`<span class="dim">truncated</span>` : nothing}
        </div>
        ${(diff.hunks || []).map((h) => this._diffHunk(h))}
      </div>`;
  }

  _fileBtn(f) {
    const on = f.path === this._selectedFile;
    const st = f.status || 'modified';
    return html`
      <button type="button" class="gd-file ${on ? 'on' : ''}" @click=${() => this._pickFile(f.path)}>
        <span class="gd-st gd-st-${st}">${ST_LABEL[st] || '·'}</span>
        <span class="gd-path" title=${f.path}>${f.path}</span>
        <span class="gd-counts">
          ${f.additions ? html`<span class="add">+${f.additions}</span>` : nothing}
          ${f.deletions ? html`<span class="del">−${f.deletions}</span>` : nothing}
        </span>
      </button>`;
  }

  _changesView(d) {
    if (!d.git?.is_git) {
      return html`<div class="git-pane"><div class="git-callout">No git checkout.</div></div>`;
    }
    const files = d.diff_files || [];
    if (!files.length) {
      return html`<div class="git-pane"><div class="git-callout ok">Working tree clean.</div></div>`;
    }
    return html`
      <div class="git-diff-layout">
        <div class="git-diff-files">
          <div class="git-diff-files-hdr">${files.length} changed</div>
          ${files.map((f) => this._fileBtn(f))}
        </div>
        <div class="git-diff-main">${this._diffPane()}</div>
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
    const nFiles = (d.diff_files || []).length;
    const ws = this.agent?.workspace || '';
    return html`
      <div class="git-tile">
        <div class="git-subtabs">
          <button class="git-sub ${this._view === 'status' ? 'on' : ''}" @click=${() => { this._view = 'status'; }}>Trees</button>
          <button class="git-sub ${this._view === 'changes' ? 'on' : ''}" @click=${() => { this._view = 'changes'; this._syncFileDiff(); }}>
            Changes${nFiles ? html` <span class="badge">${nFiles}</span>` : nothing}
          </button>
          <span class="sp"></span>
          <button class="btn sm" title="Refresh" @click=${() => {
            live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-detail`);
            live.invalidate(`/api/workspaces/${encodeURIComponent(ws)}/git-diff`);
          }}>${icon('restart', 11)}</button>
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
