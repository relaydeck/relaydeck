// Skills lens — inventory, import, and managed skill workflows.

import { html, render } from '@relaydeck/ui';

const SOURCE_LABEL = {
  workspace: 'Workspace',
  'runtime-plugin': 'Plugin',
  'claude-project': 'Claude project',
  'claude-user': 'Claude user',
  'codex-user': 'Codex',
  'codex-system': 'Codex system',
};

const VENDOR = new Set(['claude-project', 'claude-user', 'codex-user', 'codex-system']);

// The fleet catalog workspace — managed skills live here as links and are
// symlinked into real workspaces on inject. Mirrors manager.CATALOG_WORKSPACE.
const CATALOG_WS = '_catalog';
const RESTART_GRACE_MS = 30000;

const CSS = `
  .sk-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden;background:var(--bg-0)}
  .sk-head{padding:18px 22px 14px;border-bottom:1px solid var(--line-1);flex-shrink:0}
  .sk-head .eyebrow{font-family:var(--f-mono);font-size:9px;letter-spacing:0;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .sk-head h1{margin:0;font-family:var(--f-sans);font-size:28px;font-weight:600;letter-spacing:0}
  .sk-head .row{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
  .sk-head .stats{display:flex;gap:8px;flex-wrap:wrap}
  .sk-head .stat{font-family:var(--f-mono);font-size:var(--t-xxs);background:var(--bg-2);border:1px solid var(--line-2);padding:3px 9px;border-radius:var(--r-1);color:var(--t-3)}
  .sk-head .stat b{color:var(--t-1);font-weight:600}
  .sk-head .stat.bad{border-color:var(--err);color:var(--err)}
  .sk-head .actions{display:flex;gap:6px;margin-left:auto}
  button.sk-btn{font:600 var(--t-xs) var(--f-mono);background:transparent;border:1px solid var(--line-2);color:var(--t-2);border-radius:var(--r-1);padding:5px 11px;cursor:pointer;min-height:28px}
  button.sk-btn:hover{color:var(--t-1);border-color:var(--line-3)}
  button.sk-btn.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-text)}
  button.sk-btn:disabled{opacity:.45;cursor:not-allowed}

  .sk-body{flex:1;min-height:0;overflow-y:auto;padding:18px 22px}
  .sk-grid{display:grid;gap:14px;grid-template-columns:minmax(300px,1fr) minmax(360px,1.25fr);align-items:start}
  .sk-wide{display:grid;gap:14px;grid-template-columns:repeat(12,1fr);align-items:start}
  @media(max-width:960px){.sk-grid,.sk-wide{grid-template-columns:1fr}}
  .sk-card{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-2);overflow:hidden}
  .sk-card.span4{grid-column:span 4}.sk-card.span8{grid-column:span 8}.sk-card.span12{grid-column:span 12}
  @media(max-width:960px){.sk-card.span4,.sk-card.span8,.sk-card.span12{grid-column:1}}
  .sk-card .hd{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line-1);gap:8px}
  .sk-card .ttl{font-family:var(--f-mono);font-size:10px;letter-spacing:0;text-transform:uppercase;color:var(--t-3)}
  .sk-card .pill{font-family:var(--f-mono);font-size:9px;background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);padding:1px 7px;border-radius:3px}
  .sk-card .bd{padding:0;min-height:0}

  .sk-row{display:grid;grid-template-columns:9px minmax(0,1fr) auto;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line-1);cursor:pointer;font-family:var(--f-mono);font-size:var(--t-xs)}
  .sk-row:last-child{border-bottom:0}
  .sk-row:hover{background:var(--bg-2)}
  .sk-row.on{background:var(--acc-soft);border-left:2px solid var(--acc);padding-left:12px}
  .sk-row .dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
  .sk-row .dot.bad{background:var(--err)}
  .sk-row .nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t-1);font-weight:600}
  .sk-row .sub{font-size:10px;color:var(--t-4);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .sk-meta{display:flex;gap:5px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
  .sk-badge{font-family:var(--f-mono);font-size:9px;background:var(--bg-2);border:1px solid var(--line-2);padding:1px 6px;border-radius:3px;color:var(--t-3);white-space:nowrap}
  .sk-badge.warn{border-color:var(--warn);color:var(--warn)}
  .sk-badge.err{border-color:var(--err);color:var(--err)}
  .sk-badge.ok{border-color:var(--ok);color:var(--ok)}
  .sk-empty{padding:44px 24px;text-align:center;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-sm)}
  .sk-msg{margin-top:10px;padding:9px 12px;background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-1);font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-2)}

  .sk-detail{padding:16px 18px;font-family:var(--f-mono)}
  .sk-detail h2{margin:0 0 4px;font-size:var(--t-lg);color:var(--t-1);font-family:var(--f-sans);font-weight:600;letter-spacing:0}
  .sk-detail .sub{color:var(--t-4);font-size:var(--t-xs);margin-bottom:14px;word-break:break-word;line-height:1.5}
  .sk-kv{display:grid;grid-template-columns:118px minmax(0,1fr);gap:5px 12px;font-size:var(--t-xs);margin-bottom:12px}
  .sk-kv .k{color:var(--t-4)}.sk-kv .v{color:var(--t-2);word-break:break-word}.sk-kv .v.bad{color:var(--err)}
  .sk-sec{font:600 var(--t-xxs) var(--f-mono);text-transform:uppercase;letter-spacing:0;color:var(--t-3);margin:14px 0 8px;border-top:1px solid var(--line-1);padding-top:10px}
  .sk-pre{background:var(--bg-0);border:1px solid var(--line-1);border-radius:4px;padding:10px 12px;font-size:var(--t-xs);color:var(--t-2);white-space:pre-wrap;max-height:280px;overflow:auto;line-height:1.5}
  .sk-bars{display:grid;gap:8px;padding:14px}
  .sk-bar{display:grid;grid-template-columns:130px minmax(0,1fr) 58px;gap:8px;align-items:center;font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3)}
  .sk-bar-track{height:8px;background:var(--bg-0);border:1px solid var(--line-1);border-radius:99px;overflow:hidden}.sk-bar-fill{height:100%;background:var(--acc)}
  .sk-ws-pick{display:flex;flex-wrap:wrap;gap:6px;padding:10px 14px;border-bottom:1px solid var(--line-1)}
  .sk-ws-pick button{font:600 var(--t-xxs) var(--f-mono);background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-2);border-radius:var(--r-1);padding:4px 10px;cursor:pointer}
  .sk-ws-pick button.on{background:var(--acc);border-color:var(--acc);color:var(--acc-text)}

  .sk-form{display:grid;gap:10px;padding:14px}
  .sk-field{display:grid;gap:4px}.sk-field label{font:600 var(--t-xxs) var(--f-mono);text-transform:uppercase;color:var(--t-3);letter-spacing:0}
  .sk-field input,.sk-field select{width:100%;box-sizing:border-box;background:var(--bg-0);border:1px solid var(--line-2);border-radius:var(--r-1);color:var(--t-1);font:var(--t-sm) var(--f-mono);padding:7px 9px}
  .sk-form .twocol{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  @media(max-width:720px){.sk-form .twocol{grid-template-columns:1fr}}
  .sk-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .sk-hubs{display:flex;gap:6px;flex-wrap:wrap;padding:14px;border-bottom:1px solid var(--line-1)}
  .sk-hub{font:600 var(--t-xxs) var(--f-mono);background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-2);border-radius:var(--r-1);padding:5px 9px;cursor:pointer;text-decoration:none}
  .sk-hub:hover{border-color:var(--line-3);color:var(--t-1)}

  .sk-import{max-width:980px;margin:0 auto;display:grid;gap:16px}
  .sk-import-lead{font:var(--t-sm) var(--f-sans);color:var(--t-3);line-height:1.55;margin:0;padding:14px 16px 0}
  .sk-import-box{padding:14px 16px 16px;display:grid;gap:12px}
  .sk-import-box textarea{width:100%;box-sizing:border-box;min-height:88px;background:var(--bg-0);border:1px solid var(--line-2);border-radius:var(--r-1);color:var(--t-1);font:var(--t-sm) var(--f-mono);padding:10px 12px;resize:vertical;line-height:1.45}
  .sk-import-box textarea:focus{outline:none;border-color:var(--acc)}
  .sk-import-hint{font:var(--t-xxs) var(--f-mono);color:var(--t-4);line-height:1.5}
  .sk-import-status{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--bg-0);border:1px solid var(--line-1);border-radius:var(--r-1);font:var(--t-xs) var(--f-mono);color:var(--t-2)}
  .sk-import-status.busy{border-color:var(--acc-line);color:var(--acc)}
  .sk-import-status.err{border-color:var(--err);color:var(--err)}
  .sk-import-status.ok{border-color:var(--ok);color:var(--ok)}
  .sk-import-meta{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 12px}
  .sk-chip{font:600 var(--t-xxs) var(--f-mono);background:var(--bg-2);border:1px solid var(--line-2);color:var(--t-3);padding:3px 8px;border-radius:99px}
  .sk-discover-list{padding:0 8px 8px;display:grid;gap:0}
  .sk-discover-item{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:10px;align-items:start;padding:12px 8px;border-bottom:1px solid var(--line-1);font-family:var(--f-mono)}
  .sk-discover-item:last-child{border-bottom:0}
  .sk-discover-item input{margin-top:3px}
  .sk-discover-item.invalid{opacity:.72}
  .sk-discover-item h3{margin:0;font:600 var(--t-sm) var(--f-sans);color:var(--t-1)}
  .sk-discover-item p{margin:4px 0 0;font:var(--t-xs) var(--f-mono);color:var(--t-4);line-height:1.45}
  .sk-discover-tags{display:flex;flex-wrap:wrap;gap:5px;justify-content:flex-end}
  .sk-import-foot{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 16px 16px;border-top:1px solid var(--line-1);background:var(--bg-0)}
  .sk-import-foot select,.sk-import-foot input{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-1);color:var(--t-1);font:var(--t-xs) var(--f-mono);padding:6px 8px}
  .sk-import-foot label{font:var(--t-xxs) var(--f-mono);color:var(--t-3);display:flex;align-items:center;gap:6px}
  .sk-import-foot .sk-tip{cursor:help;border-bottom:1px dotted var(--t-4)}
  .sk-deploy-pick{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 12px}
  .sk-deploy-pick label{font:var(--t-xxs) var(--f-mono);color:var(--t-2);display:flex;align-items:center;gap:4px}

  .sk-inj{display:grid;gap:12px;padding:14px 16px 18px}
  .sk-inj-card{background:var(--bg-0);border:1px solid var(--line-2);border-radius:var(--r-2);overflow:hidden}
  .sk-inj-hd{display:flex;align-items:center;gap:9px;padding:11px 14px;border-bottom:1px solid var(--line-1)}
  .sk-inj-hd .nm{font:600 var(--t-sm) var(--f-sans);color:var(--t-1)}
  .sk-inj-hd .src{font:var(--t-xxs) var(--f-mono);color:var(--t-4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px}
  .sk-inj-hd .sp{margin-left:auto;display:flex;gap:8px;align-items:center;min-width:0}
  .sk-inj-bar{display:flex;align-items:center;gap:7px;padding:8px 14px;border-bottom:1px solid var(--line-1);flex-wrap:wrap;background:var(--bg-1)}
  .sk-inj-bar .lbl{font:600 var(--t-xxs) var(--f-mono);text-transform:uppercase;color:var(--t-3);letter-spacing:0}
  .sk-inj-bar select{background:var(--bg-0);border:1px solid var(--line-2);border-radius:var(--r-1);color:var(--t-1);font:var(--t-xxs) var(--f-mono);padding:3px 6px}
  .sk-chips{display:flex;flex-wrap:wrap;gap:7px;padding:12px 14px}
  .sk-wschip{display:inline-flex;flex-direction:column;gap:3px;border:1px solid var(--line-2);background:var(--bg-1);border-radius:var(--r-1);padding:6px 10px;cursor:pointer;min-width:90px;text-align:left;font-family:var(--f-mono)}
  .sk-wschip:hover{border-color:var(--line-3)}
  .sk-wschip.on{background:var(--acc-soft);border-color:var(--acc)}
  .sk-wschip.busy{opacity:.5;cursor:wait}
  .sk-wschip .w{display:flex;align-items:center;gap:6px;font-size:var(--t-xs);color:var(--t-1);font-weight:600}
  .sk-wschip .tick{width:13px;height:13px;border-radius:3px;border:1px solid var(--line-3);display:inline-flex;align-items:center;justify-content:center;font-size:9px;color:var(--acc-text);flex-shrink:0}
  .sk-wschip.on .tick{background:var(--acc);border-color:var(--acc)}
  .sk-wschip .h{font-size:9px;color:var(--t-4);white-space:nowrap}
  .sk-inj-empty{padding:30px 18px;text-align:center;color:var(--t-3);font:var(--t-sm) var(--f-mono);line-height:1.6}

  .sk-patchwrap{max-width:1120px;margin:0 auto}
  .sk-patch-heads{display:grid;grid-template-columns:1fr 184px 1fr;align-items:center;padding:14px 18px 8px;font:600 var(--t-xxs) var(--f-mono);text-transform:uppercase;letter-spacing:0;color:var(--t-3)}
  .sk-patch-heads .hint{text-transform:none;text-align:center;color:var(--t-4);font-weight:400;letter-spacing:0}
  .sk-patch-heads span:last-child{text-align:right}
  .sk-patch{display:grid;grid-template-columns:1fr 184px 1fr;align-items:start;padding:0 18px 6px}
  .sk-col{display:flex;flex-direction:column;min-width:0}
  .sk-wires{width:100%;display:block;overflow:visible;pointer-events:none;align-self:stretch}
  .sk-wires .wire{stroke:var(--line-3);stroke-width:1.5;opacity:.3}
  .sk-wires .wire.dim{stroke-width:1.25;opacity:.12}
  .sk-wires .wire.on{stroke:var(--acc);stroke-width:2.25;opacity:.95}
  .sk-pnode{height:46px;margin-bottom:10px;display:flex;align-items:center;gap:9px;padding:0 11px;background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-1);cursor:pointer;font-family:var(--f-mono);text-align:left;box-sizing:border-box;transition:border-color .08s,background .08s}
  .sk-pnode:hover{border-color:var(--line-3)}
  .sk-pnode .pn-main{min-width:0;flex:1;display:flex;flex-direction:column;gap:1px}
  .sk-pnode .pn-nm{font-size:var(--t-xs);color:var(--t-1);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sk-pnode .pn-sub{font-size:9px;color:var(--t-4);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sk-pnode .pn-port{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--line-3);background:var(--bg-0);flex-shrink:0}
  .sk-pnode.skill .pn-port{margin-left:6px}
  .sk-pnode.skill.sel{border-color:var(--acc);background:var(--acc-soft)}
  .sk-pnode.skill.sel .pn-port{background:var(--acc);border-color:var(--acc)}
  .sk-pnode.ws .pn-port.in{margin-right:6px}
  .sk-pnode.ws.on{border-color:var(--acc);background:var(--acc-soft)}
  .sk-pnode.ws.on .pn-port{background:var(--acc);border-color:var(--acc)}
  .sk-pnode.busy{opacity:.5;cursor:wait}
  .sk-pnode .pn-on{color:var(--acc)}
  .sk-pnode .pn-count{font-size:9px;color:var(--t-3);background:var(--bg-2);border:1px solid var(--line-2);border-radius:99px;padding:1px 6px;flex-shrink:0}
  .sk-patch-detail{margin:8px 18px 18px;border:1px solid var(--line-2);border-radius:var(--r-2);background:var(--bg-0);overflow:hidden}
  .sk-patch-detail .pd-head{display:flex;align-items:center;gap:8px;padding:11px 14px;border-bottom:1px solid var(--line-1);flex-wrap:wrap}
  .sk-patch-detail .pd-nm{font:600 var(--t-sm) var(--f-sans);color:var(--t-1)}
  .sk-patch-detail .pd-src{margin-left:auto;font:var(--t-xxs) var(--f-mono);color:var(--t-4);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .sk-patch-detail .pd-bar{display:flex;align-items:center;gap:7px;padding:9px 14px;border-bottom:1px solid var(--line-1);flex-wrap:wrap}
  .sk-patch-detail .pd-mode{font:var(--t-xxs) var(--f-mono);color:var(--t-3);display:flex;gap:5px;align-items:center}
  .sk-patch-detail .pd-bar select{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-1);color:var(--t-1);font:var(--t-xxs) var(--f-mono);padding:3px 6px}
  .sk-patch-detail .pd-err{padding:8px 14px;color:var(--err);font:var(--t-xs) var(--f-mono);border-bottom:1px solid var(--line-1)}
  .sk-patch-detail .pd-md{padding:10px 14px}
  .sk-patch-detail .pd-md-h{font:600 var(--t-xxs) var(--f-mono);text-transform:uppercase;color:var(--t-3);margin-bottom:6px}
  .sk-patch-detail .pd-md pre{margin:0;background:var(--bg-1);border:1px solid var(--line-1);border-radius:4px;padding:10px 12px;font:var(--t-xs) var(--f-mono);color:var(--t-2);white-space:pre-wrap;max-height:240px;overflow:auto;line-height:1.5}

  .sk-pending{display:flex;align-items:center;gap:10px;margin:0 16px 6px;padding:9px 12px;border:1px solid var(--warn);background:var(--bg-1);border-radius:var(--r-1);font-family:var(--f-mono)}
  .sk-pending-ic{color:var(--warn);font-size:14px;flex-shrink:0}
  .sk-pending-txt{flex:1;min-width:0;font-size:var(--t-xs);color:var(--t-2);line-height:1.45}
  .sk-pending-txt b{color:var(--t-1)}
  .sk-pending-confirm{display:flex;align-items:center;gap:6px;font:600 var(--t-xxs) var(--f-mono);color:var(--warn);white-space:nowrap}
  .sk-pnode .pn-stale{font:600 9px var(--f-mono);color:var(--bg-0);background:var(--warn);border-radius:99px;padding:1px 6px;flex-shrink:0}
  .sk-stat-pending{cursor:pointer;border-color:var(--warn)!important;color:var(--warn)!important}
  .sk-stat-pending b{color:var(--warn)!important}
`;

