"use strict";

// Live log tail for a single worker repo. Behaviour is unchanged from the
// original dashboard: one EventSource at a time, bounded DOM, auto-scroll when
// pinned to the bottom.

// Cap retained DOM lines so a long-lived stream can't grow unbounded.
// Matches the supervisor's default max_buffer_lines.
const MAX_LOG_LINES = 5000;

let activeStream = null; // the single live EventSource (closed when switching repos)

function isPinnedToBottom(pre) {
  // Treat "within 4px of the bottom" as pinned to tolerate sub-pixel scrolling.
  return pre.scrollHeight - pre.clientHeight - pre.scrollTop < 4;
}

export function loadLog(repo) {
  const title = document.getElementById("log-title");
  const pre = document.getElementById("log");
  const select = document.getElementById("log-repo");
  if (select && select.value !== repo) select.value = repo;

  // Close any previous stream so the browser doesn't leak connections.
  if (activeStream) {
    activeStream.close();
    activeStream = null;
  }

  title.textContent = `— ${repo} (live)`;
  pre.textContent = "";

  const es = new EventSource(`/api/logs/${repo}/stream`);
  activeStream = es;

  es.onmessage = (event) => {
    // Ignore late messages from a stream we've already switched away from.
    if (activeStream !== es) return;
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
    if (activeStream === es && !pre.textContent) {
      pre.textContent = "(log stream unavailable)";
    }
  };
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
}

export function init() {
  const select = document.getElementById("log-repo");
  if (!select) return;
  select.addEventListener("change", () => {
    if (select.value) loadLog(select.value);
  });
}
