// The gallery. Every extracted component × its variants, grouped by origin
// screen, tagged with the THREE orthogonal concerns:
//   visual (composable classes) · semantic (what it is + domain lifecycle state)
//   · behavioural (JS dynamics + hooks). Visual primitives take VISUAL tones;
//   semantic composites map domain state → tone (dotTone/stageTone).
import * as C from '../ds/components/index.js';
import { el, mi } from '../ds/util/dom.js';
import { REPOS, WORKERS, SKILLS, STAGES, EPIC, repoShort, wkNum, stageTone, STATE_LABEL } from '../ds/util/data.js';
import { section, block, specimen, cluster } from './gallery.js';

const TOC = [];
function add(root, sec) { root.append(sec); TOC.push([sec.id, sec.querySelector('.sb-h2').textContent]); }

// ---------------------------------------------------------------- PRIMITIVES
function primitives(root) {
  const s = section('primitives', 'Primitives', 'ld-kit.jsx + loony-dev.html — domain-agnostic visual atoms. Their classes are composable.');

  s.append(block('Btn', { visual: '.ld-btn + variant + size (composable)', semantic: '(usually none)', behavioural: 'onClick, id, [disabled]' }, [
    cluster('variants (sm)', C.BTN_VARIANTS.map((v) => C.Btn({ variant: v, size: 'sm', label: v }))),
    cluster('sizes / icons', [
      C.Btn({ variant: 'primary', size: 'md', icon: 'add', label: 'Assign issue' }),
      C.Btn({ variant: 'danger', size: 'sm', iconRight: 'arrow_forward', label: 'Review' }),
      C.Btn({ variant: 'outline', size: 'sm', icon: 'sync', label: 'Resync' }),
    ]),
  ]));

  s.append(block('Tag', { visual: '.tag + colour tone (composable)', semantic: 'caller maps stage/state → tone', behavioural: '(none)' }, [
    cluster('tones', C.TAG_TONES.map((t) => C.Tag({ tone: t, label: t }))),
    cluster('with icon', [C.Tag({ tone: 'red', icon: 'warning', label: 'conflicts' }), C.Tag({ tone: 'green', icon: 'check', label: 'merged' }), C.Tag({ tone: 'blue', icon: 'hub', label: 'capo' })]),
  ]));

  s.append(block('Sdot · StatePill', { visual: '.sdot + colour (green/accent/amber/red/hollow), .pulse', semantic: 'caller maps lifecycle → colour (dotTone)', behavioural: '(none)' }, [
    cluster('tones', C.SDOT_TONES.map((t) => C.Sdot({ tone: t, size: 12 }))),
    cluster('pulse (live)', [C.Sdot({ tone: 'accent', size: 12, pulse: true })]),
    specimen('statepill', C.StatePill({ tone: 'green', label: 'capo · online' })),
  ]));

  s.append(block('Avatar', { visual: '.ava + colour tone (neutral/accent/ink) + --ava-sz', semantic: 'caller maps actor role → tone (avatarTone)', behavioural: 'glyph injected — never a bot name' }, [
    cluster('tones', C.AVATAR_TONES.map((t) => C.Avatar({ tone: t, glyph: t.slice(0, 2) }))),
    cluster('sizes', [20, 24, 26, 30].map((z) => C.Avatar({ tone: 'neutral', glyph: 'TX', size: z }))),
  ]));

  s.append(block('Icon · Eyebrow · Stat · Hairline', { visual: '.material-symbols / .eyebrow / .stat-n,.stat-l / .hairline', semantic: 'caller supplies content', behavioural: '(none)' }, [
    cluster('icons', ['hub', 'dashboard', 'sensors', 'merge', 'bolt'].map((n) => C.Icon(n, { size: 22 }))),
    specimen('eyebrow', C.Eyebrow({ label: 'live activity' })),
    specimen('stat', C.Stat({ n: '12', label: 'workers active' })),
    specimen('hairline', (() => { const w = el('div'); w.style.width = '160px'; w.append(C.Hairline()); return w; })()),
  ]));

  s.append(block('Card', { visual: '.card + subtle/flat/pad (composable, e.g. .card.muted)', semantic: 'caller adds .story/.task etc. on same node', behavioural: 'caller adds id + onClick' }, [
    specimen('.card.pad', C.Card({ pad: 'md', children: el('div', null, 'A surface.') })),
    specimen('.card.subtle', C.Card({ subtle: true, pad: 'md', children: el('div', null, 'Subtle.') })),
    specimen('with .card-head', C.Card({ pad: false, head: [el('span', 'b', 'Header'), C.Tag({ tone: 'ghost', label: '3d old' })], children: (() => { const d = el('div'); d.style.padding = '12px var(--pad-card)'; d.textContent = 'body'; return d; })() })),
  ]));

  s.append(block('ScreenHead · Crumbs · Segmented · Subtab', { visual: '.screen-head / .crumbs / .seg / .subtab + .on', semantic: 'titles + nav targets', behavioural: 'onClick navigation' }, [
    specimen('screen-head', C.ScreenHead({ title: 'Fleet', sub: 'Your cross-repo worklist.', right: C.Segmented({ ariaLabel: 'Worklist layout', options: [{ label: 'Board', value: 'b', icon: 'view_list' }, { label: 'Kanban', value: 'k', icon: 'view_kanban' }], value: 'b' }) })),
    cluster('segmented · text + icon', [C.Segmented({ options: [{ label: 'Skills', value: 's' }, { label: 'Commands', value: 'c' }], value: 's' }), C.Segmented({ ariaLabel: 'Kind', options: [{ label: 'Skills', value: 's', icon: 'bolt' }, { label: 'Commands', value: 'c', icon: 'terminal' }], value: 'c' })]),
    specimen('crumbs', C.Crumbs({ items: [{ label: 'Fleet', icon: 'dashboard', onClick: () => {} }, { label: 'acme-inc/core', onClick: () => {} }, { label: '#455' }] })),
    cluster('subtabs', [C.Subtab({ label: 'All', on: true }), C.Subtab({ label: 'Mine' }), C.Subtab({ label: 'Blocked' })]),
  ]));

  add(root, s);
}

