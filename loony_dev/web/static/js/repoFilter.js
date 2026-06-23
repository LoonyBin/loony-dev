"use strict";

// Shared repo-filter list — the selectable repo sidebar element first built for
// the Fleet worklist (#188), now reused by the Skills library (#258 / Phase 4).
// An eyebrow head (+ an optional Clear) over a list of selectable rows
// (name + optional count), with one active row and an `onSelect(key)` callback.
// The visual classes (.fleet-repos-head / .fleet-repo-list / .fleet-repo*) live
// in app.css; this is the one renderer both screens share.
//
// opts:
//   eyebrow    — the head label ("Filter by repo" / "Filter by source")
//   items      — [{ key, label, title?, count? }]
//   activeKey  — the selected key (null = none)
//   onSelect   — (key) => void; the Clear button calls onSelect(null)
//   clearable  — show a Clear button in the head while activeKey is set
//   emptyText  — message when items is empty
//   footer     — an optional node appended below the list (Fleet's connect + note)
import { el } from "/static/ds/util/dom.js";

export function renderRepoFilter(host, {
  eyebrow, items = [], activeKey, onSelect,
  clearable = false, emptyText = "No repos.", footer = null,
} = {}) {
  if (!host) return;
  host.innerHTML = "";

  const head = el("div", "fleet-repos-head");
  head.append(el("span", "eyebrow", eyebrow));
  if (clearable && activeKey != null) {
    const clear = el("button", "fleet-clear", "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => onSelect(null));
    head.append(clear);
  }
  host.append(head);

  if (!items.length) {
    host.append(el("p", "empty", emptyText));
  } else {
    const list = el("div", "fleet-repo-list");
    for (const it of items) {
      const btn = el("button", "fleet-repo");
      btn.type = "button";
      const active = it.key === activeKey;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
      btn.append(el("span", "fleet-repo-name", it.label));
      if (it.title) btn.title = it.title;
      if (it.count != null) btn.append(el("span", "fleet-repo-count", String(it.count)));
      btn.addEventListener("click", () => onSelect(it.key));
      list.append(btn);
    }
    host.append(list);
  }

  if (footer) host.append(footer);
}
