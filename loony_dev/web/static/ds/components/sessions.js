// Sessions composites (ld-sessions: Live / IssueDetail / Skills screens). Visual
// axis only; every factory takes id/class/attrs/style/onClick seams via
// splitSeams+seam. Bot identity (glyph/speaker) is caller-supplied — never baked.
import { el, mi, append, seam, splitSeams } from '../util/dom.js';
import { Avatar, Btn, Eyebrow, Hairline, Tag } from './primitives.js';
import { dotTone } from '../util/data.js';

/**
 * ChatBubble({ side, glyph, speaker, children }) — one chat message. Both
 * directions are STRICTLY PARALLEL: a `.msg.{side}` row holding an optional
 * Avatar (when `glyph` is given) + a `.bubble.{side}` box. `sent` right-aligns
 * with no avatar; `received` puts the avatar on the left. The only difference is
 * the composable side class — no divergent markup, and `.bubble` is reusable on
 * its own. `glyph`/`speaker` are caller-supplied — never a baked name.
 */
export function ChatBubble(props = {}) {
  const [{ side = 'received', glyph, speaker, children }, seams] = splitSeams(props);
  const bubble = el('div', `bubble ${side}`);
  if (speaker) {
    bubble.append(el('span', 'b bubble-speaker', speaker + ':'));
    bubble.append(document.createTextNode(' '));
  }
  append(bubble, children);

  const msg = el('div', `msg ${side}`);
  // Per the contract a `sent` bubble right-aligns with NO avatar; only the
  // `received` side carries one (when a glyph is supplied).
  if (side === 'received' && glyph != null) msg.append(Avatar({ tone: 'neutral', glyph, size: 26 }));
  msg.append(bubble);
  return seam(msg, seams);
}

/** CodeChip({ text }) — inline .code-chip used inside bubbles. */
export function CodeChip(props = {}) {
  const [{ text }, seams] = splitSeams(props);
  return seam(el('span', 'code-chip', text), seams);
}

/** RefChip({ text }) — inline .ref-chip used inside bubbles. */
export function RefChip(props = {}) {
  const [{ text }, seams] = splitSeams(props);
  return seam(el('span', 'ref-chip', text), seams);
}

/**
 * ChatComposer({ placeholder, sendIcon, disabled }) — a steer input + a Send
 * button. `disabled` greys both the input and the Send button (e.g. a steer bar
 * shown before its drive bridge is wired); the caller wires submit via seams.
 */
export function ChatComposer(props = {}) {
  const [{ placeholder = 'Steer…', sendIcon = 'send', disabled = false }, seams] = splitSeams(props);
  const row = el('div', 'row ac g10 chat-composer');

  const input = el('input', 'grow chat-input');
  input.setAttribute('placeholder', placeholder);
  if (disabled) input.setAttribute('disabled', '');
  row.append(input);
  row.append(Btn({ variant: 'primary', size: 'md', icon: sendIcon, label: 'Send', attrs: disabled ? { disabled: '' } : undefined }));
  return seam(row, seams);
}

/**
 * TimelineRow({ title, who, when, state, live, avatar, chip, rail, whenAlign }) —
 * one event on the activity timeline. Two compositions share one factory:
 *
 *  • RAIL (default): a marker column (.tl-dot + .tl-line connector) over a text
 *    block (.tl-title + "who · when" .tl-meta). "Last-ness" is STRUCTURAL — CSS
 *    (`.tl-row:last-child`) drops the bottom gap and hides the trailing
 *    connector, so the factory needs no `last` flag.
 *
 *  • FLAT (the app's richer activity row): pass `rail: false` to drop the
 *    connector, `avatar: {tone,glyph}` for an actor avatar after the dot, `chip`
 *    (a string or {label}) for a small skill chip after the title, and
 *    `whenAlign: 'right'` to push `when` to a trailing column (so the meta line
 *    carries only `who`). No information is lost relative to the legacy markup.
 *
 * `state` is a domain lifecycle value mapped to the visual tl-dot colour via
 * dotTone(); `live` pulses the current event.
 */
export function TimelineRow(props = {}) {
  const [{ title, who, when, state, live = false, avatar, chip, rail = true, whenAlign }, seams] = splitSeams(props);
  // The rail shape top-aligns the marker to the title; the flat shape centres the
  // row (dot · avatar · label · time) and carries the .tl-flat modifier.
  const row = el('div', `row g10 tl-row${rail ? '' : ' ac tl-flat'}`);

  const marker = el('div', 'col ac tl-marker');
  marker.append(el('span', `tl-dot ${state ? dotTone(state) : ''}${live ? ' pulse' : ''}`.trim()));
  if (rail) marker.append(el('span', 'tl-line'));   // connector; CSS hides it on the last row
  row.append(marker);

  if (avatar) row.append(Avatar({ ...avatar, size: 24 }));   // optional actor avatar (flat shape)

  const text = el('div', 'grow tl-text');
  const titleEl = el('div', `sb tl-title${state === 'blocked' ? ' t-blocked' : ''}`, title);
  if (chip) {
    const c = typeof chip === 'string' ? { label: chip } : chip;
    titleEl.append(document.createTextNode(' '));
    titleEl.append(el('span', 'tl-chip', c.label));
  }
  text.append(titleEl);
  // who · when meta line (rail shape). When whenAlign==='right', `when` moves to
  // the trailing column instead, so the meta carries only `who`.
  const meta = [who, whenAlign === 'right' ? null : when].filter((x) => x != null && x !== '').join(' · ');
  if (meta) text.append(el('div', 'faint tl-meta', meta));
  row.append(text);

  if (whenAlign === 'right' && when != null && when !== '') {
    row.append(el('span', 'faint tnum tl-when', when));
  }
  return seam(row, seams);
}

