// GitHub tab — contributed by the github plugin.
//
// Per-workspace card grid showing repo binding, cursor state, rule
// summary, and recent github.* events. "Sync now" button per card
// triggers POST /api/plugins/github/sync to force a one-shot poll.
//
// Live updates: subscribes to the global SSE stream and re-renders
// the affected workspace card when:
//   - github.<EventName> arrives (new event landed, refresh cursor)
//   - workspace.updated arrives (opt-in/out toggled or repo changed)
//
// MIGRATED to @relaydeck/ui (build-less, light-DOM Lit). Keeps the
// framework-neutral plugin contract: default-export class with
// mount(container, api, ctx)/unmount() + sections(). The dense
// rule-builder form is rendered with lit-html but its text-input
// handlers mutate the model WITHOUT re-rendering (so a focused
// textarea/input is never yanked out from under the cursor); only
// structural edits (add/remove rule·predicate·action, change event
// type, switch mode) trigger a re-render.

import { html, render } from '@relaydeck/ui';

const PANEL_CSS = `
  /* flex:1 + min-height:0 (not height:100%) so .gh-body scrolls inside the
     flex-column .detail-host instead of overflowing + getting clipped. */
  .gh-wrap{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden;background:var(--bg-0)}
  .gh-head{display:flex;align-items:flex-start;justify-content:space-between;padding:18px 22px 14px;border-bottom:1px solid var(--line-1);background:var(--bg-0);gap:12px;flex:0 0 auto}
  .gh-head .eyebrow{font-family:var(--f-mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .gh-head h1{margin:0;font-family:var(--f-sans);font-size:28px;font-weight:600;letter-spacing:-.02em;color:var(--t-1)}
  .gh-head .sub{font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3);margin-top:6px}
  .gh-head .stats{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .gh-head .stat{font-family:var(--f-mono);font-size:var(--t-xxs);background:var(--bg-2);border:1px solid var(--line-2);padding:3px 9px;border-radius:var(--r-1);color:var(--t-3)}
  .gh-head .stat b{color:var(--t-1);font-weight:600}
  .gh-head .stat.ok b{color:var(--ok)}
  .gh-head .controls button{background:transparent;border:1px solid var(--line-2);color:var(--t-2);font:400 var(--t-xs) var(--f-mono);padding:4px 12px;border-radius:var(--r-1);cursor:pointer}
  .gh-head .controls button:hover{color:var(--t-1);border-color:var(--line-3)}
  .gh-head .controls button.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-text);font-weight:600}

  .gh-overview{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));margin-bottom:16px}
  .gh-mini{background:var(--bg-1);border:1px solid var(--line-2);border-radius:var(--r-3);padding:12px 14px;font-family:var(--f-mono);cursor:pointer;transition:border-color .12s}
  .gh-mini:hover{border-color:var(--acc-line)}
  .gh-mini .ws{font-weight:600;color:var(--t-1);font-size:var(--t-sm);margin-bottom:4px}
  .gh-mini .repo{color:var(--acc);font-size:var(--t-xs);margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-mini .row{display:flex;gap:8px;font-size:10px;color:var(--t-3);flex-wrap:wrap}
  .gh-mini .dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:4px}
  .gh-mini .dot.on{background:var(--ok)} .gh-mini .dot.off{background:var(--t-4)}
  .gh-mini.unconfigured .repo{color:var(--t-4);font-style:italic}
  .gh-mini.unconfigured{border-style:dashed}
  .gh-setup-cta{margin-top:8px;font-size:10px;color:var(--acc)}
  .gh-workspace-detail{display:flex;flex-direction:column;gap:0}
  .gh-workspace-detail .gh-editor{display:block;border-top:0}
  .gh-workspace-kicker{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-family:var(--f-mono);font-size:var(--t-xxs);color:var(--t-3);padding:10px 14px;border-bottom:1px solid var(--line-1);background:var(--bg-0)}
  .gh-workspace-kicker .pill{border:1px solid var(--line-2);background:var(--bg-1);border-radius:3px;padding:2px 7px}
  .gh-workspace-kicker .pill.ok{color:var(--ok);border-color:rgba(74,222,128,.32)}
  .gh-workspace-kicker .pill.err{color:var(--err);border-color:rgba(244,114,114,.36)}
  .gh-body{flex:1;min-height:0;overflow-y:auto;padding:18px 22px;display:flex;flex-direction:column;gap:14px}
  .gh-body::-webkit-scrollbar{width:6px}
  .gh-body::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}

  .gh-empty{padding:80px;text-align:center;color:var(--t-3);font-family:var(--f-mono);font-size:var(--t-sm)}
  .gh-empty .hint{color:var(--t-4);font-size:var(--t-xs);margin-top:8px;line-height:1.6}
  .gh-empty code{background:var(--bg-2);padding:1px 6px;border-radius:3px;color:var(--acc)}

  .gh-card{border:1px solid var(--line-1);border-radius:6px;background:var(--bg-0);font-family:var(--f-mono)}
  .gh-card.errored{border-color:var(--err)}
  .gh-card-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line-1);background:var(--bg-1);border-radius:6px 6px 0 0}
  .gh-card-head .ws{font-weight:600;color:var(--t-1);font-size:var(--t-sm)}
  .gh-card-head .repo{margin-left:10px;color:var(--acc);font-size:var(--t-xs)}
  .gh-card-head .repo.missing{color:var(--t-4);font-style:italic}
  .gh-card-head .status{display:inline-flex;align-items:center;gap:5px;font-size:var(--t-xxs);color:var(--t-3);text-transform:uppercase;letter-spacing:.05em}
  .gh-card-head .status .dot{width:6px;height:6px;border-radius:50%}
  .gh-card-head .status .dot.on{background:var(--ok)}
  .gh-card-head .status .dot.off{background:var(--t-4)}
  .gh-card-head .actions{display:flex;gap:6px;margin-left:auto}
  .gh-card-head .actions button{background:transparent;border:1px solid var(--line-2);color:var(--t-2);font:400 var(--t-xxs) var(--f-mono);padding:3px 10px;border-radius:3px;cursor:pointer}
  .gh-card-head .actions button:hover{color:var(--t-1);border-color:var(--line-3)}
  .gh-card-head .actions button.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-text);font-weight:600}
  .gh-card-head .actions button:disabled{opacity:.4;cursor:not-allowed}

  .gh-workflow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:4px}
  .gh-workflow .step{border:1px solid var(--line-1);background:var(--bg-1);border-radius:6px;padding:10px 12px;font-family:var(--f-mono)}
  .gh-workflow .step .k{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--t-4);margin-bottom:4px}
  .gh-workflow .step .v{font-size:var(--t-xs);color:var(--t-1)}
  @media (max-width: 760px){.gh-workflow{grid-template-columns:1fr}.gh-quick{grid-template-columns:1fr}.gh-quick .buttons{justify-content:flex-start}}
  .gh-card.setup{border-style:dashed;border-color:var(--line-2)}
  .gh-card.setup .gh-card-head{background:var(--bg-0)}
  .gh-quick{display:grid;grid-template-columns:minmax(260px,1fr) auto;gap:10px;align-items:end;padding:14px;background:var(--bg-1);border-top:1px solid var(--line-1)}
  .gh-quick .field{display:flex;flex-direction:column;gap:4px;font-family:var(--f-mono)}
  .gh-quick .field label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--t-4)}
  .gh-quick input{background:var(--bg-0);border:1px solid var(--line-2);border-radius:3px;color:var(--t-1);font:400 var(--t-xs) var(--f-mono);padding:6px 8px;box-sizing:border-box;width:100%}
  .gh-quick input:focus{outline:none;border-color:var(--acc)}
  .gh-quick .buttons{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
  .gh-quick button{background:transparent;border:1px solid var(--line-2);color:var(--t-2);font:400 var(--t-xxs) var(--f-mono);padding:5px 10px;border-radius:3px;cursor:pointer}
  .gh-quick button.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-text);font-weight:600}
  .gh-quick button:hover{border-color:var(--line-3);color:var(--t-1)}

  .gh-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1px;background:var(--line-1);border-bottom:1px solid var(--line-1)}
  .gh-grid .cell{padding:8px 12px;background:var(--bg-0);font-size:var(--t-xxs)}
  .gh-grid .cell .lbl{color:var(--t-4);text-transform:uppercase;letter-spacing:.05em;font-size:9px;margin-bottom:2px}
  .gh-grid .cell .val{color:var(--t-1);font-size:var(--t-xs);word-break:break-all}
  .gh-grid .cell .val.muted{color:var(--t-4);font-style:italic}
  .gh-grid .cell .val.err{color:var(--err)}

  .gh-rules{padding:8px 14px;border-bottom:1px solid var(--line-1)}
  .gh-rules .title{font-size:var(--t-xxs);text-transform:uppercase;color:var(--t-3);letter-spacing:.05em;margin-bottom:6px}
  .gh-rules .row{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:var(--t-xs)}
  .gh-rules .row .name{color:var(--t-1);min-width:140px}
  .gh-rules .row .when{flex:1;color:var(--t-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-rules .row .do{color:var(--ok);font-size:var(--t-xxs)}
  .gh-rules .none{color:var(--t-4);font-style:italic;font-size:var(--t-xs)}
  .gh-rules .err{color:var(--err);font-size:var(--t-xs)}

  .gh-events{padding:8px 14px}
  .gh-events .title{font-size:var(--t-xxs);text-transform:uppercase;color:var(--t-3);letter-spacing:.05em;margin-bottom:6px;display:flex;align-items:center;justify-content:space-between}
  .gh-events .row{display:flex;gap:10px;padding:2px 0;font-size:var(--t-xs);color:var(--t-2)}
  .gh-events .row .ts{color:var(--t-4);min-width:50px}
  .gh-events .row .et{color:var(--cyan);min-width:140px}
  .gh-events .row .ec{color:var(--t-2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-events .none{color:var(--t-4);font-style:italic;font-size:var(--t-xs)}

  .gh-toast{font-size:var(--t-xxs);color:var(--ok);margin-right:8px}
  .gh-toast.err{color:var(--err)}

  .gh-editor{border-top:1px solid var(--line-1);padding:10px 14px;background:var(--bg-1);display:none}
  .gh-card.editing .gh-editor{display:block}
  .gh-editor .title{font-size:var(--t-xxs);text-transform:uppercase;color:var(--t-3);letter-spacing:.05em;margin-bottom:6px;display:flex;align-items:center;justify-content:space-between}
  .gh-editor .title .path{color:var(--t-4);font-size:var(--t-xxs);text-transform:none;letter-spacing:0}
  .gh-editor textarea{width:100%;min-height:280px;background:var(--bg-0);border:1px solid var(--line-2);border-radius:4px;color:var(--t-1);font:400 var(--t-xs) var(--f-mono);padding:8px 10px;resize:vertical;box-sizing:border-box;tab-size:2;line-height:1.5}
  .gh-editor textarea:focus{outline:none;border-color:var(--acc)}
  .gh-editor .actions{display:flex;align-items:center;gap:8px;margin-top:8px}
  .gh-editor .actions button{font:500 var(--t-xs) var(--f-mono);padding:5px 14px;border-radius:3px;cursor:pointer;border:0}
  .gh-editor .actions button.save{background:var(--acc);color:#08090b}
  .gh-editor .actions button.save:hover{background:var(--acc-d)}
  .gh-editor .actions button.save:disabled{opacity:.5;cursor:not-allowed}
  .gh-editor .actions button.cancel{background:transparent;border:1px solid var(--line-2);color:var(--t-2)}
  .gh-editor .actions button.cancel:hover{color:var(--t-1);border-color:var(--line-3)}
  .gh-editor .msg{margin-left:auto;font-size:var(--t-xxs);font-family:var(--f-mono)}
  .gh-editor .msg.err{color:var(--err)}
  .gh-editor .msg.ok{color:var(--ok)}

  /* Mode toggle */
  .gh-editor-tabs{display:inline-flex;gap:0;border:1px solid var(--line-2);border-radius:3px;overflow:hidden;margin-left:8px}
  .gh-editor-tabs button{background:transparent;border:0;color:var(--t-3);font:400 var(--t-xxs) var(--f-mono);padding:3px 12px;cursor:pointer}
  .gh-editor-tabs button.active{background:var(--bg-2);color:var(--t-1)}
  .gh-editor-tabs button:hover:not(.active){color:var(--t-1);background:var(--bg-2)}

  /* Structured editor */
  .gh-form{display:flex;flex-direction:column;gap:12px}
  .gh-form .top-grid{display:grid;grid-template-columns:1fr 140px;gap:10px;padding:10px;background:var(--bg-0);border:1px solid var(--line-2);border-radius:5px}
  .gh-form .field{display:flex;flex-direction:column;gap:3px}
  .gh-form .field label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--t-3)}
  .gh-form input[type=text],.gh-form input[type=number],.gh-form select,.gh-form textarea{
    background:var(--bg-1);border:1px solid var(--line-2);border-radius:3px;color:var(--t-1);
    font:400 var(--t-xs) var(--f-mono);padding:5px 8px;width:100%;box-sizing:border-box;
  }
  .gh-form input:focus,.gh-form select:focus,.gh-form textarea:focus{outline:none;border-color:var(--acc)}
  .gh-form textarea{min-height:50px;resize:vertical;line-height:1.4}

  .gh-rules-list{display:flex;flex-direction:column;gap:8px}
  .gh-rules-list .title{display:flex;align-items:center;justify-content:space-between;font-size:var(--t-xxs);text-transform:uppercase;color:var(--t-3);letter-spacing:.05em}
  .gh-rules-list .title .tools{display:flex;gap:6px;align-items:center}
  .gh-rules-list .title button.add{background:var(--acc-soft);border:1px solid var(--acc-line);color:var(--acc);font:500 var(--t-xxs) var(--f-mono);padding:3px 10px;border-radius:3px;cursor:pointer}
  .gh-rules-list .title button.add:hover{background:var(--acc);color:#08090b}
  .gh-rules-list .title button.quick{background:transparent;border:1px solid var(--line-2);color:var(--t-2);font:400 var(--t-xxs) var(--f-mono);padding:3px 8px;border-radius:3px;cursor:pointer;text-transform:none;letter-spacing:0}
  .gh-rules-list .title button.quick:hover{border-color:var(--acc-line);color:var(--acc)}
  .gh-rules-list .empty{padding:18px;text-align:center;color:var(--t-4);font-style:italic;font-size:var(--t-xs);background:var(--bg-0);border:1px dashed var(--line-2);border-radius:5px}

  .gh-rule{background:var(--bg-0);border:1px solid var(--line-2);border-radius:5px;padding:10px 12px}
  .gh-rule .rh{display:flex;align-items:center;gap:8px;margin-bottom:8px}
  .gh-rule .rh input[type=text]{flex:1}
  .gh-rule .rh .ico{font-weight:700;color:var(--acc);font-size:var(--t-sm)}
  .gh-rule .rh button.del{background:transparent;border:1px solid var(--line-2);color:var(--err);font:400 var(--t-xxs) var(--f-mono);padding:3px 8px;border-radius:3px;cursor:pointer}
  .gh-rule .rh button.del:hover{background:var(--err);color:var(--bg-0);border-color:var(--err)}
  .gh-flow{display:grid;grid-template-columns:minmax(0,1fr) 32px minmax(0,1fr);gap:8px;align-items:stretch;margin:8px 0 10px}
  .gh-flow .node{border:1px solid var(--line-1);background:var(--bg-1);border-radius:5px;padding:8px 10px;min-width:0}
  .gh-flow .node .k{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--t-4);margin-bottom:4px}
  .gh-flow .node .v{font-size:var(--t-xs);color:var(--t-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-flow .node .s{font-size:10px;color:var(--t-3);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-flow .arr{display:flex;align-items:center;justify-content:center;color:var(--acc);font-size:18px}
  @media (max-width: 760px){.gh-flow{grid-template-columns:1fr}.gh-flow .arr{transform:rotate(90deg);height:22px}}

  .gh-vars{margin:8px 0 10px;border:1px solid var(--line-1);background:var(--bg-0);border-radius:5px;padding:7px 9px}
  .gh-vars summary{cursor:pointer;display:flex;align-items:center;gap:8px;list-style:none;font:600 10px var(--f-mono);text-transform:uppercase;letter-spacing:.06em;color:var(--t-2)}
  .gh-vars summary::-webkit-details-marker{display:none}
  .gh-vars summary .sub{margin-left:auto;color:var(--t-4);font-weight:400;text-transform:none;letter-spacing:0}
  .gh-var-body{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:8px}
  .gh-var-group{min-width:0}
  .gh-var-group .g{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--t-4);margin-bottom:5px}
  .gh-var-list{display:flex;flex-wrap:wrap;gap:4px}
  .gh-var{border:1px solid var(--line-2);background:var(--bg-1);border-radius:3px;color:var(--t-2);font:400 10px var(--f-mono);padding:3px 6px;cursor:pointer;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-var:hover{border-color:var(--acc);color:var(--acc);background:var(--acc-soft)}
  .gh-var-stdin{border-left:1px dashed var(--line-2);padding-left:8px;color:var(--t-3);font-size:10px;line-height:1.5}
  .gh-var-stdin code{font-family:var(--f-mono);font-size:10px;color:var(--t-1);background:var(--bg-1);border:1px solid var(--line-2);border-radius:3px;padding:1px 4px}

  .gh-rule .sect{margin-top:6px;padding-top:8px;border-top:1px dashed var(--line-2)}
  .gh-rule .sect-head{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--t-3);margin-bottom:5px;display:flex;align-items:center;justify-content:space-between}
  .gh-rule .sect-head button.add{background:transparent;border:1px solid var(--line-2);color:var(--t-2);font:400 9px var(--f-mono);padding:2px 8px;border-radius:3px;cursor:pointer;text-transform:none;letter-spacing:0}
  .gh-rule .sect-head button.add:hover{color:var(--acc);border-color:var(--acc)}

  .gh-preds{display:flex;flex-direction:column;gap:5px}
  .gh-preds .pred{display:grid;grid-template-columns:130px 1fr 28px;gap:5px;align-items:center}
  .gh-preds .pred select,.gh-preds .pred input{font-size:var(--t-xs)}
  .gh-preds .pred button.x{background:transparent;border:1px solid var(--line-2);color:var(--t-3);font:400 11px var(--f-mono);padding:0;cursor:pointer;border-radius:3px;height:22px}
  .gh-preds .pred button.x:hover{color:var(--err);border-color:var(--err)}

  .gh-actions-list{display:flex;flex-direction:column;gap:5px}
  .gh-act{background:var(--bg-1);border:1px solid var(--line-2);border-radius:4px;padding:6px 8px}
  .gh-act .ah{display:flex;align-items:center;gap:6px;margin-bottom:4px}
  .gh-act .ah select{width:140px;font-size:var(--t-xs)}
  .gh-act .ah button.x{background:transparent;border:1px solid var(--line-2);color:var(--t-3);font:400 11px var(--f-mono);padding:0 5px;cursor:pointer;border-radius:3px;height:22px;margin-left:auto}
  .gh-act .ah button.x:hover{color:var(--err);border-color:var(--err)}
  .gh-act .ab{display:grid;grid-template-columns:90px 1fr;gap:4px 8px;font-size:var(--t-xs)}
  .gh-act .ab .lbl{color:var(--t-4);font-size:9px;text-transform:uppercase;align-self:center}
  .gh-act .ab textarea{min-height:34px}
  .gh-act .ab .span{grid-column:1 / span 2}
  .gh-act .ab .check{display:flex;align-items:center;gap:6px;color:var(--t-3);font-size:10px}
  .gh-act .ab .check input{width:auto}
  .gh-act .hint{color:var(--t-4);font-size:9px;margin-top:3px;font-style:italic}

  /* Auth bar */
  .gh-auth{display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--line-1);font-family:var(--f-mono);font-size:var(--t-xs);flex:0 0 auto}
  .gh-auth.ok{background:rgba(74,222,128,.04)}
  .gh-auth.warn{background:rgba(244,180,80,.06)}
  .gh-auth.err{background:rgba(244,114,114,.06)}
  .gh-auth .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .gh-auth.ok .dot{background:var(--ok)}
  .gh-auth.warn .dot{background:var(--warn)}
  .gh-auth.err .dot{background:var(--err)}
  .gh-auth .msg{color:var(--t-1);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .gh-auth .msg .user{color:var(--acc)}
  .gh-auth .msg .host{color:var(--t-3);font-size:var(--t-xxs);margin-left:4px}
  .gh-auth .cmd{display:inline-flex;align-items:center;gap:5px;background:var(--bg-0);border:1px solid var(--line-2);border-radius:3px;padding:2px 8px;font:400 var(--t-xxs) var(--f-mono);color:var(--t-2)}
  .gh-auth .cmd code{color:var(--t-1)}
  .gh-auth button{background:transparent;border:1px solid var(--line-2);color:var(--t-2);font:400 var(--t-xxs) var(--f-mono);padding:3px 9px;border-radius:3px;cursor:pointer}
  .gh-auth button:hover{color:var(--t-1);border-color:var(--line-3)}
  .gh-auth button.copy{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
  .gh-auth button.copy:hover{background:var(--acc);color:#08090b}
`;

