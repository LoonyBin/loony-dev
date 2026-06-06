"use strict";

// Minimal vanilla-JS dashboard: fetch the read-only API and render three tables.
// No framework, no build step.

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

function setRows(tableId, rows, render, emptyText) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  if (!rows.length) {
    const table = tbody.closest("table");
    const cols = table.querySelectorAll("thead th").length || 1;
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.className = "empty";
    td.colSpan = cols;
    td.textContent = emptyText;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const row of rows) tbody.appendChild(render(row));
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text == null ? "" : String(text);
  return td;
}

function renderWorker(w) {
  const tr = document.createElement("tr");
  const repoTd = document.createElement("td");
  const link = document.createElement("button");
  link.type = "button";
  link.className = "repo-link";
  link.textContent = w.repo;
  link.addEventListener("click", () => loadLog(w.repo));
  repoTd.appendChild(link);
  tr.appendChild(repoTd);
  tr.appendChild(cell(w.pid));
  const statusTd = document.createElement("td");
  statusTd.className = `status status-${w.status}`;
  statusTd.textContent = w.status;
  tr.appendChild(statusTd);
  tr.appendChild(cell(w.started_at));
  return tr;
}

function renderWorktree(w) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(w.repo));
  tr.appendChild(cell(w.detached ? "(detached)" : w.branch));
  tr.appendChild(cell(w.head ? w.head.slice(0, 10) : ""));
  tr.appendChild(cell(w.path));
  return tr;
}

function renderSession(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.session_id));
  tr.appendChild(cell(s.repo));
  tr.appendChild(cell(s.key));
  return tr;
}

function formatAge(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
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
  } catch (err) {
    window.alert(`Failed to kill PID ${pid}: ${err.message}`);
  }
  refresh();
}

function renderStuck(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.worker_repo));
  tr.appendChild(cell(s.task_key));
  tr.appendChild(cell(s.pid));
  const cmd = cell(s.cmdline);
  cmd.className = "cmdline";
  tr.appendChild(cmd);
  tr.appendChild(cell(formatAge(s.age_seconds)));
  tr.appendChild(cell(s.blocked_on));
  const actionTd = document.createElement("td");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "kill-btn";
  btn.textContent = "Kill";
  btn.addEventListener("click", () => killProcess(s.pid));
  actionTd.appendChild(btn);
  tr.appendChild(actionTd);
  return tr;
}

function renderStuckSection(stuck) {
  const banner = document.getElementById("stuck-banner");
  const section = document.getElementById("stuck-section");
  const has = stuck.length > 0;
  banner.hidden = !has;
  section.hidden = !has;
  if (has) {
    const noun = stuck.length === 1 ? "process" : "processes";
    banner.textContent = `⚠ ${stuck.length} stuck ${noun} detected — a Claude descendant appears wedged.`;
    setRows("stuck", stuck, renderStuck, "");
  }
}

async function loadLog(repo) {
  const title = document.getElementById("log-title");
  const pre = document.getElementById("log");
  title.textContent = `— ${repo}`;
  pre.textContent = "Loading…";
  try {
    const data = await getJSON(`/api/logs/${repo}/tail`);
    pre.textContent = data.lines.length ? data.lines.join("\n") : "(empty log)";
  } catch (err) {
    pre.textContent = `Failed to load log: ${err.message}`;
  }
}

async function refresh() {
  try {
    const [workers, worktrees, sessions, stuck] = await Promise.all([
      getJSON("/api/workers"),
      getJSON("/api/worktrees"),
      getJSON("/api/sessions"),
      getJSON("/api/stuck"),
    ]);
    renderStuckSection(stuck);
    setRows("workers", workers, renderWorker, "No workers discovered.");
    setRows("worktrees", worktrees, renderWorktree, "No worktrees found.");
    setRows("sessions", sessions, renderSession, "No active sessions.");
  } catch (err) {
    console.error("dashboard refresh failed", err);
  }
}

refresh();
setInterval(refresh, 5000);