// ----------------------------------------------------------------- LIFECYCLE
function lifecycle(root) {
  const s = section('lifecycle', 'Lifecycle', 'The pipeline Stepper — minimal markup (<ol><li>), CSS counters + connectors; done/here are composable item-level classes.');
  s.append(block('Stepper', { visual: '.stepper ol/li + .done/.here/.detour (CSS counters)', semantic: 'maps board stage → step', behavioural: 'current = stepIndexForStage(stage)' }, [
    specimen('current = 0', C.Stepper({ current: 0 })),
    specimen('current = 2', C.Stepper({ current: 2 })),
    specimen('current = 4', C.Stepper({ current: 4 })),
    specimen('current = 6 (done)', C.Stepper({ current: 6 })),
    specimen('conflict detour', C.Stepper({ current: 4, conflict: true })),
  ]));
  add(root, s);
}

// ------------------------------------------------------------ NAVIGATION SHELL
function navigation(root) {
  const s = section('navigation', 'Navigation shell', 'loony-dev.html rail — brand, nav items, user card, settings.');
  s.append(block('BrandMark · BrandWord', { visual: '.brand-mark + i.on/.ac / .brand-word', semantic: '(brand)', behavioural: 'collapse toggle' }, [
    specimen('mark', C.BrandMark()),
    specimen('word', C.BrandWord()),
  ]));
  s.append(block('NavItem · NavEyebrow', { visual: '.navitem + .on / .nav-eyebrow / .needsdot', semantic: 'a destination', behavioural: 'onClick → route; count' }, [
    (() => { const col = el('div', 'col'); col.style.width = '236px'; col.style.gap = '2px';
      col.append(C.NavEyebrow({ label: 'Operate' }));
      col.append(C.NavItem({ icon: 'hub', label: 'Cockpit' }));
      col.append(C.NavItem({ icon: 'dashboard', label: 'Fleet', on: true, count: 12 }));
      col.append(C.NavItem({ icon: 'sensors', label: 'Live', live: true }));
      col.append(C.NavItem({ icon: 'warning', label: 'Conflicts', needs: true }));
      return specimen('nav list', col); })(),
  ]));
  s.append(block('UserCard · SettingsPopover', { visual: '.user-card / .settings-pop + .pop-item.on', semantic: 'the operator identity', behavioural: 'opens settings menu; id/handlers' }, [
    (() => { const w = el('div'); w.style.width = '236px'; w.append(C.UserCard({ name: 'operator', status: 'operator · online', kind: 'operator', glyph: 'OP' })); return specimen('user card', w); })(),
    C.SettingsPopover ? specimen('settings popover', C.SettingsPopover({ items: [{ icon: 'extension', label: 'Skills library', count: SKILLS.length, on: true }, { icon: 'tune', label: 'Preferences' }, { icon: 'vpn_key', label: 'Connected repos' }] })) : null,
  ]));
  add(root, s);
}

