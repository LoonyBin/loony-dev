"use strict";

// App-shell data orchestrator. The Alpine store that drives view switching /
// theme / the stuck indicator is registered by the inline bootstrap in
// index.html; this deferred module owns the state feed for the per-view
// modules. The server stays the source of truth.
//
// State is consumed from the consolidated /api/events SSE stream (#155); the
// per-resource poll is kept as a fallback for when SSE is unavailable, and for
// the immediate refresh after a kill action.

import { getJSON } from "./api.js";
import * as overview from "./overview.js";
import * as sessions from "./sessions.js";
import * as repos from "./repos.js";
import * as repoDetail from "./repoDetail.js";
import * as logs from "./logs.js";
import * as entries from "./entries.js";

const POLL_INTERVAL_MS = 5000;

let knownRepos = [];
let isPolling = false;
let pollTimer = null;

// Fan a consolidated state snapshot out to every view module. The snapshot
// shape matches the four per-resource endpoints so the SSE and poll paths feed
// identical data.
function applyState(state) {
  const workers = state.workers || [];
  const worktrees = state.worktrees || [];
  const sess = state.sessions || [];
  const stuck = state.stuck || [];

  const stuckCount = overview.renderStuck(stuck);
  const store = window.Alpine && window.Alpine.store("app");
  if (store) store.stuckCount = stuckCount;

  repos.render(workers, worktrees, stuck);
  sessions.render(sess);
  repoDetail.update(state);

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
}

async function poll() {
  if (isPolling) return;
  isPolling = true;
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
    applyState({
      workers: workersR.value,
      worktrees: worktreesR.value,
      sessions: sessionsR.value,
      stuck: stuckR.status === "fulfilled" ? stuckR.value : [],
    });
  } catch (err) {
    console.error("dashboard refresh failed", err);
  } finally {
    isPolling = false;
  }
}

function startPolling() {
  if (pollTimer) return;
  poll();
  pollTimer = setInterval(poll, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// Subscribe to the consolidated state stream. While it delivers, the poll
// fallback stays off; on error (EventSource auto-reconnects in the background)
// we resume polling so the dashboard keeps updating until SSE recovers.
function connect() {
  if (!window.EventSource) {
    startPolling();
    return;
  }
  const es = new EventSource("/api/events");
  es.onmessage = (event) => {
    stopPolling();
    try {
      applyState(JSON.parse(event.data));
    } catch (err) {
      console.error("bad /api/events payload", err);
      startPolling();
    }
  };
  es.onerror = () => {
    startPolling();
  };
}

function start() {
  entries.init();
  logs.init();
  repoDetail.init();

  // Allow modules (e.g. the kill button) to force an immediate refresh.
  window.addEventListener("dashboard:refresh", poll);

  connect();
}

// Deferred modules run after the document is fully parsed, so every element the
// view modules touch already exists.
start();
