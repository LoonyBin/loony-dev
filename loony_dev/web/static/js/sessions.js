"use strict";

// Remote-control session card (#157). Builds one card per repo's remote-control
// relay session, surfacing the claude.ai join URL as a large tappable "Open
// session" button plus a scannable QR code of that same URL, so the user can
// scan it from a phone while looking at the dashboard on desktop and land in the
// exact same remote-control session. The interactive surface is Claude's own
// hosted relay (see loony_dev/supervisor.py) — there is no PTY/websocket bridge
// here.
//
// The standalone Sessions grid was retired in #221: the only consumer is now the
// Live screen (repoDetail), which embeds renderSessionCard per repo in its
// compact variant. Pending (no join_url yet) and offline (process dead) sessions
// render an explicit state instead of a broken link.

import { formatAge, icon } from "./dom.js";

// How fresh the connection file's mtime must be before we flag the session as
// stale. The remote-control session rewrites it as it runs; a long-idle mtime
// usually means the relay has gone quiet.
const STALE_AFTER_S = 120;

// QR sizing: pixels per module and quiet-zone margin (also in pixels). The
// generated GIF is rendered 1:1 and capped by CSS, so a modest cell size keeps
// the data URL small while staying crisp.
const QR_CELL_PX = 4;
const QR_MARGIN_PX = 12;

// Seconds since the ISO-8601 timestamp, or null if absent/unparseable.
function ageSeconds(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

// Build an <img> holding a QR-code data URL for `url`, or null if the CDN QR
// library failed to load (ad-block / SRI / offline) so the caller can fall back
// to a link-only card.
function qrImage(url) {
  const qrcode = window.qrcode;
  if (typeof qrcode !== "function") return null;
  try {
    // typeNumber 0 = auto-pick the smallest symbol that fits; "M" error
    // correction tolerates a bit of phone-camera noise without bloating it.
    const qr = qrcode(0, "M");
    qr.addData(url);
    qr.make();
    const img = document.createElement("img");
    img.className = "session-qr-img";
    img.src = qr.createDataURL(QR_CELL_PX, QR_MARGIN_PX);
    img.alt = "QR code to open this session on a phone";
    return img;
  } catch (err) {
    console.error("QR render failed", err);
    return null;
  }
}

// claude.ai relay deep-links are always https. Reject anything else so a
// javascript:/data: URL from a tampered connection file can never become a
// clickable href. Returns the normalized href, or null if unusable.
function safeJoinUrl(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `session-badge session-badge-${kind}`;
  span.textContent = text;
  return span;
}

// One labelled line in the card's metadata block (e.g. "Session id  loony-x").
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

function livenessBadge(s) {
  if (s.alive === false) return badge("offline", "offline");
  if (s.alive === true) return badge("live", "live");
  return badge("unknown", "unknown");
}

// The action area below the metadata: the join button + QR when the session is
// reachable, or an explicit pending/offline notice otherwise.
function renderState(card, s) {
  // A dead process can't be joined no matter what URL it last advertised.
  if (s.alive === false) {
    const note = document.createElement("p");
    note.className = "session-note session-note-offline";
    note.textContent = "Session offline — the remote-control process is not running.";
    card.appendChild(note);
    return;
  }

  // Alive (or liveness unknown) but Claude hasn't emitted the deep-link yet.
  if (!s.join_url) {
    const note = document.createElement("p");
    note.className = "session-note";
    note.textContent = "Session starting… waiting for the join link.";
    card.appendChild(note);
    return;
  }

  // Alive with a URL, but it isn't a usable https deep-link.
  const joinUrl = safeJoinUrl(s.join_url);
  if (!joinUrl) {
    const note = document.createElement("p");
    note.className = "session-note session-note-offline";
    note.textContent = "Session link unavailable — the join URL is not a valid https link.";
    card.appendChild(note);
    return;
  }

  const joinLink = document.createElement("a");
  joinLink.className = "btn btn-primary session-open";
  joinLink.href = joinUrl;
  joinLink.target = "_blank";
  joinLink.rel = "noopener noreferrer";
  joinLink.textContent = "Open session ";
  joinLink.appendChild(icon("open_in_new"));
  card.appendChild(joinLink);

  const qrWrap = document.createElement("div");
  qrWrap.className = "session-qr";
  const img = qrImage(joinUrl);
  if (img) {
    qrWrap.appendChild(img);
    const hint = document.createElement("p");
    hint.className = "session-qr-hint muted";
    hint.textContent = "Scan to open on your phone";
    qrWrap.appendChild(hint);
  } else {
    const hint = document.createElement("p");
    hint.className = "session-qr-hint muted";
    hint.textContent = "QR code unavailable — use the button above.";
    qrWrap.appendChild(hint);
  }
  card.appendChild(qrWrap);
}

// Build one session card. The #189 Live repo-detail panel passes
// { compact: true } to drop the per-card repo title + liveness badge (its panel
// header already carries them); a non-compact call keeps them. The join button /
// QR / starting-offline-stale states are identical either way.
export function renderSessionCard(s, { compact = false } = {}) {
  const card = document.createElement("div");
  card.className = compact ? "session-card session-card-compact" : "session-card";

  if (!compact) {
    const head = document.createElement("div");
    head.className = "session-head";
    const title = document.createElement("span");
    title.className = "session-repo";
    title.textContent = s.repo || s.session_id || "(unknown repo)";
    head.append(title, livenessBadge(s));
    card.appendChild(head);
  }

  const meta = document.createElement("div");
  meta.className = "session-meta";
  meta.appendChild(metaRow("Session id", s.session_id || "—", true));
  if (s.mode) meta.appendChild(metaRow("Mode", s.mode, false));
  const age = ageSeconds(s.updated_at);
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
