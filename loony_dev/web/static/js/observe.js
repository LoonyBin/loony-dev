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

let active = null; // { ws, taskKey, seen:Set, toolCards:Map } for the open view

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

function renderToolUse(ev) {
  const el = block("tool");
  el.appendChild(label(`🔧 ${ev.tool || "tool"}`));
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
  if (ev.tool_use_id && active) active.toolCards.set(ev.tool_use_id, result);
  return el;
}

function renderToolResult(ev) {
  // Prefer attaching the result to its originating tool card; if the card isn't
  // present (result before call, or out-of-order), render a standalone block.
  const slot = ev.tool_use_id && active ? active.toolCards.get(ev.tool_use_id) : null;
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

function renderEvent(ev) {
  switch (ev.kind) {
    case "user": return renderUser(ev);
    case "assistant": return renderAssistant(ev);
    case "thinking": return renderThinking(ev);
    case "tool_use": return renderToolUse(ev);
    case "tool_result": return renderToolResult(ev);
    case "stop": return renderStop(ev);
    case "interrupt": return renderInterrupt(ev);
    default: return null;
  }
}

// Apply one event idempotently: a previously-seen `id` is a no-op, so replaying
// the whole transcript on reconnect never duplicates a node.
function applyEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.id && active.seen.has(ev.id)) return;
  if (ev.id) active.seen.add(ev.id);
  const node = renderEvent(ev);
  if (!node) return; // e.g. a tool_result attached to an existing card in place
  const conv = document.getElementById("observe-conv");
  if (!conv) return;
  const atBottom = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 40;
  conv.appendChild(node);
  if (atBottom) conv.scrollTop = conv.scrollHeight;
}

function connect(taskKey) {
  const conv = document.getElementById("observe-conv");
  if (conv) conv.innerHTML = "";
  // Fresh per-connection state: on reconnect we replay from zero, so the seen-id
  // set and tool-card map must reset too (the DOM was cleared above).
  active.seen = new Set();
  active.toolCards = new Map();

  const ws = new WebSocket(wsUrl(taskKey));
  active.ws = ws;
  setStatus("connecting…");
  ws.onopen = () => setStatus("live", "live");
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    applyEvent(ev);
  };
  ws.onclose = () => setStatus("disconnected", "off");
  ws.onerror = () => setStatus("connection error", "off");
}

export function openObserve(taskKey) {
  const modal = document.getElementById("observe-modal");
  const title = document.getElementById("observe-title");
  if (!modal) return;
  closeObserve(); // drop any prior connection
  modal.hidden = false;
  if (title) title.textContent = `Observing: ${taskKey}`;
  active = { ws: null, taskKey, seen: new Set(), toolCards: new Map() };
  connect(taskKey);
  const closeBtn = document.getElementById("observe-close");
  openModalA11y(modal, closeObserve, closeBtn);
}

export function closeObserve() {
  if (active && active.ws) {
    try { active.ws.close(); } catch (_) { /* already closing */ }
  }
  active = null;
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
