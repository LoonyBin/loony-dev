"use strict";

// App-shell data orchestrator. The Alpine store that drives view switching /
// theme / the stuck indicator is registered by the inline bootstrap in
// index.html; this deferred module owns the 5s poll loop that feeds the
// per-view modules. The server stays the source of truth.

import { getJSON } from "./api.js";
import * as overview from "./overview.js";
import * as sessions from "./sessions.js";
import * as repos from "./repos.js";
import * as logs from "./logs.js";
import * as entries from "./entries.js";
import * as attach from "./attach.js";

const POLL_INTERVAL_MS = 5000;

let knownRepos = [];
let isPolling = false;

async function poll() {
  if (isPolling) return;
  isPolling = true;
  try {
    const [workersR, worktreesR, sessionsR, stuckR, taskSessionsR] = await Promise.allSettled([
      getJSON("/api/workers"),
      getJSON("/api/worktrees"),
      getJSON("/api/sessions"),
      getJSON("/api/stuck"),
      getJSON("/api/task-sessions"),
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
    const sess = sessionsR.value;
    const stuck = stuckR.status === "fulfilled" ? stuckR.value : [];
    const taskSessions = taskSessionsR.status === "fulfilled" ? taskSessionsR.value : [];

    const stuckCount = overview.renderStuck(stuck);
    const store = window.Alpine && window.Alpine.store("app");
    if (store) store.stuckCount = stuckCount;

    overview.renderWorkers(workers);
    overview.renderWorktrees(worktrees);
    sessions.render(sess);
    attach.render(taskSessions);
    repos.render(workers, worktrees);

    // Keep the per-repo pickers in sync with discovered repos.
    const next = [...new Set([
      ...workers.map((w) => w.repo).filter(Boolean),
      ...worktrees.map((w) => w.repo).filter(Boolean),
    ])].sort();
    if (next.join("\n") !== knownRepos.join("\n")) {
      knownRepos = next;
      entries.setKnownRepos(next);
      logs.setRepos(next);
    }
  } catch (err) {
    console.error("dashboard refresh failed", err);
  } finally {
    isPolling = false;
  }
}

function start() {
  entries.init();
  logs.init();
  attach.init();

  // Allow modules (e.g. the kill button) to force an immediate refresh.
  window.addEventListener("dashboard:refresh", poll);

  poll();
  setInterval(poll, POLL_INTERVAL_MS);
}

// Deferred modules run after the document is fully parsed, so every element the
// view modules touch already exists.
start();
