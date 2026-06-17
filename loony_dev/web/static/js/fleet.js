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

// Stat-metric filters (#223). The four state metric cards double as a single
// mutually-exclusive worklist filter: clicking a metric sets it, clicking the
// active one clears back to "all". `matchesFilter` decides which rows survive.
// The in-review / in-conflict filters never match today on a filesystem-only
// snapshot but light up once the #219 GitHub feed supplies the real stage.
const FILTER_LABEL = {
  needs: "Need you",
  running: "Running",
  review: "In review",
  conflict: "Conflicts",
};

// A row "needs you" when a human gate is required: the process wedged (stuck)
// or the lifecycle parked it in `in-error`. Drives both the metric count and
// the per-row Review button.
function needsYou(row) {
  return Boolean(row.stuck) || (row.labels || []).includes("in-error");
}

function matchesFilter(row, filter) {
  if (filter === "needs") return needsYou(row);
  if (filter === "running") return row.state === "active";
  if (filter === "review") return row.stage === "In Review";
  if (filter === "conflict") return row.stage === "Conflicts";
  return true; // "all"
}

// Heuristic stage → running skill label. There is no live "current skill" field
// in the snapshot yet, so we infer the phase-appropriate command from the stage
// — and only for rows that are actively running (else `—`). Replace with a real
// per-pipeline skill feed when the backend surfaces one.
const STAGE_SKILL = {
  "Planning": "plan-issue",
  "Implementing": "implement-issue",
  "PR Open": "implement-issue",
  "In Review": "address-reviews",
  "Conflicts": "resolve-conflicts",
};
function skillFor(row) {
  if (row.state !== "active") return "—";
  return STAGE_SKILL[row.stage] || "—";
}

// The six stat metrics, in document (3-col × 2-row) order. The four with a
// `filter` key are clickable worklist filters; `repos online` / `PRs open` are
// static. `tone` colors the number + icon (red/amber/blue, else neutral).
const METRICS = [
  { key: "reposOnline", label: "repos online", icon: "folder_open", tone: "" },
  { key: "prsOpen", label: "PRs open", icon: "merge", tone: "", deferrable: true },
  { key: "needsYou", label: "need you", icon: "pan_tool", tone: "amber", filter: "needs" },
  { key: "running", label: "running", icon: "bolt", tone: "blue", filter: "running" },
  { key: "inReview", label: "in review", icon: "rate_review", tone: "amber", filter: "review" },
  { key: "inConflict", label: "in conflict", icon: "warning", tone: "red", filter: "conflict" },
];

// View state persists across SSE snapshots so a re-render never clobbers the
// operator's current layout / filter selection.
const state = {
  view: "board", // "board" | "kanban"
  filter: "all", // "all" | "needs" | "running" | "review" | "conflict"
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

  return {
    reposOnline: reposOnline.size,
    haveCounts,
    prsOpen,
    workersTotal: runningWorkers.length,
    workersBusy: busy,
    // Consistent with the `needs` filter (stuck OR in-error), not raw stuck.
    needsYou: rows.filter(needsYou).length,
    running: rows.filter((r) => r.state === "active").length,
    inReview: rows.filter((r) => r.stage === "In Review").length,
    inConflict: rows.filter((r) => r.stage === "Conflicts").length,
  };
}

// ---- Filtering --------------------------------------------------------------

// Rows passing the active metric filter. Repo filter is applied separately so
// the repo sidebar counts reflect the metric selection without zeroing
// themselves out (faceted search).
function metricFiltered(rows) {
  if (state.filter === "all") return rows;
  return rows.filter((r) => matchesFilter(r, state.filter));
}

// The fully composed set: metric filter AND the repo filter.
function filteredRows(rows) {
  let out = metricFiltered(rows);
  if (state.repo) out = out.filter((r) => r.repo === state.repo);
  return out;
}