export default class SkillsPanel {
  constructor() {
    this.root = null;
    this.api = null;
    this._ctx = null;
    this._section = 'inventory';
    this.skills = [];
    this.overview = { summary: {}, groups: [], links: [], agents_by_workspace: {} };
    this.workspaces = [];
    this.hubs = [];
    this.selectedKey = null;
    this.detail = null;
    this.wsFilter = '';
    this.unsubEvent = null;
    this._msg = '';
    this.importSource = '';
    this.importPhase = 'idle'; // idle | discovering | ready | importing
    this.importStatus = '';
    this.importError = false;
    this.resolved = null;
    this.selectedSubpaths = new Set();
    this.form = { mode: 'symlink' };
    this.deployScope = 'all';
    this.deployWorkspaces = new Set();
    this.injMode = {};            // per-alias inject mode override
    this.injBusy = new Set();     // `${alias}@${ws}` toggles in flight
    this.patchSkill = null;       // selected skill in the patch bay
    this.patchDetail = null;      // fetched /catalog/<alias> detail
    this._patchLoading = null;    // alias whose detail is in flight
    this._onResize = null;
    this._alive = false;          // false after unmount → deferred renders no-op
    this._isCurrent = null;       // shell's "is this mount still the active one?"
    this.agents = [];             // /api/agents — for restart-to-apply state
    this._confirmRestartAffected = false;
    this._restartBusy = false;
    this._restartHoldIds = new Set();
    this._restartHoldUntil = 0;
    this._restartHoldTimer = null;
    this._restartPollTimer = null;
    this._reloadTimer = null;
    this._reloadInFlight = false;
    this._reloadAgain = false;
    this._agentsReloadTimer = null;
    this._agentsReloadInFlight = false;
    this._agentsReloadAgain = false;
  }

