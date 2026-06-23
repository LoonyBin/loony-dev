// Fleet-screen visual components (from ld-kit.jsx + loony-dev.html). Visual axis
// only; every factory takes id/class/attrs/style/onClick seams via splitSeams+
// seam. Domain classes/ids are the caller's job — injected through the seams.
import { el, mi, append, seam, splitSeams } from '../util/dom.js';
import { Btn, Tag, Avatar, Icon } from './primitives.js';
import { WORKERS, stageTone, wkNum, repoShort } from '../util/data.js';

/**
 * WorkerPoolMatrix — a card with a pool-grid of dots (busy/free) over a
 * big busy/total gauge. `total`/`busy` drive the dot count + the footer figure.
 */
export function WorkerPoolMatrix(props = {}) {
  const [{ total = 16, busy = 12 }, seams] = splitSeams(props);
  const c = el('div', 'card pad pool-card');

  const grid = el('div', 'pool-grid');
  for (let i = 0; i < total; i++) {
    grid.append(el('span', `sdot pool-dot ${i < busy ? 'busy' : 'free'}`));
  }
  c.append(grid);

  const foot = el('div', 'pool-foot');
  const n = el('div', 'stat-n ink pool-count');
  n.append(document.createTextNode(String(busy)));
  const frac = el('span', 'faint pool-frac');
  frac.textContent = `/${total}`;
  n.append(frac);
  foot.append(n);
  foot.append(el('div', 'stat-l', 'workers active'));
  c.append(foot);

  return seam(c, seams);
}

/**
 * Metric — a small KPI card: a big number + tone-coloured icon over a label.
 * tone ∈ {red, amber, blue, undefined} → composable .t-<tone> modifier class
 * (CSS colours the number + icon). `filterable` adds the .clickrow class;
 * `on` adds the active .metric.on state.
 */
export function Metric(props = {}) {
  const [{ n, label, tone, icon, on = false, filterable = false }, seams] = splitSeams(props);
  let cls = 'card pad-sm metric';
  if (filterable) cls += ' clickrow';
  if (on) cls += ' on';
  if (tone) cls += ` t-${tone}`;
  const c = el('div', cls);

  const top = el('div', 'row ac jb');
  top.append(el('div', 'metric-n', n));
  if (icon) top.append(append(el('span', 'metric-ico'), Icon(icon, { size: 19 })));
  c.append(top);

  c.append(el('div', 'metric-l', label));
  return seam(c, seams);
}

/**
 * FleetRow — a board <tr> for one worker: identity, issue, repo, stage, skill,
 * updated, action. `worker` carries the DATA: { name, avatar:{tone,glyph},
 * issue, title, repo, stage, skill, upd }. Identity is caller-supplied data, not
 * derived from an id format — the avatar is optional; `name` is the display
 * label (a real bot login like `trixy`, or a sample worker name). `actions` is a
 * row of caller-supplied nodes for the trailing cell (a Review Btn, a chevron,
 * or nothing) — composable, like SkillCard's actions. The trailing cell stays
 * present (empty data-label) so the column grid is stable.
 */
export function FleetRow(props = {}) {
  const [{ worker = {}, actions }, seams] = splitSeams(props);
  const w = worker;
  const tr = el('tr');
  // data-label on every cell drives the container-query reflow (table → stacked
  // cards on a narrow container); see .board-wrap in ld-system.css.
  const td = (cls, label) => { const c = el('td', cls); c.setAttribute('data-label', label); return c; };

  // 1. worker identity (avatar + name — both caller data)
  const td1 = td(null, 'Worker');
  const idRow = el('div', 'row ac g8');
  if (w.avatar) idRow.append(Avatar({ ...w.avatar, size: 24 }));
  idRow.append(el('span', 'b', w.name));
  td1.append(idRow);
  tr.append(td1);

  // 2. issue + title
  const td2 = td(null, 'Issue');
  td2.append(el('span', 'faint tnum', `#${w.issue}`));
  td2.append(document.createTextNode(' '));
  td2.append(el('span', 'sb', w.title));
  tr.append(td2);

  // 3. repo
  const td3 = td(null, 'Repo');
  td3.append(Tag({ tone: 'neutral', icon: 'folder', label: repoShort(w.repo) }));
  tr.append(td3);

  // 4. stage
  const td4 = td(null, 'Stage');
  td4.append(Tag({ tone: stageTone(w.stage), label: w.stage }));
  tr.append(td4);

  // 5. skill
  const td5 = td('mut fleet-skill', 'Skill running');
  td5.textContent = w.skill;
  tr.append(td5);

  // 6. updated
  const td6 = td('faint tnum', 'Updated');
  td6.textContent = w.upd;
  tr.append(td6);

  // 7. action — caller-supplied node(s); empty when none.
  const td7 = td('fleet-action', '');
  if (actions) for (const a of (Array.isArray(actions) ? actions : [actions])) if (a) td7.append(a);
  tr.append(td7);

  return seam(tr, seams);
}