// `owner/name` → `name` for compact display; full repo goes in a `title`.
function repoShort(repo) {
  if (!repo) return "—";
  const parts = String(repo).split("/");
  return parts[parts.length - 1] || String(repo);
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

// Tall worker-pool card: an 8-col dot grid (busy = accent, idle = recessed)
// above the busy/total headline + "workers active" label.
function poolCard(busy, total) {
  const card = el("div", "fleet-pool");
  const grid = el("div", "fleet-pool-grid");
  for (let i = 0; i < total; i++) {
    grid.appendChild(el("span", `fleet-pool-dot ${i < busy ? "busy" : "idle"}`));
  }
  card.appendChild(grid);
  if (!total) {
    card.appendChild(el("div", "fleet-pool-note", "no workers online"));
    return card;
  }
  const count = el("div", "fleet-pool-count", String(busy));
  count.appendChild(el("span", "fleet-pool-total", `/${total}`));
  card.appendChild(count);
  card.appendChild(el("div", "fleet-pool-label", "workers active"));
  return card;
}

// One metric card. The four state metrics (those with `filter`) toggle the
// worklist filter; the active one gets the accent ring + soft fill.
function metricCard(metric, value, deferred) {
  const card = el("div", `fleet-metric${metric.tone ? ` tone-${metric.tone}` : ""}`);
  const isFilter = Boolean(metric.filter);
  const active = isFilter && state.filter === metric.filter;
  if (isFilter) {
    card.classList.add("clickable");
    if (active) card.classList.add("active");
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-pressed", String(active));
    card.title = active ? "Showing only — click to clear" : "Filter the worklist";
    const toggle = () => {
      state.filter = active ? "all" : metric.filter;
      draw();
    };
    card.addEventListener("click", toggle);
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });
  }
  const top = el("div", "fleet-metric-top");
  const n = el("span", "fleet-metric-n", deferred ? "—" : String(value));
  if (deferred) {
    n.classList.add("fleet-metric-deferred");
    n.title = "Awaiting backend feed";
  }
  top.appendChild(n);
  const ico = icon(metric.icon);
  ico.classList.add("fleet-metric-ico");
  top.appendChild(ico);
  card.appendChild(top);
  card.appendChild(el("span", "fleet-metric-label", metric.label));
  return card;
}

function renderStatStrip(snapshot, rows) {
  const host = document.getElementById("fleet-stats");
  if (!host) return;
  const s = buildStats(snapshot, rows);
  host.innerHTML = "";
  host.appendChild(poolCard(s.workersBusy, s.workersTotal));
  const grid = el("div", "fleet-metrics");
  for (const m of METRICS) {
    // Open counts come from GitHub state; defer to `—` only when no repo feed.
    const deferred = Boolean(m.deferrable) && !s.haveCounts;
    grid.appendChild(metricCard(m, s[m.key], deferred));
  }
  host.appendChild(grid);
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

// A repo tag: folder glyph + short repo name, full repo in the title.
function repoTag(repo) {
  const tag = el("span", "tag neutral");
  const ico = icon("folder");
  ico.classList.add("tag-ico");
  tag.appendChild(ico);
  tag.appendChild(document.createTextNode(repoShort(repo)));
  if (repo) tag.title = repo;
  return tag;
}

// Columns: Worker · Issue · Repo · Stage · Skill running · Updated · action.
// The bot worker identity isn't surfaced in the snapshot (one bot per repo), so
// the Worker cell reuses the existing `trixy` avatar convention rather than the
// mock's illustrative `tx-NN` / `capo` sample names (#218).
function boardRow(row) {
  const tr = el("tr", "fleet-row");
  clickable(tr, row);

  const worker = el("td", null);
  worker.dataset.label = "Worker";
  const wrap = el("div", "fleet-worker");
  wrap.appendChild(el("span", "avatar trixy", "TX"));
  wrap.appendChild(el("span", "fleet-worker-name", "trixy"));
  worker.appendChild(wrap);
  tr.appendChild(worker);

  const issue = el("td", "fleet-issue-cell");
  issue.dataset.label = "Issue";
  issue.appendChild(el("span", "fleet-num", `#${row.number}`));
  issue.appendChild(document.createTextNode(" "));
  issue.appendChild(el("span", "fleet-issue-title", row.title));
  tr.appendChild(issue);

  const repo = el("td", null);
  repo.dataset.label = "Repo";
  repo.appendChild(repoTag(row.repo));
  tr.appendChild(repo);

  const stage = el("td", null);
  stage.dataset.label = "Stage";
  stage.appendChild(stageTag(row.stage));
  tr.appendChild(stage);

  const skill = el("td", "fleet-skill", skillFor(row));
  skill.dataset.label = "Skill running";
  tr.appendChild(skill);

  const updated = el("td", "fleet-updated", relTime(row.lastUpdate));
  updated.dataset.label = "Updated";
  tr.appendChild(updated);

  // Review-action: a Review button when the row needs a human, else a chevron.
  const action = el("td", "fleet-action-cell");
  action.dataset.label = "";
  if (needsYou(row)) {
    const btn = el("button", "ld-btn danger sm");
    btn.type = "button";
    btn.appendChild(document.createTextNode("Review"));
    btn.appendChild(icon("arrow_forward"));
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      goPipeline(row);
    });
    action.appendChild(btn);
  } else {
    action.appendChild(icon("chevron_right"));
  }
  tr.appendChild(action);

  return tr;
}

