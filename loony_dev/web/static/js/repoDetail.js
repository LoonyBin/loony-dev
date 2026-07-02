"use strict";

// Live screen (#158 → restyled by #189 → promoted to a primary nav destination
// by #221): the always-on per-repo main-branch session surface. Three regions —
// header quick-actions, a center session panel (embedding the #157 join-link/QR
// card, the folded-in remote-control session surface, and a live worker
// transcript), and a ~284px right sidebar (Repos switcher, Repo context, Recent
// commits, Workers here) — over a secondary block of worktrees and scoped stuck
// processes.
//
// As a primary destination Live is reachable at bare `#live` (no repo): show()
// then resolves a default repo — last-viewed (localStorage) → first discovered
// repo from the snapshot → an explicit "pick a repo" state — and navigates to it
// exactly once (see the single-goRepo guard in show()/update()).
//
// Navigation is driven by the Alpine store: app.js's wireDetailViews() effect
// calls show(repo|null) whenever the active view/repo changes (issue #239 moved
// this off a load-order-fragile inline x-effect). Consolidated state arrives via
// update(state) from the app-shell orchestrator (the #155 /api/events stream);
// we filter it to the current repo. The worker transcript renders the repo's
// structured agent activity (the #270 fleet feed, filtered to this repo) as
// conversation-style turns via streamActivity() (#259).
//
// Graceful-degradation contract (#189): every element with no backing snapshot
// data is visibly non-functional — a disabled .ld-btn with a tooltip, an
// external GitHub deep-link, or an explicit "—"/"unavailable" line — never a
// fabricated value. #224 wired the previously-degraded items to real data:
// open issue/PR counts (the #219 GitHub feed), recent commits (a real local
// `git log` via /api/repos/.../commits), and per-pipeline workers (issue# +
// stage from the feed). The still-degraded items (spin-up worker, resync main,
// connect repo, last sync, the disabled steer bar) remain honest placeholders.

import { cell, setRows, formatAge, icon, goRepo, goPipeline, stageTone } from "./dom.js";
// Shared design-system components (static/ds/) — single source of truth (#258 / Phase 4).
import { StatePill, Tag, Avatar, Btn } from "/static/ds/components/primitives.js";
import { interruptSession } from "./overview.js";
import { streamActivity } from "./logs.js";
import { renderSessionCard } from "./sessions.js";

let current = null; // repo currently displayed ("owner/name"), or null
let lastState = null; // latest consolidated snapshot {workers, worktrees, sessions, stuck}
let stopActivity = null; // stop fn for the sidebar Activity-timeline stream (#282)

// Persisted last-viewed repo, so re-opening bare #live lands where you left off.
const LAST_REPO_KEY = "loony-live-repo";
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } };

// Is the Live primary destination currently active? Used to tell a bare-#live
// show(null) (resolve a default repo) apart from a navigate-away show(null).
function isLiveView() {
  const store = window.Alpine && window.Alpine.store("app");
  return !!(store && store.view === "live");
}

// The discovered-repo set from a snapshot (workers + worktrees + sessions),
// sorted. Mirrors renderReposList's union without seeding a current repo.
function discoveredRepos(state) {
  return [...new Set([
    ...((state ? state.workers : []) || []).map((w) => w.repo),
    ...((state ? state.worktrees : []) || []).map((w) => w.repo),
    ...((state ? state.sessions : []) || []).map((s) => s.repo),
  ])].filter(Boolean).sort((a, b) => a.localeCompare(b));
}

// Resolve a default repo for bare #live: the last-viewed repo (trusted before
// the first snapshot, validated against the discovered set after), else the
// first discovered repo, else null (no repos discovered yet).
function resolveDefaultRepo() {
  const discovered = discoveredRepos(lastState);
  const last = lsGet(LAST_REPO_KEY);
  if (last && (discovered.length === 0 || discovered.includes(last))) return last;
  return discovered[0] || null;
}

function muted(text) {
  const p = document.createElement("p");
  p.className = "muted";
  p.textContent = text;
  return p;
}

