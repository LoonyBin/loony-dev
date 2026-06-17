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

// Default icon-tile glyph per kind, when the entry omits an `icon` frontmatter
// field. A material-symbols-outlined name.
const KIND_ICONS = { skills: "bolt", commands: "terminal" };

function entryEls() {
  return {
    kind: document.getElementById("entry-kind"),
    scope: document.getElementById("entry-scope"),
    repoLabel: document.getElementById("entry-repo-label"),
    repo: document.getElementById("entry-repo"),
    name: document.getElementById("entry-name"),
    owner: document.getElementById("entry-owner"),
    trigger: document.getElementById("entry-trigger"),
    phase: document.getElementById("entry-phase"),
    content: document.getElementById("entry-content"),
    error: document.getElementById("entry-error"),
    modal: document.getElementById("entry-modal"),
    modalTitle: document.getElementById("entry-modal-title"),
    newLabel: document.getElementById("entry-new-label"),
  };
}

// Singular noun for the current kind, for drawer titles ("New skill").
function kindNoun() {
  return entryEls().kind.value === "commands" ? "command" : "skill";
}

// Material-symbols glyph for an entry's icon tile: an explicit `icon` field
// wins, else the per-kind default.
function entryIcon(e) {
  return e.icon || KIND_ICONS[entryEls().kind.value] || "bolt";
}

// --- Frontmatter round-trip helpers ----------------------------------------
// Minimal, flat top-level scalar handling only — mirrors the backend parser
// (loony_dev/web/entries.py:_parse_frontmatter), which is all the cards read.

// Parse a leading `---`-fenced block into { key: value }. No fence => {}.
function parseFrontmatter(content) {
  const text = content || "";
  if (!/^---\r?\n/.test(text)) return {};
  const lines = text.split(/\r?\n/);
  const fields = {};
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === "---") return fields;  // closing fence
    const line = lines[i];
    if (/^\s/.test(line)) continue;                 // skip nested/indented lines
    const idx = line.indexOf(":");
    if (idx < 0) continue;
    const key = line.slice(0, idx).trim();
    if (!key || key.startsWith("#")) continue;
    fields[key] = line.slice(idx + 1).trim().replace(/^['"]|['"]$/g, "");
  }
  return {};  // no closing fence => malformed, treat as no frontmatter
}

// Emit a scalar value literally. The backend parser
// (loony_dev/web/entries.py:_parse_frontmatter) strips wrapper quotes but never
// unescapes, so JSON-style escaping would persist stray backslashes in saved
// metadata. Only collapse newlines, which would break the single-line
// `key: value` frontmatter format.
function fmValue(v) {
  return String(v).replace(/\r?\n/g, " ");
}