const STARTER_YAML = `# Repo to poll. The operator's \`gh auth\` decides which
# GitHub host this resolves against (github.com or enterprise).
repo: owner/repo

# How often to poll (seconds). Plugin setting is the fallback.
poll_interval_s: 30

rules:
  # When a PR is opened, ping a reviewer agent.
  - name: pr-opened-review
    when:
      event: PullRequestEvent
      action: opened
    do:
      - agent.message:
          to: <agent-id>
          body: "Review PR #{{ pull_request.number }}: {{ pull_request.title }}"

  # Uncomment to route bug-labeled issues to a triager:
  # - name: bug-to-triager
  #   when:
  #     event: IssuesEvent
  #     action: labeled
  #     label: bug
  #   do:
  #     - agent.message:
  #         to: triager
  #         body: "Bug filed: {{ issue.title }}"
`;

const MAX_EVENTS_PER_WS = 30;

function _displayConfigError(msg) {
  if (!msg) return '';
  return String(msg)
    .replace(/github\.yaml/g, 'GitHub routing config')
    .replace(/\bYAML\b/g, 'advanced config');
}

// Event types we surface in the dropdown. The poller doesn't restrict
// this list — operators can type any custom event name in the YAML
// view, but the dropdown covers the GitHub Events API surface that
// most rules care about. https://docs.github.com/en/rest/using-the-rest-api/github-event-types
const EVENT_TYPES = [
  'CommitCommentEvent', 'CreateEvent', 'DeleteEvent', 'DiscussionEvent',
  'ForkEvent', 'GollumEvent', 'IssueCommentEvent', 'IssuesEvent',
  'MemberEvent', 'PublicEvent', 'PullRequestEvent',
  'PullRequestReviewEvent', 'PullRequestReviewCommentEvent',
  'PushEvent', 'ReleaseEvent', 'WatchEvent',
];

