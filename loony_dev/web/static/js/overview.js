"use strict";

// Overview view: a global stuck-process banner/table. Worker and worktree
// detail moved into the per-repo drill-down (#158); the Overview is now a
// roll-up of repo cards (see repos.js) plus this cross-repo stuck signal.

import { cell, setRows, formatAge } from "./dom.js";

// Send SIGTERM to a wedged Claude descendant (escalated to SIGKILL server-side).
// Shared with the per-repo drill-down's scoped stuck table.
export async function killProcess(pid) {
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