// Board card header: "N workers" (no filter), or the "N workers / M shown"
// dual-count when a metric filter is active so the total-vs-visible context
// survives filtering, plus a removable chip naming the filter (clears to "all").
function renderBoardHead(shown, total) {
  const host = document.getElementById("fleet-board-head");
  if (!host) return;
  host.innerHTML = "";
  const count = el("span", "fleet-board-count");
  count.appendChild(el("span", "fleet-board-count-n", String(total)));
  count.appendChild(document.createTextNode(" workers"));
  if (state.filter !== "all") {
    count.appendChild(document.createTextNode(" / "));
    count.appendChild(el("span", "fleet-board-count-n", String(shown.length)));
    count.appendChild(document.createTextNode(" shown"));
  }
  host.appendChild(count);
  if (state.filter !== "all") {
    host.appendChild(chip(FILTER_LABEL[state.filter] || state.filter, () => {
      state.filter = "all";
      draw();
    }));
  }
}

function renderBoard(shown, totalRows) {
  const tbody = document.querySelector("#fleet-board .fleet-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!shown.length) {
    const tr = el("tr");
    const td = el("td", "fleet-empty-row",
      totalRows === 0 ? "No active pipelines yet." : "No items match this filter.");
    td.colSpan = 7;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const r of shown) tbody.appendChild(boardRow(r));
}

// ---- Mobile triage ----------------------------------------------------------

// The mobile "Needs your call" triage list (#227): a pinned, always-present
// priority subset of the worklist, shown only at phone widths (CSS owns the
// breakpoint — see .fleet-triage in app.css). It reuses the exact `needsYou`
// predicate that drives the board's Review button + the "need you" metric, so
// the three never diverge. It honours the active repo filter (faceted, like the
// rest of Fleet) but is deliberately INDEPENDENT of the metric-card selection:
// it's a fixed triage surface, not a filtered view, so it stays put while the
// operator pivots the board. Each card taps through to the Issue ▸ PR detail.
function triageCard(row) {
  // A native <button> already carries role + keyboard activation, so wire the
  // click directly rather than via clickable() (which is for non-button nodes).
  const card = el("button", "triage-card");
  card.type = "button";
  card.addEventListener("click", () => goPipeline(row));

  const title = el("div", "triage-title");
  title.appendChild(el("span", "fleet-num", `#${row.number}`));
  title.appendChild(document.createTextNode(" "));
  title.appendChild(el("span", "triage-title-text", row.title));
  card.appendChild(title);

  const meta = el("div", "triage-meta");
  meta.appendChild(stageTag(row.stage));
  meta.appendChild(el("span", "triage-where", `${repoShort(row.repo)} · ${relTime(row.lastUpdate)}`));
  card.appendChild(meta);

  return card;
}

function renderTriage(rows) {
  const host = document.getElementById("fleet-triage");
  if (!host) return;
  // Pass the FULL pre-filter rows; the triage list owns its own filtering so a
  // metric-card selection never empties it. Repo filter still applies (faceted).
  let items = rows.filter(needsYou);
  if (state.repo) items = items.filter((r) => r.repo === state.repo);

  host.innerHTML = "";
  host.hidden = false; // CSS owns visibility; clear the JS-disabled fallback.
  host.appendChild(el("div", "eyebrow", "Needs your call"));
  if (!items.length) {
    host.appendChild(el("p", "triage-empty", "Nothing needs you right now."));
    return;
  }
  const list = el("div", "triage-list");
  for (const r of items) list.appendChild(triageCard(r));
  host.appendChild(list);
}

// ---- Kanban -----------------------------------------------------------------

