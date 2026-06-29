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

// "Stuck" is now derived from the snapshot heartbeat age (#270), not /proc, so a
// row no longer carries a pid/cmdline. The pid-keyed Kill escalation is therefore
// dropped — Interrupt (ESC, addressed by session_id) is the intervention. A hard
// kill, if ever needed, would resolve the live pid server-side at click time, off
// the list path; that is a noted follow-up, not part of this read-path change.
function renderStuckRow(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.worker_repo, "Repo"));
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
