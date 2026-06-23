// Navigation shell (from ld-system.css §3 "navigation shell"). Visual axis only;
// every factory takes id/class/attrs/style/onClick seams via splitSeams+seam.
import { el, mi, append, seam, splitSeams } from '../util/dom.js';
import { Avatar } from './primitives.js';
import { avatarTone } from '../util/data.js';   // UserCard maps its actor role → avatar colour

/**
 * BrandMark — .brand-mark grid of 9 cells. on/accent pattern:
 *   [0,4,6,8] = .on, [1] = .ac, the rest plain. aria-hidden container.
 */
export function BrandMark(props = {}) {
  const [, seams] = splitSeams(props);
  const m = el('div', 'brand-mark');
  m.setAttribute('aria-hidden', 'true');
  const on = new Set([0, 4, 6, 8]);
  for (let i = 0; i < 9; i++) {
    m.append(el('i', on.has(i) ? 'on' : i === 1 ? 'ac' : null));
  }
  return seam(m, seams);
}

/** BrandWord — .brand-word "loony·dev" with an accent .dot middot. */
export function BrandWord(props = {}) {
  const [, seams] = splitSeams(props);
  const w = el('div', 'brand-word');
  w.append(document.createTextNode('loony'));
  w.append(el('span', 'dot', '·'));
  w.append(document.createTextNode('dev'));
  return seam(w, seams);
}

/**
 * NavItem — .navitem rail button. icon glyph + .nav-label. `on` = active.
 * `count` (!=null) renders a trailing .count, else `needs` renders a .needsdot.
 * `live` adds the .live-i blip to the icon span.
 */
export function NavItem(props = {}) {
  const [{ icon, label, on = false, count, needs = false, live = false }, seams] = splitSeams(props);
  const b = el('button', `navitem${on ? ' on' : ''}`);
  b.setAttribute('type', 'button');
  const ico = el('span', 'material-symbols-outlined' + (live ? ' live-i' : ''), icon);
  ico.setAttribute('aria-hidden', 'true');   // decorative glyph; .nav-label carries the name
  b.append(ico);
  b.append(el('span', 'nav-label', label));
  if (count != null) b.append(el('span', 'count tnum', count));
  else if (needs) b.append(el('span', 'needsdot'));
  return seam(b, seams);
}

/** NavEyebrow — .nav-eyebrow caps section label. */
export function NavEyebrow(props = {}) {
  const [{ label }, seams] = splitSeams(props);
  return seam(el('div', 'nav-eyebrow', label), seams);
}

/**
 * UserCard — .user-card: an Avatar + a .grow column of .nm name over .st status
 * (status prefixed by the .d dot). `kind`/`glyph` flow through to Avatar.
 */
export function UserCard(props = {}) {
  const [{ name, status, kind = 'operator', glyph }, seams] = splitSeams(props);
  const c = el('div', 'user-card');
  c.append(Avatar({ tone: avatarTone(kind), glyph, size: 30 }));
  const grow = el('div', 'grow');
  grow.append(el('div', 'nm', name));
  const st = el('div', 'st');
  st.append(el('span', 'd'));
  st.append(document.createTextNode(' ' + (status ?? '')));
  grow.append(st);
  c.append(grow);
  return seam(c, seams);
}

/**
 * SettingsPopover — .settings-pop: a .pop-cap then a .pop-item per item.
 * item = {icon, label, count, on}; `count` (!=null) renders a trailing .count.
 */
export function SettingsPopover(props = {}) {
  const [{ cap = 'Settings', items = [] }, seams] = splitSeams(props);
  const p = el('div', 'settings-pop');
  p.append(el('div', 'pop-cap', cap));
  for (const it of items) {
    const b = el('button', `pop-item${it.on ? ' on' : ''}`);
    b.setAttribute('type', 'button');
    const ico = el('span', 'material-symbols-outlined', it.icon);
    ico.setAttribute('aria-hidden', 'true');   // decorative glyph; the label text carries the name
    b.append(ico);
    b.append(document.createTextNode(it.label));
    if (it.count != null) b.append(el('span', 'count tnum', it.count));
    p.append(b);
  }
  return seam(p, seams);
}

/** Sample nav items for the gallery. */
export const NAV_DEMO = [
  { icon: 'hub', label: 'Cockpit' },
  { icon: 'dashboard', label: 'Fleet', count: 12 },
  { icon: 'sensors', label: 'Live', live: true },
];
