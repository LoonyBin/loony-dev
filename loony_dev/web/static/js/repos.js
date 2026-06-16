"use strict";

// Repos roll-up (Overview): one card per discovered repo summarising worker
// status, worktree count, and any stuck processes. Tapping a card opens the
// per-repo drill-down page (#158).

import { goRepo, icon } from "./dom.js";

export function render(workers, worktrees, stuck = []) {
  const container = document.getElementById("repos-list");
  if (!container) return;

  const byRepo = new Map();
  const ensure = (repo) => {
    if (!byRepo.has(repo)) {
      byRepo.set(repo, { repo, statuses: new Set(), worktrees: 0, stuck: 0 });
    }
    return byRepo.get(repo);
  };
  // A repo can have several workers; collect every status so a repo with
  // mixed worker states isn't reduced to whichever one happened to be last.
  for (const w of workers) ensure(w.repo).statuses.add(w.status);
  for (const w of worktrees) ensure(w.repo).worktrees += 1;
  for (const s of stuck) if (s.worker_repo) ensure(s.worker_repo).stuck += 1;

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
    // A <button> so the whole card is keyboard-focusable and tappable.
    const card = document.createElement("button");
    card.type = "button";
    card.className = "repo-card";
    card.addEventListener("click", () => goRepo(r.repo));

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
    if (r.stuck > 0) {
      const stuckBadge = document.createElement("span");
      stuckBadge.className = "stuck-badge";
      stuckBadge.appendChild(icon("warning"));
      stuckBadge.appendChild(document.createTextNode(`${r.stuck} stuck`));
      meta.appendChild(stuckBadge);
    }
    card.appendChild(meta);

    container.appendChild(card);
  }
}
