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

import { cell, setRows } from "./dom.js";
import { apiText } from "./api.js";

let active = null; // { ws, term, taskKey, resizeHandler } for the open terminal

// --- Modal accessibility helpers (focus trap + ESC + focus restore) --------

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), ' +
  'input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(modal) {
  return Array.from(modal.querySelectorAll(FOCUSABLE))
    .filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function trapTab(modal, e) {
  if (e.key !== "Tab") return;
  const items = focusables(modal);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

// Open a modal accessibly: remember the opener, install an ESC + focus-trap
// keydown handler, and move focus inside. closeFn runs on ESC.
function openModalA11y(modal, closeFn, focusTarget) {
  // Drop any stale handler (e.g. a re-open without an intervening close) so we
  // never leave a dangling keydown listener bound.
  if (modal._a11y) {
    modal.removeEventListener("keydown", modal._a11y.keyHandler);
    modal._a11y = null;
  }
  const opener = document.activeElement;
  const keyHandler = (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeFn(); return; }
    trapTab(modal, e);
  };
  modal.addEventListener("keydown", keyHandler);
  modal._a11y = { opener, keyHandler };
  const target = focusTarget || focusables(modal)[0];
  if (target) { try { target.focus(); } catch (_) { /* not focusable */ } }
}

// Tear down the handler installed by openModalA11y and restore focus to the
// element that opened the modal.
function closeModalA11y(modal) {
  const st = modal && modal._a11y;
  if (!st) return;
  modal.removeEventListener("keydown", st.keyHandler);
  modal._a11y = null;
  if (st.opener && typeof st.opener.focus === "function") {
    try { st.opener.focus(); } catch (_) { /* opener gone */ }
  }
}

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
    el.textContent = opts.refused
      ? "🤖 bot has the mic — keystroke ignored (read-only until the turn ends)"
      : "🤖 bot has the mic — read-only (press ESC to interrupt the turn)";
    el.className = "attach-mic mic-bot";
  } else {
    el.textContent = "⌨ you have the mic — type to steer; ESC interrupts";
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
  openModalA11y(modal, closeAttachModal, closeBtn);
}

function renderTaskSession(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.task_key, "Task"));
  tr.appendChild(cell(s.repo, "Repo"));
  tr.appendChild(cell(s.status || "—", "Status"));

  const actions = document.createElement("td");
  actions.dataset.label = "Action";

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
