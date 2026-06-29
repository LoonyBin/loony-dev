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
//
// streamActivity() (#259) is the Live screen's transcript source: it consumes the
// cross-fleet structured activity SSE (#270, /api/activity/stream), filters to one
// repo, and renders each event as a TimelineRow turn — a conversation-style
// transcript, not a raw <pre> log dump.

import { TimelineRow } from "/static/ds/components/sessions.js";
import { formatAge, avatarTone } from "./dom.js";

// Cap retained DOM lines so a long-lived stream can't grow unbounded.
// Matches the supervisor's default max_buffer_lines.
const MAX_LOG_LINES = 5000;

// Cap retained transcript rows. The activity feed is far sparser than the raw
// worker log (one row per lifecycle event, not per stderr line), so a much
// smaller bound keeps the panel light while holding plenty of history.
const MAX_ACTIVITY_ROWS = 500;

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

// --- Activity transcript (#259) ------------------------------------------------
//
// The Live screen renders the repo's structured agent activity as a transcript.
// The three event→props mappers below mirror issueDetail.js's renderTimeline; the
// minor duplication is deliberate (#259 chose not to rewire issueDetail or add a
// shared activity module). A later dedupe into a ds adapter is a possible
// follow-up.

// state_tone (event store vocabulary) → TimelineRow dot tone (lifecycle key).
function activityTone(stateTone) {
  const t = String(stateTone || "").toLowerCase();
  if (t === "blocked") return "blocked";
  if (t === "review") return "review";
  return "active"; // active / none / unknown
}

// Short chip label from a structured event `type` (#269 closed vocabulary), or
// null to omit the chip.
const ACTIVITY_TYPE_CHIP = {
  dispatched: "dispatch",
  phase_enter: "phase",
  turn_start: "turn",
  turn_complete: "turn",
  error: "error",
  terminal: "done",
};

// Two-letter avatar glyph for an activity actor (a config-resolved identity:
// the bot account, capo, human, system). A non-default bot login derives its
// own initials rather than assuming `trixy`.
function activityGlyph(actor) {
  const a = String(actor || "").toLowerCase();
  if (a === "human" || a === "operator") return "OP";
  if (a === "system") return "SY";
  if (a === "capo") return "CA";
  if (a === "trixy" || a === "bot") return "TX";
  const initials = String(actor || "bot").replace(/[^a-z0-9]/gi, "").slice(0, 2);
  return initials ? initials.toUpperCase() : "BT";
}

// Parse an ISO-8601 event timestamp into a relative age in seconds, or null when
// unparseable. The #269 event log stamps ISO-8601 with an explicit offset, which
// Date.parse handles directly; the legacy zoneless "2026-06-17 12:00:00,123"
// format is normalised and pinned to UTC (append `Z`) so the age isn't shifted
// by the viewer's timezone.
function activityAge(ts) {
  if (!ts) return null;
  const raw = String(ts);
  const legacy = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?$/.test(raw);
  const normalized = legacy ? `${raw.replace(",", ".").replace(" ", "T")}Z` : raw;
  const t = Date.parse(normalized);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

// Build one transcript row from a structured activity event
// ({ts, actor, type, what, target, state_tone}, #269) via the shared TimelineRow
// factory — flat shape, mirroring issueDetail's renderTimeline.
function activityRow(ev) {
  const actor = ev.actor || "system";
  const age = activityAge(ev.ts);
  const row = TimelineRow({
    rail: false,
    whenAlign: "right",
    state: activityTone(ev.state_tone),
    avatar: { tone: avatarTone(actor), glyph: activityGlyph(actor) },
    title: ev.what || "",
    chip: ACTIVITY_TYPE_CHIP[String(ev.type || "")] || null,
    when: age == null ? "" : formatAge(age),
  });
  // The dot + avatar are decorative; hide them from the a11y tree.
  row.querySelectorAll(".tl-dot, .ava").forEach((n) => n.setAttribute("aria-hidden", "true"));
  return row;
}

// Live transcript of `repo`'s structured agent activity into the `host` element.
// Consumes the cross-fleet activity SSE (#270) and filters client-side to this
// repo (each event carries its `repo`). Bounds retained rows, auto-scrolls when
// pinned, and shows an honest empty state until the first matching event arrives.
// Returns a stop function that closes the stream. (#259)
export function streamActivity(repo, host, lines = MAX_ACTIVITY_ROWS) {
  // One bound for both the fetch backlog and the retained-row trim, so they can't
  // drift if a caller passes a custom `lines`.
  const maxRows = Math.max(1, Number(lines) || MAX_ACTIVITY_ROWS);
  host.innerHTML = "";
  const empty = document.createElement("p");
  empty.className = "muted";
  empty.textContent = "No agent activity for this repo yet.";
  host.appendChild(empty);

  const es = new EventSource(`/api/activity/stream?lines=${encodeURIComponent(maxRows)}`);
  let closed = false;

  es.onmessage = (event) => {
    if (closed) return;
    let ev;
    try {
      ev = JSON.parse(event.data);
    } catch (e) {
      return; // ignore a malformed frame rather than tearing down the stream
    }
    if (!ev || ev.repo !== repo) return; // not this repo's activity
    if (empty.parentNode === host) host.removeChild(empty);

    const pinned = isPinnedToBottom(host);
    host.appendChild(activityRow(ev));
    // Trim oldest rows to bound memory (same cap as the fetch backlog).
    while (host.childElementCount > maxRows) {
      host.removeChild(host.firstElementChild);
    }
    if (pinned) host.scrollTop = host.scrollHeight;
  };

  es.onerror = () => {
    // EventSource auto-reconnects; only surface an error if nothing arrived yet.
    if (!closed && empty.parentNode === host) {
      empty.textContent = "(activity stream unavailable)";
    }
  };

  return () => { closed = true; es.close(); };
}