// Merge the given top-level scalars into content's `---` block (updating keys in
// place, appending new ones, creating a block when none exists). A key whose
// value is empty/blank is removed. Other frontmatter lines are preserved.
function mergeFrontmatter(content, updates) {
  const text = content || "";
  const set = {};
  for (const [k, v] of Object.entries(updates)) set[k] = (v == null ? "" : String(v).trim());

  const hasFence = /^---\r?\n/.test(text);
  let head = [];
  let body = text;
  if (hasFence) {
    const lines = text.split(/\r?\n/);
    let end = -1;
    for (let i = 1; i < lines.length; i++) {
      if (lines[i].trim() === "---") { end = i; break; }
    }
    if (end >= 0) {
      head = lines.slice(1, end);
      body = lines.slice(end + 1).join("\n");
    }
  }

  const seen = new Set();
  const out = [];
  for (const line of head) {
    if (/^\s/.test(line) || line.indexOf(":") < 0) { out.push(line); continue; }
    const key = line.slice(0, line.indexOf(":")).trim();
    if (Object.prototype.hasOwnProperty.call(set, key)) {
      seen.add(key);
      if (set[key]) out.push(`${key}: ${fmValue(set[key])}`);  // drop when blank
    } else {
      out.push(line);
    }
  }
  for (const [k, v] of Object.entries(set)) {
    if (!seen.has(k) && v) out.push(`${k}: ${fmValue(v)}`);
  }

  if (!out.length) return body;  // nothing left to fence — emit a bare body
  return `---\n${out.join("\n")}\n---\n${body.startsWith("\n") ? body : "\n" + body}`;
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

function mi(name) {
  const i = document.createElement("span");
  i.className = "material-symbols-outlined";
  i.setAttribute("aria-hidden", "true");
  i.textContent = name;
  return i;
}

// The card's owner badge (#226): a managed entry renders the bot-avatar style
// (avatar + login), keyed off the structural `managed` flag — never a literal
// name. A hand-authored entry with an explicit owner shows a manager tag; with
// no owner, a neutral "hand-authored" tag.
function renderOwnerBadge(e) {
  if (e.managed) {
    const badge = document.createElement("span");
    badge.className = "skill-owner";
    const av = document.createElement("span");
    av.className = "avatar bot";
    av.textContent = (e.owner || "?").charAt(0).toUpperCase();
    const label = document.createElement("span");
    label.className = "skill-owner-name";
    label.textContent = e.owner || "managed";
    badge.title = `${e.owner || "managed"} (managed entry)`;
    badge.append(av, label);
    return badge;
  }
  const tag = document.createElement("span");
  if (e.owner) {
    tag.className = "tag blue";
    tag.title = `${e.owner} (manager)`;
    tag.append(mi("hub"), document.createTextNode(e.owner));
  } else {
    tag.className = "tag ghost";
    tag.title = "Hand-authored";
    tag.textContent = "hand-authored";
  }
  return tag;
}

// Build one library card (#226): an accent icon tile + monospace name + owner
// badge head row, a description, a hairline divider, a "Triggers on" eyebrow +
// trigger / ghost phase-tag row, then per-card Edit / Delete actions. Reuses the
// #186 .avatar / .tag / .eyebrow / .ld-btn primitives.
function renderCard(e) {
  const card = document.createElement("div");
  card.className = "skill-card";

  const head = document.createElement("div");
  head.className = "skill-card-head";
  const ident = document.createElement("div");
  ident.className = "skill-ident";
  const tile = document.createElement("span");
  tile.className = "skill-icon";
  tile.appendChild(mi(entryIcon(e)));
  const name = document.createElement("span");
  name.className = "skill-name";
  name.textContent = e.name;
  ident.append(tile, name);
  head.append(ident, renderOwnerBadge(e));
  card.appendChild(head);

  if (e.description) {
    const desc = document.createElement("p");
    desc.className = "skill-desc";
    desc.textContent = e.description;
    desc.title = e.description;
    card.appendChild(desc);
  }

  if (e.trigger || e.phase) {
    card.appendChild(document.createElement("hr")).className = "skill-divider";
    const meta = document.createElement("div");
    meta.className = "skill-card-meta";
    const trig = document.createElement("div");
    trig.className = "skill-trigger";
    if (e.trigger) {
      const eyebrow = document.createElement("span");
      eyebrow.className = "eyebrow";
      eyebrow.textContent = "Triggers on";
      const val = document.createElement("span");
      val.className = "skill-trigger-value";
      val.textContent = e.trigger;
      val.title = e.trigger;
      trig.append(eyebrow, val);
    }
    meta.appendChild(trig);
    if (e.phase) {
      const tag = document.createElement("span");
      tag.className = `tag ${PHASE_COLORS[e.phase] || "ghost"}`;
      tag.textContent = e.phase;
      meta.appendChild(tag);
    }
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
  const { name, owner, trigger, phase, content } = entryEls();
  name.value = "";
  owner.value = "";
  trigger.value = "";
  phase.value = "";
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
  openDrawer(`Author ${kindNoun()}`, entryEls().name);
}

// Keep the ScreenHead primary button label tracking the current kind
// ("Author skill" / "Author command").
function syncNewLabel() {
  const { newLabel } = entryEls();
  if (newLabel) newLabel.textContent = `Author ${kindNoun()}`;
}

function openEdit(name) {
  resetEditor();
  openDrawer(name, entryEls().content);
  loadEntry(name);
}

async function loadEntry(name) {
  const { kind, name: nameInput, owner, trigger, phase, content } = entryEls();
  showEntryError("");
  try {
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    const data = await getJSON(`/api/${k}/${encodeURIComponent(name)}?${params}`);
    nameInput.value = data.name;
    content.value = data.content;
    const fm = parseFrontmatter(data.content);
    owner.value = fm.owner || "";
    trigger.value = fm.trigger || "";
    phase.value = fm.phase || "";
    selectedEntry = data.name;
  } catch (err) {
    showEntryError(`Failed to load: ${err.message}`);
  }
}

async function saveEntry() {
  const { kind, name, owner, trigger, phase, content } = entryEls();
  const entryName = (name.value || "").trim();
  showEntryError("");
  if (!entryName) { showEntryError("Name is required."); return; }
  try {
    const k = validateKind(kind.value);
    const params = entryScopeParams();
    // Fold the structured fields back into the content's frontmatter so the
    // cards re-render the metadata. The raw-markdown PUT contract is unchanged.
    const body = mergeFrontmatter(content.value, {
      owner: owner.value,
      trigger: trigger.value,
      phase: phase.value,
    });
    content.value = body;
    await apiText(`/api/${k}/${encodeURIComponent(entryName)}?${params}`, {
      method: "PUT",
      headers: { "Content-Type": "text/markdown" },
      body,
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
  els.kind.addEventListener("change", () => { syncNewLabel(); resetEditor(); refreshEntries(); });
  els.scope.addEventListener("change", () => { updateRepoPicker(); resetEditor(); refreshEntries(); });
  els.repo.addEventListener("change", () => { resetEditor(); refreshEntries(); });
  document.getElementById("entry-new").addEventListener("click", newEntry);
  document.getElementById("entry-save").addEventListener("click", saveEntry);
  document.getElementById("entry-delete").addEventListener("click", deleteFromDrawer);
  document.getElementById("entry-cancel").addEventListener("click", closeDrawer);
  document.getElementById("entry-cancel-2").addEventListener("click", closeDrawer);
  syncNewLabel();
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
