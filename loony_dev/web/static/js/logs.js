"use strict";

// Live log tail for a single worker repo. Behaviour is unchanged from the
// original dashboard: one EventSource at a time, bounded DOM, auto-scroll when
// pinned to the bottom.

// Cap retained DOM lines so a long-lived stream can't grow unbounded.
// Matches the supervisor's default max_buffer_lines.
const MAX_LOG_LINES = 5000;

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

// Live-tail `repo`'s worker log into the `pre` element. Returns a stop function
// that closes the stream. Shared by the global Logs view and the per-repo
// drill-down (#158) so both get the same bounded-DOM / auto-scroll behaviour.
export function streamLog(repo, pre) {
  pre.textContent = "";
  const es = new EventSource(`/api/logs/${repo}/stream`);
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

export function loadLog(repo) {
  const title = document.getElementById("log-title");
  const pre = document.getElementById("log");
  const select = document.getElementById("log-repo");
  if (select && select.value !== repo) select.value = repo;

  // Close any previous stream so the browser doesn't leak connections.
  closeActiveStream();

  title.textContent = `— ${repo} (live)`;
  activeStream = streamLog(repo, pre);
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
  }
}

export function init() {
  const select = document.getElementById("log-repo");
  if (!select) return;
  select.addEventListener("change", () => {
    if (select.value) {
      loadLog(select.value);
    } else {
      closeActiveStream();
      clearLogPane();
    }
  });
}