// Seconds since an ISO-8601 timestamp, or null if absent/unparseable.
function ageSeconds(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

// An external GitHub anchor styled as an .ld-btn. Always opens in a new tab and
// drops the opener so the dashboard tab can't be navigated by the target.
function ghButton(label, href, variant) {
  // Link-button via the shared factory: an <a class="ld-btn"> (rel=noopener
  // noreferrer is applied automatically for target=_blank).
  return Btn({ variant, size: "sm", label, href, target: "_blank" });
}

// A disabled-with-tooltip .ld-btn for an action with no backend yet.
function pendingButton(label, why) {
  const b = Btn({ variant: "ghost", size: "sm", label, attrs: { disabled: "" } });
  b.title = why;
  return b;
}

// A small Tag chip via the shared factory (stage→tone is the canonical
// stageTone() in dom.js, shared with Fleet).
function tag(text, tone) {
  return Tag({ tone: tone || "neutral", label: text });
}

// Header quick-actions: live deep-links where a zero-backend GitHub URL exists,
// disabled-with-tooltip placeholders where they'd need new backend (#189 forbids
// building it here). Order mirrors the issue: New issue · Spin up worker ·
// Search code · Resync main.
function renderQuickActions(repo) {
  const bar = document.getElementById("repo-quick-actions");
  if (!bar) return;
  bar.innerHTML = "";
  const base = `https://github.com/${repo}`;
  bar.appendChild(ghButton("New issue", `${base}/issues/new`, "soft"));
  bar.appendChild(pendingButton("Spin up worker", "No endpoint yet — follow-up"));
  bar.appendChild(ghButton("Search code", `${base}/search?type=code`, "outline"));
  bar.appendChild(pendingButton("Resync main", "No endpoint yet — follow-up"));
}

// Map the remote-control server's health (#304) into a .statepill indicator.
// `status` is authoritative (running / restarting / errored); `alive` is the
// fallback for a connection file that predates the status field.
function sessionLiveness(s) {
  if (!s) return StatePill({ tone: "red", label: "no server" });
  switch (s.status) {
    case "running": return StatePill({ tone: "accent", label: "running" });
    case "restarting": return StatePill({ tone: "amber", label: "restarting" });
    case "errored": return StatePill({ tone: "red", label: "errored" });
    default:
      if (s.alive === false) return StatePill({ tone: "red", label: "offline" });
      if (s.alive === true) return StatePill({ tone: "accent", label: "running" });
      return StatePill({ tone: "amber", label: "unknown" });
  }
}

// One labelled meta line ("Key  loony-x") for the session panel header.
function metaLine(label, value, mono) {
  const row = document.createElement("div");
  row.className = "live-meta-row";
  const k = document.createElement("span");
  k.className = "eyebrow";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = mono ? "live-meta-val mono" : "live-meta-val";
  v.textContent = value == null || value === "" ? "—" : String(value);
  row.append(k, v);
  return row;
}

function renderSession(repo, state) {
  const titleEl = document.getElementById("repo-session-title");
  const liveEl = document.getElementById("repo-session-liveness");
  const metaEl = document.getElementById("repo-session-meta");
  const body = document.getElementById("repo-session-body");
  if (!body) return;
  const s = ((state ? state.sessions : []) || []).find((x) => x.repo === repo);

  if (titleEl) titleEl.textContent = repo;
  if (liveEl) {
    liveEl.innerHTML = "";
    liveEl.appendChild(sessionLiveness(s));
  }
  if (metaEl) {
    metaEl.innerHTML = "";
    if (s) {
      if (s.status) metaEl.appendChild(metaLine("Status", s.status, false));
      const age = ageSeconds(s.updated_at);
      if (age != null) metaEl.appendChild(metaLine("Updated", `${formatAge(age)} ago`, false));
    }
  }

  body.innerHTML = "";
  if (!s) {
    body.appendChild(muted("No remote-control server for this repo yet."));
    return;
  }
  // Embed the shared server-health card (#304, compact: the panel header already
  // names the repo + health). It explains that on-demand sessions are created
  // from claude.ai/code, and surfaces restarting/errored states.
  body.appendChild(renderSessionCard(s, { compact: true }));
}

// Repos switcher: recompute the discovered-repo set locally from the snapshot
// (workers + worktrees + sessions) — never reach into #187's store internals.
// Sessions are included so a repo with a live session but no worker/worktree
// still appears; the current repo is seeded so a deep-link to a session-only
// repo isn't dropped. Each row switches repo via goRepo(); the active is flagged.
function renderReposList(repo, state) {
  const box = document.getElementById("repo-repos-list");
  if (!box) return;
  box.innerHTML = "";
  const repos = [...new Set([
    repo,
    ...((state ? state.workers : []) || []).map((w) => w.repo),
    ...((state ? state.worktrees : []) || []).map((w) => w.repo),
    ...((state ? state.sessions : []) || []).map((s) => s.repo),
  ])]
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b));

  if (!repos.length) {
    box.appendChild(muted("No repos discovered."));
    return;
  }
  const workers = (state ? state.workers : []) || [];
  for (const r of repos) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "repo-switch-row";
    if (r === repo) {
      btn.classList.add("active");
      btn.setAttribute("aria-current", "true");
    }
    // design Repos row: folder icon + name (left) + worker count (right)
    const left = document.createElement("span");
    left.className = "repo-switch-label";
    const fi = icon("folder");
    fi.classList.add("repo-switch-icon");
    left.appendChild(fi);
    const name = document.createElement("span");
    name.className = "repo-switch-name";
    name.textContent = r;
    left.appendChild(name);
    btn.appendChild(left);
    const count = document.createElement("span");
    count.className = "repo-switch-count tnum";
    count.textContent = String(workers.filter((w) => w.repo === r).length);
    btn.appendChild(count);
    btn.addEventListener("click", () => goRepo(r));
    box.appendChild(btn);
  }
}