// --------------------------------------------------------------------- FLEET
function fleet(root) {
  const s = section('fleet', 'Fleet', 'ld-fleet.jsx — semantic composites of the worklist (board + kanban + metric strip).');
  s.append(block('WorkerPoolMatrix · Metric', { visual: '.pool-grid/.pool-dot + busy/free / .metric + .on', semantic: 'pool gauge / a KPI (some are filters)', behavioural: 'onClick → toggle filter' }, [
    specimen('worker pool', C.WorkerPoolMatrix({ total: 16, busy: 12 })),
    cluster('metrics', [
      C.Metric({ n: 4, label: 'repos online', icon: 'folder_open' }),
      C.Metric({ n: 3, label: 'need you', tone: 'amber', icon: 'pan_tool', filterable: true, on: true }),
      C.Metric({ n: 4, label: 'running', tone: 'blue', icon: 'bolt', filterable: true }),
      C.Metric({ n: 2, label: 'in conflict', tone: 'red', icon: 'warning', filterable: true }),
    ]),
  ]));
  s.append(block('FleetTable · FleetRow', { visual: 'table.board in .board-wrap (container-query reflow); composes Tag/Avatar/Btn', semantic: 'a worker on an issue; stage→tone', behavioural: 'tr onClick → issue; data-label drives the reflow' }, [
    specimen('board · wide', (() => { const w = el('div', 'card board-wrap'); w.style.width = '820px'; w.append(C.FleetTable({ rows: WORKERS.slice(0, 5) })); return w; })()),
    specimen('board · narrow → reflows to cards (no JS, no media query)', (() => { const w = el('div', 'card board-wrap'); w.style.width = '340px'; w.append(C.FleetTable({ rows: WORKERS.slice(0, 3) })); return w; })()),
  ]));
  s.append(block('RepoFilterItem', { visual: '.row.clickrow + .on', semantic: 'a repo as a filter', behavioural: 'onClick → setRepo' }, [
    (() => { const w = el('div'); w.style.width = '260px';
      w.append(C.RepoFilterItem({ label: 'All repos', count: WORKERS.length, on: true }));
      REPOS.forEach((r) => w.append(C.RepoFilterItem({ label: repoShort(r.id), count: r.open })));
      return specimen('repo filter list', w); })(),
  ]));
  s.append(block('KanbanColumn · KanbanCard', { visual: '.kanban-col/.kanban-drop/.kanban-card', semantic: 'workers grouped by stage; .needs', behavioural: 'card onClick → issue' }, [
    (() => { const av = (w) => ({ ...w, avatar: { tone: 'neutral', glyph: wkNum(w.id) } });
      const conflicts = WORKERS.filter((w) => w.stage === 'Conflicts');
      const col = C.KanbanColumn({ stage: 'Conflicts', count: conflicts.length, conflict: true, children: conflicts.map((w) => C.KanbanCard({ worker: av(w) })) });
      return specimen('column · Conflicts', col); })(),
    (() => { const av = (w) => ({ ...w, avatar: { tone: 'neutral', glyph: wkNum(w.id) } });
      const impl = WORKERS.filter((w) => w.stage === 'Implementing');
      return specimen('column · Implementing', C.KanbanColumn({ stage: 'Implementing', count: impl.length, children: impl.map((w) => C.KanbanCard({ worker: av(w) })) })); })(),
  ]));
  add(root, s);
}