  _pendingAgents() {
    return (this.agents || []).filter(a => a.status === 'running' && a.restart_pending);
  }

  _pendingByWs() {
    const m = {};
    for (const a of this._pendingAgents()) {
      if (a.workspace) m[a.workspace] = (m[a.workspace] || 0) + 1;
    }
    return m;
  }

  sections() {
    const s = this.overview.summary || {};
    const vendor = this.skills.filter(x => VENDOR.has(x.source_type)).length;
    const wss = this._workspaceNames().length;
    return [
      { group: 'Inventory', items: [
        { id: 'inventory', label: 'Active', icon: 'bolt', badge: s.active || '' },
        { id: 'workspace', label: 'Workspaces', icon: 'layers', badge: wss || '' },
        { id: 'vendor', label: 'Vendor', icon: 'eye', badge: vendor || '' },
      ] },
      { group: 'Manage', items: [
        { id: 'import', label: 'Add skill', icon: 'download' },
        { id: 'inject', label: 'Inject', icon: 'grid', badge: this._catalogSkills().length || '' },
      ] },
    ];
  }

  async mount(container, api, ctx) {
    this.api = api;
    this._ctx = ctx || null;
    this.root = container;
    this._alive = true;
    this._isCurrent = ctx?.isCurrent || null;
    if (ctx?.section) this._section = ctx.section;
    if (!document.getElementById('skills-panel-css')) {
      const s = document.createElement('style');
      s.id = 'skills-panel-css';
      s.textContent = CSS;
      document.head.appendChild(s);
    }
    render(html`<div class="sk-wrap"><div class="sk-body"><div class="sk-empty">loading...</div></div></div>`, this.root);
    await this._reload();
    // The shell may have superseded this mount (a newer renderDetail wiped the
    // container) while _reload was in flight — bail so we don't render into
    // detached lit markers.
    if (!this._stillMounted()) return;
    this._subscribeLive();
    this._onResize = () => this._paintWires();
    window.addEventListener('resize', this._onResize);
    ctx?.onSectionChange?.((id) => { this._section = id; this._render(); });
    this._render();
  }

  _stillMounted() {
    return this._alive && !!this.root && (!this._isCurrent || this._isCurrent());
  }

  unmount() {
    this._alive = false;
    if (this.unsubEvent) this.unsubEvent();
    if (this._onResize) window.removeEventListener('resize', this._onResize);
    if (this._reloadTimer) clearTimeout(this._reloadTimer);
    this._reloadTimer = null;
    if (this._agentsReloadTimer) clearTimeout(this._agentsReloadTimer);
    this._agentsReloadTimer = null;
    if (this._restartHoldTimer) clearTimeout(this._restartHoldTimer);
    this._restartHoldTimer = null;
    if (this._restartPollTimer) clearInterval(this._restartPollTimer);
    this._restartPollTimer = null;
  }

  async _reload({ clearMsg = true } = {}) {
    if (this._reloadInFlight) {
      this._reloadAgain = true;
      return;
    }
    this._reloadInFlight = true;
    try {
      const [skills, overview, workspaces, hubs, agents] = await Promise.all([
        this._json('/api/plugins/skills/skills'),
        this._json('/api/plugins/skills/overview'),
        this._json('/api/workspaces').catch(() => ({ workspaces: [] })),
        this._json('/api/plugins/skills/hubs').catch(() => ({ hubs: [] })),
        this._json('/api/agents').catch(() => ([])),
      ]);
      this.skills = skills.skills || [];
      this.overview = overview || this.overview;
      this.workspaces = Array.isArray(workspaces) ? workspaces : (workspaces.workspaces || []);
      this.hubs = hubs.hubs || [];
      this.agents = Array.isArray(agents) ? agents : (agents.agents || []);
      if (!this.deployWorkspaces.size && this._workspaceNames().length) {
        this.deployWorkspaces = new Set(this._workspaceNames());
      }
      if (clearMsg) this._msg = '';
    } catch (e) {
      this._msg = `load failed: ${e.message || e}`;
    }
    this._reloadInFlight = false;
    // A reload triggered by a live event may resolve after the lens was
    // unmounted/superseded; don't touch the reused nav/detail DOM.
    if (!this._stillMounted()) return;
    this._pruneRestartHold();
    this._ctx?.refreshNav?.();
    this._render();
    if (this._reloadAgain && this._stillMounted()) {
      this._reloadAgain = false;
      this._scheduleReload({ clearMsg });
    }
  }