// Per-event-type action sub-values (`payload.action`).
const ACTIONS_BY_EVENT = {
  'CommitCommentEvent': ['created'],
  'DiscussionEvent': ['created'],
  'ForkEvent': ['forked'],
  'MemberEvent': ['added'],
  'PullRequestEvent': [
    'opened', 'closed', 'reopened', 'edited', 'labeled', 'unlabeled',
    'review_requested', 'review_request_removed', 'ready_for_review',
    'synchronize', 'assigned', 'unassigned', 'converted_to_draft',
  ],
  'IssuesEvent': [
    'opened', 'closed', 'reopened', 'edited', 'deleted',
    'labeled', 'unlabeled', 'assigned', 'unassigned',
    'pinned', 'unpinned', 'milestoned', 'demilestoned',
  ],
  'IssueCommentEvent': ['created', 'edited', 'deleted'],
  'PullRequestReviewEvent': ['submitted', 'edited', 'dismissed'],
  'PullRequestReviewCommentEvent': ['created', 'edited', 'deleted'],
  'ReleaseEvent': ['published', 'unpublished', 'created', 'edited', 'deleted', 'prereleased', 'released'],
  'WatchEvent': ['started'],
};

// Predicate keys beyond event/action.
const PREDICATE_KEYS = [
  'label', 'actor', 'requested_reviewer', 'ref', 'ref_type',
  'sender', 'assignee', 'milestone',
];

const ACTION_TYPES = ['agent.message', 'model', 'bus.emit', 'gh', 'script', 'code'];

const ACTION_HINTS = {
  'agent.message': 'Send a peer message. Template variables render before delivery.',
  'model': 'Run a configured model on the event, then emit the result and/or send it to an agent.',
  'code': 'Run inline python/sh/bash with event JSON on stdin. Operator-authored code runs as a subprocess.',
  'script': 'Run a script. Event JSON piped on stdin. cwd=workspace.',
  'gh': 'Run `gh <args>`. Template substitution applied to args.',
  'bus.emit': 'Emit a generic plugin event other plugins can subscribe to.',
};

const ACTION_TEMPLATE_TARGET = {
  'agent.message': 'body',
  'model': 'prompt',
  'gh': 'args',
  'bus.emit': 'data',
  'script': 'path',
  'code': 'body',
};

const COMMON_TEMPLATE_VARS = [
  ['type', 'event type'],
  ['action', 'payload action'],
  ['actor.login', 'sender'],
  ['repo.name', 'owner/repo'],
  ['created_at', 'timestamp'],
  ['org.login', 'org'],
];

const EVENT_TEMPLATE_VARS = {
  CommitCommentEvent: [
    ['comment.body', 'comment body'],
    ['comment.html_url', 'comment URL'],
    ['comment.commit_id', 'commit SHA'],
    ['comment.user.login', 'commenter'],
  ],
  CreateEvent: [
    ['ref', 'ref name'],
    ['ref_type', 'branch/tag'],
    ['full_ref', 'full ref'],
    ['master_branch', 'default branch'],
    ['pusher_type', 'pusher type'],
  ],
  DeleteEvent: [
    ['ref', 'ref name'],
    ['ref_type', 'branch/tag'],
    ['full_ref', 'full ref'],
    ['pusher_type', 'pusher type'],
  ],
  DiscussionEvent: [
    ['discussion.title', 'discussion title'],
    ['discussion.html_url', 'discussion URL'],
    ['discussion.user.login', 'author'],
    ['discussion.number', 'number'],
  ],
  ForkEvent: [
    ['forkee.full_name', 'fork name'],
    ['forkee.html_url', 'fork URL'],
    ['forkee.owner.login', 'fork owner'],
  ],
  GollumEvent: [
    ['pages[0].page_name', 'page name'],
    ['pages[0].title', 'page title'],
    ['pages[0].action', 'page action'],
    ['pages[0].html_url', 'page URL'],
  ],
  IssueCommentEvent: [
    ['issue.number', 'issue/PR #'],
    ['issue.title', 'issue/PR title'],
    ['issue.html_url', 'issue/PR URL'],
    ['comment.body', 'comment body'],
    ['comment.html_url', 'comment URL'],
    ['comment.user.login', 'commenter'],
  ],
  IssuesEvent: [
    ['issue.number', 'issue #'],
    ['issue.title', 'title'],
    ['issue.html_url', 'URL'],
    ['issue.user.login', 'author'],
    ['label.name', 'label'],
    ['assignee.login', 'assignee'],
    ['labels[0].name', 'first label'],
  ],
  MemberEvent: [
    ['member.login', 'member'],
    ['member.html_url', 'profile URL'],
  ],
  PublicEvent: [],
  PullRequestEvent: [
    ['number', 'PR #'],
    ['pull_request.number', 'PR #'],
    ['pull_request.title', 'title'],
    ['pull_request.html_url', 'URL'],
    ['pull_request.user.login', 'author'],
    ['pull_request.head.ref', 'head branch'],
    ['pull_request.base.ref', 'base branch'],
    ['requested_reviewer.login', 'requested reviewer'],
    ['label.name', 'label'],
    ['assignee.login', 'assignee'],
  ],
  PullRequestReviewEvent: [
    ['pull_request.number', 'PR #'],
    ['pull_request.title', 'PR title'],
    ['pull_request.html_url', 'PR URL'],
    ['review.state', 'review state'],
    ['review.user.login', 'reviewer'],
    ['review.body', 'review body'],
    ['review.html_url', 'review URL'],
  ],
  PullRequestReviewCommentEvent: [
    ['pull_request.number', 'PR #'],
    ['pull_request.title', 'PR title'],
    ['pull_request.html_url', 'PR URL'],
    ['comment.body', 'comment body'],
    ['comment.path', 'file path'],
    ['comment.diff_hunk', 'diff hunk'],
    ['comment.html_url', 'comment URL'],
    ['comment.user.login', 'commenter'],
  ],
  PushEvent: [
    ['ref', 'full ref'],
    ['head', 'head SHA'],
    ['before', 'before SHA'],
    ['push_id', 'push id'],
  ],
  ReleaseEvent: [
    ['release.name', 'release name'],
    ['release.tag_name', 'tag'],
    ['release.html_url', 'release URL'],
    ['release.author.login', 'author'],
    ['release.body', 'notes'],
  ],
  WatchEvent: [
    ['actor.login', 'starrer'],
    ['repo.name', 'repo'],
  ],
};

const POPULAR_TEMPLATE_VARS = [
  ['issue.number', 'issue #'],
  ['issue.title', 'issue title'],
  ['pull_request.number', 'PR #'],
  ['pull_request.title', 'PR title'],
  ['comment.body', 'comment body'],
  ['label.name', 'label'],
  ['ref', 'branch/ref'],
];

export default class GithubPanel {
  constructor() {
    this.root = null;
    this.api = null;
    this.host = null;
    this._ctx = null;
    this._section = 'overview';
    this.state = null;
    this.events = new Map();
    this.unsubEvent = null;
    this.busy = new Set();
    this.auth = null;
    this.agents = [];
    this.editing = null;
    this.focusWs = null;
    this.editorMode = 'form';
    this.editorText = null;
    this.editorParsed = null;
    this.editorPath = null;
    this.editorMsg = null;
    this._loadingEditor = null;
    this._tplTarget = null;
    // Stable anchor nodes the reactive layer renders into.
    this._headEl = null;
    this._authBarEl = null;
    this._bodyEl = null;
  }

  sections() {
    const wss = (this.state && this.state.workspaces) || [];
    const configured = wss.filter(w => w.configured).length;
    let evCount = 0;
    for (const list of this.events.values()) evCount += list.length;
    const wsItems = wss.map(w => ({
      id: `workspace:${w.workspace}`,
      label: w.workspace,
      icon: 'layers',
      badge: w.config_error && w.configured ? '!' : (w.running ? '●' : (w.configured ? '•' : '')),
    }));
    const groups = [
      { group: 'GitHub', items: [
        { id: 'overview', label: 'Overview', icon: 'git', badge: configured || '' },
        { id: 'activity', label: 'Activity', icon: 'activity', badge: evCount || '' },
      ] },
    ];
    if (wsItems.length) {
      groups.push({ group: 'Workspaces', items: wsItems });
    }
    groups.push(
      { group: 'Setup', items: [
        { id: 'auth', label: 'Auth', icon: 'vault', badge: this.auth?.auth_ok ? '✓' : '' },
      ] },
    );
    return groups;
  }

  async mount(container, api, ctx) {
    this.api = api;
    this.host = ctx?.host || null;
    this._ctx = ctx || null;
    this.root = container;
    if (ctx?.section) this._section = ctx.section;

    if (!document.getElementById('github-panel-css')) {
      const s = document.createElement('style');
      s.id = 'github-panel-css';
      s.textContent = PANEL_CSS;
      document.head.appendChild(s);
    }

    // Static structural skeleton + stable anchor nodes. The reactive
    // layer renders into #gh-head / #gh-auth-bar / #gh-body only, so the
    // outer .gh-wrap is never re-created out from under us.
    this.root.innerHTML = `
      <div class="gh-wrap">
        <div class="gh-head" id="gh-head"></div>
        <div id="gh-auth-bar"></div>
        <div class="gh-body" id="gh-body"></div>
      </div>`;
    this._headEl = this.root.querySelector('#gh-head');
    this._authBarEl = this.root.querySelector('#gh-auth-bar');
    this._bodyEl = this.root.querySelector('#gh-body');

    await this._reload();
    // Auth can shell out to `gh auth status`; keep it off the critical
    // render path so the lens never sits at "loading..." while that probe runs.
    this._reloadAuth();
    this._subscribeLive();
    ctx?.onSectionChange?.((id) => {
      this._section = id;
      if (id && id.startsWith('workspace:')) {
        const ws = id.slice('workspace:'.length);
        this.focusWs = ws;
        this._ensureEditorOpen(ws);
      }
      this._render();
    });
  }

  unmount() {
    if (this.unsubEvent) this.unsubEvent();
  }

  // ── Data ──────────────────────────────────────────────────────

  async _reload() {
    try {
      this.state = await this.api.getJSON('/api/plugins/github/state');
    } catch (e) {
      this.state = {workspaces: []};
      this._renderError(`failed to load github state: ${e.message || e}`);
      return;
    }
    this._ctx?.refreshNav?.();
    this._render();
  }

