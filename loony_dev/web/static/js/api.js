"use strict";

// Thin fetch helpers shared by the view modules. No framework, no build step.

export async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

// Like fetch but raises with the server-provided `detail` on non-2xx so the
// editor can surface a useful message. Returns the raw Response on success.
export async function apiText(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* no body */ }
    throw new Error(detail);
  }
  return resp;
}
