// Cockpit-screen visual factories (ld-cockpit catalog): the dependency DAG, the
// compact "dag field", the epic-zone card, the story list, and the live-activity
// row. Visual axis only — every node takes id/class/attrs/style/onClick seams via
// splitSeams+seam; the caller injects the semantic class/id. The visual RECIPE
// (sizes, colour, type) lives in css/cockpit.css as classes; the only inline
// `.style` here is DATA-DRIVEN LAYOUT (node positions, canvas/field size),
// carried as CSS custom properties (--x/--y/--w/--h) the CSS rules consume.
import { el, mi, append, seam, splitSeams } from '../util/dom.js';
import { Tag, Avatar, Sdot } from './primitives.js';
import { dotTone, nodeLook, avatarTone } from '../util/data.js';

// These are SEMANTIC composites: they take a domain lifecycle `state` and map it
// to the VISUAL tone (dotTone) / node look (nodeLook) when composing primitives.
const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * DagNode — .node + state, absolutely positioned at (left, top), fixed 132×52.
 * When `live`, a .wk worker badge floats at the corner. The .nrow carries the
 * Sdot (pulsing when live) + .nk key; the .nt title sits below. Position is
 * data-driven layout → carried on --x/--y custom properties; the fixed size and
 * everything visual live in the .node rule (css/cockpit.css + ld-system.css).
 */
export function DagNode(props = {}) {
  const [{ k, title, state = 'gated', worker, live = false, left, top }, seams] = splitSeams(props);
  const n = el('div', ('node ' + nodeLook(state)).trim());
  n.style.setProperty('--x', left + 'px');
  n.style.setProperty('--y', top + 'px');
  if (live) n.append(el('span', 'wk', worker));
  const row = el('div', 'nrow');
  row.append(Sdot({ tone: dotTone(state), size: 9, pulse: live }));
  row.append(el('span', 'nk', k));
  n.append(row);
  n.append(el('span', 'nt', title));
  return seam(n, seams);
}

/**
 * DagEdge — an SVG <path> cubic bezier from (x1,y1) to (x2,y2) with the control
 * points at the horizontal midpoint. `solid` = a merged edge (accent, no dash);
 * `compact` thins the stroke + dash for the dag-field scale.
 */