  async _reloadAuth() {
    try {
      this.auth = await this.api.getJSON('/api/plugins/github/auth');
    } catch (e) {
      this.auth = {installed: false, auth_ok: false, hint: `auth probe failed: ${e.message || e}`};
    }
    this._ctx?.refreshNav?.();
    this._render();
  }

  // ── Render ────────────────────────────────────────────────────

  _render() {
    if (!this.root) return;
    this._renderHead();
    if (this._section !== 'auth') this._renderAuthBar();
    else if (this._authBarEl) render(html``, this._authBarEl);

    const body = this._bodyEl;
    if (!body) return;
    const wss = (this.state && this.state.workspaces) || [];

    if (this._section === 'activity') {
      render(this._activitySection(wss), body);
      return;
    }
    if (this._section === 'auth') {
      render(this._authSection(), body);
      return;
    }
    if (this._section && this._section.startsWith('workspace:')) {
      const ws = this._section.slice('workspace:'.length);
      const found = wss.find(w => w.workspace === ws);
      render(this._workspaceSection(found, ws), body);
      return;
    }
    if (this._section === 'workspaces') {
      if (!wss.length) {
        render(html`<div class="gh-empty">No workspaces registered yet.</div>`, body);
        return;
      }
      const show = this.focusWs ? wss.filter(w => w.workspace === this.focusWs) : wss;
      render(html`${this._workflow(show)}${show.map(w => this._card(w))}`, body);
      return;
    }

    // overview
    if (!wss.length) {
      render(html`<div class="gh-empty">No workspaces registered.<br>
        <span class="hint">Add a workspace from the header pill, then configure GitHub here.</span></div>`, body);
      return;
    }
    const configured = wss.filter(w => w.configured);
    render(html`
      ${this._workflow(wss)}
      <div class="gh-overview">${wss.map(w => this._mini(w))}</div>
      ${configured.length ? html`
        <div style="font-family:var(--f-mono);font-size:10px;color:var(--t-4);text-transform:uppercase;letter-spacing:.08em;margin:8px 0 6px">Active</div>
        ${configured.filter(w => w.running).map(w => this._card(w))}` : html``}`, body);
  }

  _workflow(wss) {
    const configured = (wss || []).filter(w => w.configured).length;
    const rules = (wss || []).reduce((n, w) => n + ((w.rules || []).length), 0);
    const running = (wss || []).filter(w => w.running).length;
    return html`<div class="gh-workflow">
      <div class="step"><div class="k">1 configure</div><div class="v">${configured}/${wss.length} workspaces have a repo</div></div>
      <div class="step"><div class="k">2 route</div><div class="v">${rules} issue/PR rule${rules === 1 ? '' : 's'} defined</div></div>
      <div class="step"><div class="k">3 run</div><div class="v">${running} poller${running === 1 ? '' : 's'} active</div></div>
    </div>`;
  }

  _renderHead() {
    const head = this._headEl;
    if (!head) return;
    const wss = (this.state && this.state.workspaces) || [];
    const configured = wss.filter(w => w.configured).length;
    const running = wss.filter(w => w.running).length;
    render(html`
      <div>
        <div class="eyebrow">Integrations</div>
        <h1>GitHub</h1>
        <div class="stats">
          <span class="stat"><b>${wss.length}</b> workspaces</span>
          <span class="stat"><b>${configured}</b> configured</span>
          <span class="stat ok"><b>${running}</b> polling</span>
        </div>
      </div>
      <div class="controls">
        <button type="button" id="gh-refresh"
          @click=${() => Promise.all([this._reload(), this._reloadAuth()])}>Refresh</button>
      </div>`, head);
  }

  _mini(w) {
    const ws = w.workspace;
    const on = w.running;
    const cfg = w.configured;
    const repo = w.repo ? w.repo : 'Not configured — click to set up';
    const rules = w.rules?.length || 0;
    return html`<div class="gh-mini ${cfg ? '' : 'unconfigured'}" data-ws=${ws}
      @click=${() => this._gotoWorkspace(ws)}>
      <div class="ws">${ws}</div>
      <div class="repo">${repo}</div>
      <div class="row">
        <span><span class="dot ${on ? 'on' : 'off'}"></span>${on ? 'polling' : (cfg ? 'stopped' : 'setup')}</span>
        ${cfg
          ? html`<span>${rules} rule${rules === 1 ? '' : 's'}</span>`
          : html`<span class="gh-setup-cta">Configure →</span>`}
      </div>
    </div>`;
  }

  _gotoWorkspace(ws) {
    this.focusWs = ws;
    this._section = `workspace:${ws}`;
    this._ctx?.setSection?.(`workspace:${ws}`);
    this._ensureEditorOpen(ws);
  }

  _workspaceSection(w, wsName) {
    if (!w) {
      return html`<div class="gh-empty">Workspace ${wsName} is no longer registered.</div>`;
    }
    const ws = w.workspace;
    const statusText = w.config_error
      ? 'config error'
      : (w.running ? 'polling' : (w.configured ? 'configured' : 'not configured'));
    const rules = (this.editorParsed?.rules || w.rules || []).length;
    const busy = this.busy.has(ws);
    const toast = this._toasts && this._toasts.get(ws);
    const evList = this.events.get(ws) || [];
    const lastError = _displayConfigError(w.cursor?.last_error || w.config_error);
    return html`
      <div class="gh-card gh-workspace-detail ${w.config_error ? 'errored' : ''}">
        <div class="gh-card-head">
          <span class="ws">${ws}</span>
          ${w.repo ? html`<span class="repo">${w.repo}</span>` : html`<span class="repo missing">no repo yet</span>`}
          <span class="status" style="margin-left:14px">
            <span class="dot ${w.running && !w.config_error ? 'on' : 'off'}"></span>${statusText}
          </span>
          <span class="actions">
            <span class="gh-toast ${toast?.err ? 'err' : ''}">${toast?.text || ''}</span>
            <button ?disabled=${busy || !w.running} @click=${() => this._sync(ws)}>${busy ? '…' : 'Sync now'}</button>
          </span>
        </div>
        <div class="gh-workspace-kicker">
          <span class="pill ${w.configured ? 'ok' : ''}">${w.configured ? 'repo configured' : 'repo required'}</span>
          <span class="pill">${rules} rule${rules === 1 ? '' : 's'}</span>
          <span class="pill">${w.poll_interval_s || this.editorParsed?.poll_interval_s || 30}s poll</span>
          ${lastError ? html`<span class="pill err" title=${lastError}>last poll error</span>` : html``}
        </div>
        ${this._editor(ws, {sticky: true})}
        <div class="gh-events">
          <div class="title">
            <span>Recent events</span>
            <span style="color:var(--t-4);font-weight:400;text-transform:none;letter-spacing:0">live · this workspace</span>
          </div>
          ${evList.length
            ? evList.slice(0, MAX_EVENTS_PER_WS).map(e => html`
                <div class="row">
                  <span class="ts">${_fmtTime(e.ts)}</span>
                  <span class="et">${e.type}</span>
                  <span class="ec">${_eventCaption(e.payload)}</span>
                </div>`)
            : html`<div class="none">No live GitHub events captured yet.</div>`}
        </div>
      </div>`;
  }

  _activitySection(wss) {
    const all = [];
    for (const w of wss) {
      for (const e of (this.events.get(w.workspace) || [])) {
        all.push({ ...e, workspace: w.workspace });
      }
    }
    all.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    if (!all.length) {
      return html`<div class="gh-empty">No GitHub events yet.<br>
        <span class="hint">Events appear when pollers match rules and emit <code>github.*</code> on the bus.</span></div>`;
    }
    return html`<div class="gh-card"><div class="gh-events" style="padding:0">
      <div class="title" style="padding:10px 14px;border-bottom:1px solid var(--line-1)">Recent activity · ${all.length}</div>
      ${all.slice(0, 60).map(e => html`
        <div class="row" style="padding:8px 14px">
          <span class="ts">${_fmtTime(e.ts)}</span>
          <span class="et">${e.workspace}</span>
          <span class="et">${e.type}</span>
          <span class="ec">${_eventCaption(e.payload)}</span>
        </div>`)}
    </div></div>`;
  }

  _authSection() {
    const a = this.auth || {};
    if (!a.installed) {
      return html`<div class="gh-empty">Install the GitHub CLI first.<br><br>
        <code>${_installHint()}</code></div>`;
    }
    if (!a.auth_ok) {
      return html`<div class="gh-card"><div class="gh-auth warn" style="border:0;border-radius:6px">
        <span class="dot"></span>
        <span class="msg">${a.hint || 'Run gh auth login'}</span>
        <button type="button" class="copy" @click=${e => this._copy('gh auth login', e.currentTarget)}>Copy command</button>
      </div>
      <div style="padding:14px;font-family:var(--f-mono);font-size:var(--t-xs);color:var(--t-3);line-height:1.6">
        <pre style="background:var(--bg-0);padding:10px;border-radius:4px;border:1px solid var(--line-1)">gh auth login</pre>
      </div></div>`;
    }
    return html`<div class="gh-card"><div class="gh-auth ok" style="border:0;border-radius:6px">
      <span class="dot"></span>
      <span class="msg">Logged in as <span class="user">${a.user || '?'}</span>
        ${a.host ? html`<span class="host">@${a.host}</span>` : html``}</span>
    </div></div>`;
  }

