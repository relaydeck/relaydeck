// uikit/toggle.js — <rd-toggle>, the standardized on/off switch.
//
// Replaces the dozens of hand-rolled `<button class="set-toggle">` + manual
// classList.toggle() + addEventListener wirings across the dashboard and
// plugins. Renders the existing `.set-toggle` look (so it themes + matches the
// `.set-toggle[data-name=…]` e2e selector) and emits a `change` event.
//
//   <rd-toggle ?on=${enabled} name="messaging"
//     @change=${(e) => save(e.detail.on)}></rd-toggle>
//
// Controlled or uncontrolled: it flips its own `on` optimistically; the parent
// may overwrite `.on` after a server round-trip to reconcile.

import { RelayElement } from './base.js';
import { html, nothing } from 'lit';
import { classMap } from 'lit';
import { ifDefined } from 'lit';

export class RdToggle extends RelayElement {
  static properties = {
    on: { type: Boolean },
    name: { type: String },
    disabled: { type: Boolean },
  };

  constructor() {
    super();
    this.on = false;
    this.disabled = false;
  }

  _flip = (e) => {
    e.stopPropagation();
    if (this.disabled) return;
    this.on = !this.on;
    this.dispatchEvent(new CustomEvent('change', {
      detail: { on: this.on }, bubbles: true, composed: true,
    }));
  };

  render() {
    return html`<button
      class=${classMap({ 'set-toggle': true, on: this.on })}
      data-name=${ifDefined(this.name)}
      ?disabled=${this.disabled}
      role="switch"
      aria-checked=${this.on ? 'true' : 'false'}
      @click=${this._flip}
    >${nothing}</button>`;
  }
}

if (!customElements.get('rd-toggle')) customElements.define('rd-toggle', RdToggle);
