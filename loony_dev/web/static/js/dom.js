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

// Replace a table body with rendered rows, or a single empty-state row.
export function setRows(tableId, rows, render, emptyText) {
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

// Open the per-repo drill-down for `repo` ("owner/name"). Falls back to the URL
// hash if Alpine has not started yet.
export function goRepo(repo) {
  const store = window.Alpine && window.Alpine.store("app");
  if (store) store.goRepo(repo);
  else location.hash = `repo/${repo}`;
}