  _card(w) {
    const ws = w.workspace;
    const hasErr = !!(w.config_error || w.cursor.last_error);
    const configErr = _displayConfigError(w.config_error);
    const cursorErr = _displayConfigError(w.cursor.last_error);
    const configured = !!w.configured;
    const repoCell = w.repo
      ? html`<span class="repo">${w.repo}</span>`
      : html`<span class="repo missing">— not configured —</span>`;
    const statusOn = w.running && !w.config_error;
    const statusText = w.config_error ? 'config error' : (w.running ? 'polling' : 'idle');
    const intervalCell = w.poll_interval_s
      ? html`<span class="val">${w.poll_interval_s}s</span>`
      : html`<span class="val muted">—</span>`;
    const cursorIdCell = w.cursor.last_event_id
      ? html`<span class="val">${w.cursor.last_event_id}</span>`
      : html`<span class="val muted">never polled</span>`;
    const cursorTsCell = w.cursor.last_poll_ts
      ? html`<span class="val">${w.cursor.last_poll_ts}</span>`
      : html`<span class="val muted">never</span>`;
    const cursorErrCell = w.cursor.last_error
      ? html`<span class="val err" title=${cursorErr}>${cursorErr.slice(0, 120)}</span>`
      : (w.config_error
          ? html`<span class="val err" title=${configErr}>${configErr}</span>`
          : html`<span class="val muted">none</span>`);

    const rulesBlock = w.config_error
      ? html`<div class="err">${configErr}</div>`
      : (w.rules.length
          ? w.rules.map(r => html`
            <div class="row">
              <span class="name">${r.name}</span>
              <span class="when">when ${_summarize(r.when)}</span>
              <span class="do">→ ${r.do.join(' + ') || '∅'}</span>
            </div>`)
          : html`<div class="none">— no rules defined —</div>`);

    const evList = this.events.get(ws) || [];
    const eventsBlock = evList.length
      ? evList.slice(0, MAX_EVENTS_PER_WS).map(e => html`
          <div class="row">
            <span class="ts">${_fmtTime(e.ts)}</span>
            <span class="et">${e.type}</span>
            <span class="ec">${_eventCaption(e.payload)}</span>
          </div>`)
      : html`<div class="none">— waiting for github.&lt;EventName&gt; on the bus —</div>`;

    const busy = this.busy.has(ws);
    const editing = this.editing === ws;
    const toast = this._toasts && this._toasts.get(ws);

    if (!configured) {
      return html`
        <div class="gh-card setup ${editing ? 'editing' : ''}">
          <div class="gh-card-head">
            <span class="ws">${ws}</span>
            ${repoCell}
            <span class="status" style="margin-left:14px">
              <span class="dot off"></span>setup needed
            </span>
            <span class="actions">
              <span class="gh-toast ${toast?.err ? 'err' : ''}">${toast?.text || ''}</span>
              <button class="primary" ?disabled=${busy} @click=${() => this._openEditor(ws)}>${editing ? 'Close' : 'Configure'}</button>
            </span>
          </div>
          <div class="gh-quick" data-ws=${ws}>
            <div class="field">
              <label>Repository</label>
              <input data-repo type="text" placeholder="owner/repo" ?disabled=${busy}>
            </div>
            <div class="buttons">
              <button ?disabled=${busy} @click=${(e) => this._quickConfigure(ws, 'issue', e.currentTarget)}>Route issues</button>
              <button ?disabled=${busy} @click=${(e) => this._quickConfigure(ws, 'pr', e.currentTarget)}>Route PRs</button>
              <button class="primary" ?disabled=${busy} @click=${(e) => this._quickConfigure(ws, 'both', e.currentTarget)}>Setup both</button>
            </div>
          </div>
          ${editing ? this._editor(ws) : html``}
        </div>`;
    }

    return html`
      <div class="gh-card ${hasErr ? 'errored' : ''} ${editing ? 'editing' : ''}">
        <div class="gh-card-head">
          <span class="ws">${ws}</span>
          ${repoCell}
          <span class="status" style="margin-left:14px">
            <span class="dot ${statusOn ? 'on' : 'off'}"></span>${statusText}
          </span>
          <span class="actions">
            <span class="gh-toast ${toast?.err ? 'err' : ''}">${toast?.text || ''}</span>
            <button @click=${() => this._openEditorWithTemplate(ws, 'issue')}>Route issue</button>
            <button @click=${() => this._openEditorWithTemplate(ws, 'pr')}>Route PR</button>
            <button @click=${() => this._openEditor(ws)}>${editing ? 'Close' : 'Edit'}</button>
            <button ?disabled=${busy || !w.running} @click=${() => this._sync(ws)}>${busy ? '…' : 'Sync now'}</button>
          </span>
        </div>
        <div class="gh-grid">
          <div class="cell"><div class="lbl">poll interval</div>${intervalCell}</div>
          <div class="cell"><div class="lbl">last event id</div>${cursorIdCell}</div>
          <div class="cell"><div class="lbl">last poll</div>${cursorTsCell}</div>
          <div class="cell"><div class="lbl">last error</div>${cursorErrCell}</div>
          <div class="cell"><div class="lbl">rules</div><span class="val">${w.rules.length}</span></div>
        </div>
        <div class="gh-rules">
          <div class="title">Rules</div>
          ${rulesBlock}
        </div>
        <div class="gh-events">
          <div class="title">
            <span>Recent events</span>
            <span style="color:var(--t-4);font-weight:400;text-transform:none;letter-spacing:0">live · keeps last ${MAX_EVENTS_PER_WS}</span>
          </div>
          ${eventsBlock}
        </div>
        ${editing ? this._editor(ws) : html``}
      </div>`;
  }

  _editor(ws, {sticky = false} = {}) {
    const msg = this.editorMsg;
    const body = this.editorMode === 'yaml'
      ? html`<textarea spellcheck="false" placeholder="loading…"
          .value=${this.editorText ?? ''}
          @input=${e => { this.editorText = e.target.value; }}
          @keydown=${e => this._editorKeydown(e)}></textarea>`
      : this._structuredEditor();
    return html`
      <div class="gh-editor">
        <div class="title">
          <span>
            GitHub routing
            <span class="gh-editor-tabs">
              <button class=${this.editorMode === 'form' ? 'active' : ''}
                @click=${() => this._switchMode('form')}>Form</button>
              <button class=${this.editorMode === 'yaml' ? 'active' : ''}
                @click=${() => this._switchMode('yaml')}>Advanced</button>
            </span>
          </span>
          <span class="path">${this.editorMode === 'yaml' ? 'advanced config' : 'workspace settings'}</span>
        </div>
        ${body}
        <div class="actions">
          <button class="save" @click=${() => this._saveEditor()}>Save</button>
          <button class="cancel" @click=${() => sticky ? this._reloadEditor(ws) : this._closeEditor()}>${sticky ? 'Reload' : 'Cancel'}</button>
          <span class="msg ${msg ? msg.kind : ''}">${msg ? msg.text : ''}</span>
          <span style="color:var(--t-4);font-size:var(--t-xxs);margin-left:auto">${this.editorMode === 'yaml' ? '⌘/Ctrl+Enter to save' : ''}</span>
        </div>
      </div>`;
  }

  _editorKeydown(e) {
    const ta = e.target;
    if (e.key === 'Tab') {
      e.preventDefault();
      const s = ta.selectionStart, x = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(x);
      ta.selectionStart = ta.selectionEnd = s + 2;
      this.editorText = ta.value;
    }
    if (e.key === 'Escape') this._closeEditor();
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') this._saveEditor();
  }

  _structuredEditor() {
    const p = this.editorParsed || {repo: '', poll_interval_s: 30, rules: []};
    const rules = (p.rules || []);
    return html`
      <div class="gh-form">
        <datalist id="gh-agent-targets">
          ${(this.agents || []).map(a => html`<option value=${a.id}>${a.name || a.id}${a.workspace ? ` · ${a.workspace}` : ''}</option>`)}
        </datalist>
        <div class="top-grid">
          <div class="field">
            <label>Repo</label>
            <input type="text" .value=${p.repo || ''} placeholder="owner/repo"
              @input=${e => { p.repo = e.target.value; }}>
          </div>
          <div class="field">
            <label>Poll (sec)</label>
            <input type="number" min="1" step="1" .value=${String(p.poll_interval_s || 30)}
              @input=${e => { p.poll_interval_s = Number(e.target.value); }}>
          </div>
        </div>
        <div class="gh-rules-list">
          <div class="title">
            <span>Rules · ${rules.length}</span>
            <span class="tools">
              <button class="quick" @click=${() => this._addTemplateRule('pr')}>PR opened</button>
              <button class="quick" @click=${() => this._addTemplateRule('issue')}>Issue opened</button>
              <button class="add" @click=${() => this._addRule()}>+ Add rule</button>
            </span>
          </div>
          ${rules.length
            ? rules.map((r, i) => this._rule(r, i))
            : html`<div class="empty">No rules yet. Click + Add rule to define one.</div>`}
        </div>
      </div>`;
  }

  _addRule() {
    const p = this.editorParsed;
    if (!p) return;
    p.rules = p.rules || [];
    p.rules.push({name: `rule-${p.rules.length + 1}`, when: {}, do: []});
    this._render();
  }

  _addTemplateRule(kind) {
    const p = this.editorParsed;
    if (!p) return;
    p.rules = p.rules || [];
    const to = this._defaultAgentForEditing();
    p.rules.push(this._templateRule(kind, to));
    this._render();
  }

  _templateRule(kind, to = '') {
    if (kind === 'pr') {
      return {
        name: 'pr-opened-route',
        when: {event: 'PullRequestEvent', action: 'opened'},
        do: [{'agent.message': {
          to,
          body: 'Review PR #{{ pull_request.number }}: {{ pull_request.title }}\n{{ pull_request.html_url }}',
        }}],
      };
    }
    return {
      name: 'issue-opened-route',
      when: {event: 'IssuesEvent', action: 'opened'},
      do: [{'agent.message': {
        to,
        body: 'Triage issue #{{ issue.number }}: {{ issue.title }}\n{{ issue.html_url }}',
      }}],
    };
  }

  _defaultAgentForEditing() {
    const ws = this.editing;
    const matches = (this.agents || []).filter(a => !ws || (a.workspace || '') === ws);
    return matches.length === 1 ? matches[0].id : '';
  }

  async _loadAgents() {
    const agents = await this.api.getJSON('/api/agents').catch(() => []);
    this.agents = Array.isArray(agents) ? agents : [];
    return this.agents;
  }

  async _defaultAgentForWorkspace(ws) {
    const agents = await this._loadAgents();
    const matches = agents.filter(a => !ws || (a.workspace || '') === ws);
    return matches.length === 1 ? matches[0].id : '';
  }

  async _quickConfigure(ws, kind, btn) {
    if (this.busy.has(ws)) return;
    const root = btn?.closest('.gh-quick');
    const repo = (root?.querySelector('[data-repo]')?.value || '').trim();
    if (!repo) {
      this._setToast(ws, 'repo required', true);
      return;
    }
    this.busy.add(ws);
    this._setToast(ws, 'preparing…', false);
    try {
      const to = await this._defaultAgentForWorkspace(ws);
      const rules = [];
      if (kind === 'both' || kind === 'issue') rules.push(this._templateRule('issue', to));
      if (kind === 'both' || kind === 'pr') rules.push(this._templateRule('pr', to));
      if (!to) {
        this._setToast(ws, 'pick an agent in the editor', true);
        if (this.editing !== ws) await this._openEditor(ws);
        this.editorMode = 'form';
        this.editorParsed = this.editorParsed || {repo: '', poll_interval_s: 30, rules: []};
        this.editorParsed.repo = repo;
        this.editorParsed.poll_interval_s = this.editorParsed.poll_interval_s || 30;
        this.editorParsed.rules = [...(this.editorParsed.rules || []), ...rules];
        this._render();
        return;
      }
      this._setToast(ws, 'saving…', false);
      await this._saveConfigPayload(ws, {workspace: ws, structured: _cleanStructured({
        repo,
        poll_interval_s: 30,
        rules,
      })}, {editor: false});
    } finally {
      this.busy.delete(ws);
      this._render();
    }
  }

  async _openEditorWithTemplate(ws, kind) {
    if (this.editing !== ws) await this._openEditor(ws);
    if (!this.editorParsed) return;
    this._addTemplateRule(kind);
  }

