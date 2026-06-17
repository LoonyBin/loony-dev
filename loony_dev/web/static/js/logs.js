"use strict";

// Live log tail for a single worker repo. Behaviour is unchanged from the
// original dashboard: one EventSource at a time, bounded DOM, auto-scroll when
// pinned to the bottom. Since #220 a secondary "scope" picker lets the same
// pane tail a single pipeline's log (worker-scope is the default).

// Cap retained DOM lines so a long-lived stream can't grow unbounded.
// Matches the supervisor's default max_buffer_lines.
const MAX_LOG_LINES = 5000;

// Value of the scope picker's default option = the universal worker log.
const WORKER_SCOPE = "";

let activeStream = null; // stop fn for the single live stream (closed when switching repos)

function closeActiveStream() {
  if (activeStream) {
    activeStream();
    activeStream = null;
  }
}

// Reset the log pane to its idle state when no repo is selected.
function clearLogPane() {
  const title = document.getElementById("log-title");
  const pre = document.getElementById("log");
  if (title) title.textContent = "";
  if (pre) pre.textContent = "Select a worker repo to live-tail its log.";
}

function isPinnedToBottom(pre) {
  // Treat "within 4px of the bottom" as pinned to tolerate sub-pixel scrolling.
  return pre.scrollHeight - pre.clientHeight - pre.scrollTop < 4;
}

// Live-tail a log into the `pre` element. With no `pipelineKey` it tails
// `repo`'s worker log (the original behaviour); given one it tails that
// pipeline's per-scope log (#220). Returns a stop function that closes the
// stream. Shared by the global Logs view and the per-repo drill-down (#158) so
// both get the same bounded-DOM / auto-scroll behaviour.
export function streamLog(repo, pre, pipelineKey = null) {
  pre.textContent = "";
  const url = pipelineKey
    ? `/api/logs/${repo}/pipelines/${encodeURIComponent(pipelineKey)}/stream`
    : `/api/logs/${repo}/stream`;
  const es = new EventSource(url);
  let closed = false;

  es.onmessage = (event) => {
    if (closed) return;
    const pinned = isPinnedToBottom(pre);
    pre.textContent += (pre.textContent ? "\n" : "") + event.data;
    // Trim to the last MAX_LOG_LINES to bound memory.
    const lines = pre.textContent.split("\n");
    if (lines.length > MAX_LOG_LINES) {
      pre.textContent = lines.slice(lines.length - MAX_LOG_LINES).join("\n");
    }
    if (pinned) pre.scrollTop = pre.scrollHeight;
  };

  es.onerror = () => {
    // EventSource auto-reconnects; only surface an error if nothing arrived yet.
    if (!closed && !pre.textContent) {
      pre.textContent = "(log stream unavailable)";
    }
  };

  return () => { closed = true; es.close(); };
}

export function loadLog(repo, pipelineKey = null) {
  const title = document.getElementById("log-title");
  const pre = document.getElementById("log");
  const select = document.getElementById("log-repo");
  if (select && select.value !== repo) select.value = repo;

  // Close any previous stream so the browser doesn't leak connections.
  closeActiveStream();

  const scopeLabel = pipelineKey ? `${repo} · ${pipelineKey}` : repo;
  title.textContent = `— ${scopeLabel} (live)`;
  activeStream = streamLog(repo, pre, pipelineKey);
}

// Populate the scope picker with the pipelines that have a log for `repo`,
// keeping "Worker" as the default. Failures leave only the worker scope so the
// pane keeps working when the pipeline list endpoint is unavailable.
async function loadScopes(repo) {
  const scope = document.getElementById("log-pipeline");
  if (!scope) return;
  scope.innerHTML = "";
  const workerOpt = document.createElement("option");
  workerOpt.value = WORKER_SCOPE;
  workerOpt.textContent = "Worker";
  scope.appendChild(workerOpt);
  scope.value = WORKER_SCOPE;
  if (!repo) return;
  try {
    const resp = await fetch(`/api/logs/${repo}/pipelines`);
    if (!resp.ok) return;
    const body = await resp.json();
    // Ignore a stale response: the user may have switched repos while this
    // fetch was in flight, so only populate when it's still the current repo.
    const select = document.getElementById("log-repo");
    if (select && select.value !== repo) return;
    for (const key of body.pipelines || []) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = key;
      scope.appendChild(opt);
    }
  } catch (_) {
    // Network/parse error: worker scope remains, which is the safe default.
  }
}

// Keep the repo picker in sync with discovered repos, preserving any selection.
export function setRepos(repos) {
  const select = document.getElementById("log-repo");
  if (!select) return;
  const prev = select.value;
  select.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = repos.length ? "Select a repo…" : "No repos discovered";
  select.appendChild(placeholder);
  for (const r of repos) {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r;
    select.appendChild(opt);
  }
  select.value = repos.includes(prev) ? prev : "";
  // The selected repo dropped out: stop streaming its now-stale log.
  if (!select.value) {
    closeActiveStream();
    clearLogPane();
    loadScopes("");
  }
}

export function init() {
  const select = document.getElementById("log-repo");
  if (!select) return;
  const scope = document.getElementById("log-pipeline");
  select.addEventListener("change", () => {
    if (select.value) {
      loadScopes(select.value);
      loadLog(select.value);
    } else {
      closeActiveStream();
      clearLogPane();
      loadScopes("");
    }
  });
  if (scope) {
    scope.addEventListener("change", () => {
      if (select.value) loadLog(select.value, scope.value || null);
    });
  }
}
