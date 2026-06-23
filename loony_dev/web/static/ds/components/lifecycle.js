// Stepper — minimal markup, CSS does the work (see .stepper in ld-system.css:
// counter() draws the number, ::after the connector, ::before the node). The
// factory only emits an <ol> of <li>s carrying a STATE class (done/here, or bare
// for future) — composable, item-level. No inline styling.
import { el, mi, seam, splitSeams } from '../util/dom.js';
import { STEP_FLOW } from '../util/data.js';

// stage (7-stage board vocab) → spine step (6-step). NOT 1:1 — Conflicts maps
// back to Review. Caller resolves a worker stage to `current` via this.
export const STAGE_TO_STEP = {
  Inbox: 'Issue', Planning: 'Plan', Implementing: 'Implement',
  'PR Open': 'PR', 'In Review': 'Review', Conflicts: 'Review', Merged: 'Merge',
};
export function stepIndexForStage(stage, steps = STEP_FLOW) {
  const i = steps.indexOf(STAGE_TO_STEP[stage]);
  return i < 0 ? 0 : i;
}

/** stepState(i, current) -> 'done' | 'here' | 'future' (the visual state). */
export function stepState(i, current) {
  return i < current ? 'done' : i === current ? 'here' : 'future';
}

/**
 * Stepper({ current, conflict, steps }) -> <ol class="stepper">.
 * Each step is `<li class="done|here">label</li>` (future = bare <li>). The
 * conflict detour is a composable `<li class="detour">` reusing the red pill look.
 */
export function Stepper(props = {}) {
  const [{ current = 2, conflict = false, steps = STEP_FLOW }, seams] = splitSeams(props);
  const ol = el('ol', 'stepper');
  steps.forEach((s, i) => {
    const st = stepState(i, current);
    ol.append(el('li', st === 'future' ? null : st, s));
  });
  if (conflict) {
    const li = el('li', 'detour');
    li.append(mi('warning', { size: 14 }));
    li.append(document.createTextNode('conflict detour'));
    ol.append(li);
  }
  return seam(ol, seams);
}
