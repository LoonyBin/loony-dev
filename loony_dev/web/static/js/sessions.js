"use strict";

// Sessions view: remote-control session table. (Join URL + QR land in #157.)

import { cell, setRows } from "./dom.js";

function renderSession(s) {
  const tr = document.createElement("tr");
  tr.appendChild(cell(s.session_id, "Session ID"));
  tr.appendChild(cell(s.repo, "Repo"));
  tr.appendChild(cell(s.key, "Key"));
  return tr;
}

export function render(sessions) {
  setRows("sessions", sessions, renderSession, "No active sessions.");
}