// ------------------------------------------------------------------- COCKPIT
function cockpit(root) {
  const s = section('cockpit', 'Cockpit', 'ld-cockpit.jsx — epic planning: dependency DAG, zone cards, story list.');

  const byK = Object.fromEntries(EPIC.nodes.map((n) => [n.k, n]));
  const NW = 132, NH = 52, CX = 168, CY = 80, PX = 8, PY = 14;
  const maxCol = Math.max(...EPIC.nodes.map((n) => n.col));
  const maxRow = Math.max(...EPIC.nodes.map((n) => n.row));
  const lx = (n) => PX + n.col * CX, ly = (n) => PY + n.row * CY;
  const edges = EPIC.edges.map(([a, b]) => { const f = byK[a], t = byK[b];
    return C.DagEdge({ x1: lx(f) + NW, y1: ly(f) + NH / 2, x2: lx(t), y2: ly(t) + NH / 2, solid: f.state === 'merged' }); });
  const nodes = EPIC.nodes.map((n) => C.DagNode({ k: n.k, title: n.t, state: n.state, worker: n.worker, live: n.live, left: lx(n), top: ly(n) }));
  const dag = C.Dag({ width: PX + maxCol * CX + NW + PX, height: PY + maxRow * CY + NH + PY, edges, nodes });

  s.append(block('Dag · DagNode · DagEdge', { visual: '.dag/.node (+.dashed/.danger) + SVG; composes Sdot', semantic: 'an issue node + its state; edge = dependency', behavioural: 'node onClick → issue (unless gated)' }, [
    specimen('dependency graph', dag, { pad: true }),
    specimen('legend', C.DagLegend()),
  ]));

  const FNW = 52, FCX = 84, FCY = 54, FPX = 18, FPY = 10;
  const flx = (n) => FPX + n.col * FCX, fly = (n) => FPY + n.row * FCY;
  const fedges = EPIC.edges.map(([a, b]) => { const f = byK[a], t = byK[b];
    return C.DagEdge({ x1: flx(f) + FNW / 2, y1: fly(f) + 10, x2: flx(t) + FNW / 2, y2: fly(t) + 10, solid: f.state === 'merged', compact: true }); });
  const fnodes = EPIC.nodes.map((n) => C.DagFieldNode({ k: n.k, state: n.state, worker: n.worker, live: n.live, left: flx(n), top: fly(n) }));
  const field = el('div', 'dagfield');
  field.style.setProperty('--w', (FPX * 2 + maxCol * FCX + FNW) + 'px');
  field.style.setProperty('--h', (FPY + maxRow * FCY + 20 + 16 + 8) + 'px');
  const fsvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  fsvg.style.cssText = 'position:absolute;inset:0;overflow:visible;pointer-events:none';
  fedges.forEach((e) => fsvg.append(e));
  field.append(fsvg);
  fnodes.forEach((n) => field.append(n));

  const epicCard = C.EpicZoneCard({ repoLabel: 'acme-inc/web', epicName: EPIC.name, live: 3, stories: EPIC.nodes.length, prs: 2, open: 4, children: field });
  s.append(block('EpicZoneCard · DagField', { visual: '.card.dotgrid + .dagfield/.df-node', semantic: 'a repo/epic zone', behavioural: 'onClick → open epic' }, [
    specimen('epic zone', (() => { const w = el('div'); w.style.width = '360px'; w.append(epicCard); return w; })()),
  ]));

  s.append(block('StoryListItem', { visual: '.card.pad-sm; composes Sdot + Tag chips', semantic: 'a story + lifecycle state; .gated', behavioural: 'onClick → issue (gated = no click)' }, [
    specimen('review', C.StoryListItem({ k: '#471', title: 'Dark-mode toggle', state: 'review', chips: [{ label: 'plan fits epic', tone: 'green', icon: 'check' }, { label: 'CodeRabbit', tone: 'amber', icon: 'schedule' }, { label: 'merge ready', tone: 'blue', icon: 'check' }] })),
    specimen('blocked', C.StoryListItem({ k: '#455', title: 'Rate limiter', state: 'blocked', chips: [{ label: 'plan fits epic', tone: 'green', icon: 'check' }, { label: 'merge blocked', tone: 'red', icon: 'block' }] })),
    specimen('gated', C.StoryListItem({ k: '#490', title: 'Webhook retry', state: 'gated', gated: true, preds: '#471' })),
  ]));

  s.append(block('LiveActivityRow', { visual: '.row.clickrow + Avatar; tone inks the verb', semantic: 'an activity event', behavioural: 'onClick → issue' }, [
    (() => { const w = el('div', 'col'); w.style.gap = '12px'; w.style.width = '300px';
      w.append(C.LiveActivityRow({ glyph: '04', who: 'tx-04', what: 'pushed review fixes', where: 'web #471', tone: 'review' }));
      w.append(C.LiveActivityRow({ glyph: '09', who: 'tx-09', what: 'hit a merge conflict', where: 'core #455', tone: 'blocked' }));
      w.append(C.LiveActivityRow({ glyph: 'CP', kind: 'manager', who: 'capo', what: 'reprioritised Theming', where: 'web', tone: null }));
      return specimen('activity feed', w); })(),
  ]));
  add(root, s);
}

