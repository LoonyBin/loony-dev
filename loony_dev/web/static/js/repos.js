"use strict";

// Repos view: a card per discovered repo (worker status + worktree count).
// Full per-repo drill-down pages land in #158; this is the shell placeholder.

import { goView } from "./dom.js";
import { loadLog } from "./logs.js";

export function render(workers, worktrees) {
  const container = document.getElementById("repos-list");
  if (!container) return;

  const byRepo = new Map();
  const ensure = (repo) => {
    if (!byRepo.has(repo)) {
      byRepo.set(repo, { repo, statuses: new Set(), worktrees: 0 });
    }
    return byRepo.get(repo);
  };
  // A repo can have several workers; collect every status so a repo with
  // mixed worker states isn't reduced to whichever one happened to be last.
  for (const w of workers) ensure(w.repo).statuses.add(w.status);
  for (const w of worktrees) ensure(w.repo).worktrees += 1;

  const repos = [...byRepo.values()].sort((a, b) => a.repo.localeCompare(b.repo));

  container.innerHTML = "";
  if (!repos.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No repos discovered.";
    container.appendChild(empty);
    return;
  }

  for (const r of repos) {
    const card = document.createElement("div");
    card.className = "repo-card";

    const name = document.createElement("div");
    name.className = "repo-name";
    name.textContent = r.repo;
    card.appendChild(name);

    const meta = document.createElement("div");
    meta.className = "repo-meta";
    const status = document.createElement("span");
    if (r.statuses.size === 1) {
      const [only] = r.statuses;
      status.className = `status status-${only}`;
      status.textContent = only;
    } else if (r.statuses.size > 1) {
      status.className = "muted";
      status.textContent = "mixed";
    } else {
      status.className = "muted";
      status.textContent = "no worker";
    }
    meta.appendChild(status);
    const wt = document.createElement("span");
    wt.textContent = `${r.worktrees} worktree${r.worktrees === 1 ? "" : "s"}`;
    meta.appendChild(wt);
    card.appendChild(meta);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "action";
    action.textContent = "View logs";
    action.addEventListener("click", () => { goView("logs"); loadLog(r.repo); });
    card.appendChild(action);

    container.appendChild(card);
  }
}