/**
 * FleetTable — a board table: a header row over a FleetRow per worker. This is
 * the SAMPLE composition: it presents each mock worker with a neutral avatar +
 * its sample name, and a Review Btn / chevron action keyed off `needs`. The app
 * supplies its own identity + actions when it composes FleetRow directly.
 */
export function FleetTable(props = {}) {
  const [{ rows = WORKERS }, seams] = splitSeams(props);
  const table = el('table', 'board');

  const thead = el('thead');
  const htr = el('tr');
  for (const h of ['Worker', 'Issue', 'Repo', 'Stage', 'Skill running', 'Updated', '']) {
    htr.append(el('th', null, h));
  }
  thead.append(htr);
  table.append(thead);

  const tbody = el('tbody');
  for (const w of rows) {
    const worker = { ...w, name: w.id, avatar: { tone: 'neutral', glyph: wkNum(w.id) } };
    const actions = w.needs
      ? Btn({ variant: 'danger', size: 'sm', iconRight: 'arrow_forward', label: 'Review' })
      : mi('chevron_right', { size: 18, color: 'var(--fg-muted)' });
    tbody.append(FleetRow({ worker, actions }));
  }
  table.append(tbody);

  return seam(table, seams);
}

/** RepoFilterItem — a clickrow filter line: label + count, active-styled when `on`. */
export function RepoFilterItem(props = {}) {
  const [{ label, count, on = false }, seams] = splitSeams(props);
  const r = el('div', `row ac jb clickrow repo-filter${on ? ' on' : ''}`);

  r.append(el('span', null, label));
  r.append(el('span', 'tnum faint repo-filter-count', count));

  return seam(r, seams);
}

/**
 * KanbanColumn — a stage column: an eyebrow header + count over a kanban-drop
 * holding the cards (or a centred placeholder when empty). `conflict` reddens
 * the count.
 */
export function KanbanColumn(props = {}) {
  const [{ stage, count, conflict = false, children }, seams] = splitSeams(props);
  const col = el('div', 'kanban-col');

  const head = el('div', 'row ac jb kanban-head');
  head.append(el('span', 'eyebrow', stage));
  head.append(el('span', `tnum kanban-count${conflict ? ' conflict' : ''}`, count));
  col.append(head);

  const drop = el('div', 'kanban-drop');
  const hasChildren = Array.isArray(children) ? children.length > 0 : children != null;
  if (hasChildren) {
    append(drop, children);
  } else {
    drop.append(el('div', 'faint kanban-empty', '—'));
  }
  col.append(drop);

  return seam(col, seams);
}

/**
 * KanbanCard — a card for one worker in a kanban column: issue + needs-tag,
 * title, repo + PR + avatar. `worker` carries the DATA: { issue, title, repo,
 * needs, pr, avatar:{tone,glyph} }. Like FleetRow, the avatar is caller data
 * (optional) rather than derived from an id format — a real bot login, not a
 * sample worker number.
 */
export function KanbanCard(props = {}) {
  const [{ worker = {} }, seams] = splitSeams(props);
  const w = worker;
  const c = el('div', `card pad-sm kanban-card${w.needs ? ' needs' : ''}`);

  const top = el('div', 'row ac jb g6');
  top.append(el('span', 'faint tnum kc-issue', `#${w.issue}`));
  if (w.needs) top.append(Tag({ tone: 'red', label: 'needs you' }));
  c.append(top);

  c.append(el('div', 'kc-title', w.title));

  const bot = el('div', 'row ac jb');
  bot.append(Tag({ tone: 'neutral', icon: 'folder', label: repoShort(w.repo) }));
  const right = el('div', 'row ac g6');
  if (w.pr) right.append(el('span', 'faint tnum kc-pr', `PR #${w.pr}`));
  if (w.avatar) right.append(Avatar({ ...w.avatar, size: 22 }));
  bot.append(right);
  c.append(bot);

  return seam(c, seams);
}