  _scheduleReload(opts = {}) {
    if (!this._stillMounted()) return;
    if (this._reloadTimer) clearTimeout(this._reloadTimer);
    this._reloadTimer = setTimeout(() => {
      this._reloadTimer = null;
      this._reload(opts);
    }, 180);
  }

  async _reloadAgents() {
    if (this._agentsReloadInFlight) {
      this._agentsReloadAgain = true;
      return;
    }
    this._agentsReloadInFlight = true;
    try {
      const agents = await this._json('/api/agents').catch(() => ([]));
      this.agents = Array.isArray(agents) ? agents : (agents.agents || []);
    } finally {
      this._agentsReloadInFlight = false;
    }
    if (!this._stillMounted()) return;
    this._pruneRestartHold();
    this._render();
    if (this._agentsReloadAgain) {
      this._agentsReloadAgain = false;
      this._scheduleAgentsReload();
    }
  }

  _scheduleAgentsReload() {
    if (!this._stillMounted()) return;
    if (this._agentsReloadTimer) clearTimeout(this._agentsReloadTimer);
    this._agentsReloadTimer = setTimeout(() => {
      this._agentsReloadTimer = null;
      this._reloadAgents();
    }, 180);
  }

  _restartHoldActive() {
    if (!this._restartHoldIds.size) return false;
    if (Date.now() <= this._restartHoldUntil) return true;
    this._clearRestartHold();
    return false;
  }

  _heldRestartAgents() {
    if (!this._restartHoldIds.size) return [];
    return (this.agents || []).filter(a => this._restartHoldIds.has(a.id));
  }

  _clearRestartHold() {
    this._restartHoldIds = new Set();
    this._restartHoldUntil = 0;
    if (this._restartHoldTimer) clearTimeout(this._restartHoldTimer);
    this._restartHoldTimer = null;
    if (this._restartPollTimer) clearInterval(this._restartPollTimer);
    this._restartPollTimer = null;
  }

  _pruneRestartHold() {
    if (!this._restartHoldIds.size) return;
    const byId = new Map((this.agents || []).map(a => [a.id, a]));
    const keep = new Set();
    for (const id of this._restartHoldIds) {
      const a = byId.get(id);
      if (!a) continue;
      if (a.status !== 'running' || a.restart_pending) keep.add(id);
    }
    this._restartHoldIds = keep;
    if (!keep.size) this._clearRestartHold();
  }

  _startRestartHold(ids) {
    const clean = (ids || []).filter(Boolean);
    if (!clean.length) return;
    this._restartHoldIds = new Set(clean);
    this._restartHoldUntil = Date.now() + RESTART_GRACE_MS;
    if (this._restartHoldTimer) clearTimeout(this._restartHoldTimer);
    this._restartHoldTimer = setTimeout(() => {
      this._clearRestartHold();
      if (this._stillMounted()) this._render();
    }, RESTART_GRACE_MS);
    if (this._restartPollTimer) clearInterval(this._restartPollTimer);
    this._restartPollTimer = setInterval(() => {
      if (this._restartHoldActive()) this._reloadAgents();
    }, 1500);
  }

  async _json(url, opts) {
    const r = await this.api.fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }

  _workspaceNames() {
    const names = new Set([
      ...this.workspaces.map(w => w.name).filter(Boolean),
      ...this.skills.map(s => s.workspace).filter(Boolean),
      ...Object.keys(this.overview.agents_by_workspace || {}),
    ]);
    return [...names].sort();
  }

  _activeGroups() {
    return (this.overview.groups || []).filter(g => g.injectable && !VENDOR.has(g.source_type));
  }

  // Backend emits one group per (workspace, skill); merge by identity so the
  // Active list shows each skill once with a meaningful "N workspaces" count.
  _activeMerged() {
    const by = new Map();
    for (const g of this._activeGroups()) {
      const k = `m:${g.source_type}:${g.name}`;
      const m = by.get(k);
      if (!m) {
        by.set(k, {
          ...g, key: k,
          workspaces: [...(g.workspaces || [])],
          deployments: [...(g.deployments || [])],
          agent_total: g.agent_total || 0, agent_running: g.agent_running || 0,
          exact_uses: g.exact_uses || 0, exact_usage_tokens: g.exact_usage_tokens || 0,
          token_estimate: g.token_estimate || 0, managed_import: !!g.managed_import,
          valid: g.valid !== false,
        });
      } else {
        m.workspaces = [...new Set([...m.workspaces, ...(g.workspaces || [])])];
        m.deployments.push(...(g.deployments || []));
        m.agent_total += g.agent_total || 0; m.agent_running += g.agent_running || 0;
        m.exact_uses += g.exact_uses || 0; m.exact_usage_tokens += g.exact_usage_tokens || 0;
        m.token_estimate = Math.max(m.token_estimate, g.token_estimate || 0);
        m.managed_import = m.managed_import || !!g.managed_import;
        m.valid = m.valid && g.valid !== false;
      }
    }
    return [...by.values()].sort((a, b) => String(a.name).localeCompare(String(b.name)));
  }

  _render() {
    if (!this._stillMounted()) return;
    const s = this.overview.summary || {};
    render(html`
      <div class="sk-wrap">
        <div class="sk-head">
          <div class="eyebrow">Cognitive layer</div>
          <h1>Skills</h1>
          <div class="row">
            <div class="stats">
              <span class="stat"><b>${s.active || 0}</b> active copies</span>
              <span class="stat"><b>${s.unique || 0}</b> unique</span>
              <span class="stat"><b>${this._fmtTokens(s.token_estimate || 0)}</b> est. tokens</span>
              <span class="stat"><b>${s.exact_skill_uses || 0}</b> exact uses</span>
              <span class="stat" title="Total LLM tokens used by agents in workspaces where skills are deployed — fleet-wide context, not per-skill attribution."><b>${this._fmtTokens(s.usage_tokens_by_skill_exposure || 0)}</b> workspace agent tokens</span>
              ${s.invalid ? html`<span class="stat bad"><b>${s.invalid}</b> invalid</span>` : ''}
              ${this._pendingAgents().length ? html`<button class="stat sk-stat-pending" type="button" title="agents running an older config — open Inject to restart them" @click=${() => { this._section = 'inject'; this._ctx?.refreshNav?.(); this._render(); }}>⟳ <b>${this._pendingAgents().length}</b> pending restart</button>` : ''}
            </div>
            <div class="actions"><button class="sk-btn primary" type="button" @click=${() => this._refreshInventory()}>Refresh inventory</button></div>
          </div>
          ${this._msg ? html`<div class="sk-msg">${this._msg}</div>` : ''}
        </div>
        <div class="sk-body">${this._sectionBody()}</div>
      </div>`, this.root);
    if (this._section === 'inject') {
      requestAnimationFrame(() => this._paintWires());
      this._ensurePatchDetail();
    }
  }

  _sectionBody() {
    if (this._section === 'workspace') return this._tmplWorkspace();
    if (this._section === 'vendor') return this._tmplVendor();
    if (this._section === 'import') return this._tmplImport();
    if (this._section === 'inject') return this._tmplInject();
    return this._tmplInventory();
  }

  _tmplInventory() {
    const groups = this._activeMerged();
    if (!groups.length) return html`<div class="sk-empty">No active injectable skills.</div>`;
    return html`<div class="sk-grid">
      <div class="sk-card"><div class="hd"><span class="ttl">Active skills</span><span class="pill">next start</span></div>
        <div class="bd">${groups.map(g => this._row(g))}</div></div>
      <div class="sk-card"><div class="hd"><span class="ttl">Detail</span></div><div class="bd">${this._detailTmpl()}</div></div>
    </div>`;
  }

  _tmplWorkspace() {
    const names = this._workspaceNames();
    const cur = this.wsFilter || names[0] || '';
    const rows = this._activeGroups().filter(g => (g.workspaces || []).includes(cur));
    const agents = (this.overview.agents_by_workspace || {})[cur] || {};
    return html`<div class="sk-grid">
      <div class="sk-card"><div class="hd"><span class="ttl">Workspace</span><span class="pill">${agents.running || 0}/${agents.total || 0} running</span></div>
        <div class="sk-ws-pick">${names.map(w => html`<button type="button" class=${w === cur ? 'on' : ''} @click=${() => { this.wsFilter = w; this.selectedKey = null; this.detail = null; this._render(); }}>${w}</button>`)}</div>
        <div class="bd">${rows.length ? rows.map(g => this._row(g)) : html`<div class="sk-empty">No active skills in this workspace.</div>`}</div></div>
      <div class="sk-card"><div class="hd"><span class="ttl">Detail</span></div><div class="bd">${this._detailTmpl()}</div></div>
    </div>`;
  }

