"use strict";

// JSONL-driven observe surface (issue #202). Renders a task session's
// conversation straight from its on-disk transcript — no live PTY required, so a
// parked session between turns reads identically to an active one. This is the
// default read-only observe view; the xterm.js Attach terminal (attach.js) stays
// for the live "drive" case.
//
// The backend (/api/sessions/{task_key}/observe) streams structured JSON events
// — the full backlog first, then live updates as the transcript grows. We render
// each event by kind and dedupe by its stable `id` so a reconnect (which replays
// the whole transcript from zero) yields an identical DOM no matter how many
// times the client reconnects.

import { openModalA11y, closeModalA11y } from "./modal.js";
import { icon } from "./dom.js";
import { ChatBubble } from "/static/ds/components/sessions.js";

// The observe stream has no per-worker identity, so every bot ("received") bubble
// carries one stable avatar glyph. The bot account is "trixy" → initials "TX"
// (Avatar uppercases + slices to two chars). Kept as a single module constant so
// the bot identity stays caller-supplied per the DS ChatBubble contract (glyph is
// never baked into the factory) rather than hard-coded inside each render fn.
const BOT_GLYPH = "TX";

// The modal's open stream ({ close }), or null. The reusable core (streamObserve)
// holds its own per-stream state, so a caller-provided host (e.g. the #190
// pipeline-detail conversation) streams independently of this modal.
let modalStream = null;

