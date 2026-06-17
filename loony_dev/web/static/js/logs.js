"use strict";

// Live log tail for a single worker repo: one EventSource per pane, bounded DOM,
// auto-scroll when pinned to the bottom. The standalone Logs page was folded in
// (#221) — log tailing now lives in the Live screen (repoDetail) and the Issue ▸
// PR detail view (issueDetail). This module is reduced to the reusable
// streamLog() helper both of those consume; the page's repo picker / loadLog /
// setRepos / init wiring was removed with the page.
//
// streamLog() also accepts an optional pipelineKey (#220): with one it tails that
// pipeline's per-scope log instead of the repo's worker log.

// Cap retained DOM lines so a long-lived stream can't grow unbounded.
// Matches the supervisor's default max_buffer_lines.
const MAX_LOG_LINES = 5000;

function isPinnedToBottom(pre) {
  // Treat "within 4px of the bottom" as pinned to tolerate sub-pixel scrolling.
  return pre.scrollHeight - pre.clientHeight - pre.scrollTop < 4;
}

// Live-tail a log into the `pre` element. With no `pipelineKey` it tails `repo`'s
// worker log (the original behaviour); given one it tails that pipeline's
// per-scope log (#220). Returns a stop function that closes the stream. Shared by
// the Live screen (#158) and the Issue ▸ PR detail view (#221) so both get the
// same bounded-DOM / auto-scroll behaviour.
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
