"use strict";

// Fleet worklist (#188): the cross-repo work-thread view that replaces the old
// Overview repo roll-up. A stat strip, a board/kanban worklist, composable
// badge + repo filters, all driven off the consolidated /api/events snapshot
// and the L Space design-system primitives from #186 (.stat, .segmented,
// .tag.*, .avatar.trixy, .statepill/.sdot.*).
//
// Data reality (#219): the snapshot now carries *partial* GitHub state —
// `snapshot.pipelines` (per-pipeline real title, label/PR-derived stage, raw
// labels) and `snapshot.repos` (per-repo open issue / open PR counts). Those
// drive the real titles, the In Review / Conflicts stages + filters, and the
// PRs-open / Issues-open / In-review / In-conflict stats. They DEGRADE
// GRACEFULLY: when GitHub state is disabled or a fetch fails the lists arrive
// empty and we fall back to the filesystem-derived coarse stage / de-slugged
// title and the deferred `—` counts. Everything derivable from workers /
// worktrees / sessions / task_sessions / stuck stays live regardless.

import { goRepo, icon, formatAge } from "./dom.js";

// Lifecycle stages, in board order. Only a coarse subset is derivable from the
// filesystem snapshot (Inbox / Implementing / PR Open); the rest render as
// empty kanban columns until a backend feed supplies label state.
const STAGES = [
  "Inbox", "Planning", "Implementing", "PR Open", "In Review", "Conflicts", "Merged",
];

// Stage → .tag color variant (#186 palette).
const STAGE_TAG = {
  "Inbox": "ghost",
  "Planning": "blue",
  "Implementing": "blue",
  "PR Open": "purple",
  "In Review": "amber",
  "Conflicts": "red",
  "Merged": "green",
};

// Badge filters. `predicate` decides which rows a badge matches; the
// lifecycle-only badges (in-review / in-conflict) never match today but still
// render so the control surface is complete (issue acceptance criteria).
const BADGES = {
  "needs-you": (r) => r.stuck,
  "running": (r) => r.state === "active",
  "in-review": (r) => r.stage === "In Review",
  "in-conflict": (r) => r.stage === "Conflicts",
};

// View state persists across SSE snapshots so a re-render never clobbers the
// operator's current layout / filter selection.
const state = {
  view: "board", // "board" | "kanban"
  badges: new Set(), // active badge keys
  repo: null, // active repo filter, or null
  snapshot: null, // last snapshot, for re-draw on a filter toggle
};

let wired = false;

// ---- Derivation -------------------------------------------------------------

// Parse a pipeline key from a `issue-N` / `pr-N` task_key or a branch whose
// prefix is `issue-N/…`. Returns {key, kind, number} or null.
function parseKey(value) {
  if (!value) return null;
  const m = String(value).match(/^(issue|pr)-(\d+)/);
  if (!m) return null;
  return { key: `${m[1]}-${m[2]}`, kind: m[1], number: Number(m[2]) };
}