// ------------------------------------------------------------------ SESSIONS
function sessions(root) {
  const s = section('sessions', 'Sessions', 'ld-sessions.jsx — Live chat, Issue detail timeline, Skills library.');

  s.append(block('ChatBubble · ChatComposer', { visual: '.msg/.bubble + .sent/.received; composer .row + input + Btn', semantic: '.bot/.user (author) composed on the same node; a conversation', behavioural: 'send handler; bubbles embed CTAs' }, [
    (() => {
      // One cohesive conversation panel (bubbles + composer), like the nav list /
      // epic zone / stepper. Each turn composes the VISUAL direction (sent/
      // received) with the caller-injected SEMANTIC author class (bot/user).
      const panel = el('div', 'card');
      panel.style.width = '460px';
      panel.style.overflow = 'hidden';
      const body = el('div', 'col g12 pad');
      body.append(C.ChatBubble({ side: 'received', class: 'bot', glyph: '09', speaker: 'tx-09', children: (() => { const f = document.createDocumentFragment(); f.append('rebased onto main, touched '); f.append(C.CodeChip({ text: 'limiter.ts' })); f.append(' — 1 conflict left.'); return f; })() }));
      body.append(C.ChatBubble({ side: 'sent', class: 'user', children: 'ship it once CI is green' }));
      body.append(C.ChatBubble({ side: 'received', class: 'bot', glyph: '09', children: 'on it — pushing the fix now.' }));
      panel.append(body);
      panel.append(C.ChatComposer({ placeholder: 'steer trixy…' }));
      return specimen('conversation', panel);
    })(),
    (() => { const w = el('div', 'card'); w.style.width = '380px'; w.style.overflow = 'hidden';
      w.append(C.ChatComposer({ placeholder: 'Ask about the repo, or "open an issue to…"', disabled: true }));
      return specimen('composer · disabled (steer before drive bridge)', w);
    })(),
  ]));

  s.append(block('TimelineRow', { visual: 'RAIL: .tl-dot + colour + .tl-line (:last-child closes it) · FLAT: dot + avatar + chip + trailing time', semantic: 'a lifecycle event; state → colour (dotTone)', behavioural: 'rail / flat shapes; .pulse (live)' }, [
    (() => { const w = el('div', 'col'); w.style.width = '320px';
      w.append(C.TimelineRow({ title: 'Opened PR #498', who: 'tx-09', when: '2h', state: 'active' }));
      w.append(C.TimelineRow({ title: 'CodeRabbit requested changes', who: 'coderabbit', when: '1h', state: 'review' }));
      w.append(C.TimelineRow({ title: 'Merge conflict on main', who: 'tx-09', when: '20m', state: 'blocked' }));
      w.append(C.TimelineRow({ title: 'Waiting for you', who: 'trixy', when: 'now', live: true }));
      return specimen('rail (default)', w); })(),
    (() => { const w = el('div', 'col'); w.style.gap = '8px'; w.style.width = '340px';
      w.append(C.TimelineRow({ rail: false, whenAlign: 'right', state: 'active', avatar: { tone: 'soft', glyph: 'TX' }, title: 'Pushed 3 commits', chip: 'implement', when: '2m' }));
      w.append(C.TimelineRow({ rail: false, whenAlign: 'right', state: 'review', avatar: { tone: 'neutral', glyph: 'SY' }, title: 'CodeRabbit requested changes', chip: 'review', when: '1m' }));
      w.append(C.TimelineRow({ rail: false, whenAlign: 'right', state: 'blocked', avatar: { tone: 'green', glyph: 'OP' }, title: 'Stuck — needs your call', when: 'now' }));
      return specimen('flat (app activity row)', w); })(),
  ]));

  s.append(block('SkillCard', { visual: '.card.skill-card; composes Avatar / Tag / Btn', semantic: 'a library entry (skill / command) + owner; phase → Tag tone', behavioural: 'caller-supplied action Btns' }, [
    specimen('managed entry', (() => { const w = el('div'); w.style.width = '340px'; const k = SKILLS[0];
      w.append(C.SkillCard({ icon: k.icon, name: k.id, owner: { managed: true, label: k.who }, desc: k.desc, trigger: k.trig, phase: { label: k.phase, tone: 'blue' }, runs: k.runs,
        actions: [C.Btn({ variant: 'outline', size: 'sm', label: 'Edit' }), C.Btn({ variant: 'ghost', size: 'sm', label: 'Delete' })] }));
      return w; })()),
    specimen('hand-authored, no trigger', (() => { const w = el('div'); w.style.width = '340px'; const k = SKILLS[3];
      w.append(C.SkillCard({ icon: k.icon, name: k.id, owner: { managed: false, label: 'hand-authored' }, desc: k.desc,
        actions: [C.Btn({ variant: 'outline', size: 'sm', label: 'Edit' }), C.Btn({ variant: 'ghost', size: 'sm', label: 'Delete' })] }));
      return w; })()),
  ]));

  s.append(block('LinkedMini · KVRow', { visual: '.card.pad-sm + .diffstat + tone / .row', semantic: 'linked issue/PR; repo context', behavioural: 'onClick → open' }, [
    (() => { const w = el('div', 'col'); w.style.gap = '8px'; w.style.width = '300px';
      w.append(C.LinkedMini({ title: 'Issue #455', tone: 'green', label: 'open' }));
      w.append(C.LinkedMini({ title: 'PR #498', tone: 'red', label: 'conflicts', diff: { add: 128, del: 34, files: 5, reviews: 2 } }));
      return specimen('linked', w); })(),
    (() => { const w = el('div', 'col'); w.style.gap = '8px'; w.style.width = '300px';
      [['Branch', 'issue-455'], ['Open issues', '5'], ['Open PRs', '3'], ['Workers here', '2']].forEach(([k, v]) => w.append(C.KVRow({ k, v })));
      return specimen('repo context', w); })(),
  ]));
  add(root, s);
}