  _tmplVendor() {
    const groups = this._groupRows(this.skills.filter(s => VENDOR.has(s.source_type)));
    if (!groups.length) return html`<div class="sk-empty">No vendor-native skills discovered.</div>`;
    return html`<div class="sk-grid">
      <div class="sk-card"><div class="hd"><span class="ttl">Vendor-native</span><span class="pill">read-only</span></div>
        <div class="bd">${groups.map(g => this._row(g))}</div></div>
      <div class="sk-card"><div class="hd"><span class="ttl">Detail</span></div><div class="bd">${this._detailTmpl()}</div></div>
    </div>`;
  }

  _tmplImport() {
    const skills = this.resolved?.skills || [];
    const validSelected = skills.filter(s =>
      this.selectedSubpaths.has(this._subpathKey(s)) && s.valid !== false
    );
    const statusCls = this.importError ? 'err' : (this.importPhase === 'ready' ? 'ok' : (this.importPhase === 'discovering' || this.importPhase === 'importing' ? 'busy' : ''));
    return html`<div class="sk-import">
      <div class="sk-card span12">
        <div class="hd"><span class="ttl">Add skills</span><span class="pill">auto-managed</span></div>
        <p class="sk-import-lead">Paste anything — GitHub repo or tree URL, <code>npx skills add …</code>, <code>npm install -g …</code>, or a local folder. Relaydeck fetches the source, finds every <code>SKILL.md</code>, and tracks it for you.</p>
        ${this.hubs.length ? html`<div class="sk-hubs">${this.hubs.map(h => html`<button class="sk-hub" type="button" @click=${() => this._useExample(h.import_hint || h.url)}>${h.name}</button>`)}</div>` : ''}
        <div class="sk-import-box">
          <textarea
            placeholder="https://github.com/mattpocock/skills/tree/main/skills/engineering/grill-with-docs"
            .value=${this.importSource}
            @input=${e => { this.importSource = e.target.value; this.importPhase = 'idle'; this.resolved = null; this.selectedSubpaths = new Set(); this._render(); }}
          ></textarea>
          <div class="sk-import-hint">Examples: repo root · skill folder · engineering/ (multi-skill) · npx skills add URL --skill name · npm install -g @scope/pkg · ~/path/to/skill</div>
          <div class="sk-actions">
            <button class="sk-btn primary" type="button" ?disabled=${!this.importSource.trim() || this.importPhase === 'discovering'} @click=${() => this._discover()}>${this.importPhase === 'discovering' ? 'Finding…' : 'Find skills'}</button>
            ${skills.length ? html`<button class="sk-btn" type="button" @click=${() => this._toggleAllSkills(true)}>Select all</button>` : ''}
            ${skills.length ? html`<button class="sk-btn" type="button" @click=${() => this._toggleAllSkills(false)}>Clear</button>` : ''}
          </div>
          ${this.importStatus ? html`<div class="sk-import-status ${statusCls}">${this.importStatus}</div>` : ''}
        </div>
        ${this.resolved ? html`<div class="sk-import-meta">
          <span class="sk-chip">${this.resolved.source?.display || this.resolved.parsed?.kind || 'source'}</span>
          ${this.resolved.source?.source_ref ? html`<span class="sk-chip">ref ${this.resolved.source.source_ref}</span>` : ''}
          ${this.resolved.source?.source_subpath ? html`<span class="sk-chip">${this.resolved.source.source_subpath}</span>` : ''}
          <span class="sk-chip">${skills.length} skill${skills.length === 1 ? '' : 's'}</span>
        </div>` : ''}
        ${skills.length ? html`<div class="sk-discover-list">
          ${skills.map(s => this._discoverRow(s))}
        </div>` : (this.importPhase === 'ready' ? html`<div class="sk-empty">No skills found at that source.</div>` : html`<div class="sk-empty">Paste a source above, then click <b>Find skills</b>.</div>`)}
        ${skills.length ? html`<div class="sk-import-foot">
          <div style="display:grid;gap:8px;flex:1;min-width:220px">
            <div style="font:600 var(--t-xxs) var(--f-mono);color:var(--t-3);text-transform:uppercase">Deploy after import</div>
            <label><input type="radio" name="sk-deploy" .checked=${this.deployScope === 'all'} @change=${() => { this.deployScope = 'all'; this._render(); }}> All workspaces (${this._workspaceNames().length})</label>
            <label><input type="radio" name="sk-deploy" .checked=${this.deployScope === 'pick'} @change=${() => { this.deployScope = 'pick'; this._render(); }}> Choose workspaces</label>
            <label><input type="radio" name="sk-deploy" .checked=${this.deployScope === 'none'} @change=${() => { this.deployScope = 'none'; this._render(); }}> Catalog only (inject later)</label>
          </div>
          <label>Install
            <select .value=${this.form.mode} @change=${e => { this.form = { ...this.form, mode: e.target.value }; this._render(); }}>
              <option value="symlink">symlink (recommended)</option>
              <option value="copy">copy into workspace</option>
              <option value="reference">reference only</option>
            </select>
          </label>
          <button class="sk-btn primary" type="button"
            ?disabled=${!validSelected.length || this.importPhase === 'importing'}
            @click=${() => this._importSelected()}>
            ${this.importPhase === 'importing' ? 'Importing…' : `Import ${validSelected.length} skill${validSelected.length === 1 ? '' : 's'}`}
          </button>
        </div>
        ${this.deployScope === 'pick' ? html`<div class="sk-deploy-pick">
          ${this._workspaceNames().map(w => html`<label><input type="checkbox" .checked=${this.deployWorkspaces.has(w)}
            @change=${e => this._toggleDeployWs(w, e.target.checked)}> ${w}</label>`)}
        </div>` : ''}` : ''}
      </div>
    </div>`;
  }

  _discoverRow(s) {
    const key = this._subpathKey(s);
    const on = this.selectedSubpaths.has(key);
    return html`<div class="sk-discover-item ${s.valid === false ? 'invalid' : ''}">
      <input type="checkbox" .checked=${on} ?disabled=${s.valid === false}
        @change=${e => this._toggleSkill(key, e.target.checked)} />
      <div>
        <h3>${s.display_name || s.name}</h3>
        <p>${s.description || s.relative_subpath || s.subpath || 'No description'}</p>
        ${s.valid === false ? html`<p style="color:var(--err)">${(s.errors || []).join('; ')}</p>` : ''}
      </div>
      <div class="sk-discover-tags">
        ${s.valid === false ? html`<span class="sk-badge err">invalid</span>` : html`<span class="sk-badge ok">ready</span>`}
        ${s.relative_subpath && s.relative_subpath !== '.' ? html`<span class="sk-badge">${s.relative_subpath}</span>` : ''}
      </div>
    </div>`;
  }

  // ── Inject: central management matrix ──────────────────────────────
  // Each managed (catalog) skill gets a row of workspace toggles. Toggling
  // symlinks/copies the one catalog copy into workspaces/<ws>/skills/<alias>
  // (inject) or removes it (un-inject) — one place to manage, many targets.

  _catalogSkills() {
    return (this.overview.links || [])
      .filter(l => l.workspace === CATALOG_WS)
      .slice()
      .sort((a, b) => String(a.alias).localeCompare(String(b.alias)));
  }

  _deployedFor(alias) {
    return new Set((this.overview.links || [])
      .filter(l => l.alias === alias && l.workspace !== CATALOG_WS)
      .map(l => l.workspace));
  }

  _wsHarnesses(ws) {
    const a = (this.overview.agents_by_workspace || {})[ws];
    if (!a || !a.types || !Object.keys(a.types).length) return '';
    return Object.entries(a.types).map(([t, n]) => `${t}·${n}`).join('  ');
  }

