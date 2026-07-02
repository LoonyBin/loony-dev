"use strict";

// Remote-control server health card (#304). Each repo runs a persistent
// `claude rc` server (see loony_dev/supervisor.py), not a single session we
// follow: the user creates sessions on demand from claude.ai/code or the mobile
// app, each isolated in its own git worktree. So there is no join URL, QR, or
// per-session conversation to render here — only the server's *health*
// (running / restarting / errored) plus process liveness and staleness.
//
// The only consumer is the Live screen (repoDetail), which embeds
// renderSessionCard per repo in its compact variant.

import { formatAge } from "./dom.js";

// How fresh the connection file's mtime must be before we flag the server as
// stale. The supervisor rewrites it on launch / restart / error transitions; a
// long-idle mtime with a "running" status usually means the process has gone
// quiet without the supervisor noticing yet.
const STALE_AFTER_S = 120;

// Seconds since the ISO-8601 timestamp, or null if absent/unparseable.
function ageSeconds(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `session-badge session-badge-${kind}`;
  span.textContent = text;
  return span;
}

// One labelled line in the card's metadata block.
function metaRow(label, value, mono) {
  const row = document.createElement("div");
  row.className = "session-meta-row";
  const k = document.createElement("span");
  k.className = "session-meta-key";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = mono ? "session-meta-val mono" : "session-meta-val";
  v.textContent = value;
  row.append(k, v);
  return row;
}

// Map the supervisor's server health (status) — falling back to raw process
// liveness — to a labelled badge. `status` is the authoritative signal (#304);
// `alive` only backs it up when the connection file predates the status field.
function serverHealthBadge(s) {
  switch (s && s.status) {
    case "running": return badge("running", "live");
    case "restarting": return badge("restarting", "unknown");
    case "errored": return badge("errored", "offline");
    default:
      if (s && s.alive === false) return badge("offline", "offline");
      if (s && s.alive === true) return badge("running", "live");
      return badge("unknown", "unknown");
  }
}

// The action area below the metadata: a short note explaining where sessions
// live, keyed off server health so an errored/restarting server reads clearly.
function renderState(card, s) {
  const note = document.createElement("p");
  note.className = "session-note";
  const status = s && s.status;
  // Match serverHealthBadge's precedence: a known status is authoritative, so
  // `alive` only decides the note when status is absent. Otherwise a stale
  // `alive === false` could show an offline note under a "running" badge.
  const hasKnownStatus =
    status === "running" || status === "restarting" || status === "errored";
  if (status === "errored" || (!hasKnownStatus && s && s.alive === false)) {
    note.classList.add("session-note-offline");
    note.textContent =
      "Remote-control server not running — sessions can't be created until it recovers.";
  } else if (status === "restarting") {
    note.textContent = "Remote-control server restarting…";
  } else if (!hasKnownStatus && (!s || s.alive == null)) {
    // No status and no PID liveness: match serverHealthBadge's "unknown" badge
    // rather than implying the server is healthy.
    note.textContent = "Remote-control server health unknown.";
  } else {
    note.textContent =
      "Create and manage sessions on demand from claude.ai/code or the mobile app.";
  }
  card.appendChild(note);
}

// Build one server-health card. The #189 Live repo-detail panel passes
// { compact: true } to drop the per-card repo title + health badge (its panel
// header already carries them); a non-compact call keeps them.
export function renderSessionCard(s, { compact = false } = {}) {
  const card = document.createElement("div");
  card.className = compact ? "session-card session-card-compact" : "session-card";

  if (!compact) {
    const head = document.createElement("div");
    head.className = "session-head";
    const title = document.createElement("span");
    title.className = "session-repo";
    title.textContent = (s && (s.repo || s.session_id)) || "(unknown repo)";
    head.append(title, serverHealthBadge(s));
    card.appendChild(head);
  }

  const meta = document.createElement("div");
  meta.className = "session-meta";
  const age = ageSeconds(s && s.updated_at);
  if (age != null) {
    const row = metaRow("Updated", `${formatAge(age)} ago`, false);
    if (age > STALE_AFTER_S) {
      row.classList.add("session-stale");
      row.title = "The connection file hasn't been touched recently.";
    }
    meta.appendChild(row);
  }
  card.appendChild(meta);

  renderState(card, s);
  return card;
}