function stat(label, value) {
  const wrap = document.createElement("div");
  wrap.className = "stat";
  const l = document.createElement("span");
  l.className = "stat-label";
  l.textContent = label;
  const v = document.createElement("span");
  v.className = "stat-value";
  v.textContent = String(value);
  wrap.append(l, v);
  return wrap;
}

// Active worker sessions in this repo, joined to the #219 GitHub pipeline feed
// for issue# + stage. Deduped by pipeline_key (the per-pipeline identity, #181)
// so the multiple phase sessions of one issue count once; falls back to task_key
// when a session predates the pipeline_key field. Each row carries the task_key
// for click-through to the Issue ▸ PR detail. This is the honest "an agent is
// working this pipeline" signal — distinct from the one-per-repo OS worker.
function repoWorkerRows(repo, state) {
  const sessions = ((state ? state.task_sessions : []) || []).filter((s) => s.repo === repo);
  const pipelines = (state ? state.pipelines : []) || [];
  const byKey = new Map();
  for (const s of sessions) {
    const key = s.pipeline_key || s.task_key;
    if (!key || byKey.has(key)) continue;
    const pipe = s.pipeline_key
      ? pipelines.find((p) => p.pipeline_key === s.pipeline_key && p.repo === repo)
      : null;
    byKey.set(key, {
      task_key: s.task_key,
      pipeline_key: s.pipeline_key || null,
      number: pipe ? pipe.number : null,
      stage: pipe ? pipe.stage : null,
    });
  }
  return [...byKey.values()];
}