export function DagEdge(props = {}) {
  const [{ x1, y1, x2, y2, solid = false, compact = false }, seams] = splitSeams(props);
  const mx = (x1 + x2) / 2;
  const p = document.createElementNS(SVG_NS, 'path');
  p.setAttribute('d', `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
  p.setAttribute('fill', 'none');
  p.setAttribute('stroke', solid ? 'var(--st-merged)' : 'var(--border-strong)');
  p.setAttribute('stroke-width', compact ? '1.75' : '2');
  p.setAttribute('stroke-dasharray', solid ? '' : (compact ? '4 5' : '5 6'));
  p.setAttribute('stroke-linecap', 'round');
  return seam(p, seams);
}

/**
 * Dag — .dag canvas (width×height) holding one absolute <svg> of `edges`
 * (DagEdge paths) under the `nodes` (DagNode divs). Canvas size is data-driven
 * layout → carried on --w/--h; the <svg>'s inset/overflow/pointer-events are
 * covered by `.dag > svg` in ld-system.css.
 */
export function Dag(props = {}) {
  const [{ width, height, edges = [], nodes = [] }, seams] = splitSeams(props);
  const d = el('div', 'dag');
  d.style.setProperty('--w', width + 'px');
  d.style.setProperty('--h', height + 'px');
  const svg = document.createElementNS(SVG_NS, 'svg');
  for (const e of edges) svg.append(e);
  d.append(svg);
  for (const n of nodes) d.append(n);
  return seam(d, seams);
}

/**
 * DagFieldNode — .df-node, the compact dag-field dot: an oversized .df-dot Sdot
 * (with an optional .df-wk worker badge) over a faint .df-k key. Position is
 * data-driven layout → carried on --x/--y custom properties.
 */
export function DagFieldNode(props = {}) {
  const [{ k, state = 'gated', worker, live = false, left, top }, seams] = splitSeams(props);
  const n = el('div', 'df-node');
  n.style.setProperty('--x', left + 'px');
  n.style.setProperty('--y', top + 'px');
  const wrap = el('span', 'df-dot-wrap');
  wrap.append(Sdot({ tone: dotTone(state), size: 20, pulse: live, class: 'df-dot' }));
  if (live) wrap.append(el('span', 'df-wk', worker));
  n.append(wrap);
  n.append(el('span', 'faint tnum df-k', k));
  return seam(n, seams);
}

/** DagLegend — the state-tone key row (merged…gated) plus a pulse=live caption. */
export function DagLegend(props = {}) {
  const [, seams] = splitSeams(props);
  const legend = [
    ['merged', 'merged'],
    ['active', 'in progress'],
    ['review', 'in review'],
    ['blocked', 'blocked'],
    ['gated', 'gated'],
  ];
  const row = el('div', 'row g16 wrap ac');
  for (const [state, label] of legend) {
    const item = el('span', 'row ac g6 dag-legend-item');
    item.append(Sdot({ tone: dotTone(state), size: 11 }));
    item.append(document.createTextNode(' ' + label));
    row.append(item);
  }
  const live = el('span', 'row ac g6 dag-legend-item');
  live.append(Sdot({ tone: 'accent', size: 11, pulse: true }));
  live.append(document.createTextNode(' pulse = live worker'));
  row.append(live);
  return seam(row, seams);
}

/**
 * EpicZoneCard — .card.pad-lg.dotgrid zone: a repo header with live/stories
 * tags, the epic name, the `children` slot (typically the DagField), a hairline,
 * and a session/PR footer.
 */
export function EpicZoneCard(props = {}) {
  const [{ repoLabel, epicName, live = 0, stories = 0, prs = 0, open = 0, children }, seams] = splitSeams(props);
  const c = el('div', 'card pad-lg dotgrid zone-repo');

  const head = el('div', 'row ac jb');
  const left = el('div', 'row ac g8 clickrow');
  left.append(mi('folder', { size: 18 }));
  left.append(el('span', 'b zr-name', repoLabel));
  head.append(left);
  const tags = el('div', 'row ac g6');
  if (live > 0) tags.append(Tag({ tone: 'blue', label: `${live} live` }));
  tags.append(Tag({ tone: 'ghost', label: `${stories} stories` }));
  head.append(tags);
  c.append(head);

  c.append(el('div', 'faint zone-epic', epicName));

  append(c, children);

  c.append(el('hr', 'hairline zone-hr'));

  const foot = el('div', 'row ac jb zone-foot');
  const sess = el('span', 'crumb');
  sess.append(mi('sensors', { size: 15 }));
  sess.append(document.createTextNode(' session · main'));
  foot.append(sess);
  const expand = el('span', 'crumb');
  expand.append(document.createTextNode(`${prs} PRs · ${open} open · expand `));
  expand.append(mi('arrow_forward', { size: 15 }));
  foot.append(expand);
  c.append(foot);

  return seam(c, seams);
}

/**
 * StoryListItem — a .card story row: Sdot (pulsing when active) + key/title.
 * When `gated`, a muted "gated on {preds}" line; otherwise a chip row of Tags.
 * `gated` is a composable modifier class (.gated) the CSS dims. chips:
 * [{ label, tone, icon }].
 */
export function StoryListItem(props = {}) {
  const [{ k, title, state = 'active', chips = [], gated = false, preds }, seams] = splitSeams(props);
  const c = el('div', `card pad-sm story-item${gated ? ' gated' : ' clickrow'}`);

  const row = el('div', 'row ac g8');
  row.append(Sdot({ tone: dotTone(state), size: 11, pulse: state === 'active' }));
  const grow = el('div', 'grow');
  grow.append(el('span', 'faint tnum', k));
  grow.append(document.createTextNode(' ' + title));
  row.append(grow);
  c.append(row);

  if (gated) {
    const g = el('div', 'mut row ac g4 story-gated');
    g.append(mi('lock', { size: 13 }));
    g.append(document.createTextNode(' gated on ' + preds));
    c.append(g);
  } else {
    const cr = el('div', 'row g6 wrap story-chips');
    for (const ch of chips) cr.append(Tag({ tone: ch.tone, icon: ch.icon, label: ch.label }));
    c.append(cr);
  }

  return seam(c, seams);
}

/**
 * LiveActivityRow — a .clickrow feed entry: an Avatar over a who/what line with
 * a faint where·just-now caption. `tone` (blocked/review) emphasises `what` via
 * a composable .act-what modifier class (.t-blocked / .t-review).
 */
export function LiveActivityRow(props = {}) {
  const [{ glyph, kind = 'worker', who, what, where, tone }, seams] = splitSeams(props);
  const r = el('div', 'row g10 as clickrow activity-row');
  r.append(Avatar({ tone: avatarTone(kind), glyph, size: 24 }));

  const body = el('div');
  body.append(el('span', 'b act-who', who));
  body.append(document.createTextNode(' '));
  const whatCls = 'act-what' + (tone === 'blocked' ? ' t-blocked' : tone === 'review' ? ' t-review' : '');
  body.append(el('span', whatCls, what));
  body.append(el('div', 'faint act-meta', where + ' · just now'));
  r.append(body);

  return seam(r, seams);
}