// De-slug a branch into an approximate title: `issue-188/web-ui-rework-fleet`
// → "Web ui rework fleet". Best-available until a backend feed carries the real
// issue title.
function deslug(branch) {
  const slug = String(branch).split("/").slice(1).join("/");
  if (!slug) return null;
  const words = slug.replace(/[-_]+/g, " ").trim();
  if (!words) return null;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Keep the newer of two ISO-8601 timestamps (lexicographic compare is correct
// for same-format UTC strings). Either may be null.
function newer(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  return b > a ? b : a;
}

// Build the derived pipeline rows by joining the snapshot collections on the
// `issue-N` / `pr-N` key. Pure (no DOM) so it stays unit-testable if a JS test
// harness is later introduced.
export function buildRows(snapshot) {
  const worktrees = snapshot.worktrees || [];
  const sessions = snapshot.sessions || [];
  const stuck = snapshot.stuck || [];
  const taskSessions = snapshot.task_sessions || [];
  const pipelines = snapshot.pipelines || [];

  const rows = new Map();
  const ensure = (p, repo) => {
    let r = rows.get(p.key);
    if (!r) {
      r = {
        key: p.key, kind: p.kind, number: p.number, repo: repo || null,
        title: null, stage: "Inbox", ghStage: null, labels: [],
        state: "idle", running: false, stuck: false, lastUpdate: null,
      };
      rows.set(p.key, r);
    }
    if (!r.repo && repo) r.repo = repo;
    return r;
  };

  for (const w of worktrees) {
    const p = parseKey(w.branch);
    if (!p) continue;
    const r = ensure(p, w.repo);
    if (!r.title) r.title = deslug(w.branch);
  }
  for (const t of taskSessions) {
    const p = parseKey(t.task_key);
    if (!p) continue;
    const r = ensure(p, t.repo);
    if (t.status === "running") r.running = true;
    r.lastUpdate = newer(r.lastUpdate, t.started_at);
  }
  for (const s of sessions) {
    const p = parseKey(s.key);
    if (!p) continue;
    const r = ensure(p, s.repo);
    if (s.alive) r.running = true;
    r.lastUpdate = newer(r.lastUpdate, s.updated_at);
  }
  for (const s of stuck) {
    const p = parseKey(s.task_key);
    if (!p) continue;
    ensure(p, s.worker_repo).stuck = true;
  }
  // GitHub state (#219): real title + label/PR-derived stage + raw labels. This
  // also SEEDS pipelines (Planning / Inbox / In Review …) that have no local
  // worktree or session yet, so the board and kanban columns populate from
  // GitHub even before the worker touches the pipeline on disk.
  for (const g of pipelines) {
    const p = parseKey(g.pipeline_key);
    if (!p) continue;
    const r = ensure(p, g.repo);
    if (g.title) r.title = g.title; // real issue/PR title wins over de-slug
    if (g.stage) r.ghStage = g.stage;
    if (Array.isArray(g.labels)) r.labels = g.labels;
    r.lastUpdate = newer(r.lastUpdate, g.updated_at);
  }

  for (const r of rows.values()) {
    // Prefer the real GitHub stage; fall back to the coarse filesystem guess
    // when GitHub state is absent (disabled / fetch failed / pipeline unseen):
    // PRs read as "PR Open", an issue with a live task session "Implementing",
    // everything else "Inbox".
    const coarse = r.kind === "pr" ? "PR Open" : r.running ? "Implementing" : "Inbox";
    r.stage = r.ghStage || coarse;
    r.state = r.stuck ? "blocked" : r.running ? "active" : "idle";
    if (!r.title) r.title = r.key;
  }

  // Newest first by issue/PR number (a stable, meaningful order).
  return [...rows.values()].sort((a, b) => b.number - a.number);
}

// Compute the stat-strip metrics. PRs-open / issues-open / in-review /
// in-conflict draw on GitHub state (#219) when present; when `snapshot.repos`
// is empty (disabled / failed) the open counts stay deferred (`—`, never a
// fake 0). In-review / in-conflict are live row counts off the real stage.
function buildStats(snapshot, rows) {
  const workers = snapshot.workers || [];
  const taskSessions = snapshot.task_sessions || [];
  const stuck = snapshot.stuck || [];
  const repos = snapshot.repos || [];

  const runningWorkers = workers.filter((w) => w.status === "running");
  const reposOnline = new Set(runningWorkers.map((w) => w.repo).filter(Boolean));
  const busyRepos = new Set(
    taskSessions.filter((t) => t.status === "running").map((t) => t.repo).filter(Boolean),
  );
  const busy = runningWorkers.filter((w) => busyRepos.has(w.repo)).length;

  // Sum the per-repo GitHub counts, ignoring repos whose fetch failed (ok=false
  // / null counts). `haveCounts` distinguishes "no GitHub feed" (defer to `—`)
  // from a genuine zero.
  const okRepos = repos.filter((r) => r.ok && typeof r.open_prs === "number");
  const haveCounts = okRepos.length > 0;
  const prsOpen = okRepos.reduce((n, r) => n + (r.open_prs || 0), 0);
  const issuesOpen = okRepos.reduce((n, r) => n + (r.open_issues || 0), 0);

  return {
    reposOnline: reposOnline.size,
    haveCounts,
    prsOpen,
    issuesOpen,
    workersTotal: runningWorkers.length,
    workersBusy: busy,
    needsYou: stuck.length,
    running: rows.filter((r) => r.state === "active").length,
    inReview: rows.filter((r) => r.stage === "In Review").length,
    inConflict: rows.filter((r) => r.stage === "Conflicts").length,
  };
}

// ---- Filtering --------------------------------------------------------------

// Rows passing the active badge filters (OR within the badge group). Repo
// filter is applied separately so the repo sidebar counts reflect the badge
// selection without zeroing themselves out.
function badgeFiltered(rows) {
  const active = [...state.badges];
  if (!active.length) return rows;
  return rows.filter((r) => active.some((b) => BADGES[b] && BADGES[b](r)));
}

// The fully composed set: badge filters AND the repo filter.
function filteredRows(rows) {
  let out = badgeFiltered(rows);
  if (state.repo) out = out.filter((r) => r.repo === state.repo);
  return out;
}

// ---- Rendering helpers ------------------------------------------------------

function relTime(iso) {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  return `${formatAge((Date.now() - ms) / 1000)} ago`;
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

function stageTag(stage) {
  return el("span", `tag ${STAGE_TAG[stage] || "neutral"}`, stage);
}

function statePill(row) {
  const pill = el("span", "statepill");
  const dotClass = row.state === "blocked" ? "blocked"
    : row.state === "active" ? "active" : "gated";
  const label = row.state === "blocked" ? "Stuck"
    : row.state === "active" ? "Running" : "Idle";
  pill.appendChild(el("span", `sdot ${dotClass}`));
  pill.appendChild(document.createTextNode(label));
  return pill;
}

// Single worker (the trixy bot). Deep-link a row to the Issue ▸ PR pipeline
// detail (#190), which keys off the pipeline's `issue-N`/`pr-N` key, so the
// detail view keeps full pipeline context. Falls back to the per-repo drill-down
// when the row has no pipeline key (or Alpine has not booted).
function goPipeline(row) {
  const store = window.Alpine && window.Alpine.store("app");
  if (store && row.repo && row.key) {
    store.goPipeline(row.repo, row.key);
    return;
  }
  if (row.repo) goRepo(row.repo);
}

// ---- Stat strip -------------------------------------------------------------

function statCard(label, value, deferred) {
  const card = el("div", "stat");
  card.appendChild(el("span", "stat-label", label));
  const v = el("span", "stat-value", deferred ? "—" : String(value));
  if (deferred) {
    v.classList.add("stat-deferred");
    v.title = "Awaiting backend feed";
  }
  card.appendChild(v);
  return card;
}

function workerPoolCard(busy, total) {
  const card = el("div", "stat stat-pool");
  card.appendChild(el("span", "stat-label", "Workers active"));
  card.appendChild(el("span", "stat-value", `${busy}/${total}`));
  const grid = el("div", "pool-grid");
  for (let i = 0; i < total; i++) {
    grid.appendChild(el("span", `pool-dot ${i < busy ? "busy" : "idle"}`));
  }
  if (!total) grid.appendChild(el("span", "pool-empty", "no workers online"));
  card.appendChild(grid);
  return card;
}

function renderStats(snapshot, rows) {
  const host = document.getElementById("fleet-stats");
  if (!host) return;
  const s = buildStats(snapshot, rows);
  host.innerHTML = "";
  host.appendChild(statCard("Repos online", s.reposOnline));
  // Open counts come from GitHub state; defer to `—` only when no repo feed.
  host.appendChild(statCard("PRs open", s.prsOpen, !s.haveCounts));
  host.appendChild(statCard("Issues open", s.issuesOpen, !s.haveCounts));
  host.appendChild(workerPoolCard(s.workersBusy, s.workersTotal));
  host.appendChild(statCard("Needs you", s.needsYou));
  host.appendChild(statCard("Running", s.running));
  host.appendChild(statCard("In review", s.inReview));
  host.appendChild(statCard("In conflict", s.inConflict));
}

// ---- Board ------------------------------------------------------------------

function clickable(node, row) {
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.addEventListener("click", () => goPipeline(row));
  node.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      goPipeline(row);
    }
  });
}

