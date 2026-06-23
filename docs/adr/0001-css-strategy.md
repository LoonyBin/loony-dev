# ADR 0001 — CSS strategy: token-driven bespoke components + modern native CSS, no utility framework (for now)

- **Status:** Accepted (2026-06-21)
- **Scope:** the web dashboard / Storybook frontend (`loony_dev/web/static/`)

## Context

We extracted the dashboard's visual layer into a build-less, token-driven,
axis-separated component library (the Storybook under `static/storybook/`). The
question arose: should we adopt a CSS framework (Tailwind was the example, but
the field is open) to avoid re-inventing wheels — e.g. table overflow / responsive
layout — rather than hand-writing those each time?

Two constraints frame the call: we want to **keep our existing design** (so
design-*imposing* frameworks — Bootstrap/Bulma/Pico — are out), and **a build
step is a tradeoff we're willing to weigh**, not a hard rule.

## Spike (what we actually tested, not opinion)

- **Tailwind v4** via the standalone CLI (a single ARM64 binary, no Node project):
  built a table + stepper in ~317ms → **8 KB tree-shaken** CSS. v4 is CSS-first
  (`@theme`), so our existing token vars (`--ld-*`, spacing, radii) map in
  directly — **tokens are not an uphill fight.**
- **Modern native CSS:** solved the responsive-table "wheel" with **container
  queries** + `data-label` (both already in our stack) in ~12 lines — the table
  reflows to stacked cards when its *container* is narrow. **Zero build.** Wired
  into the Storybook (`FleetTable`, `.board-wrap`) and verified rendering on a
  real headless browser (wide = table, narrow = cards).

## Decision

**Do not adopt a CSS utility framework now.** Keep the current architecture —
design tokens (`colors_and_type.css`) + a thin utility layer (`.row/.col/.g8`…) +
bespoke, token-driven component CSS (`ld-system.css` + per-screen files). Solve
layout "wheels" with **modern native CSS**: container queries, `:has()`,
`color-mix()`.

## Rationale

1. A framework would **not** have prevented the overflow bug that prompted this —
   you still write `overflow-x-auto`. The real fix for "tables that don't fit" is
   **container queries**, which are native and need no framework.
2. Tailwind doesn't help our **highest-value components** — the Stepper's
   `counter()`/`::before`/`::after`/`:last-child`, the DAG, the bubble tails have
   no utility equivalents; they stay bespoke CSS either way.
3. To preserve our **minimal-markup** principle we'd `@apply` utilities into
   component classes — which converges back to roughly the architecture we have,
   with Tailwind as the values layer. So it's a **lateral move with a build cost**,
   not a leap.

## Consequences

- No new toolchain; the dashboard stays deployable as static files.
- Responsiveness comes from container queries, scoped per component.
- **Reversible:** if we later want a utility vocabulary dashboard-wide for
  velocity across many new screens, Tailwind v4 (single-binary build, tokens map
  cleanly) or UnoCSS is a legitimate, low-cost-to-defer choice — revisit then.

## Alternatives considered

- **Tailwind v4** — viable, single-binary build; deferred (above).
- **UnoCSS** — similar to Tailwind, heavier (Node/Vite) toolchain.
- **Open Props** — tokens only (no build); we already have a token file, and it
  doesn't solve the layout wheel.
- **Sass/PostCSS** — `@use`/`@mixin`/`@extend` ergonomics without design opinions;
  a build step for marginal gain over native CSS nesting (already shipping).
- **Bootstrap / Bulma / Pico** — rejected: they impose a design language; we keep
  ours.
