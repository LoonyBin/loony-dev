"use strict";

// Thin fetch helpers shared by the view modules. No framework, no build step.

// Bound every request so a hung connection can't wedge the poll loop in app.js
// (isPolling would stay true forever, blocking all later refreshes).
const FETCH_TIMEOUT_MS = 10000;

async function fetchWithTimeout(url, opts = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    return await fetch(url, { ...opts, signal: controller.signal });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error(`${url} -> timeout`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export async function getJSON(url) {
  const resp = await fetchWithTimeout(url);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

// Like fetch but raises with the server-provided `detail` on non-2xx so the
// editor can surface a useful message. Returns the raw Response on success.
export async function apiText(url, opts) {
  const resp = await fetchWithTimeout(url, opts);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* no body */ }
    throw new Error(detail);
  }
  return resp;
}
