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

import { cell, setRows, icon, goPipeline } from "./dom.js";
import { apiText } from "./api.js";
import { openModalA11y, closeModalA11y } from "./modal.js";
import { openObserve } from "./observe.js";

let active = null; // { ws, term, taskKey, resizeHandler, onClose } for the open terminal

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
  const closed = active;
  // Null `active` *before* firing onClose so a release that navigates away (and
  // re-enters closeTerminal) can't recurse, and onClose fires exactly once.
  active = null;
  if (closed.resizeHandler) {
    window.removeEventListener("resize", closed.resizeHandler);
  }
  try { closed.ws.close(); } catch (_) { /* already closing */ }
  try { closed.term.dispose(); } catch (_) { /* already disposed */ }
  if (closed.onClose) {
    try { closed.onClose(); } catch (_) { /* release is best-effort */ }
  }
}

function closeAttachModal() {
  closeTerminal();
  const modal = document.getElementById("attach-modal");
  if (modal) {
    modal.hidden = true;
    closeModalA11y(modal);
  }
}

// Exported (issue #190) so the pipeline-detail view can reuse the live-PTY
// terminal as its take-over fallback when a session is attachable. Behaviour is
// unchanged from the Sessions-table Attach button when called one-arg.
//
// Options (issue #200, all optional — one-arg call sites are unchanged):
//   * attachUrl — connect to this WS path verbatim instead of wsUrl(taskKey).
//       The drive flow resumes a *new* PTY whose attach URL the backend returns.
//   * onClose   — invoked exactly once when the terminal tears down (Close, WS
//       drop, or navigate-away); the drive flow releases its pipeline lease here.
//   * title     — overrides the modal title text.
export function openTerminal(taskKey, { attachUrl, onClose, title: titleText } = {}) {
  const modal = document.getElementById("attach-modal");
  const host = document.getElementById("attach-term");
  const title = document.getElementById("attach-title");
  if (!modal || !host || !window.Terminal) {
    alert("Terminal unavailable (xterm.js failed to load).");
    // The caller expected the terminal to own the lease release; fire onClose so
    // a failed open doesn't leak the drive lease.
    if (onClose) { try { onClose(); } catch (_) { /* best-effort */ } }
    return;
  }
  closeTerminal();
  host.innerHTML = "";
  modal.hidden = false;
  if (title) title.textContent = titleText || `Attached: ${taskKey}`;

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

  const url = attachUrl
    ? `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${attachUrl}`
    : wsUrl(taskKey);
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";

  const sendResize = () => {
    if (fit) { try { fit.fit(); } catch (_) { return; } }
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
    }
  };
  active = { ws, term, taskKey, resizeHandler: sendResize, onClose };

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
  ws.onclose = () => {
    safeWrite(term, "\r\n\x1b[2m[detached]\x1b[0m\r\n");
    // A drive session (onClose set) owns a pipeline lease: when the PTY WS drops
    // the resumed session is gone, so tear down to release the lease promptly.
    // A plain attach (no onClose) stays open showing [detached] as before.
    if (active && active.ws === ws && active.onClose) closeTerminal();
  };
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

  // Open is the entry point to the Issue ▸ PR detail view (#190): the
  // full-page stepper + timeline + steer surface for this pipeline. Routed by
  // task_key (the snapshot row id); the detail view reads pipeline_key off it.
  const open = document.createElement("button");
  open.type = "button";
  open.className = "action";
  open.textContent = "Open";
  open.title = "Open the Issue ▸ PR detail view";
  open.disabled = !s.repo;
  open.addEventListener("click", () => goPipeline(s.repo, s.task_key));
  actions.appendChild(open);

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

// Shared inject helper (issue #190): POST an operator-tagged turn the
// orchestrator runs next. Reused by the Steer modal here and by the
// pipeline-detail reply input, so both drive the single canonical endpoint.
export function injectTurn(taskKey, prompt) {
  return apiText(`/api/sessions/${encodeURIComponent(taskKey)}/inject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
}

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
    await injectTurn(taskKey, prompt);
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
