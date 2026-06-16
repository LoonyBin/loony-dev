"use strict";

// Overview view: a global stuck-process banner/table. Worker and worktree
// detail moved into the per-repo drill-down (#158); the Overview body is now
// the Fleet worklist (see fleet.js) and this cross-repo stuck signal sits
// above it.

import { cell, setRows, formatAge, icon } from "./dom.js";

// ESC interrupt: the primary, reversible intervention. It aborts the in-flight
// turn but leaves the session alive, so no confirmation prompt is needed.
// Exported so the per-repo drill-down's scoped stuck table shares it.
export async function interruptSession(sessionId) {
  try {
    const resp = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST" },
    );
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`${resp.status}: ${detail}`);
    }
    // No manual refresh needed: the /api/events stream re-emits the new state
    // (the turn aborted) within a couple of seconds.
  } catch (err) {
    window.alert(`Failed to interrupt session ${sessionId}: ${err.message}`);
  }
}

// Kill is the escalation path (SIGTERM → SIGKILL); it ends the process, so it
// keeps a confirmation prompt. Shared with the per-repo drill-down's scoped
// stuck table.
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
    banner.textContent = "";
    banner.appendChild(icon("warning"));
    banner.appendChild(document.createTextNode(
      `${stuck.length} stuck ${noun} detected — a Claude descendant appears wedged.`));
    setRows("stuck", stuck, renderStuckRow, "");
  }
  return stuck.length;
}
