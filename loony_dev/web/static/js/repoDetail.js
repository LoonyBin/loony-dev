"use strict";

// Per-repo Live screen (#158 → restyled by #189): the always-on per-repo
// main-branch session surface. Three regions — header quick-actions, a center
// session panel (embedding the #157 join-link/QR card), and a ~284px right
// sidebar (Repos switcher, Repo context, Recent commits, Workers here) — over a
// secondary block of worktrees, scoped stuck processes, and a live log tail.
//
// Navigation is driven by the Alpine store: index.html's `x-effect` calls
// show(repo|null) whenever the active view/repo changes. Consolidated state
// arrives via update(state) from the app-shell orchestrator (the #155
// /api/events stream); we filter it to the current repo. The log tail uses the
// existing /api/logs/{owner}/{repo}/stream endpoint via streamLog().
//
// Graceful-degradation contract (#189): every element with no backing snapshot
// data is visibly non-functional — a disabled .ld-btn with a tooltip, an
// external GitHub deep-link, or an explicit "—"/"unavailable" line — never a
// fabricated value. The degraded items (spin-up worker, resync main, connect
// repo, open issue/PR counts, last sync, recent-commits list) are follow-ups.

import { cell, setRows, formatAge, icon, goRepo } from "./dom.js";
import { killProcess, interruptSession } from "./overview.js";
import { streamLog } from "./logs.js";
import { renderSessionCard } from "./sessions.js";

let current = null; // repo currently displayed ("owner/name"), or null
let lastState = null; // latest consolidated snapshot {workers, worktrees, sessions, stuck}
let stopLog = null; // stop fn for the active log stream

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
  const a = document.createElement("a");
  a.className = `ld-btn sm ${variant}`;
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = label;
  return a;
}

// A disabled-with-tooltip .ld-btn for an action with no backend yet.
function pendingButton(label, why) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ld-btn sm ghost";
  b.textContent = label;
  b.disabled = true;
  b.title = why;
  return b;
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

// Map a session's liveness into a .statepill + .sdot indicator.
function sessionLiveness(s) {
  const pill = document.createElement("span");
  pill.className = "statepill";
  const dot = document.createElement("span");
  if (!s || s.alive === false) {
    dot.className = "sdot blocked";
    pill.append(dot, document.createTextNode(s ? "offline" : "no session"));
  } else if (s.alive === true) {
    dot.className = "sdot active";
    pill.append(dot, document.createTextNode("live"));
  } else {
    dot.className = "sdot review";
    pill.append(dot, document.createTextNode("unknown"));
  }
  return pill;
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
      metaEl.appendChild(metaLine("Key", s.key, true));
      if (s.mode) metaEl.appendChild(metaLine("Mode", s.mode, false));
      const age = ageSeconds(s.updated_at);
      if (age != null) metaEl.appendChild(metaLine("Updated", `${formatAge(age)} ago`, false));
    }
  }

  body.innerHTML = "";
  if (!s) {
    body.appendChild(muted("No active session for this repo yet."));
    return;
  }
  // Embed the shared #157 card (compact: the panel header already names the
  // repo + liveness). It supplies the join button, QR, and starting/offline/
  // stale states.
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
  for (const r of repos) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "repo-switch-row";
    if (r === repo) {
      btn.classList.add("active");
      btn.setAttribute("aria-current", "true");
    }
    const name = document.createElement("span");
    name.className = "repo-switch-name";
    name.textContent = r;
    btn.appendChild(name);
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

// Repo context: the facts the snapshot actually carries (branch + HEAD from the
// main non-detached worktree, worker/worktree counts) as honest values; the
// rest (open issues/PRs, last sync) as GitHub deep-links or "—" placeholders so
// no count is fabricated.
function renderContext(repo, state) {
  const box = document.getElementById("repo-context");
  if (!box) return;
  box.innerHTML = "";
  const workers = ((state ? state.workers : []) || []).filter((w) => w.repo === repo);
  const worktrees = ((state ? state.worktrees : []) || []).filter((w) => w.repo === repo);
  const main = worktrees.find((w) => !w.detached) || worktrees[0];

  const stats = document.createElement("div");
  stats.className = "context-stats";
  stats.appendChild(stat("Workers here", workers.length));
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

  // No snapshot field carries these — link out to GitHub rather than fabricate.
  const issuesLink = ghButton("View on GitHub ", `https://github.com/${repo}/issues`, "ghost");
  issuesLink.appendChild(icon("open_in_new"));
  add("Open issues", issuesLink);
  const prsLink = ghButton("View on GitHub ", `https://github.com/${repo}/pulls`, "ghost");
  prsLink.appendChild(icon("open_in_new"));
  add("Open PRs", prsLink);

  const lastSync = document.createElement("span");
  lastSync.textContent = "—";
  lastSync.title = "No last-sync timestamp in the snapshot — follow-up";
  add("Last sync", lastSync);
  box.appendChild(dl);
}

