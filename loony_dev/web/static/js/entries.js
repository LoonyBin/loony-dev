"use strict";

// Skills & Commands editor.
// Not part of the 5s poll: the editing UI must never auto-clobber the textarea.
// The list refreshes only on explicit actions / scope changes; the orchestrator
// feeds discovered repos in via setKnownRepos().

import { getJSON, apiText } from "./api.js";
import { cell, setRows } from "./dom.js";

const VALID_KINDS = new Set(["skills", "commands"]);

let knownRepos = [];      // ["owner/repo", ...] from the latest workers/worktrees fetch
let selectedEntry = null; // currently-loaded entry name

// Guard the kind segment before it is interpolated into an API path. The
// <select> only offers known values, but validate anyway so a tampered DOM
// can't redirect requests to an arbitrary endpoint.
function validateKind(value) {
  if (!VALID_KINDS.has(value)) throw new Error(`Unknown entry kind: ${value}`);
  return value;
}

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
  const { kind, scope } = entryEls();
  showEntryError("");
  if (scope.value === "repo" && !entryScopeParams().get("repo")) {
    setRows("entries-list", [], () => {}, "Select a repo.");
    return;
  }
  try {
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    const rows = await getJSON(`/api/${k}?${params}`);
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
  tr.appendChild(cell(e.name, "Name"));
  tr.appendChild(cell(e.size, "Size"));
  tr.appendChild(cell(e.modified_at, "Modified"));
  tr.addEventListener("click", () => loadEntry(e.name));
  return tr;
}

async function loadEntry(name) {
  const { kind, name: nameInput, content } = entryEls();
  showEntryError("");
  try {
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    const data = await getJSON(`/api/${k}/${encodeURIComponent(name)}?${params}`);
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
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    await apiText(`/api/${k}/${encodeURIComponent(entryName)}?${params}`, {
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
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    await apiText(`/api/${k}/${encodeURIComponent(entryName)}?${params}`, { method: "DELETE" });
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

export function init() {
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

// Keep the per-repo picker in sync with discovered repos (cheap, no clobber).
// If the picker had to drop the previously-selected repo, reset the editor
// rather than silently letting Save/Delete act on a different repo.
export function setKnownRepos(next) {
  if (next.join("\n") === knownRepos.join("\n")) return;
  knownRepos = next;
  if (updateRepoPicker()) newEntry();
}