// Repo context: the facts the snapshot actually carries (branch + HEAD from the
// main non-detached worktree, worktree count) plus the #219 GitHub feed's open
// issue/PR counts and the per-pipeline worker count. A missing/failed repo feed
// entry falls back to the honest GitHub deep-link so no count is fabricated;
// last sync stays a "—" placeholder (no snapshot field yet).
function renderContext(repo, state) {
  const box = document.getElementById("repo-context");
  if (!box) return;
  box.innerHTML = "";
  const worktrees = ((state ? state.worktrees : []) || []).filter((w) => w.repo === repo);
  const main = worktrees.find((w) => !w.detached) || worktrees[0];
  const repoFeed = ((state ? state.repos : []) || []).find((r) => r.repo === repo);
  const workerCount = repoWorkerRows(repo, state).length;

  const stats = document.createElement("div");
  stats.className = "context-stats";
  stats.appendChild(stat("Workers here", workerCount));
  stats.appendChild(stat("Worktrees", worktrees.length));
  box.appendChild(stats);

  const dl = document.createElement("dl");
  dl.className = "kv";
  const add = (label, value, valueClass) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = value == null || value === "" ? "—" : String(value);
    if (valueClass) dd.className = valueClass;
    dl.append(dt, dd);
  };
  add("Branch", main ? (main.detached ? "(detached)" : main.branch) : "—");
  add("HEAD", main && main.head ? main.head.slice(0, 10) : "—", "mono");

  // Open counts come from the #219 feed when the repo's fetch succeeded; a
  // missing entry or a failed fetch (ok=false / null count) degrades to the
  // GitHub deep-link rather than fabricating a number.
  const countOrLink = (count, href) => {
    if (count != null) {
      const span = document.createElement("span");
      span.className = "b tnum";
      span.textContent = String(count);
      return span;
    }
    const link = ghButton("View on GitHub ", href, "ghost");
    link.appendChild(icon("open_in_new"));
    return link;
  };
  const feedOk = repoFeed && repoFeed.ok;
  add("Open issues", countOrLink(feedOk ? repoFeed.open_issues : null, `https://github.com/${repo}/issues`));
  add("Open PRs", countOrLink(feedOk ? repoFeed.open_prs : null, `https://github.com/${repo}/pulls`));

  const lastSync = document.createElement("span");
  lastSync.textContent = "—";
  lastSync.title = "No last-sync timestamp in the snapshot — follow-up";
  add("Last sync", lastSync);
  box.appendChild(dl);
}

// Recent commits: a real local `git log` from the repo's base checkout, fetched
// on demand from /api/repos/{owner}/{repo}/commits (the snapshot deliberately
// omits per-repo logs). Called once per repo switch from show() — not on every
// 2s snapshot — so it never spams git. A stale response (the repo switched
// mid-fetch) is dropped, and an empty/failed fetch keeps an honest note.
async function loadCommits(repo) {
  const box = document.getElementById("repo-commits");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(muted("Loading commits…"));
  let commits = [];
  try {
    const resp = await fetch(`/api/repos/${repo}/commits`);
    if (resp.ok) commits = (await resp.json()).commits || [];
  } catch (e) {
    commits = [];
  }
  // The repo switched while we were fetching — discard this now-stale result.
  if (repo !== current) return;
  box.innerHTML = "";
  if (!commits.length) {
    const note = muted("Commit history unavailable — open the repo on GitHub.");
    note.title = "No local git log for this checkout (or git unavailable).";
    box.appendChild(note);
    return;
  }
  for (const c of commits) {
    const row = document.createElement("div");
    row.className = "commit-row";
    row.title = c.subject;

    const sha = document.createElement("span");
    sha.className = "mono commit-sha";
    sha.textContent = c.short_sha;

    const mid = document.createElement("div");
    mid.className = "commit-mid";
    const subject = document.createElement("span");
    subject.className = "commit-subject";
    subject.textContent = c.subject;
    const meta = document.createElement("span");
    meta.className = "commit-meta muted";
    meta.textContent = `${c.author} · ${c.rel_date}`;
    mid.append(subject, meta);

    row.append(sha, mid);
    box.appendChild(row);
  }
}

