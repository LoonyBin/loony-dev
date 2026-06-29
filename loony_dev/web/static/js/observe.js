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

// The modal's open stream ({ close }), or null. The reusable core (streamObserve)
// holds its own per-stream state, so a caller-provided host (e.g. the #190
// pipeline-detail conversation) streams independently of this modal.
let modalStream = null;

function wsUrl(taskKey) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/api/sessions/${encodeURIComponent(taskKey)}/observe`;
}

// WS URL for the always-on base (remote-control) session of `repo` ("owner/name",
// #282). The base session has no task key, so it is addressed by its repo path:
// each segment is encoded independently (repo names carry no slash) and the same
// scheme logic as wsUrl() is reused.
export function liveObserveUrl(repo) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const [owner, name] = String(repo).split("/");
  const path = `${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
  return `${scheme}//${location.host}/api/repos/${path}/live/observe`;
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

function block(kind) {
  const el = document.createElement("div");
  el.className = `obs obs-${kind}`;
  return el;
}

function label(text) {
  const el = document.createElement("div");
  el.className = "obs-label";
  el.textContent = text;
  return el;
}

function body(text) {
  const el = document.createElement("div");
  el.className = "obs-body";
  el.textContent = text || "";
  return el;
}

function renderUser(ev) {
  const el = block("user");
  el.appendChild(label("user"));
  el.appendChild(body(ev.text));
  return el;
}

function renderAssistant(ev) {
  const el = block("assistant");
  el.appendChild(label("assistant"));
  el.appendChild(body(ev.text));
  return el;
}

// Thinking is collapsed by default — a <details> the reader can expand.
function renderThinking(ev) {
  const el = block("thinking");
  const det = document.createElement("details");
  const sum = document.createElement("summary");
  sum.textContent = "thinking";
  det.appendChild(sum);
  det.appendChild(body(ev.text));
  el.appendChild(det);
  return el;
}

function renderToolUse(ev, stream) {
  const el = block("tool");
  const lab = label(ev.tool || "tool");
  lab.prepend(icon("build"));
  // Separate the icon from the tool name so a font-load failure degrades to
  // readable "build tool" rather than "buildtool" (matches attach.js spacing).
  lab.insertBefore(document.createTextNode(" "), lab.childNodes[1] || null);
  el.appendChild(lab);
  const args = formatArgs(ev.args);
  if (args) {
    const pre = document.createElement("pre");
    pre.className = "obs-tool-args";
    pre.textContent = args;
    el.appendChild(pre);
  }
  // A slot the matching tool_result fills in (paired by tool_use_id).
  const result = document.createElement("div");
  result.className = "obs-tool-result";
  el.appendChild(result);
  if (ev.tool_use_id && stream) stream.toolCards.set(ev.tool_use_id, result);
  return el;
}

function renderToolResult(ev, stream) {
  // Prefer attaching the result to its originating tool card; if the card isn't
  // present (result before call, or out-of-order), render a standalone block.
  const slot = ev.tool_use_id && stream ? stream.toolCards.get(ev.tool_use_id) : null;
  const target = slot || block("tool");
  if (!slot) target.appendChild(label("tool result"));
  const out = document.createElement("pre");
  out.className = ev.is_error ? "obs-tool-out obs-tool-err" : "obs-tool-out";
  out.textContent = ev.text || "";
  target.appendChild(out);
  return slot ? null : target; // null => already attached in place
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
  const atBottom = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 40;
  conv.appendChild(node);
  if (atBottom) conv.scrollTop = conv.scrollHeight;
}

// Reusable observe core (issue #190): connect to a task's observe WS and render
// its transcript into *conv* (a caller-provided container element). *onStatus*
// (optional) is called with (text, kind) on connection-state changes. Returns a
// controller `{ close }`; the caller is responsible for tearing the stream down
// when its host goes away. The modal surface below is one such caller.
export function streamObserve(taskKey, conv, onStatus) {
  return streamObserveAt(wsUrl(taskKey), conv, onStatus);
}

// The streaming core, targeting an arbitrary observe WS URL (#282). Both the
// per-task observe (`wsUrl(taskKey)`) and the per-repo base-session observe
// (`liveObserveUrl(repo)`) feed the identical event pump and render path; only the
// URL differs. Returns the same `{ close }` controller as streamObserve.
export function streamObserveAt(url, conv, onStatus) {
  const status = onStatus || (() => {});
  const stream = { ws: null, conv, seen: new Set(), toolCards: new Map() };
  if (conv) conv.innerHTML = "";
  const ws = new WebSocket(url);
  stream.ws = ws;
  status("connecting…", null);
  ws.onopen = () => status("live", "live");
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    applyEvent(stream, ev);
  };
  ws.onclose = () => status("disconnected", "off");
  ws.onerror = () => status("connection error", "off");
  return {
    close() {
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
