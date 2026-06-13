"use strict";

// Overview view: stuck-process banner/table, workers table, worktrees table.

import { cell, setRows, formatAge, goView } from "./dom.js";
import { loadLog } from "./logs.js";

function renderWorker(w) {
  const tr = document.createElement("tr");
  const repoTd = document.createElement("td");
  repoTd.dataset.label = "Repo";
  const link = document.createElement("button");
  link.type = "button";
  link.className = "repo-link";
  link.textContent = w.repo;
  // Clicking a worker jumps to the Logs view and live-tails it.
  link.addEventListener("click", () => { goView("logs"); loadLog(w.repo); });
  repoTd.appendChild(link);
  tr.appendChild(repoTd);
  tr.appendChild(cell(w.pid, "PID"));
  const statusTd = document.createElement("td");
  statusTd.dataset.label = "Status";
  statusTd.className = `status status-${w.status}`;
  statusTd.textContent = w.status;
  tr.appendChild(statusTd);
  tr.appendChild(cell(w.started_at, "Started"));
  return tr;
}

function renderWorktree(w) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(w.repo, "Repo"));
  tr.appendChild(cell(w.detached ? "(detached)" : w.branch, "Branch"));
  tr.appendChild(cell(w.head ? w.head.slice(0, 10) : "", "HEAD"));
  tr.appendChild(cell(w.path, "Path"));
  return tr;
}

async function killProcess(pid) {
  if (!window.confirm(`Send SIGTERM to PID ${pid}? It will be SIGKILLed if it does not exit.`)) {
    return;
  }
  try {
    const resp = await fetch(`/api/processes/${pid}/kill`, { method: "POST" });
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`${resp.status}: ${detail}`);
    }
    // No manual refresh needed: the /api/events stream re-emits the new state
    // (the process gone from the stuck table) within a couple of seconds.
  } catch (err) {
    window.alert(`Failed to kill PID ${pid}: ${err.message}`);
  }
}

function renderStuckRow(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.worker_repo, "Repo"));
  tr.appendChild(cell(s.task_key, "Task"));
  tr.appendChild(cell(s.pid, "PID"));
  const cmd = cell(s.cmdline, "Cmdline");
  cmd.className = "cmdline";
  tr.appendChild(cmd);
  tr.appendChild(cell(formatAge(s.age_seconds), "Age"));
  tr.appendChild(cell(s.blocked_on, "Blocked on"));
  const actionTd = document.createElement("td");
  actionTd.dataset.label = "Action";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "kill-btn";
  btn.textContent = "Kill";
  btn.addEventListener("click", () => killProcess(s.pid));
  actionTd.appendChild(btn);
  tr.appendChild(actionTd);
  return tr;
}

export function renderWorkers(workers) {
  setRows("workers", workers, renderWorker, "No workers discovered.");
}

export function renderWorktrees(worktrees) {
  setRows("worktrees", worktrees, renderWorktree, "No worktrees found.");
}

// Render the stuck banner + table and return the count so the orchestrator can
// surface it in the persistent top-bar indicator.
export function renderStuck(stuck) {
  const banner = document.getElementById("stuck-banner");
  const section = document.getElementById("stuck-section");
  const has = stuck.length > 0;
  banner.hidden = !has;
  section.hidden = !has;
  if (has) {
    const noun = stuck.length === 1 ? "process" : "processes";
    banner.textContent = `⚠ ${stuck.length} stuck ${noun} detected — a Claude descendant appears wedged.`;
    setRows("stuck", stuck, renderStuckRow, "");
  }
  return stuck.length;
}
