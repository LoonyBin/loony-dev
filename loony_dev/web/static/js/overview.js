"use strict";

// Overview view: stuck-process banner/table, workers table, worktrees table.

import { cell, setRows, formatAge, goView, requestRefresh } from "./dom.js";
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

// ESC interrupt: the primary, reversible intervention. It aborts the in-flight
// turn but leaves the session alive, so no confirmation prompt is needed.
async function interruptSession(sessionId) {
  try {
    const resp = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST" },
    );
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`${resp.status}: ${detail}`);
    }
  } catch (err) {
    window.alert(`Failed to interrupt session ${sessionId}: ${err.message}`);
  }
  requestRefresh();
}

// Kill is the escalation path (SIGTERM → SIGKILL); it ends the process, so it
// keeps a confirmation prompt.
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
  } catch (err) {
    window.alert(`Failed to kill PID ${pid}: ${err.message}`);
  }
  requestRefresh();
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
