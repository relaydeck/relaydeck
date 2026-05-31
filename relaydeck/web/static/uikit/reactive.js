// uikit/reactive.js — Lit reactive controllers that bridge relaydeck's data
// layer (data.js `live` store + SSE bus) to Lit's reactive lifecycle.
//
// The dashboard already has a reactive data layer: the `live` store fetches a
// resource, caches it, and pushes fresh data to subscribers on SSE events /
// heartbeat / reconnect (see data.js). Lit does NOT replace that — these
// controllers plug the store INTO Lit components so a component re-renders when
// its data changes and unsubscribes itself when removed. No manual teardown.

import { live, events } from '../data.js';

// LiveController — subscribe a RelayElement to one live-store resource path.
//
//   class MyTile extends RelayElement {
//     #agents = new LiveController(this, '/api/agents');
//     render() { return html`${(this.#agents.value || []).map(a => ...)}`; }
//   }
//
// Subscribes on connect, unsubscribes on disconnect, requests a re-render on
// every push. `.value` is the latest data (seeded from the store cache so the
// first paint isn't empty when another view already warmed the key).
export class LiveController {
  constructor(host, key, { onUpdate } = {}) {
    this.host = host;
    this.key = key;
    this.value = key ? live.peek(key) : undefined;
    this._onUpdate = onUpdate;
    this._unsub = null;
    this._connected = false;
    host.addController(this);
  }

  hostConnected() { this._connected = true; this._subscribe(); }
  hostDisconnected() { this._connected = false; this._unsub?.(); this._unsub = null; }

  /** Point the controller at a different resource (e.g. when a prop selecting
   *  the agent arrives or changes). (Re)subscribes if currently connected —
   *  works whether the key is set before or after the element connects. */
  setKey(key) {
    if (key === this.key) return;
    this.key = key;
    this.value = key ? live.peek(key) : undefined;
    if (this._connected) { this._unsub?.(); this._unsub = null; this._subscribe(); }
    this.host.requestUpdate();
  }

  _subscribe() {
    if (!this.key) return;
    this._unsub = live.subscribe(this.key, (data) => {
      this.value = data;
      try { this._onUpdate?.(data); } catch (_) {}
      this.host.requestUpdate();
    });
  }
}

// EventsController — subscribe a RelayElement to the raw SSE event bus. Use
// when a component reacts to events that aren't a single live-store resource
// (e.g. a toast on `agent.error`, a flash on `usage.record`). Pass `types` to
// filter by event-type prefix; omit to receive everything.
//
//   #ev = new EventsController(this, { types: ['agent.', 'loop.'],
//                                      onEvent: (e) => this._note(e) });
export class EventsController {
  constructor(host, { types, onEvent, rerender = true } = {}) {
    this.host = host;
    this.types = types || null;
    this._onEvent = onEvent;
    this._rerender = rerender;
    this._unsub = null;
    host.addController(this);
  }

  hostConnected() {
    this._unsub = events.subscribe((e) => {
      const t = (e && e.type) || '';
      if (this.types && !this.types.some((p) => t.startsWith(p))) return;
      try { this._onEvent?.(e); } catch (_) {}
      if (this._rerender) this.host.requestUpdate();
    });
  }

  hostDisconnected() { this._unsub?.(); this._unsub = null; }
}