  _rule(rule, idx) {
    const p = this.editorParsed;
    const name = rule.name || '';
    const when = rule.when || {};
    const evt = when.event || '';
    const action = when.action || '';
    const otherPreds = Object.entries(when).filter(([k]) => k !== 'event' && k !== 'action');

    const actionList = (evt && ACTIONS_BY_EVENT[evt]) ? ACTIONS_BY_EVENT[evt] : null;
    const actionCustom = action && actionList && !actionList.includes(action) ? action : null;
    const actionKinds = (rule.do || [])
      .map(a => (a && typeof a === 'object' ? Object.keys(a)[0] : ''))
      .filter(Boolean);

    return html`
      <div class="gh-rule" data-rule=${idx}>
        <div class="rh">
          <span class="ico">▸</span>
          <input type="text" placeholder="rule name" .value=${name}
            @input=${e => { rule.name = e.target.value; }}>
          <button class="del" @click=${() => this._removeRule(idx)}>Remove</button>
        </div>
        <div class="gh-flow">
          <div class="node">
            <div class="k">When</div>
            <div class="v">${evt || 'Any GitHub event'}</div>
            <div class="s">${action ? `payload.action = ${action}` : 'Any action'}${otherPreds.length ? ` · ${otherPreds.length} filter${otherPreds.length === 1 ? '' : 's'}` : ''}</div>
          </div>
          <div class="arr">→</div>
          <div class="node">
            <div class="k">Do</div>
            <div class="v">${actionKinds.length ? actionKinds.join(' + ') : 'No action yet'}</div>
            <div class="s">${actionKinds.length ? `${actionKinds.length} step${actionKinds.length === 1 ? '' : 's'} run in order` : 'Add at least one action to make the rule useful'}</div>
          </div>
        </div>
        <div class="sect">
          <div class="sect-head">
            <span>When</span>
            <button class="add" @click=${() => this._addPredicate(rule)}>+ predicate</button>
          </div>
          <div class="gh-preds">
            <div class="pred" data-fixed="event">
              <select @change=${e => this._setEvent(rule, e.target.value)}>
                <option value="" ?selected=${!evt}>(any event)</option>
                ${EVENT_TYPES.map(t => html`<option value=${t} ?selected=${t === evt}>${t}</option>`)}
                ${(EVENT_TYPES.includes(evt) || !evt) ? html`` : html`<option value=${evt} selected>${evt} (custom)</option>`}
              </select>
              <span style="font-size:var(--t-xxs);color:var(--t-4);align-self:center">event type</span>
              <span></span>
            </div>
            <div class="pred" data-fixed="action">
              <select ?disabled=${!actionList} @change=${e => this._setAction(rule, e.target.value)}>
                <option value="" ?selected=${!action}>(any action)</option>
                ${actionList ? actionList.map(a => html`<option value=${a} ?selected=${a === action}>${a}</option>`) : html``}
                ${actionCustom ? html`<option value=${actionCustom} selected>${actionCustom} (custom)</option>` : html``}
              </select>
              <span style="font-size:var(--t-xxs);color:var(--t-4);align-self:center">payload.action</span>
              <span></span>
            </div>
            ${otherPreds.map(([k, v], pi) => this._pred(rule, k, v, pi))}
          </div>
        </div>
        ${this._templateSheet(rule, idx)}
        <div class="sect">
          <div class="sect-head">
            <span>Do (${(rule.do || []).length})</span>
            <button class="add" @click=${() => this._addAction(rule)}>+ action</button>
          </div>
          <div class="gh-actions-list">
            ${(rule.do || []).length
              ? (rule.do || []).map((act, ai) => this._action(rule, act, ai))
              : html`<div style="padding:8px;color:var(--t-4);font-size:var(--t-xs);font-style:italic">No actions yet — this rule will match but do nothing.</div>`}
          </div>
        </div>
      </div>`;
  }

  _templateSheet(rule, idx) {
    const evt = rule.when?.event || '';
    const eventVars = evt ? (EVENT_TEMPLATE_VARS[evt] || []) : POPULAR_TEMPLATE_VARS;
    const eventTitle = evt || 'Popular';
    const activeTarget = this._tplTarget && this._tplTarget.rule === rule
      ? this._tplTarget
      : this._defaultTemplateTarget(rule);
    const target = activeTarget
      ? `${activeTarget.kind}.${activeTarget.field}`
      : 'no action target';
    return html`
      <details class="gh-vars" open>
        <summary>
          <span>Variables</span>
          <span class="sub">${eventTitle} · ${target}</span>
        </summary>
        <div class="gh-var-body">
          ${this._templateGroup('Common event', COMMON_TEMPLATE_VARS, rule)}
          ${eventVars.length
            ? this._templateGroup(eventTitle, eventVars, rule)
            : html`<div class="gh-var-group"><div class="g">${eventTitle}</div><div class="gh-var-list"><span style="color:var(--t-4);font-size:10px">No extra payload fields.</span></div></div>`}
          <div class="gh-var-stdin">
            <div class="g">Script / code stdin</div>
            <div><code>json.load(sys.stdin)</code></div>
            <div><code>jq -r '.payload.issue.title'</code></div>
          </div>
        </div>
      </details>`;
  }

  _templateGroup(title, vars, rule) {
    return html`
      <div class="gh-var-group">
        <div class="g">${title}</div>
        <div class="gh-var-list">
          ${vars.map(([path, label]) => html`
            <button type="button" class="gh-var"
              title=${`Insert {{ ${path} }} · ${label}`}
              @click=${() => this._insertTemplate(rule, path)}>
              {{ ${path} }}
            </button>`)}
        </div>
      </div>`;
  }

  _rememberTemplateTarget(e, rule, idx, kind, field) {
    const el = e.currentTarget;
    this._tplTarget = {
      rule, idx, kind, field,
      start: typeof el.selectionStart === 'number' ? el.selectionStart : null,
      end: typeof el.selectionEnd === 'number' ? el.selectionEnd : null,
      value: typeof el.value === 'string' ? el.value : null,
    };
  }

  _captureTemplateSelection(e) {
    if (!this._tplTarget) return;
    const el = e.currentTarget;
    if (typeof el.selectionStart === 'number') this._tplTarget.start = el.selectionStart;
    if (typeof el.selectionEnd === 'number') this._tplTarget.end = el.selectionEnd;
    if (typeof el.value === 'string') this._tplTarget.value = el.value;
  }

  _defaultTemplateTarget(rule) {
    const list = rule.do || [];
    for (let i = 0; i < list.length; i++) {
      const [kind] = Object.entries(list[i] || {})[0] || [];
      const field = ACTION_TEMPLATE_TARGET[kind];
      if (field) return {rule, idx: i, kind, field, start: null, end: null};
    }
    return null;
  }

  _insertTemplate(rule, path) {
    const token = `{{ ${path} }}`;
    const explicitTarget = this._tplTarget && this._tplTarget.rule === rule;
    const target = explicitTarget ? this._tplTarget : this._defaultTemplateTarget(rule);
    if (!target) {
      this._copyText(token);
      this.editorMsg = {kind: 'ok', text: `copied ${token}`};
      this._render();
      return;
    }
    const action = (target.rule.do || [])[target.idx];
    const params = action && action[target.kind];
    if (!params) return;

    if (target.kind === 'bus.emit' && target.field === 'data' && explicitTarget) {
      const current = target.value ?? (params.data ? JSON.stringify(params.data, null, 0) : '');
      const text = _insertToken(current, token, target.start, target.end);
      try {
        const parsed = text ? JSON.parse(text) : {};
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not object');
        params.data = parsed;
      } catch (_) {
        this.editorMsg = {kind: 'err', text: 'data must stay valid JSON'};
        this._render();
        return;
      }
    } else if (target.kind === 'bus.emit' && target.field === 'data') {
      const data = (params.data && typeof params.data === 'object' && !Array.isArray(params.data))
        ? {...params.data}
        : {};
      let key = _templateDataKey(path);
      let n = 2;
      while (Object.prototype.hasOwnProperty.call(data, key)) {
        key = `${_templateDataKey(path)}_${n++}`;
      }
      data[key] = token;
      params.data = data;
    } else if (target.kind === 'gh' && target.field === 'args') {
      const current = Array.isArray(params.args) ? params.args.join(' ') : String(params.args || '');
      const text = _insertToken(current, token, target.start, target.end, {spaceAtEnd: true});
      params.args = _splitArgs(text);
    } else {
      const current = String(params[target.field] || '');
      params[target.field] = _insertToken(current, token, target.start, target.end);
    }

    this.editorMsg = {kind: 'ok', text: `inserted ${token}`};
    this._render();
  }

  async _copyText(text) {
    try { await navigator.clipboard.writeText(text); } catch (_) {}
  }

  _removeRule(idx) {
    const p = this.editorParsed;
    if (!p || !p.rules) return;
    p.rules.splice(idx, 1);
    this._render();
  }

  _setEvent(rule, value) {
    rule.when = rule.when || {};
    if (value) rule.when.event = value;
    else delete rule.when.event;
    // Reset action when event changes (action choices depend on event).
    delete rule.when.action;
    this._render();
  }

  _setAction(rule, value) {
    rule.when = rule.when || {};
    if (value) rule.when.action = value;
    else delete rule.when.action;
    // Leaf control: avoid remounting the dropdown while the user is picking.
  }

  _addPredicate(rule) {
    rule.when = rule.when || {};
    // Pick the first predicate key not already in use.
    const used = new Set(Object.keys(rule.when));
    const next = PREDICATE_KEYS.find(k => !used.has(k)) || PREDICATE_KEYS[0];
    rule.when[next] = '';
    this._render();
  }

  _pred(rule, key, v, pi) {
    // The displayed key may not be the model key after a key-change; we
    // re-derive the live key from the current `when` ordering on each
    // mutation so handlers always target the right entry.
    const liveKey = () => {
      const others = Object.entries(rule.when || {}).filter(([k]) => k !== 'event' && k !== 'action');
      return (others[pi] || [])[0];
    };
    return html`
      <div class="pred" data-pred=${pi}>
        <select @change=${e => this._changePredKey(rule, liveKey(), e.target.value)}>
          ${PREDICATE_KEYS.map(opt => html`<option value=${opt} ?selected=${opt === key}>${opt}</option>`)}
          ${PREDICATE_KEYS.includes(key) ? html`` : html`<option value=${key} selected>${key} (custom)</option>`}
        </select>
        <input type="text" .value=${Array.isArray(v) ? v.join(',') : v}
          placeholder="value (comma-separated for OR-match)"
          @input=${e => this._changePredVal(rule, liveKey(), e.target.value)}>
        <button class="x" title="remove predicate" @click=${() => this._removePred(rule, liveKey())}>✕</button>
      </div>`;
  }

  _changePredKey(rule, oldKey, newKey) {
    if (oldKey == null) return;
    const cur = rule.when[oldKey];
    delete rule.when[oldKey];
    rule.when[newKey] = cur;
    this._render();
  }

  _changePredVal(rule, key, value) {
    if (key == null) return;
    // Comma-separated → list (OR-match shorthand).
    rule.when[key] = value.includes(',')
      ? value.split(',').map(s => s.trim()).filter(Boolean)
      : value;
  }

  _removePred(rule, key) {
    if (key == null) return;
    delete rule.when[key];
    this._render();
  }

  _addAction(rule) {
    rule.do = rule.do || [];
    rule.do.push({'agent.message': {to: '', body: ''}});
    this._render();
  }

