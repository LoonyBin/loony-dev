// Primitives (from ld-kit.jsx + loony-dev.html). Visual axis only; every factory
// takes id/class/attrs/style/onClick seams via splitSeams+seam.
import { el, mi, append, seam, splitSeams } from '../util/dom.js';

/** Icon — Material Symbols. Wraps mi() with the design's visual knobs. */
export function Icon(name, opts = {}) {
  const [{ size, fill, weight, color } = {}, seams] = splitSeams(opts);
  return seam(mi(name, { size, fill, weight, color }), seams);
}

export const BTN_VARIANTS = ['primary', 'soft', 'outline', 'ghost', 'danger'];
export const BTN_SIZES = ['sm', 'md'];

/**
 * Btn — .ld-btn. variant + size + optional leading/trailing icon. Given an
 * `href` it renders a link-button `<a class="ld-btn …">` (opening external
 * targets safely: target='_blank' gets rel='noopener noreferrer' by default),
 * otherwise a `<button>`.
 */
export function Btn(props = {}) {
  const [{ variant = 'outline', size = 'sm', icon, iconRight, label, href, target, rel }, seams] = splitSeams(props);
  const b = el(href != null ? 'a' : 'button', `ld-btn ${variant} ${size}`);
  if (href != null) {
    b.setAttribute('href', href);
    if (target) b.setAttribute('target', target);
    if (rel || target === '_blank') b.setAttribute('rel', rel || 'noopener noreferrer');
  } else {
    b.setAttribute('type', 'button');   // never accidentally submit a form
  }
  const isz = size === 'md' ? 18 : 16;
  if (icon) b.append(mi(icon, { size: isz }));
  if (label != null) b.append(document.createTextNode(label));
  if (iconRight) b.append(mi(iconRight, { size: isz }));
  return seam(b, seams);
}

export const TAG_TONES = ['neutral', 'ghost', 'blue', 'green', 'amber', 'red', 'purple'];

/** Tag — .tag + tone. Optional leading icon. */
export function Tag(props = {}) {
  const [{ tone = 'neutral', icon, label }, seams] = splitSeams(props);
  const t = el('span', `tag ${tone}`);
  if (icon) t.append(mi(icon, { size: 14 }));
  if (label != null) t.append(document.createTextNode(label));
  return seam(t, seams);
}

// VISUAL colour tones (domain-agnostic, composable). A semantic lifecycle state
// (merged/blocked) maps to one of these via dotTone() in the caller layer.
export const SDOT_TONES = ['green', 'accent', 'amber', 'red', 'hollow'];

/** Sdot — .sdot + visual colour tone. `size` rides --dot-sz (default 8px in
 * CSS); `pulse` to animate. */
export function Sdot(props = {}) {
  const [{ tone = 'hollow', size, pulse = false }, seams] = splitSeams(props);
  const d = el('span', `sdot ${tone}${pulse ? ' pulse' : ''}`);
  if (size) d.style.setProperty('--dot-sz', size + 'px');
  return seam(d, seams);
}

/** StatePill — .statepill wrapping an .sdot of the given visual tone + label. */
export function StatePill(props = {}) {
  const [{ tone = 'green', label }, seams] = splitSeams(props);
  const p = el('span', 'statepill');
  p.append(Sdot({ tone, size: 8 }));
  if (label != null) p.append(document.createTextNode(label));
  return seam(p, seams);
}

// AVATAR de-tangle: the design named these by ACTOR (.ava.trixy/.capo/.user),
// baking bot/operator NAMES (per-install config) into visual classes. The visual
// layer now carries domain-agnostic colour TONES; the actor→tone map (avatarTone)
// lives in the caller, exactly like dotTone for the status dots.
export const AVATAR_TONES = ['neutral', 'soft', 'accent', 'ink', 'green'];

/** Avatar — .ava + visual colour tone. `size` rides the --ava-sz custom property
 * (font scales off it in CSS); `glyph` initials are caller-supplied — never a name. */
export function Avatar(props = {}) {
  const [{ tone = 'neutral', glyph = '?', size }, seams] = splitSeams(props);
  const a = el('span', `ava ${tone}`);
  if (size) a.style.setProperty('--ava-sz', size + 'px');
  a.textContent = String(glyph).slice(0, 2).toUpperCase();
  return seam(a, seams);
}