function boardRow(row) {
  const tr = el("tr", "fleet-row");
  clickable(tr, row);

  const num = el("td", null);
  num.dataset.label = "#";
  num.appendChild(el("span", "fleet-num", `#${row.number}`));
  tr.appendChild(num);

  const title = el("td", "fleet-title-cell", row.title);
  title.dataset.label = "Title";
  tr.appendChild(title);

  const repo = el("td", null, row.repo || "—");
  repo.dataset.label = "Repo";
  tr.appendChild(repo);

  const stage = el("td", null);
  stage.dataset.label = "Stage";
  stage.appendChild(stageTag(row.stage));
  tr.appendChild(stage);

  const worker = el("td", null);
  worker.dataset.label = "Worker";
  worker.appendChild(el("span", "avatar trixy", "TX"));
  tr.appendChild(worker);

  const updated = el("td", null, relTime(row.lastUpdate));
  updated.dataset.label = "Updated";
  tr.appendChild(updated);

  const stateCell = el("td", null);
  stateCell.dataset.label = "State";
  stateCell.appendChild(statePill(row));
  tr.appendChild(stateCell);

  return tr;
}

function renderBoard(rows) {
  const tbody = document.querySelector("#fleet-board .fleet-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  for (const r of rows) tbody.appendChild(boardRow(r));
}

// ---- Kanban -----------------------------------------------------------------

function kanbanCard(row) {
  const card = el("div", "fleet-card");
  clickable(card, row);

  const head = el("div", "fleet-card-head");
  head.appendChild(el("span", "fleet-num", `#${row.number}`));
  head.appendChild(el("span", `sdot ${row.state === "blocked" ? "blocked"
    : row.state === "active" ? "active" : "gated"}`));
  card.appendChild(head);

  card.appendChild(el("div", "fleet-card-title", row.title));

  const meta = el("div", "fleet-card-meta");
  meta.appendChild(el("span", "avatar trixy", "TX"));
  meta.appendChild(el("span", "fleet-card-repo", row.repo || "—"));
  card.appendChild(meta);

  return card;
}

function renderKanban(rows) {
  const host = document.getElementById("fleet-kanban");
  if (!host) return;
  const byStage = new Map(STAGES.map((s) => [s, []]));
  for (const r of rows) {
    if (byStage.has(r.stage)) byStage.get(r.stage).push(r);
  }
  host.innerHTML = "";
  for (const stage of STAGES) {
    const items = byStage.get(stage);
    const col = el("div", "fleet-col");
    const head = el("div", "fleet-col-head");
    head.appendChild(el("span", "fleet-col-title", stage));
    head.appendChild(el("span", "fleet-col-count", String(items.length)));
    col.appendChild(head);
    const body = el("div", "fleet-col-body");
    if (!items.length) {
      body.appendChild(el("p", "fleet-col-empty", "—"));
    } else {
      for (const r of items) body.appendChild(kanbanCard(r));
    }
    col.appendChild(body);
    host.appendChild(col);
  }
}

// ---- Repo sidebar -----------------------------------------------------------

function renderRepoSidebar(rows) {
  const host = document.getElementById("fleet-repos");
  if (!host) return;
  // Counts reflect the badge filter but not the repo filter (faceted search).
  const base = badgeFiltered(rows);
  const counts = new Map();
  for (const r of base) {
    if (!r.repo) continue;
    counts.set(r.repo, (counts.get(r.repo) || 0) + 1);
  }
  const repos = [...counts.keys()].sort((a, b) => a.localeCompare(b));

  host.innerHTML = "";
  const head = el("div", "fleet-repos-head");
  head.appendChild(el("span", "eyebrow", "Repos"));
  if (state.repo) {
    const clear = el("button", "fleet-clear", "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => {
      state.repo = null;
      draw();
    });
    head.appendChild(clear);
  }
  host.appendChild(head);

  if (!repos.length) {
    host.appendChild(el("p", "empty", "No repos."));
    return;
  }
  for (const repo of repos) {
    const btn = el("button", "fleet-repo");
    btn.type = "button";
    if (repo === state.repo) {
      btn.classList.add("active");
      btn.setAttribute("aria-pressed", "true");
    } else {
      btn.setAttribute("aria-pressed", "false");
    }
    btn.appendChild(el("span", "fleet-repo-name", repo));
    btn.appendChild(el("span", "fleet-repo-count", String(counts.get(repo))));
    btn.addEventListener("click", () => {
      state.repo = state.repo === repo ? null : repo;
      draw();
    });
    host.appendChild(btn);
  }
}

