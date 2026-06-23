// Mobile composites (ld-mobile phone-frame chrome). Visual axis only; every
// factory takes id/class/attrs/style/onClick seams via splitSeams+seam.
import { el, mi, append, seam, splitSeams } from '../util/dom.js';

/** PhoneFrame({ children }) — the .phone bezel wrapping a .phone-screen. */
export function PhoneFrame(props = {}) {
  const [{ children }, seams] = splitSeams(props);
  const phone = el('div', 'phone');
  const screen = el('div', 'phone-screen');
  append(screen, children);
  phone.append(screen);
  return seam(phone, seams);
}

/** PhoneStatus({ dark }) — the status bar: clock + signal/wifi/battery glyphs.
 * `dark` is a composable .dark modifier; CSS recolours the bar and the glyphs
 * inherit it (no inline colour). */
export function PhoneStatus(props = {}) {
  const [{ dark = false }, seams] = splitSeams(props);
  const bar = el('div', `phone-status${dark ? ' dark' : ''}`);
  bar.append(el('span', 'tnum', '9:41'));
  const icons = el('div', 'row ac g5');
  icons.append(mi('signal_cellular_alt', { size: 15 }));
  icons.append(mi('wifi', { size: 15 }));
  icons.append(mi('battery_full', { size: 15 }));
  bar.append(icons);
  return seam(bar, seams);
}

/**
 * PhoneNav({ active, items }) — the bottom tab bar. items = [[label, icon], …];
 * the tab whose label === active gets .on and a filled glyph.
 */
export function PhoneNav(props = {}) {
  const [{ active = 'Fleet', items = [['Fleet', 'dashboard'], ['Queue', 'list_alt'], ['Cockpit', 'hub'], ['Chat', 'forum']] }, seams] = splitSeams(props);
  const nav = el('div', 'phone-nav');
  for (const [label, icon] of items) {
    const on = label === active;
    const tab = el('div', `nav-tab${on ? ' on' : ''}`);
    tab.append(mi(icon, { size: 22, fill: on ? 1 : 0 }));
    tab.append(el('span', 'nt-label', label));
    nav.append(tab);
  }
  return seam(nav, seams);
}