/** Eyebrow — .eyebrow caps label. */
export function Eyebrow(props = {}) {
  const [{ label }, seams] = splitSeams(props);
  return seam(el('div', 'eyebrow', label), seams);
}

/** Hairline — hr.hairline divider. */
export function Hairline(props = {}) {
  const [, seams] = splitSeams(props);
  return seam(el('hr', 'hairline'), seams);
}

/**
 * Card — .card surface. variant subtle/flat; pad 'sm'|'md'|'lg' (.pad-sm/.pad/.pad-lg);
 * optional `head` (renders a .card-head row) and `children`. Composes with a
 * caller `class` (e.g. 'side', 'grow', or a semantic class) on the same node.
 */
export function Card(props = {}) {
  const [{ subtle = false, flat = false, pad, head, children }, seams] = splitSeams(props);
  let cls = 'card';
  if (subtle) cls += ' subtle';
  if (flat) cls += ' flat';
  if (pad === 'sm') cls += ' pad-sm';
  else if (pad === 'lg') cls += ' pad-lg';
  else if (pad) cls += ' pad';
  const c = el('div', cls);
  if (head != null) c.append(append(el('div', 'card-head'), head));
  append(c, children);
  return seam(c, seams);
}

/** Stat — .stat-n number over .stat-l label. */
export function Stat(props = {}) {
  const [{ n, label }, seams] = splitSeams(props);
  const w = el('div');
  w.append(el('div', 'stat-n', n));
  w.append(el('div', 'stat-l', label));
  return seam(w, seams);
}

/** ScreenHead — .screen-head: title + sub on the left, `right` slot on the right. */
export function ScreenHead(props = {}) {
  const [{ title, sub, right }, seams] = splitSeams(props);
  const h = el('div', 'screen-head');
  const left = el('div');
  left.append(el('h1', 'screen-title', title));
  if (sub != null) left.append(el('p', 'screen-sub', sub));
  h.append(left);
  if (right != null) h.append(append(el('div', 'screen-head-right'), right));
  return seam(h, seams);
}

/** Crumbs — .crumbs. items: [{label, icon?, onClick?}]; last (no onClick) = current. */
export function Crumbs(props = {}) {
  const [{ items = [] }, seams] = splitSeams(props);
  const c = el('div', 'crumbs');
  items.forEach((it, i) => {
    if (i > 0) c.append(append(el('span', 'crumb-sep'), mi('chevron_right', { size: 15 })));
    if (it.onClick) {
      const b = el('button', 'crumb');
      b.setAttribute('type', 'button');
      if (it.icon) b.append(mi(it.icon, { size: 15 }));
      b.append(document.createTextNode(it.label));
      b.addEventListener('click', it.onClick);
      c.append(b);
    } else {
      c.append(el('span', 'crumb-cur', it.label));
    }
  });
  return seam(c, seams);
}

/**
 * Segmented — the generic .seg pill toggle group (board ↔ kanban, skills ↔
 * commands, any single-select view/kind switch). options: [{ value, label,
 * icon? }] — an optional `icon` (Material Symbol name) renders before the label.
 * `value` = the active option; `onChange(value)` fires on click; `ariaLabel`
 * names the group. Each button reflects active via `.on` + aria-pressed.
 */
export function Segmented(props = {}) {
  const [{ options = [], value, onChange, ariaLabel }, seams] = splitSeams(props);
  const s = el('div', 'seg');
  s.setAttribute('role', 'group');
  if (ariaLabel) s.setAttribute('aria-label', ariaLabel);
  for (const o of options) {
    const active = o.value === value;
    const b = el('button', active ? 'on' : null);
    b.setAttribute('type', 'button');
    b.setAttribute('aria-pressed', String(active));
    if (o.icon) b.append(mi(o.icon, { size: 18 }));   // mi() marks it aria-hidden
    b.append(document.createTextNode(o.label));
    if (onChange) b.addEventListener('click', () => onChange(o.value));
    s.append(b);
  }
  return seam(s, seams);
}

/** Subtab — .subtab pill. `on` = active. */
export function Subtab(props = {}) {
  const [{ label, on = false }, seams] = splitSeams(props);
  const b = el('button', `subtab${on ? ' on' : ''}`, label);
  b.setAttribute('type', 'button');
  return seam(b, seams);
}
