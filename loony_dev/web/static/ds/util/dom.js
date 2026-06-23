// Shared internals for the visual-axis factories. Mirrors the design's own MI()
// helper and the app's dom.js idiom, so a factory node is indistinguishable from
// a hand-built one.
//
// AXIS RULE: a factory owns the VISUAL axis only — it never bakes in a domain
// class or an id. The caller injects the SEMANTIC class and the BEHAVIOURAL
// hooks (id, data-*, handlers) through the seams handled by `seam()` below.

/** Create an element with optional class + text. `text` of 0 renders "0". */
export function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = String(text);
  return n;
}

/**
 * Material Symbols icon, matching the design's <MI>. Supports the design's
 * visual knobs (size/fill/weight/color) via font-variation-settings.
 */
export function mi(name, { size = 20, fill = 0, weight = 400, color } = {}) {
  const s = el('span', 'material-symbols-outlined');
  s.setAttribute('aria-hidden', 'true');
  s.textContent = name;
  s.style.fontSize = size + 'px';
  s.style.fontVariationSettings = `'FILL' ${fill}, 'wght' ${weight}, 'GRAD' 0, 'opsz' ${size}`;
  if (color) s.style.color = color;
  return s;
}

/** Append children (nodes or strings; nullish skipped) to a node. Returns node. */
export function append(node, children) {
  if (children == null) return node;
  for (const ch of Array.isArray(children) ? children : [children]) {
    if (ch == null || ch === false) continue;
    node.append(ch instanceof Node ? ch : document.createTextNode(String(ch)));
  }
  return node;
}

/**
 * Land caller-injected seams onto a visual node:
 *   class — extra (semantic) classes appended after the visual classes
 *   id    — behavioural hook
 *   attrs — data-* and aria-* the app's JS reads/writes
 *   style — caller layout/position overrides (DAG coords, widths) — kept
 *           explicit because positioning is layout, not the visual recipe
 *   onClick / on — event wiring (`on` = { event: handler })
 */
export function seam(node, { id, class: extra, attrs, style, onClick, on } = {}) {
  // classList.add (not `className +=`): works on SVG nodes too — DagEdge returns
  // an SVG <path> whose `className` is a read-only SVGAnimatedString.
  if (extra) for (const cls of String(extra).split(/\s+/)) if (cls) node.classList.add(cls);
  if (id) node.id = id;
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (style) for (const [k, v] of Object.entries(style)) node.style[k] = typeof v === 'number' ? v + 'px' : v;
  if (onClick) node.addEventListener('click', onClick);
  if (on) for (const [ev, fn] of Object.entries(on)) node.addEventListener(ev, fn);
  return node;
}

/** Split factory props into visual props vs the caller seams. */
export function splitSeams({ id, class: cls, attrs, style, onClick, on, ...rest } = {}) {
  return [rest, { id, class: cls, attrs, style, onClick, on }];
}