function wsUrl(taskKey) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/api/sessions/${encodeURIComponent(taskKey)}/observe`;
}

function setStatus(text, kind) {
  const el = document.getElementById("observe-status");
  if (!el) return;
  el.textContent = text;
  el.className = kind ? `observe-status observe-status-${kind}` : "observe-status muted";
}

// Pretty-print tool args (a JSON object) compactly; fall back to a string.
function formatArgs(args) {
  if (args == null) return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch (_) {
    return String(args);
  }
}

// A lightweight, non-bubble system divider (turn-ended / interrupted). These are
// not chat turns in the prototype, so they stay centred dividers rather than
// ChatBubbles.
function block(kind) {
  const el = document.createElement("div");
  el.className = `obs obs-${kind}`;
  return el;
}

// A pre-wrap text block for the body of a thinking bubble (kept monospace + wrap
// so multi-line reasoning stays legible inside the bubble).
function body(text) {
  const el = document.createElement("div");
  el.className = "obs-body";
  el.textContent = text || "";
  return el;
}

// A collapsible tool card (<details>): the summary shows a glyph + name and the
// caller fills the body (args <pre>, result slot). Default-open so tool output
// stays legible without a click (matching the pre-#283 always-visible behaviour),
// while remaining collapsible per the acceptance criteria.
function toolCard(name) {
  const det = document.createElement("details");
  det.className = "obs-tool-card";
  det.open = true;
  const sum = document.createElement("summary");
  sum.className = "obs-tool-summary";
  sum.appendChild(icon("build"));
  // Separate the icon from the tool name so a font-load failure degrades to a
  // readable "build tool" rather than "buildtool" (matches attach.js spacing).
  sum.appendChild(document.createTextNode(" " + (name || "tool")));
  det.appendChild(sum);
  return det;
}

// User turns are right-aligned accent bubbles with no avatar (DS `sent` side).
function renderUser(ev) {
  return ChatBubble({ side: "sent", class: "obs-user", children: ev.text || "" });
}

// Assistant turns are left-aligned bubbles carrying the bot avatar (DS `received`).
function renderAssistant(ev) {
  return ChatBubble({ side: "received", glyph: BOT_GLYPH, class: "obs-assistant", children: ev.text || "" });
}

// Thinking is a received bubble holding a default-collapsed <details> the reader
// can expand; the muted/italic treatment lives in CSS (.obs-thinking .bubble).
function renderThinking(ev) {
  const det = document.createElement("details");
  det.className = "obs-thinking-card";
  const sum = document.createElement("summary");
  sum.className = "obs-thinking-summary";
  sum.textContent = "thinking";
  det.appendChild(sum);
  det.appendChild(body(ev.text));
  return ChatBubble({ side: "received", glyph: BOT_GLYPH, class: "obs-thinking", children: det });
}

function renderToolUse(ev, stream) {
  const det = toolCard(ev.tool);
  const args = formatArgs(ev.args);
  if (args) {
    const pre = document.createElement("pre");
    pre.className = "obs-tool-args";
    pre.textContent = args;
    det.appendChild(pre);
  }
  // A slot the matching tool_result fills in (paired by tool_use_id). Kept verbatim
  // (div + toolCards.set) so the #202 pairing model is unchanged by the restyle.
  const result = document.createElement("div");
  result.className = "obs-tool-result";
  det.appendChild(result);
  if (ev.tool_use_id && stream) stream.toolCards.set(ev.tool_use_id, result);
  return ChatBubble({ side: "received", glyph: BOT_GLYPH, class: "obs-tool", children: det });
}

function renderToolResult(ev, stream) {
  // Prefer attaching the result to its originating tool card; if the card isn't
  // present (result before call, or out-of-order), render a standalone tool bubble.
  const slot = ev.tool_use_id && stream ? stream.toolCards.get(ev.tool_use_id) : null;
  const out = document.createElement("pre");
  out.className = ev.is_error ? "obs-tool-out obs-tool-err" : "obs-tool-out";
  out.textContent = ev.text || "";
  if (slot) {
    // Attaching into an existing card grows the conversation just like an append,
    // so keep a bottom-pinned viewer pinned (applyEvent's append path is skipped
    // when we return null, so we run the same scroll-pin here).
    withBottomPin(stream && stream.conv, () => slot.appendChild(out));
    return null; // already attached in place
  }
  const det = toolCard("tool result");
  det.appendChild(out);
  return ChatBubble({ side: "received", glyph: BOT_GLYPH, class: "obs-tool", children: det });
}

function renderStop(ev) {
  const el = block("stop");
  el.textContent = `— turn ended (${ev.stop_reason || "end_turn"}) —`;
  return el;
}

function renderInterrupt() {
  const el = block("interrupt");
  el.textContent = "— interrupted by user —";
  return el;
}

function renderEvent(ev, stream) {
  switch (ev.kind) {
    case "user": return renderUser(ev);
    case "assistant": return renderAssistant(ev);
    case "thinking": return renderThinking(ev);
    case "tool_use": return renderToolUse(ev, stream);
    case "tool_result": return renderToolResult(ev, stream);
    case "stop": return renderStop(ev);
    case "interrupt": return renderInterrupt(ev);
    default: return null;
  }
}

// Run a DOM mutation that grows `conv` while preserving bottom-stickiness: a
// viewer already pinned to the bottom is kept there, while one scrolled up is
// left undisturbed. The at-bottom test must be read BEFORE the mutation, since
// appending changes scrollHeight. Shared by the append path and the
// tool_result attach-in-place path so both scroll consistently.
function withBottomPin(conv, mutate) {
  if (!conv) { mutate(); return; }
  const atBottom = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 40;
  mutate();
  if (atBottom) conv.scrollTop = conv.scrollHeight;
}

// Apply one event idempotently into the stream's container: a previously-seen
// `id` is a no-op, so replaying the whole transcript on reconnect never
// duplicates a node.
function applyEvent(stream, ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.id && stream.seen.has(ev.id)) return;
  if (ev.id) stream.seen.add(ev.id);
  const node = renderEvent(ev, stream);
  if (!node) return; // e.g. a tool_result attached to an existing card in place
  const conv = stream.conv;
  if (!conv) return;
  withBottomPin(conv, () => conv.appendChild(node));
}

// Reusable observe core (issue #190): connect to a task's observe WS and render
// its transcript into *conv* (a caller-provided container element). *onStatus*
// (optional) is called with (text, kind) on connection-state changes. Returns a
// controller `{ close }`; the caller is responsible for tearing the stream down
// when its host goes away. The modal surface below is one such caller.
export function streamObserve(taskKey, conv, onStatus) {
  return streamObserveAt(wsUrl(taskKey), conv, onStatus);
}

// The streaming core, targeting an arbitrary observe WS URL. The per-task observe
// (`wsUrl(taskKey)`) feeds it via streamObserve; kept as a separate seam so any
// future observe surface can reuse the identical event pump and render path.
// Returns the same `{ close }` controller as streamObserve.
export function streamObserveAt(url, conv, onStatus) {
  const status = onStatus || (() => {});
  // `seen`/`toolCards`/`conv` persist across reconnects: the backend replays the
  // full backlog from zero on every (re)connect, and applyEvent dedupes by stable
  // `id`, so a dropped socket recovers into the identical DOM without clearing it.
  const stream = { ws: null, conv, seen: new Set(), toolCards: new Map() };
  if (conv) conv.innerHTML = "";

  // Reconnect with exponential backoff so the Live screen's primary conversation
  // pane (#282) is as resilient to a transient drop / dashboard restart as the
  // sidebar's auto-reconnecting EventSource feed — instead of going permanently
  // dead on the first close. `closed` guards against a reconnect racing an
  // explicit close(); `retryTimer` is cleared on teardown.
  let closed = false;
  let retryTimer = null;
  let backoff = 1000;
  const MAX_BACKOFF = 15000;

  function connect() {
    if (closed) return;
    const ws = new WebSocket(url);
    stream.ws = ws;
    status("connecting…", null);
    ws.onopen = () => {
      backoff = 1000; // a successful connection resets the backoff
      status("live", "live");
    };
    ws.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      applyEvent(stream, ev);
    };
    ws.onerror = () => status("connection error", "off");
    ws.onclose = (event) => {
      stream.ws = null;
      if (closed) return;
      // 4404 is the backend's deliberate "no base/observable session" close
      // (routes.py) — an expected, non-transient state (e.g. the base session
      // hasn't started or written its transcript yet), not a network blip. Show
      // it distinctly but keep retrying with backoff so the pane recovers on its
      // own once the session comes up.
      const unavailable = event && event.code === 4404;
      status(unavailable ? "no session" : "reconnecting…", "off");
      retryTimer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, MAX_BACKOFF);
    };
  }

  connect();
  return {
    close() {
      closed = true;
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      if (stream.ws) {
        try { stream.ws.close(); } catch (_) { /* already closing */ }
        stream.ws = null;
      }
    },
  };
}

export function openObserve(taskKey) {
  const modal = document.getElementById("observe-modal");
  const title = document.getElementById("observe-title");
  if (!modal) return;
  closeObserve(); // drop any prior connection
  modal.hidden = false;
  if (title) title.textContent = `Observing: ${taskKey}`;
  const conv = document.getElementById("observe-conv");
  modalStream = streamObserve(taskKey, conv, setStatus);
  const closeBtn = document.getElementById("observe-close");
  openModalA11y(modal, closeObserve, closeBtn);
}

export function closeObserve() {
  if (modalStream) {
    modalStream.close();
    modalStream = null;
  }
  const modal = document.getElementById("observe-modal");
  if (modal && !modal.hidden) {
    modal.hidden = true;
    closeModalA11y(modal);
  }
}

export function init() {
  const close = document.getElementById("observe-close");
  if (close) close.addEventListener("click", closeObserve);
}
