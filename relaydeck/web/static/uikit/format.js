// uikit/format.js — escaping, icons, brand marks, and formatters, re-exported
// so dashboard components AND third-party plugin authors reach them from one
// import (`@relaydeck/ui`). Every plugin used to hand-roll its own esc() and
// formatters; now there is one source of truth.

import { html } from 'lit';
import { unsafeHTML } from 'lit';
import {
  esc, iconSVG, brandSVG, providerMark, brandHex, hasBrand, providerSlug,
  knownIconNames, sparklineSVG, areaChartSVG, fmtNum, fmtCost, relTime,
  formatTs, uptimeStr, toMs, visualStatus, BRAND_SVG,
} from '../primitives.js';

// Pure (string/value) helpers — pass through unchanged.
export {
  esc, iconSVG, brandSVG, providerMark, brandHex, hasBrand, providerSlug,
  knownIconNames, sparklineSVG, areaChartSVG, fmtNum, fmtCost, relTime,
  formatTs, uptimeStr, toMs, visualStatus, BRAND_SVG,
};

// Template helpers — inline the vendored SVGs into Lit templates safely.
// (iconSVG/brandSVG return trusted, self-authored SVG strings.)
export const icon = (name, size = 14) => html`${unsafeHTML(iconSVG(name, size))}`;
export const brand = (slug, size = 16, color = 'currentColor') => html`${unsafeHTML(brandSVG(slug, size, color))}`;
export const providerIcon = (name, size = 14) => html`${unsafeHTML(providerMark(name, size))}`;
export const spark = (data, opts) => html`${unsafeHTML(sparklineSVG(data, opts))}`;
export const areaChart = (data, opts) => html`${unsafeHTML(areaChartSVG(data, opts))}`;