/**
 * SkillCard({ skill }) — a skill catalog card. skill = {id,icon,who,desc,trig,
 * phase,runs}. The owner label/avatar derive from skill.who (data, not a bake):
 * a 'capo' owner renders a hub Tag, otherwise an Avatar (first 2 letters of who)
 * beside the who label.
 */
/**
 * SkillCard({ icon, name, owner, desc, trigger, phase, runs, actions }) — a
 * library-entry card (skill OR command). `name` is the mono identifier;
 * `owner` = { managed, label } (managed → soft avatar + login; else a
 * hand-authored ghost tag). `desc`, `trigger`, `phase` ({label, tone}) are
 * OPTIONAL and omitted when absent. `runs` rides a present-but-hidden
 * "used N× today" line (a future feature). `actions` is a row of caller-supplied
 * buttons (Edit / Delete / …) — composable; order/variant are the caller's.
 */
export function SkillCard(props = {}) {
  const [{ icon, name, owner, desc, trigger, phase, runs, actions }, seams] = splitSeams(props);
  const card = el('div', 'card pad-lg skill-card');

  const head = el('div', 'row ac jb');
  const headL = el('div', 'row ac g10');
  const tile = el('span', 'skill-icon');
  if (icon) tile.append(mi(icon, { size: 20 }));
  headL.append(tile);
  const nameEl = el('span', 'b skill-id', name);
  if (name != null) nameEl.title = String(name);
  headL.append(nameEl);
  head.append(headL);
  const ob = ownerBadge(owner);
  if (ob) head.append(ob);
  card.append(head);

  if (desc != null && desc !== '') {
    const d = el('div', 'mut skill-desc', desc);
    d.title = String(desc);
    card.append(d);
  }

  if (trigger || phase) {
    card.append(Hairline());
    const row = el('div', 'row ac jb');
    const left = el('div');
    left.append(Eyebrow({ label: 'Triggers on' }));
    if (trigger) {
      const tv = el('div', 'sb skill-trig', trigger);
      tv.title = String(trigger);
      left.append(tv);
    }
    row.append(left);
    if (phase) row.append(Tag({ tone: phase.tone || 'ghost', label: phase.label }));
    card.append(row);
  }

  const footer = el('div', 'row ac jb skill-foot');
  const used = el('span', 'faint skill-used');
  used.hidden = true;   // future "used N× today" — present in markup, hidden for now
  used.append('used ');
  used.append(el('span', 'tnum b skill-runs', `${runs != null ? runs : 0}×`));
  used.append(' today');
  footer.append(used);
  // Normalize to an array so a single action node isn't silently dropped
  // (a lone DOM node has no `.length`) — consistent with FleetRow's actions.
  const actionList = Array.isArray(actions) ? actions : (actions ? [actions] : []);
  if (actionList.length) {
    const acts = el('div', 'row g8 skill-actions');
    for (const a of actionList) if (a) acts.append(a);
    footer.append(acts);
  }
  card.append(footer);

  return seam(card, seams);
}

// Owner badge: managed/downloaded → a soft avatar + login; hand-authored → a
// ghost tag. (Not "worker vs manager" — the real app distinction is managed.)
function ownerBadge(owner) {
  if (!owner) return null;
  if (owner.managed) {
    const w = el('span', 'row ac g6');
    w.append(Avatar({ tone: 'soft', glyph: String(owner.label || '?').slice(0, 2), size: 20 }));
    w.append(el('span', 'sb skill-owner', owner.label || 'managed'));
    return w;
  }
  return Tag({ tone: 'ghost', label: owner.label || 'hand-authored' });
}

/**
 * LinkedMini({ title, tone, label, diff }) — a compact linked-PR card. `diff` =
 * {add,del,files,reviews} renders a .diffstat row; omit it for a bare card.
 */
export function LinkedMini(props = {}) {
  const [{ title, tone, label, diff }, seams] = splitSeams(props);
  const card = el('div', 'card pad-sm');
  const head = el('div', 'row ac jb');
  const t = el('span', 'b linked-title', title);
  head.append(t);
  head.append(Tag({ tone, label }));
  card.append(head);
  if (diff) {
    const ds = el('div', 'diffstat tnum');
    ds.append(el('span', 'add', `+${diff.add}`));
    ds.append(el('span', 'del', `−${diff.del}`));
    ds.append(el('span', null, `${diff.files} files`));
    ds.append(el('span', null, `${diff.reviews} reviews`));
    card.append(ds);
  }
  return seam(card, seams);
}

/** KVRow({ k, v }) — a key/value line: muted key on the left, bold tnum value. */
export function KVRow(props = {}) {
  const [{ k, v }, seams] = splitSeams(props);
  const row = el('div', 'row ac jb g8 kv-row');
  row.append(el('span', 'mut', k));
  const val = el('span', 'b tnum kv-val', v);
  row.append(val);
  return seam(row, seams);
}