// ---- Chips + live count -----------------------------------------------------

function chip(label, onRemove) {
  const c = el("span", "fleet-chip");
  c.appendChild(document.createTextNode(label));
  const x = el("button", "fleet-chip-x");
  x.type = "button";
  x.setAttribute("aria-label", `Remove ${label} filter`);
  x.appendChild(icon("close"));
  x.addEventListener("click", onRemove);
  c.appendChild(x);
  return c;
}

const BADGE_LABEL = {
  "needs-you": "Needs you",
  "running": "Running",
  "in-review": "In review",
  "in-conflict": "In conflict",
};

function renderChips(shown) {
  const host = document.getElementById("fleet-chips");
  if (!host) return;
  host.innerHTML = "";
  for (const b of state.badges) {
    host.appendChild(chip(BADGE_LABEL[b] || b, () => {
      state.badges.delete(b);
      draw();
    }));
  }
  if (state.repo) {
    host.appendChild(chip(state.repo, () => {
      state.repo = null;
      draw();
    }));
  }
  host.appendChild(el("span", "fleet-count", `${shown} shown`));
}

// ---- Control state sync -----------------------------------------------------

function syncControls(rows) {
  // View toggle.
  document.querySelectorAll("#fleet-view [data-fleet-view]").forEach((btn) => {
    const on = btn.dataset.fleetView === state.view;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  });
  // Badge active states + per-badge availability counts (from the full row set
  // so the numbers show what's available, independent of the current combo).
  document.querySelectorAll("#fleet-badges [data-fleet-badge]").forEach((btn) => {
    const key = btn.dataset.fleetBadge;
    btn.classList.toggle("active", state.badges.has(key));
    btn.setAttribute("aria-pressed", String(state.badges.has(key)));
  });
  document.querySelectorAll("[data-fleet-badge-count]").forEach((node) => {
    const key = node.dataset.fleetBadgeCount;
    node.textContent = String(rows.filter((r) => BADGES[key] && BADGES[key](r)).length);
  });
}

