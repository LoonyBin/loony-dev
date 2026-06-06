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

// Cap retained DOM lines so a long-lived stream can't grow unbounded.
// Matches the supervisor's default max_buffer_lines.
const MAX_LOG_LINES = 5000;

let activeStream = null; // the single live EventSource (closed when switching repos)

function isPinnedToBottom(pre) {
  // Treat "within 4px of the bottom" as pinned to tolerate sub-pixel scrolling.
  return pre.scrollHeight - pre.clientHeight - pre.scrollTop < 4;
}

function loadLog(repo) {
  const title = document.getElementById("log-title");
  const pre = document.getElementById("log");

  // Close any previous stream so the browser doesn't leak connections.
  if (activeStream) {
    activeStream.close();
    activeStream = null;
  }

  title.textContent = `— ${repo} (live)`;
  pre.textContent = "";

  const es = new EventSource(`/api/logs/${repo}/stream`);
  activeStream = es;

  es.onmessage = (event) => {
    // Ignore late messages from a stream we've already switched away from.
    if (activeStream !== es) return;
    const pinned = isPinnedToBottom(pre);
    pre.textContent += (pre.textContent ? "\n" : "") + event.data;
    // Trim to the last MAX_LOG_LINES to bound memory.
    const lines = pre.textContent.split("\n");
    if (lines.length > MAX_LOG_LINES) {
      pre.textContent = lines.slice(lines.length - MAX_LOG_LINES).join("\n");
    }
    if (pinned) pre.scrollTop = pre.scrollHeight;
  };

  es.onerror = () => {
    // EventSource auto-reconnects; only surface an error if nothing arrived yet.
    if (activeStream === es && !pre.textContent) {
      pre.textContent = "(log stream unavailable)";
    }
  };
}

async function refresh() {
  try {
    const [workers, worktrees, sessions] = await Promise.all([
      getJSON("/api/workers"),
      getJSON("/api/worktrees"),
      getJSON("/api/sessions"),
    ]);
    setRows("workers", workers, renderWorker, "No workers discovered.");
    setRows("worktrees", worktrees, renderWorktree, "No worktrees found.");
    setRows("sessions", sessions, renderSession, "No active sessions.");
  } catch (err) {
    console.error("dashboard refresh failed", err);
  }
}

refresh();
setInterval(refresh, 5000);
