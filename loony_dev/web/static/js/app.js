"use strict";

// App-shell data orchestrator. The Alpine store that drives view switching /
// theme / the stuck indicator is registered by the inline bootstrap in
// index.html; this deferred module owns the live data feed for the per-view
// modules. The server stays the source of truth.
//
// Real-time push (issue #159): a single resilient EventSource consumes the
// consolidated /api/events stream (#155) and re-renders the views on every
// snapshot. There is no polling timer — the stuck banner, the per-repo roll-up
// cards, the session table, and the per-repo drill-down (#158) update the
// instant the server state changes. The browser's EventSource auto-reconnects
// when the stream drops; we surface a subtle "reconnecting…" indicator while it
// is down and recreate the source ourselves if the browser gives up entirely.

import * as overview from "./overview.js";
import * as fleet from "./fleet.js";
import * as repoDetail from "./repoDetail.js";
import * as issueDetail from "./issueDetail.js";
import * as entries from "./entries.js";
import * as attach from "./attach.js";
import * as observe from "./observe.js";

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
// per-resource endpoints the old poll fetched. The skills/commands editor
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
  const stuck = snapshot.stuck || [];

  const stuckCount = overview.renderStuck(stuck);
  const store = appStore();
  if (store) store.stuckCount = stuckCount;

  // Sessions (remote-control grid + per-task table) are folded into Live and the
  // Issue ▸ PR detail view (#221), so there are no standalone session writers
  // here anymore — the remote-control card renders per-repo inside repoDetail.
  // Fleet is the primary destination: a cross-repo stat strip + board/kanban
  // built by joining the snapshot collections on the pipeline key.
  fleet.render(snapshot);
  repoDetail.update(snapshot);
  issueDetail.update(snapshot);

  // Keep the per-repo pickers in sync with discovered repos (cheap, no clobber).
  const next = [...new Set([
    ...workers.map((w) => w.repo).filter(Boolean),
    ...worktrees.map((w) => w.repo).filter(Boolean),
  ])].sort();
  if (next.join("\n") !== knownRepos.join("\n")) {
    knownRepos = next;
    entries.setKnownRepos(next);
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

// Dispatch the active view → its detail module's show(), and keep dispatching as
// the route changes. This used to live in two inline `x-effect`s in index.html,
// but those were load-order fragile (issue #239): Alpine evaluates an x-effect
// once synchronously at startup, *before* this deferred module assigns
// window.repoDetail / window.issueDetail. The effects guarded on
// `window.repoDetail && …`, so that first pass short-circuited before ever
// reading view/repo — registering zero reactive dependencies and never
// re-running, so show() was effectively never called and the detail panels
// stayed empty. Driving the dispatch from here (after init() has set the
// globals, with Alpine already started) mirrors how Fleet is rendered from
// applySnapshot() and removes the coupling at the source.
function wireDetailViews() {
  const store = appStore();
  if (!store || !window.Alpine || !window.Alpine.effect) return;
  window.Alpine.effect(() => {
    // Read the tracked fields first and unconditionally so Alpine always
    // registers them as dependencies (even before the module globals exist),
    // guaranteeing the effect re-runs on every nav click / hashchange.
    const view = store.view;
    const repo = store.repo;
    const pipelineRoute = store.pipelineRoute;
    if (window.repoDetail) {
      window.repoDetail.show(view === "live" ? repo : null);
    }
    if (window.issueDetail) {
      window.issueDetail.show(
        view === "pipeline" ? repo : null,
        view === "pipeline" ? pipelineRoute : null,
      );
    }
  });
}

function start() {
  entries.init();
  attach.init();
  observe.init();
  repoDetail.init();
  fleet.init();
  issueDetail.init();
  wireDetailViews();
  connect();
}

// Deferred modules run after the document is fully parsed, so every element the
// view modules touch already exists.
start();