// Kanban card: a needs-you tag + the issue number up top, the title, then a
// repo tag and the PR# + worker avatar. PR# is only known for `pr`-kind rows
// (the number *is* the PR); issue rows know a PR exists but not its number, so
// it's omitted there.
function kanbanCard(row) {
  const card = el("div", "fleet-card");
  if (needsYou(row)) card.classList.add("needs");
  clickable(card, row);

  const head = el("div", "fleet-card-head");
  head.appendChild(el("span", "fleet-num", `#${row.number}`));
  if (needsYou(row)) head.appendChild(el("span", "tag red", "needs you"));
  card.appendChild(head);

  card.appendChild(el("div", "fleet-card-title", row.title));

  const meta = el("div", "fleet-card-meta");
  meta.appendChild(repoTag(row.repo));
  const actor = el("div", "fleet-card-actor");
  if (row.kind === "pr") actor.appendChild(el("span", "fleet-card-pr", `PR #${row.number}`));
  actor.appendChild(el("span", "avatar trixy", "TX"));
  meta.appendChild(actor);
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
  // Counts reflect the metric filter but not the repo filter (faceted search).
  const base = metricFiltered(rows);
  const counts = new Map();
  for (const r of base) {
    if (!r.repo) continue;
    counts.set(r.repo, (counts.get(r.repo) || 0) + 1);
  }
  const repos = [...counts.keys()].sort((a, b) => a.localeCompare(b));

  host.innerHTML = "";
  const head = el("div", "fleet-repos-head");
  head.appendChild(el("span", "eyebrow", "Filter by repo"));
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
  } else {
    const list = el("div", "fleet-repo-list");
    for (const repo of repos) {
      const btn = el("button", "fleet-repo");
      btn.type = "button";
      if (repo === state.repo) {
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
      } else {
        btn.setAttribute("aria-pressed", "false");
      }
      btn.appendChild(el("span", "fleet-repo-name", repoShort(repo)));
      btn.title = repo;
      btn.appendChild(el("span", "fleet-repo-count", String(counts.get(repo))));
      btn.addEventListener("click", () => {
        state.repo = state.repo === repo ? null : repo;
        draw();
      });
      list.appendChild(btn);
    }
    host.appendChild(list);
  }

  // "Connect repo" (#240): the mock shows an outline button beneath the repo
  // list. There is no connect-repo endpoint yet, so this is a disabled stub with
  // the same honest tooltip as the Live sidebar's "+ Connect repo" affordance —
  // matching the project's honest-placeholder convention.
  const connect = el("button", "ld-btn sm outline fleet-repos-connect", "Connect repo");
  connect.type = "button";
  connect.disabled = true;
  connect.title = "No endpoint yet — follow-up";
  connect.prepend(icon("add"));
  host.appendChild(connect);

  // The explanatory note: repos filter the worklist; they don't navigate.
  const note = el("p", "fleet-repos-note");
  note.appendChild(document.createTextNode("Repos filter the worklist — they don't navigate. Open a repo's session from "));
  note.appendChild(el("strong", null, "Live"));
  note.appendChild(document.createTextNode("."));
  host.appendChild(note);
}

// ---- Filter chip ------------------------------------------------------------

// A removable filter chip (used in the board header for the active metric
// filter). `onRemove` clears the filter.
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

// ---- Control state sync -----------------------------------------------------

// Sync the view toggle's active state. The metric-filter active state is
// rebuilt inside renderStatStrip on every draw, so it needs no sync here.
function syncControls() {
  document.querySelectorAll("#fleet-view [data-fleet-view]").forEach((btn) => {
    const on = btn.dataset.fleetView === state.view;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  });
}

// ---- Draw -------------------------------------------------------------------

function draw() {
  if (!state.snapshot) return;
  const rows = buildRows(state.snapshot);
  const shown = filteredRows(rows);

  renderStatStrip(state.snapshot, rows);
  syncControls();
  renderRepoSidebar(rows);
  // Mobile-only triage list (#227): pass the full pre-filter rows so a
  // metric-card selection never empties it. CSS shows it at phone widths only.
  renderTriage(rows);

  const board = document.getElementById("fleet-board");
  const kanban = document.getElementById("fleet-kanban");
  if (board) board.hidden = state.view !== "board";
  if (kanban) kanban.hidden = state.view !== "kanban";

  if (state.view === "board") {
    renderBoardHead(shown, rows.length);
    renderBoard(shown, rows.length);
  } else {
    renderKanban(shown);
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
}

// Re-entrant on every SSE snapshot; preserves the current view/filter state.
export function render(snapshot) {
  state.snapshot = snapshot;
  draw();
}
