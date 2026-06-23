// Sample data for the gallery, lifted from the design source (ld-kit.jsx).
// Bot/worker/repo names (trixy, capo, tx-09, acme-inc/…) are ILLUSTRATIVE only —
// manager/worker GitHub accounts are per-install config; never hardcode them in
// a component. Stories pass these in as data, exactly as the screens do.

export const STAGES = ['Inbox', 'Planning', 'Implementing', 'PR Open', 'In Review', 'Conflicts', 'Merged'];
export const STEP_FLOW = ['Issue', 'Plan', 'Implement', 'PR', 'Review', 'Merge'];

// SEMANTIC → VISUAL maps. These are the bridge: a domain lifecycle state
// (issue-tracking) resolves to a domain-agnostic VISUAL colour class. They live
// in the caller layer (the app), not in a visual component.

// stage → Tag tone (the design's stageTone). The app's three divergent copies
// should converge here (see STORYBOOK.md, de-tangle #2).
const STAGE_TONE = {
  Inbox: 'ghost', Planning: 'neutral', Implementing: 'blue',
  'PR Open': 'neutral', 'In Review': 'amber', Conflicts: 'red', Merged: 'green',
};
export const stageTone = (s) => STAGE_TONE[s] || 'neutral';

// lifecycle state → dot colour (visual). merged→green, active→accent, …
const DOT_TONE = { merged: 'green', active: 'accent', review: 'amber', blocked: 'red', gated: 'hollow' };
export const dotTone = (state) => DOT_TONE[state] || 'hollow';

// lifecycle state → DAG-node look modifier (visual). gated→dashed, blocked→danger.
const NODE_LOOK = { gated: 'dashed', blocked: 'danger' };
export const nodeLook = (state) => NODE_LOOK[state] || '';

// actor role → avatar colour (visual). worker→neutral, manager→accent, operator→ink.
// Roles are structural archetypes, never the bot/operator login itself.
const AVATAR_TONE = { worker: 'neutral', manager: 'accent', operator: 'ink' };
export const avatarTone = (role) => AVATAR_TONE[role] || 'neutral';

export const REPOS = [
  { id: 'core', name: 'acme-inc/core', open: 5, prs: 3 },
  { id: 'web', name: 'acme-inc/web', open: 4, prs: 2 },
  { id: 'runtime', name: 'roadrunner-corp/agent-runtime', open: 6, prs: 4 },
  { id: 'pay', name: 'roadrunner-corp/payments', open: 3, prs: 2 },
];
export const repoName = (id) => (REPOS.find((r) => r.id === id) || {}).name || id;
export const repoShort = (id) => repoName(id).split('/').pop();
export const wkNum = (id) => id.replace('tx-', '').toUpperCase();

export const WORKERS = [
  { id: 'tx-04', issue: 471, title: 'Add dark-mode toggle', repo: 'web', stage: 'In Review', skill: 'address-pr-review', upd: '2m', needs: true, pr: 512 },
  { id: 'tx-09', issue: 455, title: 'Rate limiter overflow under burst', repo: 'core', stage: 'Conflicts', skill: 'resolve-conflicts', upd: '6m', needs: true, pr: 498 },
  { id: 'tx-02', issue: 421, title: 'Stripe 3DS challenge flow', repo: 'pay', stage: 'In Review', skill: 'address-pr-review', upd: '11m', needs: true, pr: 287 },
  { id: 'tx-07', issue: 482, title: 'Flaky auth test on CI', repo: 'runtime', stage: 'Implementing', skill: 'implement-issue', upd: '1m', needs: false, pr: null },
  { id: 'tx-11', issue: 460, title: 'Memory leak in worktree GC', repo: 'runtime', stage: 'Implementing', skill: 'implement-issue', upd: '4m', needs: false, pr: null },
  { id: 'tx-05', issue: 490, title: 'Webhook retry with backoff', repo: 'core', stage: 'Planning', skill: 'implement-issue', upd: '3m', needs: false, pr: null },
  { id: 'tx-12', issue: 478, title: 'Onboarding empty states', repo: 'web', stage: 'Planning', skill: 'implement-issue', upd: '8m', needs: false, pr: null },
  { id: 'tx-03', issue: 468, title: 'Refactor session store', repo: 'runtime', stage: 'PR Open', skill: 'implement-issue', upd: '14m', needs: false, pr: 503 },
  { id: 'tx-08', issue: 487, title: 'Add /healthz endpoint', repo: 'core', stage: 'PR Open', skill: 'implement-issue', upd: '20m', needs: false, pr: 509 },
  { id: 'tx-10', issue: 501, title: 'Conflict in migration 0042', repo: 'pay', stage: 'Conflicts', skill: 'resolve-conflicts', upd: '9m', needs: false, pr: 281 },
  { id: 'tx-06', issue: 495, title: 'Docs: authoring skills', repo: 'web', stage: 'Inbox', skill: 'prioritise-issues', upd: '22m', needs: false, pr: null },
  { id: 'tx-01', issue: 433, title: 'Cursor-based pagination', repo: 'core', stage: 'Merged', skill: 'implement-issue', upd: '1h', needs: false, pr: 470 },
];

export const SKILLS = [
  { id: 'implement-issue', who: 'trixy', icon: 'code', desc: 'Plan, branch in an isolated worktree, write code + tests, open a PR.', trig: 'Issue assigned to @trixy', runs: 7, phase: 'Plan → PR' },
  { id: 'address-pr-review', who: 'trixy', icon: 'rate_review', desc: 'Read review threads, apply requested changes, reply, re-request review.', trig: 'Changes requested on linked PR', runs: 5, phase: 'Review' },
  { id: 'resolve-conflicts', who: 'trixy', icon: 'merge', desc: 'Rebase on main, resolve merge conflicts, re-run checks, push.', trig: 'PR base drifted / merge blocked', runs: 4, phase: 'Conflicts' },
  { id: 'prioritise-issues', who: 'capo', icon: 'sort', desc: 'Rank the backlog by impact, deps & energy; propose what to pick up next.', trig: 'New issues / daily planning', runs: 3, phase: 'Backlog' },
  { id: 'plan-epic', who: 'capo', icon: 'account_tree', desc: 'Break an epic into ordered issues with dependencies and assign workers.', trig: 'Epic opened or operator request', runs: 2, phase: 'Project' },
  { id: 'triage-inbox', who: 'capo', icon: 'inbox', desc: 'Label, dedupe and route new issues to the right repo & worker.', trig: 'Issue created / mentioned', runs: 9, phase: 'Inbox' },
];

// A representative epic for the Cockpit DAG stories (node/edge shape per the
// ld-cockpit catalog). col/row are grid coords; state drives the dot tone.
export const EPIC = {
  id: 'theming', repo: 'web', name: 'Theming & dark mode',
  nodes: [
    { k: '#433', t: 'Cursor pagination', col: 0, row: 0, state: 'merged' },
    { k: '#455', t: 'Rate limiter', col: 1, row: 0, state: 'blocked', live: true, worker: '09' },
    { k: '#471', t: 'Dark-mode toggle', col: 1, row: 1, state: 'review', live: true, worker: '04' },
    { k: '#482', t: 'Flaky auth test', col: 2, row: 0, state: 'active', live: true, worker: '07' },
    { k: '#490', t: 'Webhook retry', col: 2, row: 1, state: 'gated' },
  ],
  edges: [['#433', '#455'], ['#433', '#471'], ['#455', '#482'], ['#471', '#490']],
};

export const STATE_LABEL = { merged: 'merged', active: 'in progress', review: 'in review', blocked: 'blocked', gated: 'gated' };
