// data.js — daemon API client + SSE bus + auth bootstrap
//
// This module owns:
//   - auth token acquisition (localStorage → /api/auth/bootstrap → paste modal)
//   - patched window.fetch that attaches Bearer to /api/* calls
//   - tokenize() helper for SSE/WS URLs
//   - api.fetch() / api.events.subscribe(cb) — the surface modules use
//   - in-memory caches for agents, workspaces, presets, workers
//
// The auth flow matches the previous PR (feat/auth-paste-token); panels
// continue to use the patched window.fetch transparently.

import { esc } from './primitives.js';

const TOKEN_KEY = 'relaydeckToken';
window.__relaydeckToken = null;

// Build version for cache-busting DYNAMIC imports of plugin assets.
// Core modules are stamped `?v=<pid>` via the server-injected importmap,
// but plugin lens/tile/widget modules are imported by raw path at runtime
// (`import('/static/plugins/<x>/panel.js')`) and would otherwise serve
// stale from browser cache after a daemon restart. This module's own URL
// carries the stamp (it's in the importmap), so we lift it from there.
export const BUILD_V = (() => {
  try { return new URL(import.meta.url).searchParams.get('v') || ''; }
  catch (_) { return ''; }
})();

/** Append the build stamp to a same-origin static URL so a daemon restart
 *  busts the browser's module cache. No-op when the stamp is unknown. */
export function stamped(url) {
  if (!BUILD_V || typeof url !== 'string' || !url.startsWith('/')) return url;
  return url + (url.includes('?') ? '&' : '?') + 'v=' + BUILD_V;
}

// ── auth ──────────────────────────────────────────────────────────
export async function verifyToken(token) {
  try {
    const r = await fetch('/api/auth/verify', {
      headers: { 'Authorization': 'Bearer ' + token },
    });
    return r.ok;
  } catch (_) { return false; }
}

async function tryStoredToken() {
  let stored = null;
  try { stored = localStorage.getItem(TOKEN_KEY); } catch (_) {}
  if (!stored) return false;
  if (await verifyToken(stored)) { window.__relaydeckToken = stored; return true; }
  try { localStorage.removeItem(TOKEN_KEY); } catch (_) {}
  return false;
}

async function tryBootstrap() {
  try {
    const r = await fetch('/api/auth/bootstrap');
    if (!r.ok) return false;
    const j = await r.json();
    if (!j.token) return false;
    window.__relaydeckToken = j.token;
    return true;
  } catch (_) { return false; }
}