  _action(rule, action, idx) {
    if (!action || typeof action !== 'object') return html``;
    const [kind, raw] = Object.entries(action)[0] || ['agent.message', {}];
    const params = raw || {};

    let fields = html``;
    if (kind === 'agent.message') {
      fields = html`
        <div class="lbl">to</div>
        <input type="text" list="gh-agent-targets" .value=${params.to || ''} placeholder="agent-id"
          @input=${e => this._setActField(rule, idx, kind, 'to', e.target.value)}>
        <div class="lbl">body</div>
        <textarea placeholder="message body — use {{ payload.path }} for substitution"
          .value=${params.body || ''}
          @focus=${e => this._rememberTemplateTarget(e, rule, idx, kind, 'body')}
          @keyup=${e => this._captureTemplateSelection(e)}
          @mouseup=${e => this._captureTemplateSelection(e)}
          @select=${e => this._captureTemplateSelection(e)}
          @input=${e => { this._setActField(rule, idx, kind, 'body', e.target.value); this._rememberTemplateTarget(e, rule, idx, kind, 'body'); }}></textarea>`;
    } else if (kind === 'model') {
      const files = Array.isArray(params.read_files) ? params.read_files.join(', ') : (params.read_files || '');
      fields = html`
        <div class="lbl">prompt</div>
        <textarea placeholder="Classify or summarize this event"
          .value=${params.prompt || ''}
          @focus=${e => this._rememberTemplateTarget(e, rule, idx, kind, 'prompt')}
          @keyup=${e => this._captureTemplateSelection(e)}
          @mouseup=${e => this._captureTemplateSelection(e)}
          @select=${e => this._captureTemplateSelection(e)}
          @input=${e => { this._setActField(rule, idx, kind, 'prompt', e.target.value); this._rememberTemplateTarget(e, rule, idx, kind, 'prompt'); }}></textarea>
        <div class="lbl">model</div>
        <input type="text" .value=${params.model || 'role:fast'} placeholder="role:fast"
          @input=${e => this._setActField(rule, idx, kind, 'model', e.target.value)}>
        <div class="lbl">send to</div>
        <input type="text" list="gh-agent-targets" .value=${params.to || ''} placeholder="optional agent-id"
          @input=${e => this._setActField(rule, idx, kind, 'to', e.target.value)}>
        <div class="lbl">emit</div>
        <input type="text" .value=${params.emit || ''} placeholder="optional bus event type"
          @input=${e => this._setActField(rule, idx, kind, 'emit', e.target.value)}>
        <div class="lbl">max tokens</div>
        <input type="number" .value=${params.max_tokens || 256} min="1"
          @input=${e => this._setActField(rule, idx, kind, 'max_tokens', e.target.value)}>
        <div class="lbl">files</div>
        <input type="text" .value=${files} placeholder="optional workspace paths, comma-separated"
          @input=${e => this._setActField(rule, idx, kind, 'read_files', e.target.value)}>
        <label class="check span"><input type="checkbox" .checked=${!!params.include_event}
          @change=${e => this._setActField(rule, idx, kind, 'include_event', e.target.checked)}>
          include the full GitHub event JSON</label>`;
    } else if (kind === 'script') {
      fields = html`
        <div class="lbl">path</div>
        <input type="text" .value=${params.path || ''} placeholder="scripts/notify.py (relative to workspace)"
          @focus=${e => this._rememberTemplateTarget(e, rule, idx, kind, 'path')}
          @keyup=${e => this._captureTemplateSelection(e)}
          @mouseup=${e => this._captureTemplateSelection(e)}
          @select=${e => this._captureTemplateSelection(e)}
          @input=${e => { this._setActField(rule, idx, kind, 'path', e.target.value); this._rememberTemplateTarget(e, rule, idx, kind, 'path'); }}>
        <div class="lbl">timeout</div>
        <input type="number" .value=${params.timeout || ''} placeholder="60" min="1"
          @input=${e => this._setActField(rule, idx, kind, 'timeout', e.target.value)}>`;
    } else if (kind === 'gh') {
      const args = Array.isArray(params.args) ? params.args.join(' ') : '';
      fields = html`
        <div class="lbl">args</div>
        <input type="text" .value=${args} placeholder='pr comment {{ pull_request.number }} --body "ack"'
          @focus=${e => this._rememberTemplateTarget(e, rule, idx, kind, 'args')}
          @keyup=${e => this._captureTemplateSelection(e)}
          @mouseup=${e => this._captureTemplateSelection(e)}
          @select=${e => this._captureTemplateSelection(e)}
          @input=${e => { this._setActField(rule, idx, kind, 'args', e.target.value); this._rememberTemplateTarget(e, rule, idx, kind, 'args'); }}>
        <div class="lbl">timeout</div>
        <input type="number" .value=${params.timeout || ''} placeholder="60" min="1"
          @input=${e => this._setActField(rule, idx, kind, 'timeout', e.target.value)}>`;
    } else if (kind === 'bus.emit') {
      const data = params.data ? JSON.stringify(params.data, null, 0) : '';
      fields = html`
        <div class="lbl">type</div>
        <input type="text" .value=${params.type || ''} placeholder="custom-event-name"
          @input=${e => this._setActField(rule, idx, kind, 'type', e.target.value)}>
        <div class="lbl">data</div>
        <input type="text" .value=${data} placeholder='{"key": "value"} — JSON object'
          @focus=${e => this._rememberTemplateTarget(e, rule, idx, kind, 'data')}
          @keyup=${e => this._captureTemplateSelection(e)}
          @mouseup=${e => this._captureTemplateSelection(e)}
          @select=${e => this._captureTemplateSelection(e)}
          @input=${e => { this._setActField(rule, idx, kind, 'data', e.target.value); this._rememberTemplateTarget(e, rule, idx, kind, 'data'); }}>`;
    } else if (kind === 'code') {
      fields = html`
        <div class="lbl">lang</div>
        <select @change=${e => this._setActField(rule, idx, kind, 'lang', e.target.value)}>
          ${['python', 'sh', 'bash'].map(v => html`<option value=${v} ?selected=${(params.lang || 'python') === v}>${v}</option>`)}
        </select>
        <div class="lbl">body</div>
        <textarea placeholder="source code; event JSON is piped on stdin"
          .value=${params.body || ''}
          @focus=${e => this._rememberTemplateTarget(e, rule, idx, kind, 'body')}
          @keyup=${e => this._captureTemplateSelection(e)}
          @mouseup=${e => this._captureTemplateSelection(e)}
          @select=${e => this._captureTemplateSelection(e)}
          @input=${e => { this._setActField(rule, idx, kind, 'body', e.target.value); this._rememberTemplateTarget(e, rule, idx, kind, 'body'); }}></textarea>
        <div class="lbl">emit</div>
        <input type="text" .value=${params.emit || ''} placeholder="optional bus event type"
          @input=${e => this._setActField(rule, idx, kind, 'emit', e.target.value)}>
        <div class="lbl">timeout</div>
        <input type="number" .value=${params.timeout || ''} placeholder="60" min="1"
          @input=${e => this._setActField(rule, idx, kind, 'timeout', e.target.value)}>`;
    }

    return html`
      <div class="gh-act" data-action=${idx}>
        <div class="ah">
          <select @change=${e => this._setActKind(rule, idx, e.target.value)}>
            ${ACTION_TYPES.map(t => html`<option value=${t} ?selected=${t === kind}>${t}</option>`)}
          </select>
          <button class="x" title="remove action" @click=${() => this._removeAction(rule, idx)}>✕</button>
        </div>
        <div class="ab">${fields}</div>
        <div class="hint">${ACTION_HINTS[kind] || ''}</div>
      </div>`;
  }

  _setActKind(rule, idx, newKind) {
    // Swap to default params for the new kind, preserving nothing
    // (different shapes, easier to start fresh).
    rule.do[idx] = {[newKind]: _defaultParamsForAction(newKind)};
    this._render();
  }

  _removeAction(rule, idx) {
    rule.do.splice(idx, 1);
    this._render();
  }

  _setActField(rule, idx, kind, field, value) {
    const cur = rule.do[idx];
    if (!cur) return;
    const params = cur[kind];
    if (!params) return;
    if (field === 'timeout' || field === 'max_tokens') {
      params[field] = value ? Number(value) : undefined;
      if (!params[field]) delete params[field];
    } else if (field === 'include_event') {
      params[field] = !!value;
    } else if (field === 'read_files') {
      const files = String(value || '')
        .split(/[,\n]/)
        .map(s => s.trim())
        .filter(Boolean);
      if (files.length) params[field] = files;
      else delete params[field];
    } else if (field === 'args' && kind === 'gh') {
      // Convert space-separated string to array; quoted args stay as one
      // token if user uses shell-like quoting.
      params.args = _splitArgs(value);
    } else if (field === 'data' && kind === 'bus.emit') {
      // Parse JSON object; fall back to {} on bad input so the field
      // doesn't ghost a stale value.
      try { params.data = value ? JSON.parse(value) : {}; }
      catch { params.data = {}; }
    } else {
      params[field] = value;
    }
  }

  _renderAuthBar() {
    const bar = this._authBarEl;
    if (!bar) return;
    const a = this.auth;
    if (!a) { render(html``, bar); return; }
    let tpl;
    if (!a.installed) {
      const cmd = _installHint();
      bar.className = 'gh-auth err';
      tpl = html`
        <span class="dot"></span>
        <span class="msg">gh CLI not installed — the plugin can't poll without it.</span>
        <span class="cmd"><code>${cmd}</code></span>
        <button class="copy" @click=${e => this._copy(cmd, e.currentTarget)}>Copy</button>`;
    } else if (!a.auth_ok) {
      bar.className = 'gh-auth warn';
      tpl = html`
        <span class="dot"></span>
        <span class="msg">gh installed but not authenticated — rules can't poll until you sign in.</span>
        <span class="cmd"><code>gh auth login</code></span>
        <button class="copy" @click=${e => this._copy('gh auth login', e.currentTarget)}>Copy</button>
        <button @click=${() => this._reloadAuth()}>Recheck</button>`;
    } else {
      bar.className = 'gh-auth ok';
      tpl = html`
        <span class="dot"></span>
        <span class="msg">Signed in
          ${a.user ? html`<span class="user">@${a.user}</span>` : html``}
          ${a.host ? html`<span class="host">on ${a.host}</span>` : html``}</span>
        <button @click=${() => this._reloadAuth()}>Recheck</button>`;
    }
    render(tpl, bar);
  }