  // ── Inject: skill ⇄ workspace patch bay ────────────────────────────
  // A bipartite wiring board. Skills (left) and workspaces (right) are
  // ports; an injection is a wire between them. Selecting a skill lights
  // its wires; clicking a workspace patches/unpatches that one skill. The
  // panel below is the focused per-skill management surface.

  _tmplInject() {
    const cats = this._catalogSkills();
    const wss = this._workspaceNames();
    if (cats.length && !cats.some(c => c.alias === this.patchSkill)) {
      this.patchSkill = cats[0].alias;
    }
    if (!cats.length) {
      return html`<div class="sk-patchwrap"><div class="sk-card span12">
        <div class="hd"><span class="ttl">Inject — skill ⇄ workspace wiring</span>
          <button class="sk-btn primary" type="button" style="margin-left:auto" @click=${() => this._refreshInventory()}>Refresh</button></div>
        <div class="sk-inj-empty">No managed skills to wire yet.<br>Use <b>Add skill</b> to import one, or open a skill in <b>Active</b>/<b>Vendor</b> and hit <b>Centralize</b>.</div>
      </div></div>`;
    }
    return html`<div class="sk-patchwrap">
      <div class="sk-card span12">
        <div class="hd">
          <span class="ttl">Inject — skill ⇄ workspace wiring</span>
          <span style="display:flex;gap:6px;margin-left:auto;align-items:center">
            <span class="pill">${cats.length} skills · ${wss.length} workspaces</span>
            <button class="sk-btn" type="button" @click=${() => this._refreshImports()}>Check updates</button>
            <button class="sk-btn primary" type="button" @click=${() => this._refreshInventory()}>Refresh</button>
          </span>
        </div>
        ${this._pendingBanner()}
        <div class="sk-patch-heads">
          <span>Skills</span>
          <span class="hint">pick a skill · click a workspace to patch ⇄ unpatch</span>
          <span>Workspaces · harnesses</span>
        </div>
        <div class="sk-patch">
          <div class="sk-col left">${cats.map(c => this._patchSkillNode(c, wss))}</div>
          <svg class="sk-wires" preserveAspectRatio="none"></svg>
          <div class="sk-col right">${wss.length ? wss.map(w => this._patchWsNode(w)) : html`<div class="sk-inj-empty" style="padding:14px">No workspaces yet.</div>`}</div>
        </div>
        ${this._patchDetailTmpl()}
      </div>
    </div>`;
  }

  _patchSkillNode(c, wss) {
    const dep = this._deployedFor(c.alias);
    const sel = this.patchSkill === c.alias;
    const warn = c.review_status === 'update-available' || c.review_status === 'changed-locally';
    return html`<button class="sk-pnode skill ${sel ? 'sel' : ''}" type="button" @click=${() => this._selectPatch(c.alias)}>
      <span class="dot ${warn ? 'warn' : ''}"></span>
      <span class="pn-main">
        <span class="pn-nm">${c.alias}</span>
        <span class="pn-sub">${dep.size}/${wss.length} ws${warn ? ' · update' : ''}</span>
      </span>
      <span class="pn-port"></span>
    </button>`;
  }

  _patchWsNode(w) {
    const sel = this.patchSkill;
    const on = sel ? this._deployedFor(sel).has(w) : false;
    const busy = sel && this.injBusy.has(`${sel}@${w}`);
    const harness = this._wsHarnesses(w);
    const total = (this.overview.links || []).filter(l => l.workspace === w).length;
    const stale = this._pendingByWs()[w] || 0;
    return html`<button class="sk-pnode ws ${on ? 'on' : ''} ${busy ? 'busy' : ''}" type="button"
      ?disabled=${busy}
      title=${on ? `Remove ${sel} from ${w}` : (sel ? `Inject ${sel} into ${w}` : 'select a skill first')}
      @click=${() => this._togglePatch(w)}>
      <span class="pn-port in"></span>
      <span class="pn-main">
        <span class="pn-nm">${w}${on ? html` <span class="pn-on">✓</span>` : ''}</span>
        <span class="pn-sub">${harness || 'no agents'}</span>
      </span>
      ${stale ? html`<span class="pn-stale" title="${stale} agent(s) here running an older config — restart to apply">⟳${stale}</span>` : ''}
      ${total ? html`<span class="pn-count" title="${total} skill(s) injected here">${total}</span>` : ''}
    </button>`;
  }

  _patchDetailTmpl() {
    const alias = this.patchSkill;
    if (!alias) return '';
    const c = this._catalogSkills().find(x => x.alias === alias) || {};
    const dep = this._deployedFor(alias);
    const wss = this._workspaceNames();
    const d = this.patchDetail && this.patchDetail.alias === alias ? this.patchDetail : null;
    const mode = this.injMode[alias] || c.mode || 'symlink';
    const st = c.review_status;
    const stCls = (st === 'update-available' || st === 'changed-locally') ? 'warn'
      : (st === 'imported' || st === 'linked') ? 'ok' : '';
    const reach = {};
    dep.forEach(w => {
      const a = (this.overview.agents_by_workspace || {})[w];
      if (a && a.types) for (const [t, n] of Object.entries(a.types)) reach[t] = (reach[t] || 0) + n;
    });
    const reachStr = Object.entries(reach).map(([t, n]) => `${t}·${n}`).join('  ');
    const errors = (d && d.meta && d.meta.errors) || [];
    return html`<div class="sk-patch-detail">
      <div class="pd-head">
        <span class="dot ${stCls === 'warn' ? 'warn' : ''}"></span>
        <span class="pd-nm">${alias}</span>
        ${st ? html`<span class="sk-badge ${stCls}">${st}</span>` : ''}
        <span class="sk-badge">${dep.size}/${wss.length} ws</span>
        <span class="sk-badge">${reachStr ? `reaches ${reachStr}` : 'no agents reached'}</span>
        <span class="pd-src" title=${c.source_url || ''}>${c.source_url || 'local'}</span>
      </div>
      <div class="pd-bar">
        <button class="sk-btn" type="button" @click=${() => this._injectAll(alias, wss, true)}>Inject all</button>
        <button class="sk-btn" type="button" @click=${() => this._injectAll(alias, wss, false)}>Remove all</button>
        <label class="pd-mode">mode
          <select .value=${mode} @change=${e => { this.injMode = { ...this.injMode, [alias]: e.target.value }; this._render(); }}>
            <option value="symlink">symlink</option>
            <option value="copy">copy</option>
          </select>
        </label>
        <span style="margin-left:auto"></span>
        <button class="sk-btn" type="button" @click=${() => this._refreshImports()}>Check updates</button>
        <button class="sk-btn" type="button" @click=${() => this._removeCatalog(alias, dep)}>Remove from catalog</button>
      </div>
      ${errors.length ? html`<div class="pd-err">${errors.join('; ')}</div>` : ''}
      <div class="pd-md">
        <div class="pd-md-h">SKILL.md</div>
        <pre>${d ? (d.body_preview || '(no SKILL.md body)') : 'loading…'}</pre>
      </div>
    </div>`;
  }

  _paintWires() {
    if (this._section !== 'inject' || !this._stillMounted()) return;
    const svg = this.root.querySelector('.sk-wires');
    if (!svg) return;
    const cats = this._catalogSkills();
    const wss = this._workspaceNames();
    const P = 56;
    const H = Math.max(cats.length, wss.length, 1) * P;
    const W = svg.clientWidth || 180;
    const cy = i => i * P + 23;
    const wire = (y1, y2, cls) =>
      `<path d="M0 ${y1} C ${W * 0.5} ${y1}, ${W * 0.5} ${y2}, ${W} ${y2}" class="${cls}" fill="none"/>`;
    const drawFor = (alias, cls) => {
      const si = cats.findIndex(c => c.alias === alias);
      if (si < 0) return '';
      const dep = this._deployedFor(alias);
      let s = '';
      wss.forEach((w, wi) => { if (dep.has(w)) s += wire(cy(si), cy(wi), cls); });
      return s;
    };
    let paths = '';
    const sel = this.patchSkill;
    if (sel) {
      cats.forEach(c => { if (c.alias !== sel) paths += drawFor(c.alias, 'wire dim'); });
      paths += drawFor(sel, 'wire on');
    } else {
      cats.forEach(c => { paths += drawFor(c.alias, 'wire'); });
    }
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.style.height = `${H}px`;
    svg.innerHTML = paths;
  }