// Recent commits: the snapshot has no commit log, so we surface the one known
// commit pointer (the main worktree HEAD) and an explicit unavailable note
// rather than fabricating a list.
function renderCommits(repo, state) {
  const box = document.getElementById("repo-commits");
  if (!box) return;
  box.innerHTML = "";
  const worktrees = ((state ? state.worktrees : []) || []).filter((w) => w.repo === repo);
  const main = worktrees.find((w) => !w.detached) || worktrees[0];
  if (main && main.head) {
    const row = document.createElement("div");
    row.className = "commit-row";
    const sha = document.createElement("span");
    sha.className = "mono";
    sha.textContent = main.head.slice(0, 10);
    const tag = document.createElement("span");
    tag.className = "tag neutral";
    tag.textContent = "HEAD";
    row.append(tag, sha);
    box.appendChild(row);
  }
  const note = muted("Full commit history is unavailable here — open the repo on GitHub.");
  note.title = "No commit log in the snapshot — follow-up";
  box.appendChild(note);
}

// Map a worker status to an .sdot lifecycle hue.
function workerDot(status) {
  if (status === "running") return "sdot active";
  if (status === "stale") return "sdot gated";
  return "sdot review";
}

// Workers in this repo: an actor avatar (the bot is trixy), a statepill state
// indicator mapped from worker status, and the age since start. Richer stage
// tags + issue/PR-row linkage + multi-actor avatars need data the snapshot
// doesn't carry yet (follow-up).
function renderWorkers(repo, state) {
  const box = document.getElementById("repo-workers");
  if (!box) return;
  box.innerHTML = "";
  const workers = ((state ? state.workers : []) || []).filter((w) => w.repo === repo);
  if (!workers.length) {
    box.appendChild(muted("No workers in this repo."));
    return;
  }
  for (const w of workers) {
    const row = document.createElement("div");
    row.className = "worker-row";

    const avatar = document.createElement("span");
    avatar.className = "avatar trixy";
    avatar.textContent = "T";
    avatar.title = "trixy";
    row.appendChild(avatar);

    const mid = document.createElement("div");
    mid.className = "worker-mid";
    const name = document.createElement("span");
    name.className = "worker-name";
    name.textContent = w.pid != null ? `trixy · pid ${w.pid}` : "trixy";
    mid.appendChild(name);

    const pill = document.createElement("span");
    pill.className = "statepill";
    const dot = document.createElement("span");
    dot.className = workerDot(w.status);
    pill.append(dot, document.createTextNode(w.status || "unknown"));
    mid.appendChild(pill);
    row.appendChild(mid);

    const age = ageSeconds(w.started_at);
    const when = document.createElement("span");
    when.className = "worker-age muted";
    when.textContent = age != null ? `${formatAge(age)} ago` : (w.started_at || "—");
    row.appendChild(when);

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

function renderStuckRow(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.task_key, "Task"));
  tr.appendChild(cell(s.pid, "PID"));
  const cmd = cell(s.cmdline, "Cmdline");
  cmd.className = "cmdline";
  tr.appendChild(cmd);
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

  // Kill stays as the secondary/danger escalation.
  const killBtn = document.createElement("button");
  killBtn.type = "button";
  killBtn.className = "kill-btn";
  killBtn.textContent = "Kill";
  killBtn.title = "SIGTERM the wedged process, escalating to SIGKILL";
  killBtn.addEventListener("click", () => killProcess(s.pid));
  actionTd.appendChild(killBtn);

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
  renderCommits(repo, lastState);
  renderWorkers(repo, lastState);

  const worktrees = (lastState ? lastState.worktrees : []).filter((w) => w.repo === repo);
  setRows("repo-worktrees", worktrees, renderWorktreeRow, "No worktrees found.");

  const stuck = (lastState ? lastState.stuck : []).filter((s) => s.worker_repo === repo);
  const section = document.getElementById("repo-stuck-section");
  if (section) section.hidden = stuck.length === 0;
  setRows("repo-stuck", stuck, renderStuckRow, "");
}

// Called by index.html's x-effect when the active view/repo changes. Manages
// the (single) log stream and triggers a render. Idempotent for the same repo
// so a re-render never restarts the log tail.
function show(repo) {
  if (repo === current) return;
  if (stopLog) {
    stopLog();
    stopLog = null;
  }
  current = repo || null;
  if (!current) return; // navigated away from the detail view

  renderAll();
  const pre = document.getElementById("repo-log");
  const title = document.getElementById("repo-log-title");
  if (title) title.textContent = `— ${current} (live)`;
  if (pre) stopLog = streamLog(current, pre);
}

// Fed the consolidated snapshot by the orchestrator; re-render only while a
// repo detail page is open. Never touches the log stream.
export function update(state) {
  lastState = state;
  if (current) renderAll();
}

export function init() {
  // Exposed on window so the Alpine `x-effect` in index.html can drive show().
  window.repoDetail = { show };
  // Honour a deep link (#repo/owner/name) if Alpine has already booted.
  const store = window.Alpine && window.Alpine.store("app");
  if (store && store.view === "repo" && store.repo) show(store.repo);
}