// Workers in this repo: one row per per-pipeline worker session (deduped), each
// a trixy avatar + the issue/PR number + its lifecycle stage chip, click-through
// to the Issue ▸ PR detail (#190). Sourced from the per-task sessions joined to
// the #219 GitHub feed (issue# + stage) rather than the one-per-repo OS worker.
// A session whose pipeline isn't in the feed still renders with its number /
// pipeline_key / task_key; an empty set reads "none right now".
function renderWorkers(repo, state) {
  const box = document.getElementById("repo-workers");
  if (!box) return;
  box.innerHTML = "";
  const rows = repoWorkerRows(repo, state);
  if (!rows.length) {
    box.appendChild(muted("none right now"));
    return;
  }
  for (const r of rows) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "worker-row clickrow";
    row.addEventListener("click", () => goPipeline(current, r.task_key));

    const avatar = Avatar({ tone: "soft", glyph: "T" });
    avatar.title = "trixy";
    row.appendChild(avatar);

    const mid = document.createElement("div");
    mid.className = "worker-mid";
    const name = document.createElement("span");
    name.className = "worker-name";
    // Prefer the GitHub issue/PR number; fall back to the pipeline/task key.
    name.textContent = r.number != null
      ? `#${r.number}`
      : (r.pipeline_key || r.task_key);
    mid.appendChild(name);
    row.appendChild(mid);

    if (r.stage) row.appendChild(tag(r.stage, stageTone(r.stage)));

    box.appendChild(row);
  }
}

function renderWorktreeRow(w) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(w.detached ? "(detached)" : w.branch, "Branch"));
  tr.appendChild(cell(w.head ? w.head.slice(0, 10) : "", "HEAD"));
  tr.appendChild(cell(w.path, "Path"));
  return tr;
}

// Heartbeat-derived stuck rows (#270): no pid/cmdline, so no Kill — Interrupt
// (ESC via session_id) is the intervention. Mirrors overview.renderStuckRow.
function renderStuckRow(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.task_key, "Task"));
  tr.appendChild(cell(formatAge(s.age_seconds), "Age"));
  tr.appendChild(cell(s.blocked_on, "Blocked on"));

  const actionTd = document.createElement("td");
  actionTd.dataset.label = "Action";

  // Interrupt (ESC) is primary — the default nudge. Disabled when the owning
  // session has no control channel advertised yet (nothing to ESC).
  const interruptBtn = document.createElement("button");
  interruptBtn.type = "button";
  interruptBtn.className = "interrupt-btn";
  interruptBtn.textContent = "Interrupt";
  if (s.session_id) {
    interruptBtn.title = "Send ESC to abort the in-flight turn (session stays alive)";
    interruptBtn.addEventListener("click", () => interruptSession(s.session_id));
  } else {
    interruptBtn.disabled = true;
    interruptBtn.title = "No control channel for this session yet";
  }
  actionTd.appendChild(interruptBtn);

  tr.appendChild(actionTd);
  return tr;
}

function renderAll() {
  const repo = current;
  if (!repo) return;
  renderQuickActions(repo);
  renderSession(repo, lastState);
  renderReposList(repo, lastState);
  renderContext(repo, lastState);
  renderWorkers(repo, lastState);

  const worktrees = (lastState ? lastState.worktrees : []).filter((w) => w.repo === repo);
  setRows("repo-worktrees", worktrees, renderWorktreeRow, "No worktrees found.");

  const stuck = (lastState ? lastState.stuck : []).filter((s) => s.worker_repo === repo);
  const section = document.getElementById("repo-stuck-section");
  if (section) section.hidden = stuck.length === 0;
  setRows("repo-stuck", stuck, renderStuckRow, "");
}

// The "pick a repo" state for bare #live with no repo resolved yet: keep the
// switcher live (so repos can be picked as they appear) and explain the empty
// session panel rather than leaving stale content.
function renderEmptyLive() {
  const bar = document.getElementById("repo-quick-actions");
  if (bar) bar.innerHTML = ""; // no repo → no per-repo deep-links
  renderReposList(null, lastState);
  const titleS = document.getElementById("repo-session-title");
  if (titleS) titleS.textContent = "";
  const liveEl = document.getElementById("repo-session-liveness");
  if (liveEl) liveEl.innerHTML = "";
  const metaEl = document.getElementById("repo-session-meta");
  if (metaEl) metaEl.innerHTML = "";
  // No repo → clear the per-repo panels so stale content from a prior repo
  // doesn't linger behind the "pick a repo" prompt.
  const commitsEl = document.getElementById("repo-commits");
  if (commitsEl) commitsEl.innerHTML = "";
  const contextEl = document.getElementById("repo-context");
  if (contextEl) contextEl.innerHTML = "";
  const workersEl = document.getElementById("repo-workers");
  if (workersEl) workersEl.innerHTML = "";
  // The streams are already torn down by show(); also wipe their panels (the
  // base-session conversation and the sidebar Activity timeline, #282) so a
  // prior repo's turns/timeline don't linger behind the "pick a repo" prompt.
  const logTitle = document.getElementById("repo-log-title");
  if (logTitle) logTitle.textContent = "";
  const logHost = document.getElementById("repo-log");
  if (logHost) logHost.innerHTML = "";
  const timelineHost = document.getElementById("live-timeline");
  if (timelineHost) timelineHost.innerHTML = "";
  const body = document.getElementById("repo-session-body");
  if (body) {
    body.innerHTML = "";
    body.appendChild(muted(discoveredRepos(lastState).length
      ? "Pick a repo from the Repos list to open its Live screen."
      : "No repos discovered yet."));
  }
}