  async _copy(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      const orig = btn.textContent;
      btn.textContent = 'Copied ✓';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    } catch (_) {}
  }

  _renderError(msg) {
    const body = this._bodyEl;
    if (body) render(html`<div class="gh-empty" style="color:var(--err)">${msg}</div>`, body);
  }

  // ── Editor ────────────────────────────────────────────────────

  async _openEditor(ws, {force = false} = {}) {
    if (this.editing === ws && !force) { this._closeEditor(); return; }
    this.editing = ws;
    this.editorMode = 'form';
    this.editorText = '';
    this.editorParsed = null;
    this.editorMsg = null;
    this._render();
    try {
      const [data, agents] = await Promise.all([
        this.api.getJSON(`/api/plugins/github/config?workspace=${encodeURIComponent(ws)}`),
        this.api.getJSON('/api/agents').catch(() => []),
      ]);
      this.agents = Array.isArray(agents) ? agents : [];
      this.editorPath = data.path;
      if (data.exists) {
        this.editorText = data.yaml || '';
        if (data.parsed) {
          this.editorParsed = _cloneParsed(data.parsed);
        } else {
          // File exists but doesn't parse — drop the operator into YAML
          // mode so they can fix it.
          this.editorMode = 'yaml';
          if (data.error) this.editorMsg = {kind: 'err', text: _displayConfigError(data.error)};
        }
      } else {
        // Fresh file — empty form with sane defaults.
        this.editorParsed = {repo: '', poll_interval_s: 30, rules: []};
        this.editorText = STARTER_YAML;
      }
    } catch (e) {
      this.editorMsg = {kind: 'err', text: `failed to load: ${e.message || e}`};
      this.editorParsed = {repo: '', poll_interval_s: 30, rules: []};
      this.editorText = STARTER_YAML;
    }
    this._render();
  }

  _ensureEditorOpen(ws) {
    if (!ws) return;
    if (this.editing === ws && (this.editorParsed || this.editorMode === 'yaml')) return;
    if (this._loadingEditor === ws) return;
    this._loadingEditor = ws;
    queueMicrotask(async () => {
      try {
        await this._openEditor(ws, {force: true});
      } finally {
        if (this._loadingEditor === ws) this._loadingEditor = null;
      }
    });
  }

  async _reloadEditor(ws) {
    if (!ws) return;
    this.editing = null;
    await this._openEditor(ws, {force: true});
  }

  _closeEditor() {
    this.editing = null;
    this.editorMode = 'form';
    this.editorText = null;
    this.editorParsed = null;
    this.editorPath = null;
    this.editorMsg = null;
    this._render();
  }

  _switchMode(mode) {
    if (mode === this.editorMode) return;
    if (mode === 'yaml') {
      // Switching to YAML view — no need to regenerate text; the
      // operator may want to edit existing yaml verbatim. The form
      // state stays separate; on save we use whichever mode is active.
      this.editorMode = 'yaml';
    } else {
      // Switching back to form — if we have parsed data, reuse it.
      // If the operator was hand-editing YAML and now wants the form,
      // they'd need a re-parse: we don't do that client-side. The
      // form just shows whatever was loaded from /config initially.
      // If parsed is null (file had a parse error), block the switch
      // with a message so the operator doesn't lose their edits.
      if (!this.editorParsed) {
        this.editorMsg = {kind: 'err', text: 'Advanced config must parse cleanly before switching to form view'};
        this._render();
        return;
      }
      this.editorMode = 'form';
    }
    this.editorMsg = null;
    this._render();
  }

  async _saveEditor() {
    if (!this.editing) return;
    const ws = this.editing;
    const payload = this.editorMode === 'yaml'
      ? {workspace: ws, yaml: this.editorText ?? ''}
      : {workspace: ws, structured: _cleanStructured(this.editorParsed)};
    this.editorMsg = {kind: 'ok', text: 'saving…'};
    this._render();
    await this._saveConfigPayload(ws, payload, {editor: true});
  }

  async _saveStructured(ws, structured) {
    if (this.busy.has(ws)) return;
    this.busy.add(ws);
    this._setToast(ws, 'saving…', false);
    try {
      await this._saveConfigPayload(ws, {workspace: ws, structured: _cleanStructured(structured)}, {editor: false});
    } finally {
      this.busy.delete(ws);
      this._render();
    }
  }

  async _saveConfigPayload(ws, payload, {editor = false} = {}) {
    try {
      const r = await this.api.fetch('/api/plugins/github/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!data.ok) {
        const errText = _displayConfigError(data.error || `HTTP ${r.status}`);
        if (editor) {
          this.editorMsg = {kind: 'err', text: errText};
          this._render();
        } else {
          this._setToast(ws, errText, true);
        }
        return;
      }
      if (editor) this.editorMsg = {kind: 'ok', text: 'saved ✓'};
      else this._setToast(ws, 'configured ✓', false);
      await this._reload();
      if (editor) {
        this.editorMsg = {kind: 'ok', text: 'saved ✓'};
        this._render();
        setTimeout(() => {
          if (this.editing === ws && this.editorMsg?.text === 'saved ✓') {
            this.editorMsg = null;
            this._render();
          }
        }, 3000);
      } else {
        setTimeout(() => { this._setToast(ws, '', false); }, 3000);
      }
    } catch (e) {
      const errText = _displayConfigError(e.message || e);
      if (editor) {
        this.editorMsg = {kind: 'err', text: errText};
        this._render();
      } else {
        this._setToast(ws, errText, true);
      }
    }
  }

  // ── Actions ───────────────────────────────────────────────────

  async _sync(ws) {
    if (this.busy.has(ws)) return;
    this.busy.add(ws);
    this._render();
    try {
      const r = await this.api.fetch('/api/plugins/github/sync', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workspace: ws}),
      });
      const data = await r.json();
      this._setToast(ws, data.ok ? 'synced ✓' : (data.error || 'failed'), !data.ok);
      if (data.ok || data.error) {
        setTimeout(() => { this._setToast(ws, '', false); }, 3000);
      }
    } catch (e) {
      this._setToast(ws, String(e.message || e), true);
    } finally {
      this.busy.delete(ws);
      // Reload cursor + render once the sync round-trips.
      await this._reload();
    }
  }

  _setToast(ws, text, err) {
    if (!this._toasts) this._toasts = new Map();
    if (text) this._toasts.set(ws, {text, err: !!err});
    else this._toasts.delete(ws);
    this._render();
  }

  // ── Live updates ──────────────────────────────────────────────

  _subscribeLive() {
    const bus = this.api && this.api.events;
    if (!bus || typeof bus.subscribe !== 'function') return;
    this.unsubEvent = bus.subscribe(ev => this._onEvent(ev));
  }

  _onEvent(ev) {
    if (!ev || !ev.type) return;

    // workspace.updated → opt-in/out may have changed; refetch.
    if (ev.type === 'workspace.updated' || ev.type === 'workspace.added' ||
        ev.type === 'workspace.removed') {
      this._reload();
      return;
    }

    // github.<EventName> → append to that workspace's recent-events list.
    if (ev.type.startsWith('github.')) {
      const ws = ev.data?.workspace || ev.payload?.workspace;
      if (!ws) return;
      const list = this.events.get(ws) || [];
      list.unshift({
        ts: ev.ts || Date.now() / 1000,
        type: ev.type,
        payload: ev.data || ev.payload || {},
      });
      this.events.set(ws, list.slice(0, MAX_EVENTS_PER_WS));
      this._render();
      // Also refetch cursor — it should have advanced.
      this._reload();
    }
  }
}

// ── small helpers ───────────────────────────────────────────────

function _summarize(when) {
  if (!when || typeof when !== 'object') return '∅';
  const parts = [];
  for (const [k, v] of Object.entries(when)) {
    if (Array.isArray(v)) parts.push(`${k}∈[${v.join(',')}]`);
    else parts.push(`${k}=${v}`);
  }
  return parts.join(' ');
}

function _fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

function _eventCaption(payload) {
  if (!payload || typeof payload !== 'object') return '';
  const p = payload.payload || payload;
  // Try common GitHub event shapes.
  if (p.action && p.issue) return `${p.action} issue #${p.issue.number}: ${p.issue.title || ''}`.slice(0, 120);
  if (p.action && p.pull_request) return `${p.action} PR #${p.pull_request.number}: ${p.pull_request.title || ''}`.slice(0, 120);
  if (p.actor) return `actor=${p.actor.login || p.actor}`;
  return JSON.stringify(p).slice(0, 120);
}

// ── Structured editor helpers ───────────────────────────────────

function _cloneParsed(p) {
  // Deep clone via JSON round-trip — the structures here are plain
  // data (strings/numbers/dicts/arrays), no Dates or functions.
  return JSON.parse(JSON.stringify(p));
}

function _cleanStructured(p) {
  // Strip empty/default values before sending so the server-side YAML
  // emission produces a minimal config that a human can still read.
  const out = {repo: p.repo || ''};
  if (p.poll_interval_s && p.poll_interval_s !== 30) {
    out.poll_interval_s = Number(p.poll_interval_s);
  }
  if (Array.isArray(p.rules) && p.rules.length) {
    out.rules = p.rules.map(r => {
      const rule = {name: r.name || 'unnamed'};
      // Strip empty `when` keys so the YAML stays clean.
      const when = {};
      for (const [k, v] of Object.entries(r.when || {})) {
        if (v === '' || v === null || v === undefined) continue;
        if (Array.isArray(v) && v.length === 0) continue;
        when[k] = v;
      }
      if (Object.keys(when).length) rule.when = when;
      const doList = (r.do || []).filter(a => a && typeof a === 'object');
      if (doList.length) rule.do = doList;
      return rule;
    });
  }
  return out;
}

function _splitArgs(s) {
  // Shell-like splitter: quoted segments stay together.
  // Good enough for the typical `gh pr comment 42 --body "ack thanks"`
  // case. We're not building a full shell parser.
  const out = [];
  let buf = '';
  let quote = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quote) {
      if (c === quote) { quote = null; continue; }
      buf += c;
    } else if (c === '"' || c === "'") {
      quote = c;
    } else if (/\s/.test(c)) {
      if (buf) { out.push(buf); buf = ''; }
    } else {
      buf += c;
    }
  }
  if (buf) out.push(buf);
  return out;
}

function _insertToken(current, token, start, end, opts = {}) {
  const s = String(current || '');
  let a = Number.isFinite(start) ? start : s.length;
  let b = Number.isFinite(end) ? end : a;
  a = Math.max(0, Math.min(a, s.length));
  b = Math.max(a, Math.min(b, s.length));
  let t = token;
  if (opts.spaceAtEnd && a === s.length && s && !/\s$/.test(s)) {
    t = ` ${token}`;
  }
  return s.slice(0, a) + t + s.slice(b);
}

function _templateDataKey(path) {
  const parts = String(path || '')
    .replace(/\[\d+\]/g, '')
    .split('.')
    .filter(Boolean);
  const raw = parts.slice(-2).join('_') || 'value';
  return raw.replace(/[^a-zA-Z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'value';
}

function _defaultParamsForAction(kind) {
  switch (kind) {
    case 'agent.message': return {to: '', body: ''};
    case 'model':         return {prompt: '', model: 'role:fast', max_tokens: 256, include_event: true};
    case 'script':        return {path: ''};
    case 'gh':            return {args: []};
    case 'bus.emit':      return {type: '', data: {}};
    case 'code':          return {lang: 'python', body: ''};
    default:              return {};
  }
}

function _installHint() {
  // Surface a sensible install command. The user-agent string is the
  // best heuristic we have client-side; default to apt since the
  // most likely server-side runs are Debian/Ubuntu.
  const ua = (navigator.userAgent || '').toLowerCase();
  if (ua.includes('mac')) return 'brew install gh';
  if (ua.includes('linux')) {
    if (ua.includes('arch') || ua.includes('cachyos')) return 'sudo pacman -S github-cli';
    return 'sudo apt install gh  # or your distro\'s equivalent';
  }
  return 'See https://cli.github.com/manual/installation';
}
