// Gallery chrome — the Storybook's own layout helpers (NOT design components).
// Kept visually neutral so specimens read clearly.
import { el } from '../ds/util/dom.js';

export function section(id, title, blurb) {
  const s = el('section', 'sb-section');
  s.id = 'sec-' + id;
  s.append(el('h2', 'sb-h2', title));
  if (blurb) s.append(el('p', 'sb-blurb', blurb));
  return s;
}

// The three orthogonal concerns. VISUAL is composable (stackable classes:
// .card.red, .stepper .here.green); SEMANTIC = what it is + domain lifecycle
// state (merged/blocked); BEHAVIOURAL = JS dynamics + hooks.
const AXIS_ORDER = [['visual', 'v'], ['semantic', 's'], ['behavioural', 'b']];

/**
 * A component block: name + the three-concern chips (render those present), then
 * specimens. axes = { visual, semantic, behavioural } (short strings).
 */
export function block(name, axes, specimens) {
  const b = el('div', 'sb-block');
  const head = el('div', 'sb-block-head');
  head.append(el('h3', 'sb-name', name));
  const ax = el('div', 'sb-axes');
  for (const [kind, cls] of AXIS_ORDER) if (axes[kind]) ax.append(axisChip(kind, axes[kind], cls));
  head.append(ax);
  b.append(head);
  const grid = el('div', 'sb-specimens');
  for (const sp of specimens) if (sp) grid.append(sp);
  b.append(grid);
  return b;
}

function axisChip(kind, text, cls) {
  const w = el('span', 'sb-axis');
  w.append(el('span', 'sb-axis-k ' + cls, kind));
  w.append(el('code', 'sb-axis-v', text));
  return w;
}

/** A single specimen: rendered node over a caption. `opts.dark` for dark stages. */
export function specimen(caption, node, opts = {}) {
  const w = el('div', 'sb-specimen');
  const stage = el('div', 'sb-stage' + (opts.dark ? ' dark' : '') + (opts.pad ? ' pad' : ''));
  if (opts.width) stage.style.width = opts.width;
  stage.append(node);
  w.append(stage);
  if (caption != null) w.append(el('div', 'sb-cap', caption));
  return w;
}

/** A horizontal cluster inside one specimen stage (e.g. all tones in a row). */
export function cluster(caption, nodes, opts = {}) {
  const wrap = el('div', 'sb-cluster');
  for (const n of nodes) if (n) wrap.append(n);
  return specimen(caption, wrap, opts);
}
