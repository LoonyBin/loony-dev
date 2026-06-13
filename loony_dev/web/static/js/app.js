"use strict";

// App-shell data orchestrator. The Alpine store that drives view switching /
// theme / the stuck indicator is registered by the inline bootstrap in
// index.html; this deferred module owns the live data feed for the per-view
// modules. The server stays the source of truth.
//
// Real-time push (issue #159): a single resilient EventSource consumes the
// consolidated /api/events stream (#155) and re-renders the views on every
// snapshot. There is no polling timer — the stuck banner and the worker /
// worktree / session tables update the instant the server state changes. The
// browser's EventSource auto-reconnects when the stream drops; we surface a
// subtle "reconnecting…" indicator while it is down and recreate the source
// ourselves if the browser gives up entirely.

import * as overview from "./overview.js";
import * as sessions from "./sessions.js";
import * as repos from "./repos.js";
import * as logs from "./logs.js";
import * as entries from "./entries.js";

// Backstop reconnect delay for the rare case where EventSource lands in the
// terminal CLOSED state (the browser only auto-retries from CONNECTING).
const RECONNECT_DELAY_MS = 3000;

let knownRepos = [];
let stream = null; // the single live EventSource
let reconnectTimer = null;

function appStore() {
  return window.Alpine && window.Alpine.store("app");
}

function setStreamConnected(connected) {
  const store = appStore();
  if (store) store.streamConnected = connected;
}

// Apply one consolidated snapshot to every live view. The payload mirrors the
// four per-resource endpoints the old poll fetched. The skills/commands editor
// is deliberately not driven from here: an incoming update must never clobber
// the textarea while the user is typing, so the editor only reacts to an actual
// change in the discovered-repo set (which just repopulates its picker).
function applySnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    console.error("Malformed snapshot (expected object):", snapshot);
    return;
  }
  const workers = snapshot.workers || [];
  const worktrees = snapshot.worktrees || [];
  const sess = snapshot.sessions || [];
  const stuck = snapshot.stuck || [];

  const stuckCount = overview.renderStuck(stuck);
  const store = appStore();
  if (store) store.stuckCount = stuckCount;

  overview.renderWorkers(workers);
  overview.renderWorktrees(worktrees);
  sessions.render(sess);
  repos.render(workers, worktrees);

  // Keep the per-repo pickers in sync with discovered repos (cheap, no clobber).
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

function connect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const es = new EventSource("/api/events");
  stream = es;

  es.onopen = () => {
    if (stream === es) setStreamConnected(true);
  };

  es.onmessage = (event) => {
    // Ignore late messages from a source we've already replaced.
    if (stream !== es) return;
    setStreamConnected(true);
    let snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch (err) {
      console.error("malformed dashboard snapshot", err);
      return;
    }
    applySnapshot(snapshot);
  };

  es.onerror = () => {
    if (stream !== es) return;
    setStreamConnected(false);
    // EventSource auto-reconnects while it can (readyState CONNECTING). If it
    // has given up (CLOSED), recreate it ourselves after a short backoff.
    if (es.readyState === EventSource.CLOSED && !reconnectTimer) {
      es.close();
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, RECONNECT_DELAY_MS);
    }
  };
}

function start() {
  entries.init();
  logs.init();
  connect();
}

// Deferred modules run after the document is fully parsed, so every element the
// view modules touch already exists.
start();