// Called by app.js's wireDetailViews() effect when the active view/repo changes.
// Manages the (single) log stream and triggers a render. Idempotent for the same
// repo so a re-render never restarts the log tail.
function show(repo) {
  // Bare #live (no repo) on the Live destination: resolve a default repo and
  // navigate to it exactly once. goRepo() re-fires the effect → show(default),
  // which falls through to a normal render below; guarding on `def !== current`
  // keeps it to a single navigation. A show(null) that's really a navigate-away
  // (view is no longer 'live') skips this and tears down instead.
  if (!repo && isLiveView()) {
    const def = resolveDefaultRepo();
    if (def && def !== current) { goRepo(def); return; }
    // Already on the default repo: adopt it so the `repo === current` early-out
    // below keeps the active streams alive instead of tearing them down for a
    // re-fire that didn't actually change repo.
    if (def) repo = def;
  }
  if (repo === current) return;
  // One live stream per Live view: the sidebar Activity timeline (bare stop fn).
  // Tear it down on every repo switch / navigate-away so the socket never leaks.
  // (The #repo-log base-session conversation stream was removed in #304.)
  if (stopActivity) {
    stopActivity();
    stopActivity = null;
  }
  current = repo || null;
  if (!current) {
    if (isLiveView()) renderEmptyLive(); // bare #live, no default yet
    return; // otherwise navigated away from the Live view
  }
  lsSet(LAST_REPO_KEY, current);

  renderAll();
  // Commits are a per-repo on-demand fetch (not snapshot-driven), so kick it off
  // once here on the repo switch rather than from renderAll's per-tick path.
  loadCommits(current);
  // #repo-log used to stream the always-on base session's live conversation, but
  // remote control is now a persistent `claude rc` server with no single followed
  // session (#304): on-demand sessions run in claude.ai/code. Show a static
  // pointer instead of a live transcript; the structured activity feed remains in
  // the sidebar "Activity timeline" card.
  const host = document.getElementById("repo-log");
  const title = document.getElementById("repo-log-title");
  if (title) title.textContent = `— ${repo}`;
  if (host) {
    host.innerHTML = "";
    host.appendChild(muted(
      "On-demand remote-control sessions run in claude.ai/code — there is no live "
      + "session conversation to show here."));
  }
  const timeline = document.getElementById("live-timeline");
  if (timeline) stopActivity = streamActivity(repo, timeline);
}

// Fed the consolidated snapshot by the orchestrator; re-render only while a
// repo detail page is open. Never touches the log stream.
export function update(state) {
  lastState = state;
  if (current) { renderAll(); return; }
  // Bare #live opened before any snapshot: now that repos may be discovered,
  // resolve the default once (same single-goRepo guard via current===null).
  if (isLiveView()) {
    const def = resolveDefaultRepo();
    if (def) { goRepo(def); return; }
    renderEmptyLive();
  }
}

export function init() {
  // Exposed on window so app.js's wireDetailViews() effect can drive show().
  window.repoDetail = { show };
  // Honour a deep link (#live/owner/name, or bare #live) if Alpine has booted.
  const store = window.Alpine && window.Alpine.store("app");
  if (store && store.view === "live") show(store.repo || null);
}