  _selectPatch(alias) {
    this.patchSkill = alias;
    this.patchDetail = null;
    this._render();   // post-render hook lazy-loads the detail for `alias`
  }

  // Fetch the selected skill's SKILL.md/meta once per alias (covers both the
  // default auto-selection and explicit clicks) without re-render loops.
  _ensurePatchDetail() {
    const alias = this.patchSkill;
    if (!alias) return;
    if (this.patchDetail && this.patchDetail.alias === alias) return;
    if (this._patchLoading === alias) return;
    this._patchLoading = alias;
    this._json(`/api/plugins/skills/catalog/${encodeURIComponent(alias)}`)
      .then(d => { this._patchLoading = null; if (d && !d.error) { this.patchDetail = d; this._render(); } })
      .catch(() => { this._patchLoading = null; });
  }

  _togglePatch(w) {
    const alias = this.patchSkill;
    if (!alias) { this._msg = 'Select a skill on the left first.'; this._render(); return; }
    if (this._deployedFor(alias).has(w)) return this._uninject(alias, w);
    return this._inject(alias, w, this.injMode[alias] || 'symlink');
  }

  // Injecting/removing a skill leaves running agents in that workspace behind
  // their config — they apply it on restart. Surface that + offer a one-click
  // (inline-confirmed) bulk restart of exactly the stale agents.
  _pendingBanner() {
    const pend = this._pendingAgents();
    const restartActive = this._restartHoldActive();
    const held = restartActive ? this._heldRestartAgents() : [];
    if (!pend.length && !held.length) return '';
    const wss = new Set(pend.map(a => a.workspace).filter(Boolean));
    const heldPending = held.filter(a => a.restart_pending).length;
    return html`<div class="sk-pending">
      <span class="sk-pending-ic">⟳</span>
      <div class="sk-pending-txt">${restartActive
        ? html`Restarting <b>${held.length}</b> affected agent${held.length === 1 ? '' : 's'} — <b>${heldPending}</b> still waiting to apply skill changes.`
        : html`<b>${pend.length}</b> agent${pend.length === 1 ? '' : 's'} in <b>${wss.size}</b> workspace${wss.size === 1 ? '' : 's'} ${pend.length === 1 ? 'is' : 'are'} running an older config — skill changes apply on restart.`}</div>
      ${restartActive
        ? html`<button class="sk-btn primary" type="button" disabled>Restarting…</button>`
        : this._confirmRestartAffected
        ? html`<span class="sk-pending-confirm">Restart ${pend.length}?
            <button class="sk-btn primary" type="button" ?disabled=${this._restartBusy} @click=${() => this._restartAffected()}>${this._restartBusy ? 'Restarting…' : 'Yes, restart'}</button>
            <button class="sk-btn" type="button" ?disabled=${this._restartBusy} @click=${() => { this._confirmRestartAffected = false; this._render(); }}>Cancel</button></span>`
        : html`<button class="sk-btn primary" type="button" @click=${() => { this._confirmRestartAffected = true; this._render(); }}>Restart affected</button>`}
    </div>`;
  }