// -------------------------------------------------------------------- MOBILE
function mobile(root) {
  const s = section('mobile', 'Mobile', 'ld-mobile.jsx — phone frame, status bar, tab bar.');
  const screen = el('div');
  screen.append(C.PhoneStatus({ dark: false }));
  const body = el('div'); body.style.cssText = 'padding:12px 18px;min-height:380px';
  body.append(C.Eyebrow({ label: 'Needs your call' }));
  WORKERS.filter((w) => w.needs).slice(0, 2).forEach((w) => {
    const card = C.Card({ pad: 'sm' }); card.style.marginTop = '10px';
    const t = el('div', 'sb'); t.style.cssText = 'font-size:12.5px;color:var(--ink)'; t.append(C.Tag({ tone: stageTone(w.stage), label: w.stage })); t.append(' ' + w.title);
    card.append(t); body.append(card);
  });
  screen.append(body);
  screen.append(C.PhoneNav({ active: 'Fleet' }));

  s.append(block('PhoneFrame · PhoneStatus · PhoneNav', { visual: '.phone / .phone-status / .phone-nav + .nav-tab.on', semantic: 'a mobile screen', behavioural: 'tab → route' }, [
    specimen('phone · Fleet', C.PhoneFrame({ children: screen })),
    specimen('status bar', (() => { const w = el('div'); w.style.width = '300px'; w.append(C.PhoneStatus({ dark: false })); return w; })()),
    specimen('tab bar', (() => { const w = el('div'); w.style.width = '300px'; w.append(C.PhoneNav({ active: 'Cockpit' })); return w; })()),
  ]));
  add(root, s);
}

export function renderStorybook(root) {
  primitives(root);
  lifecycle(root);
  navigation(root);
  fleet(root);
  cockpit(root);
  sessions(root);
  mobile(root);

  const nav = document.getElementById('sb-toc');
  if (nav) for (const [id, label] of TOC) { const a = el('a', null, label); a.href = '#' + id; nav.append(a); }
}
