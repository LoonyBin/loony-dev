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

// --- Skills & Commands editor -------------------------------------------
// Not part of the 5s poll: editing UI must never auto-clobber the textarea.
// The list refreshes only on explicit actions / scope changes.

let knownRepos = [];      // ["owner/repo", ...] from the latest worktrees/workers fetch
let selectedEntry = null; // currently-loaded entry name

function entryEls() {
  return {
    kind: document.getElementById("entry-kind"),
    scope: document.getElementById("entry-scope"),
    repoLabel: document.getElementById("entry-repo-label"),
    repo: document.getElementById("entry-repo"),
    name: document.getElementById("entry-name"),
    content: document.getElementById("entry-content"),
    error: document.getElementById("entry-error"),
  };
}

function entryScopeParams() {
  const { scope, repo } = entryEls();
  const params = new URLSearchParams();
  if (scope.value === "repo") {
    const [owner, name] = (repo.value || "").split("/");
    params.set("scope", "repo");
    if (owner) params.set("owner", owner);
    if (name) params.set("repo", name);
  } else {
    params.set("scope", "global");
  }
  return params;
}

function showEntryError(msg) {
  entryEls().error.textContent = msg || "";
}

async function apiText(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* no body */ }
    throw new Error(detail);
  }
  return resp;
}

// Returns true if the selected repo changed (e.g. the previous one vanished),
// so callers can avoid silently retargeting Save/Delete to a different repo.
function updateRepoPicker() {
  const { scope, repoLabel, repo } = entryEls();
  const isRepo = scope.value === "repo";
  repoLabel.style.display = isRepo ? "" : "none";
  if (!isRepo) return false;
  const prev = repo.value;
  repo.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = knownRepos.length ? "Select a repo…" : "No repos discovered";
  repo.appendChild(placeholder);
  for (const r of knownRepos) {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r;
    repo.appendChild(opt);
  }
  repo.value = knownRepos.includes(prev) ? prev : "";
  return repo.value !== prev;
}

async function refreshEntries() {
  const { kind, scope, content } = entryEls();
  showEntryError("");
  const tbody = document.querySelector("#entries-list tbody");
  if (scope.value === "repo" && !entryScopeParams().get("repo")) {
    setRows("entries-list", [], () => {}, "Select a repo.");
    return;
  }
  try {
    const params = entryScopeParams();
    const rows = await getJSON(`/api/${kind.value}?${params}`);
    setRows("entries-list", rows, renderEntry, "No entries installed.");
  } catch (err) {
    setRows("entries-list", [], () => {}, "Failed to load entries.");
    showEntryError(`Failed to list: ${err.message}`);
  }
}

function renderEntry(e) {
  const tr = document.createElement("tr");
  tr.className = "entry-row";
  if (e.name === selectedEntry) tr.classList.add("selected");
  tr.appendChild(cell(e.name));
  tr.appendChild(cell(e.size));
  tr.appendChild(cell(e.modified_at));
  tr.addEventListener("click", () => loadEntry(e.name));
  return tr;
}

async function loadEntry(name) {
  const { kind, name: nameInput, content } = entryEls();
  showEntryError("");
  try {
    const params = entryScopeParams();
    const data = await getJSON(`/api/${kind.value}/${encodeURIComponent(name)}?${params}`);
    nameInput.value = data.name;
    content.value = data.content;
    selectedEntry = data.name;
    refreshEntries();
  } catch (err) {
    showEntryError(`Failed to load: ${err.message}`);
  }
}

async function saveEntry() {
  const { kind, name, content } = entryEls();
  const entryName = (name.value || "").trim();
  showEntryError("");
  if (!entryName) { showEntryError("Name is required."); return; }
  try {
    const params = entryScopeParams();
    await apiText(`/api/${kind.value}/${encodeURIComponent(entryName)}?${params}`, {
      method: "PUT",
      headers: { "Content-Type": "text/markdown" },
      body: content.value,
    });
    selectedEntry = entryName;
    await refreshEntries();
  } catch (err) {
    showEntryError(`Failed to save: ${err.message}`);
  }
}

async function deleteEntry() {
  const { kind, name, content } = entryEls();
  const entryName = (name.value || "").trim();
  showEntryError("");
  if (!entryName) { showEntryError("Name is required."); return; }
  try {
    const params = entryScopeParams();
    await apiText(`/api/${kind.value}/${encodeURIComponent(entryName)}?${params}`, { method: "DELETE" });
    name.value = "";
    content.value = "";
    selectedEntry = null;
    await refreshEntries();
  } catch (err) {
    showEntryError(`Failed to delete: ${err.message}`);
  }
}

function newEntry() {
  const { name, content } = entryEls();
  name.value = "";
  content.value = "";
  selectedEntry = null;
  showEntryError("");
  refreshEntries();
}

function initEntryEditor() {
  const els = entryEls();
  els.kind.addEventListener("change", () => { newEntry(); });
  els.scope.addEventListener("change", () => { updateRepoPicker(); newEntry(); });
  els.repo.addEventListener("change", () => { newEntry(); });
  document.getElementById("entry-save").addEventListener("click", saveEntry);
  document.getElementById("entry-delete").addEventListener("click", deleteEntry);
  document.getElementById("entry-new").addEventListener("click", newEntry);
  updateRepoPicker();
  refreshEntries();
}

async function refresh() {
  try {
    const [workersR, worktreesR, sessionsR, stuckR] = await Promise.allSettled([
      getJSON("/api/workers"),
      getJSON("/api/worktrees"),
      getJSON("/api/sessions"),
      getJSON("/api/stuck"),
    ]);
    if (
      workersR.status !== "fulfilled" ||
      worktreesR.status !== "fulfilled" ||
      sessionsR.status !== "fulfilled"
    ) {
      throw new Error("core dashboard endpoints failed");
    }
    const workers = workersR.value;
    const worktrees = worktreesR.value;
    const sessions = sessionsR.value;
    const stuck = stuckR.status === "fulfilled" ? stuckR.value : [];
    renderStuckSection(stuck);
    setRows("workers", workers, renderWorker, "No workers discovered.");
    setRows("worktrees", worktrees, renderWorktree, "No worktrees found.");
    setRows("sessions", sessions, renderSession, "No active sessions.");

    // Keep the per-repo picker in sync with discovered repos (cheap, no clobber).
    const repos = new Set([...workers.map((w) => w.repo), ...worktrees.map((w) => w.repo)]);
    const next = [...repos].sort();
    if (next.join("\n") !== knownRepos.join("\n")) {
      knownRepos = next;
      // If the picker had to drop the previously-selected repo, reset the editor
      // rather than silently letting Save/Delete act on a different repo.
      if (updateRepoPicker()) newEntry();
    }
  } catch (err) {
    console.error("dashboard refresh failed", err);
  }
}

initEntryEditor();
refresh();
setInterval(refresh, 5000);
