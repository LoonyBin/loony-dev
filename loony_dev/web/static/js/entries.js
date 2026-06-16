"use strict";

// Skills & Commands library (#191).
// Entries render as a card grid (one card per skill/command), with the editor
// living in a drawer overlay. Not part of the 5s poll: the editing UI must never
// auto-clobber the textarea. The grid refreshes only on explicit actions / scope
// changes; the orchestrator feeds discovered repos in via setKnownRepos().

import { getJSON, apiText } from "./api.js";
import { openModalA11y, closeModalA11y } from "./modal.js";

const VALID_KINDS = new Set(["skills", "commands"]);

// Lifecycle phase → .tag color variant (#186 primitive). Unknown phases fall
// back to neutral so a future phase name still renders a (muted) chip.
const PHASE_COLORS = {
  planning: "blue",
  development: "green",
  ci: "amber",
  review: "purple",
  conflict: "red",
  stuck: "neutral",
};

let knownRepos = [];      // ["owner/repo", ...] from the latest workers/worktrees fetch
let selectedEntry = null; // name being edited in the drawer (create mode => null)

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
    modal: document.getElementById("entry-modal"),
    modalTitle: document.getElementById("entry-modal-title"),
  };
}

// Singular noun for the current kind, for drawer titles ("New skill").
function kindNoun() {
  return entryEls().kind.value === "commands" ? "command" : "skill";
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

// Replace the card grid with rendered cards, or a single empty-state message.
function setCards(containerId, rows, render, emptyText) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  for (const row of rows) container.appendChild(render(row));
}

async function refreshEntries() {
  const { kind, scope } = entryEls();
  showEntryError("");
  if (scope.value === "repo" && !entryScopeParams().get("repo")) {
    setCards("entries-grid", [], renderCard, "Select a repo.");
    return;
  }
  try {
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    const rows = await getJSON(`/api/${k}?${params}`);
    setCards("entries-grid", rows, renderCard, "No entries installed.");
  } catch (err) {
    console.error("Failed to load entries:", err);
    setCards("entries-grid", [], renderCard, "Failed to load entries.");
  }
}

// Build one library card: monospace name + owner avatar, description, an
// optional "triggers on …" line, and an optional lifecycle-phase tag, with
// per-card Edit / Delete actions. Reuses the #186 .avatar / .tag / .ld-btn
// primitives rather than inventing parallel classes.
function renderCard(e) {
  const card = document.createElement("div");
  card.className = "skill-card";

  const head = document.createElement("div");
  head.className = "skill-card-head";
  const name = document.createElement("span");
  name.className = "skill-name";
  name.textContent = e.name;
  head.appendChild(name);
  if (e.owner) {
    const isTrixy = e.owner === "trixy";
    const av = document.createElement("span");
    av.className = `avatar ${isTrixy ? "trixy" : "capo"}`;
    av.textContent = e.owner.charAt(0).toUpperCase();
    av.title = isTrixy ? "trixy (loony-dev managed)" : "capo (hand-authored)";
    head.appendChild(av);
  }
  card.appendChild(head);

  if (e.description) {
    const desc = document.createElement("p");
    desc.className = "skill-desc";
    desc.textContent = e.description;
    desc.title = e.description;
    card.appendChild(desc);
  }

  if (e.trigger) {
    const trig = document.createElement("p");
    trig.className = "skill-trigger";
    trig.textContent = `triggers on ${e.trigger}`;
    trig.title = e.trigger;
    card.appendChild(trig);
  }

  if (e.phase) {
    const meta = document.createElement("div");
    meta.className = "skill-card-meta";
    const tag = document.createElement("span");
    tag.className = `tag ${PHASE_COLORS[e.phase] || "neutral"}`;
    tag.textContent = e.phase;
    meta.appendChild(tag);
    card.appendChild(meta);
  }

  const actions = document.createElement("div");
  actions.className = "skill-card-actions";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "ld-btn sm outline";
  edit.textContent = "Edit";
  edit.addEventListener("click", () => openEdit(e.name));
  actions.appendChild(edit);
  const del = document.createElement("button");
  del.type = "button";
  del.className = "ld-btn sm ghost";
  del.textContent = "Delete";
  del.addEventListener("click", () => confirmDelete(e.name));
  actions.appendChild(del);
  card.appendChild(actions);

  return card;
}

