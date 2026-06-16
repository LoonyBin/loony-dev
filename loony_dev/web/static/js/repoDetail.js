"use strict";

// Per-repo drill-down (#158): aggregates everything for one repo on one page —
// worker status, worktrees, the remote-control session card, scoped stuck
// processes, and an embedded live log tail.
//
// Navigation is driven by the Alpine store: index.html's `x-effect` calls
// show(repo|null) whenever the active view/repo changes. Consolidated state
// arrives via update(state) from the app-shell orchestrator (the #155
// /api/events stream); we filter it to the current repo. The log tail uses the
// existing /api/logs/{owner}/{repo}/stream endpoint via streamLog().

import { cell, setRows, formatAge, icon } from "./dom.js";
import { killProcess, interruptSession } from "./overview.js";
import { streamLog } from "./logs.js";

let current = null; // repo currently displayed ("owner/name"), or null
let lastState = null; // latest consolidated snapshot {workers, worktrees, sessions, stuck}
let stopLog = null; // stop fn for the active log stream

function muted(text) {
  const p = document.createElement("p");
  p.className = "muted";
  p.textContent = text;
  return p;
}

function addKV(dl, label, value, valueClass) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value == null || value === "" ? "—" : String(value);
  if (valueClass) dd.className = valueClass;
  dl.appendChild(dt);
  dl.appendChild(dd);
}

function renderWorker(repo, state) {
  const body = document.getElementById("repo-worker-body");
  if (!body) return;
  body.innerHTML = "";
  const workers = (state ? state.workers : []).filter((w) => w.repo === repo);
  if (!workers.length) {
    body.appendChild(muted("No worker running."));
    return;
  }
  for (const w of workers) {
    const dl = document.createElement("dl");
    dl.className = "kv";
    addKV(dl, "PID", w.pid);
    addKV(dl, "Status", w.status, `status status-${w.status}`);
    addKV(dl, "Started", w.started_at);
    body.appendChild(dl);
  }
}

function renderSession(repo, state) {
  const body = document.getElementById("repo-session-body");
  if (!body) return;
  body.innerHTML = "";
  const s = (state ? state.sessions : []).find((x) => x.repo === repo);
  if (!s) {
    body.appendChild(muted("No active session."));
    return;
  }
  const dl = document.createElement("dl");
  dl.className = "kv";
  addKV(dl, "Session", s.session_id);
  addKV(dl, "Key", s.key);
  body.appendChild(dl);
  if (s.join_url) {
    const a = document.createElement("a");
    a.className = "btn btn-primary join-link";
    a.href = s.join_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "Open join link ";
    a.appendChild(icon("open_in_new"));
    body.appendChild(a);
    // QR rendering arrives with the shared #157 session-card component; until
    // then the join link itself is the actionable hand-off.
  } else {
    body.appendChild(muted("Waiting for a join link…"));
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
  renderWorker(repo, lastState);
  renderSession(repo, lastState);

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
