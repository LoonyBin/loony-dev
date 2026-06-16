"use strict";

// Shared modal accessibility helpers (focus trap + ESC + focus restore).
// Used by the per-task Attach terminal (attach.js, #164) and the JSONL-driven
// Observe conversation view (observe.js, #202) so both overlays trap focus and
// restore it on close identically.

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), ' +
  'input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function focusables(modal) {
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
// keydown handler, and move focus inside. closeFn runs on ESC, unless
// opts.closeOnEscape is false — the attach terminal reserves ESC to interrupt
// the active bot turn, so it must not double as a modal-close key.
export function openModalA11y(modal, closeFn, focusTarget, opts = {}) {
  const closeOnEscape = opts.closeOnEscape ?? true;
  // Drop any stale handler (e.g. a re-open without an intervening close) so we
  // never leave a dangling keydown listener bound.
  if (modal._a11y) {
    modal.removeEventListener("keydown", modal._a11y.keyHandler);
    modal._a11y = null;
  }
  const opener = document.activeElement;
  const keyHandler = (e) => {
    if (e.key === "Escape" && closeOnEscape) { e.preventDefault(); closeFn(); return; }
    trapTab(modal, e);
  };
  modal.addEventListener("keydown", keyHandler);
  modal._a11y = { opener, keyHandler };
  const target = focusTarget || focusables(modal)[0];
  if (target) { try { target.focus(); } catch (_) { /* not focusable */ } }
}

// Tear down the handler installed by openModalA11y and restore focus to the
// element that opened the modal.
export function closeModalA11y(modal) {
  const st = modal && modal._a11y;
  if (!st) return;
  modal.removeEventListener("keydown", st.keyHandler);
  modal._a11y = null;
  if (st.opener && typeof st.opener.focus === "function") {
    try { st.opener.focus(); } catch (_) { /* opener gone */ }
  }
}