// --- Drawer (editor overlay) ----------------------------------------------

// Reset the editor fields without touching the grid or the drawer visibility.
function resetEditor() {
  const { name, content } = entryEls();
  name.value = "";
  content.value = "";
  selectedEntry = null;
  showEntryError("");
}

function openDrawer(title, focusTarget) {
  const { modal, modalTitle } = entryEls();
  modalTitle.textContent = title;
  modal.hidden = false;
  openModalA11y(modal, closeDrawer, focusTarget);
}

// Single close path (Save-success, Delete-success, Cancel, ESC): always tears
// down the focus-trap handler via closeModalA11y before hiding so the keydown
// listener is never leaked and focus is restored to the opener.
function closeDrawer() {
  const { modal } = entryEls();
  if (!modal) return;
  closeModalA11y(modal);
  modal.hidden = true;
}

function newEntry() {
  resetEditor();
  openDrawer(`New ${kindNoun()}`, entryEls().name);
}

function openEdit(name) {
  resetEditor();
  openDrawer(name, entryEls().content);
  loadEntry(name);
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
    closeDrawer();
    await refreshEntries();
  } catch (err) {
    showEntryError(`Failed to save: ${err.message}`);
  }
}

// Delete by explicit name. Returns an error string to display, or null on
// success — callers choose where to surface it (drawer vs. card path).
async function deleteNamed(entryName) {
  if (!entryName) return "Name is required.";
  try {
    const k = validateKind(entryEls().kind.value);
    const params = entryScopeParams();
    await apiText(`/api/${k}/${encodeURIComponent(entryName)}?${params}`, { method: "DELETE" });
    return null;
  } catch (err) {
    return `Failed to delete: ${err.message}`;
  }
}

// Drawer Delete: error stays in the open drawer; success closes + refreshes.
// In edit mode delete the entry that was loaded (selectedEntry), not the typed
// name — the user may have edited the name field before pressing Delete.
async function deleteFromDrawer() {
  const entryName = selectedEntry || (entryEls().name.value || "").trim();
  showEntryError("");
  const err = await deleteNamed(entryName);
  if (err) { showEntryError(err); return; }
  closeDrawer();
  await refreshEntries();
}

// Card Delete: the drawer is closed, so confirm first and surface any error via
// an alert before refreshing the grid.
async function confirmDelete(name) {
  if (!window.confirm(`Delete "${name}"? This cannot be undone.`)) return;
  const err = await deleteNamed(name);
  if (err) { window.alert(err); return; }
  await refreshEntries();
}

export function init() {
  const els = entryEls();
  els.kind.addEventListener("change", () => { resetEditor(); refreshEntries(); });
  els.scope.addEventListener("change", () => { updateRepoPicker(); resetEditor(); refreshEntries(); });
  els.repo.addEventListener("change", () => { resetEditor(); refreshEntries(); });
  document.getElementById("entry-new").addEventListener("click", newEntry);
  document.getElementById("entry-save").addEventListener("click", saveEntry);
  document.getElementById("entry-delete").addEventListener("click", deleteFromDrawer);
  document.getElementById("entry-cancel").addEventListener("click", closeDrawer);
  document.getElementById("entry-cancel-2").addEventListener("click", closeDrawer);
  updateRepoPicker();
  refreshEntries();
}

// Keep the per-repo picker in sync with discovered repos (cheap, no clobber).
// If the picker had to drop the previously-selected repo, reset the editor
// rather than silently letting Save/Delete act on a different repo.
export function setKnownRepos(next) {
  if (next.join("\n") === knownRepos.join("\n")) return;
  knownRepos = next;
  if (updateRepoPicker()) { resetEditor(); refreshEntries(); }
}