  async _restartAffected() {
    this._restartBusy = true;
    this._render();
    try {
      const res = await this._json('/api/agents/restart', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ only_pending: true }),
      });
      this._startRestartHold(res.restarting || this._pendingAgents().map(a => a.id));
      this._msg = `Restarting ${res.count || 0} affected agent${(res.count || 0) === 1 ? '' : 's'}…`;
    } catch (e) {
      this._msg = String(e.message || e);
    }
    this._restartBusy = false;
    this._confirmRestartAffected = false;
    await this._reload({ clearMsg: false });
  }

  async _injReq(url, body, busyKey, okMsg) {
    const next = new Set(this.injBusy); next.add(busyKey); this.injBusy = next;
    this._render();
    try {
      const res = await this._json(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok === false) throw new Error(res.error || 'failed');
      this._msg = okMsg;
    } catch (e) {
      this._msg = String(e.message || e);
    }
    const after = new Set(this.injBusy); after.delete(busyKey); this.injBusy = after;
    await this._reload({ clearMsg: false });
  }

  _inject(alias, ws, mode) {
    return this._injReq('/api/plugins/skills/deploy', { alias, workspace: ws, mode },
      `${alias}@${ws}`, `Injected ${alias} → ${ws} (active on next agent start).`);
  }

  _uninject(alias, ws) {
    return this._injReq('/api/plugins/skills/unlink', { workspace: ws, alias },
      `${alias}@${ws}`, `Removed ${alias} from ${ws}.`);
  }

  async _injectAll(alias, wss, on) {
    const deployed = this._deployedFor(alias);
    const mode = this.injMode[alias] || 'symlink';
    const targets = on ? wss.filter(w => !deployed.has(w)) : wss.filter(w => deployed.has(w));
    if (!targets.length) { this._msg = on ? 'Already in every workspace.' : 'Not injected anywhere.'; this._render(); return; }
    this._msg = `${on ? 'Injecting' : 'Removing'} ${alias} ${on ? 'into' : 'from'} ${targets.length} workspace(s)…`;
    this._render();
    for (const w of targets) {
      const url = on ? '/api/plugins/skills/deploy' : '/api/plugins/skills/unlink';
      const body = on ? { alias, workspace: w, mode } : { workspace: w, alias };
      try {
        await this._json(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      } catch (_) {}
    }
    this._msg = `${on ? 'Injected' : 'Removed'} ${alias} ${on ? 'into' : 'from'} ${targets.length} workspace(s).`;
    await this._reload({ clearMsg: false });
  }

  async _removeCatalog(alias, deployed) {
    this._msg = `Removing ${alias} everywhere…`;
    this._render();
    for (const w of deployed) {
      try {
        await this._json('/api/plugins/skills/unlink', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace: w, alias }) });
      } catch (_) {}
    }
    try {
      await this._json('/api/plugins/skills/unlink', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace: CATALOG_WS, alias }) });
      this._msg = `Removed ${alias} from catalog and ${deployed.size} workspace(s).`;
    } catch (e) {
      this._msg = String(e.message || e);
    }
    await this._reload({ clearMsg: false });
  }

  async _centralize(skill) {
    const path = skill?.path || this.detail?.path;
    if (!path) { this._msg = 'no path available for this skill'; this._render(); return; }
    const alias = this._alias(skill.name || skill.display_name || (this.detail && this.detail.name));
    this._msg = `Centralizing ${alias}…`;
    this._render();
    try {
      const res = await this._json('/api/plugins/skills/catalog', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, alias }),
      });
      if (res.ok === false) throw new Error(res.error || 'failed');
      this._msg = `Centralized ${alias}. Toggle workspaces below to inject it.`;
      this._section = 'inject';
      this._ctx?.refreshNav?.();
    } catch (e) {
      this._msg = String(e.message || e);
    }
    await this._reload({ clearMsg: false });
  }

  _row(g) {
    const key = g.key || `id:${g.id}`;
    const dot = g.valid === false ? 'bad' : '';
    return html`<div class="sk-row ${key === this.selectedKey ? 'on' : ''}" @click=${() => this._select(key, g)}>
      <span class="dot ${dot}"></span>
      <div><div class="nm">${g.display_name || g.name}</div><div class="sub">${g.description || SOURCE_LABEL[g.source_type] || g.source_type}</div></div>
      <div class="sk-meta">
        ${g.managed_import ? html`<span class="sk-badge ok">managed</span>` : ''}
        ${g.workspaces?.length ? html`<span class="sk-badge">${g.workspaces.length} ws</span>` : ''}
        ${g.exact_uses ? html`<span class="sk-badge ok">${g.exact_uses} used</span>` : ''}
      </div>
    </div>`;
  }

  _detailTmpl() {
    if (!this.selectedKey) return html`<div class="sk-detail sk-empty">Select a skill.</div>`;
    const g = this._findSelected();
    if (!g) return html`<div class="sk-detail sk-empty">Not found.</div>`;
    const d = this.detail || g.deployments?.[0] || g;
    const kv = (k, v, cls = '') => html`<div class="k">${k}</div><div class="v ${cls}">${v || '-'}</div>`;
    const importLink = d.import_link || g.deployments?.find(x => x.import_link)?.import_link;
    return html`<div class="sk-detail">
      <h2>${g.display_name || d.display_name || d.name}</h2>
      <div class="sub">${g.description || d.description || '-'}</div>
      <div class="sk-kv">
        ${kv('source', SOURCE_LABEL[d.source_type] || d.source_type)}
        ${kv('owner', d.owner_plugin || '-')}
        ${kv('valid', (g.valid ?? d.valid) ? 'yes' : 'NO', (g.valid ?? d.valid) ? '' : 'bad')}
        ${kv('workspaces', (g.workspaces || [d.workspace].filter(Boolean)).join(', '))}
        ${kv('agents', g.agent_total ? `${g.agent_running}/${g.agent_total} running` : '-')}
        ${kv('exact usage', g.exact_uses ? `${g.exact_uses} events · ${this._fmtTokens(g.exact_usage_tokens || 0)} tokens` : '-')}
        ${kv('path', d.path || '-')}
        ${importLink?.source_url ? kv('source', importLink.source_url) : ''}
      </div>
      ${!g.managed_import && (g.valid ?? d.valid) && d.path && d.source_type !== 'runtime-plugin' ? html`<div class="sk-actions" style="margin-bottom:12px">
        <button class="sk-btn primary" type="button" @click=${() => this._centralize(d)}>Centralize for injection</button>
        <span style="font:var(--t-xxs) var(--f-mono);color:var(--t-4)">manage from one place, inject anywhere</span>
      </div>` : ''}
      ${g.exact_uses ? html`<div class="sk-sec">Exact usage</div><div class="sk-pre">Recorded by explicit skill usage events from a harness, plugin, or API client.</div>` : ''}
      ${(d.errors || []).length ? html`<div class="sk-sec">Errors</div><div class="sk-pre">${d.errors.join('\n')}</div>` : ''}
      ${d.body_preview ? html`<div class="sk-sec">SKILL.md</div><div class="sk-pre">${d.body_preview}</div>` : ''}
    </div>`;
  }

  _groupRows(rows) {
    return rows.map(s => this._rowGroup(s));
  }

  _rowGroup(s) {
    return { key: `id:${s.id}`, ...s, deployments: [s], workspaces: s.workspace ? [s.workspace] : [], deployment_count: 1 };
  }

  _findSelected() {
    return [...this._activeMerged(), ...(this.overview.groups || []), ...this._groupRows(this.skills)]
      .find(g => (g.key || `id:${g.id}`) === this.selectedKey);
  }

  async _select(key, group) {
    this.selectedKey = key;
    const g = group || this._findSelected();
    const rep = g?.deployments?.[0] || g;
    this.detail = rep;
    this._render();
    if (!rep?.id) return;
    try {
      const data = await this._json(`/api/plugins/skills/skills/${encodeURIComponent(rep.id)}`);
      if (!data.error) { this.detail = data; this._render(); }
    } catch (_) {}
  }

  _useExample(url) {
    this.importSource = url;
    this.importPhase = 'idle';
    this.resolved = null;
    this.selectedSubpaths = new Set();
    this.importStatus = '';
    this.importError = false;
    this._render();
  }

  _toggleDeployWs(name, on) {
    const next = new Set(this.deployWorkspaces);
    if (on) next.add(name); else next.delete(name);
    this.deployWorkspaces = next;
    this._render();
  }

  _subpathKey(s) {
    return String(s.relative_subpath || s.subpath || '.');
  }

  _toggleSkill(key, on) {
    const next = new Set(this.selectedSubpaths);
    if (on) next.add(key); else next.delete(key);
    this.selectedSubpaths = next;
    this._render();
  }

  _toggleAllSkills(on) {
    const skills = this.resolved?.skills || [];
    if (!on) {
      this.selectedSubpaths = new Set();
    } else {
      this.selectedSubpaths = new Set(
        skills.filter(s => s.valid !== false).map(s => this._subpathKey(s))
      );
    }
    this._render();
  }

  async _discover() {
    const source = this.importSource.trim();
    if (!source) return;
    this.importPhase = 'discovering';
    this.importError = false;
    this.importStatus = 'Fetching source and searching for SKILL.md files…';
    this.resolved = null;
    this.selectedSubpaths = new Set();
    this._render();
    try {
      const res = await this._json('/api/plugins/skills/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source }),
      });
      if (!res.ok) throw new Error(res.error || 'discover failed');
      this.resolved = res;
      const skills = res.skills || [];
      const valid = skills.filter(s => s.valid !== false);
      this.selectedSubpaths = new Set(
        valid.length === 1 ? [this._subpathKey(valid[0])] : valid.map(s => this._subpathKey(s))
      );
      this.importPhase = 'ready';
      this.importStatus = skills.length
        ? `Found ${skills.length} skill${skills.length === 1 ? '' : 's'}. ${valid.length} ready to import.`
        : 'No skills found.';
      this.importError = !skills.length;
    } catch (e) {
      this.importPhase = 'idle';
      this.importStatus = String(e.message || e);
      this.importError = true;
      this.resolved = null;
    }
    this._render();
  }

  async _importSelected() {
    if (!this.resolved) return;
    const skills = (this.resolved.skills || []).filter(s =>
      this.selectedSubpaths.has(this._subpathKey(s)) && s.valid !== false
    );
    if (!skills.length) return;

    this.importPhase = 'importing';
    this.importStatus = 'Registering managed skills and deploying…';
    this._render();
    const selections = skills.map(s => ({
      subpath: this._subpathKey(s),
      alias: this._alias(s.name || s.display_name),
    }));
    let deploy_to = 'all';
    if (this.deployScope === 'none') deploy_to = 'none';
    else if (this.deployScope === 'pick') deploy_to = [...this.deployWorkspaces];
    try {
      const res = await this._json('/api/plugins/skills/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          resolved: this.resolved,
          selections,
          deploy_to,
          mode: this.form.mode,
        }),
      });
      if (!res.ok) throw new Error(res.error || 'import failed');
      const n = (res.imports || []).length;
      const ws = (res.deployed_workspaces || []).length;
      this.importPhase = 'ready';
      this.importStatus = deploy_to === 'none'
        ? `Added ${n} skill${n === 1 ? '' : 's'} to managed catalog.`
        : `Added ${n} skill${n === 1 ? '' : 's'} and deployed to ${ws} workspace${ws === 1 ? '' : 's'}. Active on next agent start.`;
      this.importError = false;
      this.resolved = null;
      this.selectedSubpaths = new Set();
      await this._reload({ clearMsg: false });
    } catch (e) {
      this.importPhase = 'ready';
      this.importStatus = String(e.message || e);
      this.importError = true;
      this._render();
    }
  }

  async _refreshInventory() {
    this._msg = 'refreshing inventory...';
    this._render();
    try {
      await this.api.fetch('/api/plugins/skills/rescan', { method: 'POST' });
      this._msg = '';
    } catch (e) {
      this._msg = String(e.message || e);
    }
    await this._reload();
  }

  async _refreshImports() {
    this._msg = 'checking managed imports...';
    this._render();
    try {
      const res = await this._json('/api/plugins/skills/refresh-imports', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: true }),
      });
      const r = res.refresh || {};
      this._msg = `${r.checked || 0} checked; ${r.changed || 0} changed; ${r.update_available || 0} update available`;
    } catch (e) {
      this._msg = String(e.message || e);
    }
    await this._reload({ clearMsg: false });
  }

  _subscribeLive() {
    const bus = this.api && this.api.events;
    if (!bus || typeof bus.subscribe !== 'function') return;
    this.unsubEvent = bus.subscribe(ev => {
      const t = ev && (ev.type || ev.event);
      const needsSkillReload = (
        t === 'skills.changed' || t === 'skills.invalid' || t === 'skills.removed' ||
        t === 'workspace.updated'
      );
      const needsAgentReload = (
        t === 'agent.status_changed' || t === 'agent.start' || t === 'agent.stop'
      );
      if (needsSkillReload) {
        this._scheduleReload();
      } else if (needsAgentReload) {
        this._scheduleAgentsReload();
      }
    });
  }

  _fmtTokens(n) {
    n = Number(n || 0);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
    return String(Math.round(n));
  }

  _alias(value) {
    return String(value || '').toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
  }
}
