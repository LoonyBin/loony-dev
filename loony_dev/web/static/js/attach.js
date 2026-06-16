"use strict";

// Per-task attach + steer (issue #164). Renders the worker-owned PTY sessions as
// a table with two affordances per task:
//   * Attach  — an embedded xterm.js terminal bridged to the session PTY over a
//               websocket (live observe + steer; read-only while the bot has the
//               mic, your keystrokes go through between turns, ESC interrupts).
//   * Steer   — a one-shot "send guidance" form that POSTs an operator-tagged
//               turn the orchestrator runs next (for users who prefer not to
//               drive a live terminal).
// xterm.js is loaded from a CDN (no build step), mirroring the htmx/Alpine setup.

import { cell, setRows, icon } from "./dom.js";
import { apiText } from "./api.js";
import { openModalA11y, closeModalA11y } from "./modal.js";
import { openObserve } from "./observe.js";

let active = null; // { ws, term, taskKey, resizeHandler } for the open terminal

// Write to the terminal defensively: async ws handlers can fire after the
// terminal has been disposed, which would otherwise throw.
function safeWrite(term, data) {
  try { term.write(data); } catch (_) { /* terminal disposed */ }
}

function wsUrl(taskKey) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/api/sessions/${encodeURIComponent(taskKey)}/attach`;
}

function setMic(holder, opts = {}) {
  const el = document.getElementById("attach-mic");
  if (!el) return;
  if (holder === "bot") {
    el.textContent = "";
    el.appendChild(icon("smart_toy"));
    el.appendChild(document.createTextNode(opts.refused
      ? " bot has the mic — keystroke ignored (read-only until the turn ends)"
      : " bot has the mic — read-only (press ESC to interrupt the turn)"));
    el.className = "attach-mic mic-bot";
  } else {
    el.textContent = "";
    el.appendChild(icon("keyboard"));
    el.appendChild(document.createTextNode(" you have the mic — type to steer; ESC interrupts"));
    el.className = "attach-mic mic-human";
  }
}

function closeTerminal() {
  if (!active) return;
  if (active.resizeHandler) {
    window.removeEventListener("resize", active.resizeHandler);
  }
  try { active.ws.close(); } catch (_) { /* already closing */ }
  try { active.term.dispose(); } catch (_) { /* already disposed */ }
  active = null;
}

function closeAttachModal() {
  closeTerminal();
  const modal = document.getElementById("attach-modal");
  if (modal) {
    modal.hidden = true;
    closeModalA11y(modal);
  }
}

function openTerminal(taskKey) {
  const modal = document.getElementById("attach-modal");
  const host = document.getElementById("attach-term");
  const title = document.getElementById("attach-title");
  if (!modal || !host || !window.Terminal) {
    alert("Terminal unavailable (xterm.js failed to load).");
    return;
  }
  closeTerminal();
  host.innerHTML = "";
  modal.hidden = false;
  if (title) title.textContent = `Attached: ${taskKey}`;

  const term = new window.Terminal({
    convertEol: false,
    cursorBlink: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 13,
  });
  const fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
  if (fit) term.loadAddon(fit);
  term.open(host);
  if (fit) { try { fit.fit(); } catch (_) { /* not yet laid out */ } }

  const ws = new WebSocket(wsUrl(taskKey));
  ws.binaryType = "arraybuffer";

  const sendResize = () => {
    if (fit) { try { fit.fit(); } catch (_) { return; } }
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
    }
  };
  active = { ws, term, taskKey, resizeHandler: sendResize };

  ws.onopen = () => { setMic("human"); sendResize(); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg && msg.type === "mic") setMic(msg.holder, { refused: msg.refused });
      return;
    }
    safeWrite(term, new Uint8Array(ev.data));
  };
  ws.onclose = () => { safeWrite(term, "\r\n\x1b[2m[detached]\x1b[0m\r\n"); };
  ws.onerror = () => { safeWrite(term, "\r\n\x1b[31m[connection error]\x1b[0m\r\n"); };

  // Keystrokes go out as raw bytes; the server gates them on the mic.
  const encoder = new TextEncoder();
  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
  });
  term.onResize(sendResize);
  window.addEventListener("resize", sendResize);

  const closeBtn = document.getElementById("attach-close");
  openModalA11y(modal, closeAttachModal, closeBtn, { closeOnEscape: false });
}

function renderTaskSession(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.task_key, "Task"));
  tr.appendChild(cell(s.repo, "Repo"));
  tr.appendChild(cell(s.status || "—", "Status"));

  const actions = document.createElement("td");
  actions.dataset.label = "Action";

  // Observe is the default surface: it renders the conversation from the JSONL
  // transcript with no live process required, so it works for parked sessions
  // between turns as well as active ones (#202).
  const observe = document.createElement("button");
  observe.type = "button";
  observe.className = "action";
  observe.textContent = "Observe";
  observe.disabled = !s.observable;
  observe.title = s.observable
    ? "Render the conversation from the session transcript"
    : "No transcript recorded for this session yet";
  observe.addEventListener("click", () => openObserve(s.task_key));
  actions.appendChild(observe);

  // Attach is the live "drive" terminal — only when a PTY bridge is present.
  const attach = document.createElement("button");
  attach.type = "button";
  attach.className = "action";
  attach.textContent = "Attach";
  attach.disabled = !s.attachable;
  attach.title = s.attachable ? "Open a live terminal" : "No live session bridge";
  attach.addEventListener("click", () => openTerminal(s.task_key));
  actions.appendChild(attach);

  const steer = document.createElement("button");
  steer.type = "button";
  steer.className = "action";
  steer.textContent = "Steer";
  steer.title = "Send one-shot guidance (runs as the next turn)";
  steer.addEventListener("click", () => openSteer(s.task_key));
  actions.appendChild(steer);

  tr.appendChild(actions);
  return tr;
}

export function render(taskSessions) {
  setRows("task-sessions", taskSessions || [], renderTaskSession, "No in-flight task sessions.");
}

// --- One-shot "send guidance" (inject) -------------------------------------

function openSteer(taskKey) {
  const modal = document.getElementById("steer-modal");
  const title = document.getElementById("steer-title");
  const text = document.getElementById("steer-text");
  const err = document.getElementById("steer-error");
  if (!modal) return;
  modal.hidden = false;
  if (title) title.textContent = `Send guidance: ${taskKey}`;
  if (err) err.textContent = "";
  if (text) {
    text.value = "";
    text.dataset.taskKey = taskKey;
    text.removeAttribute("aria-invalid");
    text.removeAttribute("aria-errormessage");
  }
  openModalA11y(modal, closeSteer, text);
}

async function submitSteer() {
  const text = document.getElementById("steer-text");
  const err = document.getElementById("steer-error");
  const send = document.getElementById("steer-send");
  if (!text) return;
  const prompt = text.value.trim();
  const taskKey = text.dataset.taskKey;
  if (!prompt) {
    if (err) err.textContent = "Guidance can't be empty.";
    text.setAttribute("aria-invalid", "true");
    text.setAttribute("aria-errormessage", "steer-error");
    text.focus();
    return;
  }
  text.removeAttribute("aria-invalid");
  text.removeAttribute("aria-errormessage");
  // Disable the send button across the in-flight POST: injecting a turn is not
  // idempotent, so a rapid double-click would enqueue duplicate operator turns.
  if (send) send.disabled = true;
  try {
    await apiText(`/api/sessions/${encodeURIComponent(taskKey)}/inject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    closeSteer();
  } catch (e) {
    if (err) {
      err.textContent = String(e.message || e);
      text.setAttribute("aria-invalid", "true");
      text.setAttribute("aria-errormessage", "steer-error");
    }
  } finally {
    if (send) send.disabled = false;
  }
}

function closeSteer() {
  const modal = document.getElementById("steer-modal");
  if (modal) {
    modal.hidden = true;
    closeModalA11y(modal);
  }
}

export function init() {
  const closeT = document.getElementById("attach-close");
  if (closeT) closeT.addEventListener("click", closeAttachModal);
  const send = document.getElementById("steer-send");
  if (send) send.addEventListener("click", submitSteer);
  const cancel = document.getElementById("steer-cancel");
  if (cancel) cancel.addEventListener("click", closeSteer);
}