function renderPasteModal() {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'signin-overlay';
    overlay.innerHTML = `
      <div class="signin-card">
        <div class="ttl">Sign in to relaydeck</div>
        <div class="sub">
          This browser doesn't have a token for <code>${esc(location.host)}</code>.
          On the daemon host, run:
          <div class="cmd">relaydeck auth token</div>
        </div>
        <input id="__relaydeck-token-input" type="password" placeholder="paste token here"
          autocomplete="off" spellcheck="false">
        <div id="__relaydeck-token-err" class="err"></div>
        <div class="actions">
          <button class="btn primary" id="__relaydeck-token-submit">Sign in</button>
        </div>
        <div class="footer">
          Stored only in this browser's local storage for <code>${esc(location.host)}</code>.
          Use <code>relaydeck auth rotate</code> to invalidate.
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#__relaydeck-token-input');
    const errEl = overlay.querySelector('#__relaydeck-token-err');
    const submit = overlay.querySelector('#__relaydeck-token-submit');
    input.focus();
    const go = async () => {
      const tok = (input.value || '').trim();
      if (!tok) return;
      submit.disabled = true; submit.textContent = 'Verifying…';
      errEl.style.display = 'none';
      const ok = await verifyToken(tok);
      if (ok) {
        try { localStorage.setItem(TOKEN_KEY, tok); } catch (_) {}
        window.__relaydeckToken = tok;
        overlay.remove();
        resolve(true);
      } else {
        errEl.textContent = 'Token rejected. Check the value on the daemon host.';
        errEl.style.display = 'block';
        submit.disabled = false; submit.textContent = 'Sign in';
        input.select();
      }
    };
    submit.addEventListener('click', go);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  });
}

export async function acquireToken() {
  if (await tryStoredToken()) return;
  if (await tryBootstrap()) return;
  await renderPasteModal();
}

// Wrap fetch so every /api/* call gets the Bearer header.
(function patchFetch() {
  const orig = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const tok = window.__relaydeckToken;
    if (tok && (url.startsWith('/api/') || url.startsWith('/healthz'))) {
      const h = new Headers(init.headers || {});
      if (!h.has('Authorization')) h.set('Authorization', 'Bearer ' + tok);
      init.headers = h;
    }
    return orig(input, init);
  };
})();

export function tokenize(url) {
  const tok = window.__relaydeckToken;
  if (!tok) return url;
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + 'token=' + encodeURIComponent(tok);
}

// ── SSE event bus ─────────────────────────────────────────────────
const eventListeners = new Set();
let sse = null;
let sseStatus = 'connecting';
const sseStatusListeners = new Set();

function notifySseStatus(s) {
  if (sseStatus === s) return;
  sseStatus = s;
  for (const cb of sseStatusListeners) { try { cb(s); } catch (_) {} }
}

export function connectSSE() {
  if (sse) try { sse.close(); } catch (_) {}
  notifySseStatus('connecting');
  sse = new EventSource(tokenize('/api/events'));
  sse.addEventListener('open', () => notifySseStatus('connected'));
  sse.addEventListener('error', () => notifySseStatus('reconnecting'));
  sse.addEventListener('message', (e) => {
    let parsed = null;
    try { parsed = JSON.parse(e.data); } catch (_) { return; }
    for (const cb of eventListeners) { try { cb(parsed); } catch (_) {} }
  });
}

export const events = {
  subscribe(cb) {
    eventListeners.add(cb);
    return () => eventListeners.delete(cb);
  },
  onStatusChange(cb) {
    sseStatusListeners.add(cb);
    cb(sseStatus);
    return () => sseStatusListeners.delete(cb);
  },
  status: () => sseStatus,
};

// ── api surface ───────────────────────────────────────────────────
async function _apiError(path, r) {
  let detail = `${path} → ${r.status}`;
  try {
    const j = await r.json();
    if (j && j.detail != null) {
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    }
  } catch (_) {}
  return new Error(detail);
}

export const api = {
  fetch: (path, init) => fetch(path, init),
  async getJSON(path) {
    const r = await fetch(path);
    if (!r.ok) throw await _apiError(path, r);
    return await r.json();
  },
  async postJSON(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw await _apiError(path, r);
    if (r.status === 204) return null;
    return await r.json();
  },
  async putJSON(path, body) {
    const r = await fetch(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw await _apiError(path, r);
    if (r.status === 204) return null;
    return await r.json();
  },
  async deleteJSON(path) {
    const r = await fetch(path, { method: 'DELETE' });
    if (!r.ok) throw await _apiError(path, r);
    if (r.status === 204) return null;
    return await r.json();
  },
  events,
  tokenize,
};

// ── Live data store (real-time foundation) ────────────────────────────
// The single reactive layer for the dashboard. A view subscribes to a
// resource (its API path); the store fetches once, caches, and pushes
// fresh data to subscribers whenever:
//   • an SSE event invalidates the resource (instant, coalesced ~200ms),
//   • a slow heartbeat fires (safety net for anything without an event),
//   • the SSE stream reconnects (revalidate everything).
// Views never poll on their own and never re-fetch on a timer — they
// `live.subscribe(path, cb)` and react. Mutations call `live.invalidate`.
const _liveSubs = new Map();      // key -> Set<cb>
const _liveCache = new Map();     // key -> last data
const _livePending = new Map();   // key -> coalesce timer
let _heartbeatTimer = null;
const HEARTBEAT_MS = 12000;

function _liveFetch(key) {
  return api.getJSON(key).then((data) => {
    _liveCache.set(key, data);
    for (const cb of (_liveSubs.get(key) || [])) { try { cb(data); } catch (_) {} }
    return data;
  }).catch(() => {});
}

function _liveInvalidate(key, immediate) {
  if (!_liveSubs.has(key)) return;            // nobody listening — skip
  if (immediate) { _liveFetch(key); return; }
  if (_livePending.has(key)) return;          // already queued
  _livePending.set(key, setTimeout(() => {
    _livePending.delete(key);
    _liveFetch(key);
  }, 200));
}

function _ensureHeartbeat() {
  if (_heartbeatTimer) return;
  _heartbeatTimer = setInterval(() => {
    if (_liveSubs.size) for (const key of _liveSubs.keys()) _liveFetch(key);
  }, HEARTBEAT_MS);
}

export const live = {
  // Subscribe to a resource path. Calls cb with cached data immediately
  // (if any), then on every refresh. Returns an unsubscribe fn.
  subscribe(key, cb) {
    let set = _liveSubs.get(key);
    if (!set) _liveSubs.set(key, set = new Set());
    set.add(cb);
    if (_liveCache.has(key)) { try { cb(_liveCache.get(key)); } catch (_) {} }
    _liveFetch(key);            // always revalidate on (re)subscribe
    _ensureHeartbeat();
    return () => {
      set.delete(cb);
      if (!set.size) { _liveSubs.delete(key); _liveCache.delete(key); }
    };
  },
  // Force-refresh every active key whose path starts with `prefix`.
  // Mutations (POST/PATCH/DELETE) call this so the UI reflects the write
  // before any event round-trips.
  invalidate(prefix) {
    for (const key of _liveSubs.keys()) if (key.startsWith(prefix)) _liveInvalidate(key, true);
  },
  // Revalidate everything currently subscribed (SSE reconnect).
  refreshAll() { for (const key of _liveSubs.keys()) _liveFetch(key); },
  peek(key) { return _liveCache.get(key); },
};

// Map an SSE event to the resources it invalidates. Called for every
// event from the bus (see app.js). Coalesced so a burst of usage records
// triggers at most one refetch per resource per ~200ms.
export function liveOnEvent(event) {
  const t = (event && event.type) || '';
  const aid = event.agent_id || (event.data && event.data.agent_id) || '';
  const agentStats = aid ? `/api/agents/${encodeURIComponent(aid)}/stats` : null;
  // Usage / cost / tokens / model calls — the high-churn signals.
  if (t === 'usage.record' || t === 'emote.update' || t.startsWith('harness.') || t.startsWith('loop.')) {
    if (agentStats) _liveInvalidate(agentStats);
    _liveInvalidate('/api/agents/usage-rollup');
    live.invalidate('/api/presets');       // preset usage + per-preset stats
    live.invalidate('/api/providers');     // provider 24h tokens/cost
    live.invalidate('/api/automations');   // worker runs/invocations
    // Context tab (per-thread fill) + usage heatmaps are usage-derived too —
    // keep them event-live, not just heartbeat-fresh.
    if (aid) {
      _liveInvalidate(`/api/agents/${encodeURIComponent(aid)}/sessions`);
      _liveInvalidate(`/api/agents/${encodeURIComponent(aid)}/usage-heatmap`);
    }
    live.invalidate('/api/usage-heatmap');  // fleet heatmap widget
  }
  if (t.startsWith('agent.')) {
    live.invalidate('/api/agents');
    if (agentStats) _liveInvalidate(agentStats);
  }
  // New peer message: refresh BOTH endpoints of the thread so the
  // Inbox tile + Messages lens light up live without polling. The
  // payload carries `from`/`to`; both rooms own a copy of the row.
  if (t === 'agent.message') {
    const d = event.data || {};
    if (d.from) live.invalidate(`/api/agents/${encodeURIComponent(d.from)}/inbox`);
    if (d.to)   live.invalidate(`/api/agents/${encodeURIComponent(d.to)}/inbox`);
    // Messages lens aggregates by fanning across agent inboxes — if
    // it's mounted it'll have its own per-agent subs that just fired.
  }
  if (t.startsWith('workspace.')) { live.invalidate('/api/workspaces'); live.invalidate('/api/worktrees'); }
  if (t.startsWith('worktree.')) { live.invalidate('/api/worktrees'); live.invalidate('/api/workspaces'); }
  if (t === 'skills.changed') live.invalidate('/api/plugins/skills');
  if (t === 'themes.changed') live.invalidate('/api/themes');
  if (t === 'system.plugin.unloaded' || t === 'system.plugin.loaded') {
    live.invalidate('/api/plugins');
    live.invalidate('/api/plugins/ui');
  }
  if (t.startsWith('external_agent.')) live.invalidate('/api/plugins/external/agents');
  if (t.startsWith('prompt.')) live.invalidate('/api/plugins/prompts');
}

// ── preferences (dashboard UI state persisted server-side) ────────
// /api/preferences GET/PUT — used for tile-state assignments, density,
// accent, last lens, last workspace. Server backs to
// ~/.relaydeck/preferences.yaml so prefs survive cache wipes.
let prefsCache = null;
let prefsPending = null;
export async function loadPreferences() {
  if (prefsCache) return prefsCache;
  if (prefsPending) return prefsPending;
  prefsPending = (async () => {
    try {
      prefsCache = await api.getJSON('/api/preferences');
    } catch (_) {
      prefsCache = {};
    }
    return prefsCache;
  })();
  return prefsPending;
}
export async function savePreferences(patch) {
  prefsCache = { ...(prefsCache || {}), ...patch };
  try {
    await api.putJSON('/api/preferences', prefsCache);
  } catch (e) {
    console.warn('save preferences failed', e);
  }
}
