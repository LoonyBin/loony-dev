"use strict";

// Small DOM/render helpers shared across views.

// Build a <td>. `label` (optional) is stored as data-label so the responsive
// CSS can render it as the field name when tables collapse to cards on mobile.
export function cell(text, label) {
  const td = document.createElement("td");
  td.textContent = text == null ? "" : String(text);
  if (label) td.dataset.label = label;
  return td;
}

// Track tables we've already warned about so the guard below logs once, not on
// every SSE tick.
const _missingTables = new Set();

// Replace a table body with rendered rows, or a single empty-state row.
// Defensive (#221): if the table was removed (e.g. a folded-in view), no-op with
// a one-time warning instead of throwing a TypeError mid-applySnapshot — a throw
// here would abort every writer queued after it.
export function setRows(tableId, rows, render, emptyText) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  if (!tbody) {
    if (!_missingTables.has(tableId)) {
      _missingTables.add(tableId);
      console.warn(`setRows: no table #${tableId} in the DOM — skipping.`);
    }
    return;
  }
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

// Build a Material Symbols Outlined glyph span (#186). Decorative by default:
// aria-hidden so screen readers skip it — the surrounding text carries meaning.
// If the icon font is blocked, the ligature name shows as plain text.
export function icon(name) {
  const el = document.createElement("span");
  el.className = "material-symbols-outlined";
  el.setAttribute("aria-hidden", "true");
  el.textContent = name;
  return el;
}

export function formatAge(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// Switch the active view via the Alpine store (falls back to the URL hash if
// Alpine has not started yet).
export function goView(view) {
  const store = window.Alpine && window.Alpine.store("app");
  if (store) store.go(view);
  else location.hash = view;
}

// Open the Live screen for `repo` ("owner/name") — the per-repo drill-down,
// promoted to a primary destination (#221). Falls back to the URL hash if Alpine
// has not started yet.
export function goRepo(repo) {
  const store = window.Alpine && window.Alpine.store("app");
  if (store) store.goRepo(repo);
  else location.hash = `live/${repo}`;
}

// Open the Issue ▸ PR pipeline-detail view (#190) for `repo` ("owner/name") and
// `taskKey` (the snapshot row's task_key). Falls back to the URL hash if Alpine
// has not started yet.
export function goPipeline(repo, taskKey) {
  const store = window.Alpine && window.Alpine.store("app");
  if (store) store.goPipeline(repo, taskKey);
  else location.hash = `pipeline/${repo}/${taskKey}`;
}

// Caller-side semantic→visual map (issue #258 / Phase 4): a domain lifecycle
// state resolves to a domain-agnostic VISUAL colour the Sdot/StatePill components
// understand. The app owns this bridge; the visual layer only knows colours.
const DOT_TONE = { merged: "green", active: "accent", review: "amber", blocked: "red", gated: "hollow" };
export function dotTone(state) {
  return DOT_TONE[state] || "hollow";
}

// Canonical GitHub-stage → Tag tone (the #186 palette). One source of truth,
// replacing the per-screen STAGE_TAG / STAGE_TONE copies (issueDetail's had
// drifted: Planning amber, PR Open blue, no Merged). Tones are visual.
const STAGE_TONE = {
  "Inbox": "ghost", "Planning": "blue", "Implementing": "blue",
  "PR Open": "purple", "In Review": "amber", "Conflicts": "red", "Merged": "green",
};
export function stageTone(stage) {
  return STAGE_TONE[stage] || "neutral";
}

// Actor role → avatar colour tone (visual). worker(trixy/bot)→soft, operator→
// green, capo→accent, system→neutral. The role/login is never a visual class.
const AVATAR_TONE = { trixy: "soft", bot: "soft", capo: "accent", operator: "green", system: "neutral" };
export function avatarTone(actor) {
  return AVATAR_TONE[actor] || "soft";
}