// ---- Draw -------------------------------------------------------------------

function draw() {
  if (!state.snapshot) return;
  const rows = buildRows(state.snapshot);
  const shown = filteredRows(rows);

  renderStats(state.snapshot, rows);
  syncControls(rows);
  renderChips(shown.length);
  renderRepoSidebar(rows);

  const board = document.getElementById("fleet-board");
  const kanban = document.getElementById("fleet-kanban");
  const empty = document.getElementById("fleet-empty");

  const isEmpty = shown.length === 0;
  if (board) board.hidden = isEmpty || state.view !== "board";
  if (kanban) kanban.hidden = isEmpty || state.view !== "kanban";
  if (empty) {
    empty.hidden = !isEmpty;
    empty.textContent = rows.length === 0
      ? "No active pipelines yet."
      : "No items match these filters.";
  }

  if (!isEmpty) {
    if (state.view === "board") renderBoard(shown);
    else renderKanban(shown);
  }
}

// Wire the static controls once. Called from app.js start().
export function init() {
  if (wired) return;
  wired = true;
  document.querySelectorAll("#fleet-view [data-fleet-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.view = btn.dataset.fleetView;
      draw();
    });
  });
  document.querySelectorAll("#fleet-badges [data-fleet-badge]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.fleetBadge;
      if (state.badges.has(key)) state.badges.delete(key);
      else state.badges.add(key);
      draw();
    });
  });
}

// Re-entrant on every SSE snapshot; preserves the current view/filter state.
export function render(snapshot) {
  state.snapshot = snapshot;
  draw();
}
