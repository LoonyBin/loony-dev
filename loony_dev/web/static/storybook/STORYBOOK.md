# loony-dev Design-System Storybook

A build-less component library extracted from the **design source-of-truth**
(`loony-dev.html` + `ld-kit.jsx` + the screen JSX, in the claude.ai/design
project), plus a gallery that documents every component across every screen.

Open it two ways (all paths are relative, so it isn't tied to the `/static`
mount):

- **Served:** `loony-dev web` → `/static/storybook/index.html`.
- **From disk:** open `storybook/index.html` directly, or — since browsers block
  ES-module loading over bare `file://` (CORS) — from a one-line static server:
  `python3 -m http.server` in `loony_dev/web/static/` then visit
  `localhost:8000/storybook/`. (Firefox can open the `file://` directly.)

It loads the design pipeline exactly — tokens (`design/colors_and_type.css`,
the L Space foundation) → Material Symbols → the extracted visual layer
(`ld-system.css`) → Manrope — so specimens render under the **real design
tokens**, not the drifted app. No bundler, no framework: ES-module factories
build DOM the way the app already does.

---

## The core principle — three orthogonal concerns

Every class/element/hook belongs to exactly one concern. They **compose** onto
the same node; **duplication across concerns is correct, not a smell.**

| Concern | Owns | Lives in | Examples |
|---|---|---|---|
| **Visual** | the whole look — base + **composable** modifier classes (colour, size, position), stackable at item *or* container level | `ld-system.css` | `.card`, `.sdot.green`, `.tag.red`, `.stepper .here`, `.muted` |
| **Semantic** | what domain thing it *is*, **and its domain lifecycle state** | the caller | `.story`, `.task`; `.merged`/`.blocked`/`.gated` (issue-tracking) |
| **Behavioural** | JS-managed runtime dynamics + the hooks that drive them | the caller | `id`, `data-*`, `hidden`/`.is-collapsed`, event handlers |

Two things that took iteration to get right:

- **Colour is visual, not a separate "state" axis.** A tone (`green/amber/red`)
  is just a composable visual class. All visual elements are composable —
  `.card.red`, `.card.muted`, `.stepper .here.green` — so there's no need to
  split "style" from "state".
- **Domain lifecycle states are *semantic*, not visual.** `.merged`/`.blocked`
  are issue-tracking concepts, so they belong to the semantic layer. The
  **visual** layer only knows colours. The bridge — `merged → green` — is the
  semantic→visual map (`dotTone()`/`stageTone()`), and it lives in the **caller**
  (the app), never inside a visual component.

This implies a clean layering:

- **Visual primitives** (`Sdot`, `Tag`, `Btn`, `Card`, `Avatar`, `Stepper`) take
  **visual** props (`tone: 'green'`, `variant`, `size`). No domain knowledge.
- **Semantic composites** (`DagNode`, `StoryListItem`, `FleetRow`,
  `TimelineRow`…) take **domain** data (a worker, a node + its `state`) and map
  it to visual tones when composing the primitives. They are the semantic layer.

**The rule:** if `.card` (visual) always co-occurs with a story (semantic), do
**not** merge them. They compose: `Card({ class:'story', id, onClick })` →
`<div class="card story" id="…">`.

## Minimal markup — the framework does the heavy lifting

A reusable component is **minimal markup + CSS doing the work**, not per-element
inline styles. State is expressed as **composable classes**, applied at item or
container level; CSS counters, sibling selectors and custom properties carry the
rest. Inline `style` is reserved for genuinely data-driven *layout* (DAG
coordinates), and even then via custom properties (`--x`, `--ava-sz`).

The Stepper is the exemplar:

```html
<ol class="stepper">
  <li class="done">Issue</li>
  <li class="here">Plan</li>
  <li>Implement</li>            <!-- bare <li> = the "future" state -->
</ol>
```

`counter()` draws the number, `li.done::before` swaps it for a check, `::after`
draws the connector, and `li.done::after` is the only accent connector (so the
bar after the active node stays muted). The factory emits **only** that markup —
no inline styles, state as one class per item.

A factory therefore owns the **style + state** layer only (all in CSS). It knows
nothing about repos/issues/workers and exposes seams for semantic + behavioural.

### The factory API

`Component(props) -> HTMLElement`. Visual props are consumed; the rest are seams
(`ds/util/dom.js`):

```js
const [{ variant, size }, seams] = splitSeams(props);   // visual vs seams
const node = el('button', `ld-btn ${variant} ${size}`);  // visual axis
return seam(node, seams);   // lands id / class / attrs / style / onClick
```

`seam()` is the single place the caller's **semantic** class and **behavioural**
hooks (`id`, `data-*`, handlers) land. Data-driven *layout* (DAG node `left/top`,
explicit widths) is passed as `style` — layout, not the visual recipe.

---

## Canonical names + the design→app mapping (for Phase 4)

The Storybook adopts the **design's** class vocabulary as canonical for the
visual axis (it is clean and domain-free). The dashboard's `app.css` is the
drifted **consumer**; Phase 4 reconciles it onto these names. The divergences:

| Visual (canonical / design) | app.css today | Notes |
|---|---|---|
| `.ava` + archetype | `.avatar` | see de-tangle below |
| `table.board` | `.fleet-table` / `.data-table` | load-bearing JS selector in fleet.js |
| `.tl-line` (+ `.tl-dot`) | `.timeline` | activity timeline connector |
| `.stat-n` / `.stat-l` | `.stat-value` / `.stat-label` | |
| `.crumbs` / `.crumb-cur` | `.breadcrumb` / `.crumb.current` | |
| `.seg` | `.segmented` | |
| `.clickrow` | `.fleet-row` / `.worker-row` | |
| `.pulse` (on `.sdot`) | `.stream-dot` | live presence |
| `.faint` / `.mut` | `.muted` (≈`.mut`) | `faint`=`--fg-muted`, `mut`=`--fg-secondary` |

### De-tangles (where the design itself tangled axes)

1. **Avatar colour ≠ bot name.** The design styles `.ava.trixy/.capo/.user` —
   baking bot/operator *names* (per-install config) into visual classes (also
   against `no-hardcoded-bot-names`). The visual layer now carries domain-agnostic
   colour tones `.ava.neutral / .accent / .ink`; the actor-role → tone map
   (`avatarTone()`: worker→neutral, manager→accent, operator→ink) lives in the
   caller, exactly like `dotTone()`. `Avatar` takes a visual `tone`; the `glyph`
   is caller-supplied — names never enter the component.
2. **One stage→tone map.** The app keeps three divergent copies; `data.js`
   `stageTone()` is the single source the `Tag` factory consumes.
3. **Stepper detour reuses `Tag`** instead of a bespoke pill.

---

## Component inventory (the stories)

`ds/components/` — visual-axis factories, grouped by origin screen:

- **primitives.js** — `Btn` `Tag` `Sdot` `StatePill` `Avatar` `Icon` `Eyebrow`
  `Hairline` `Card` `Stat` `ScreenHead` `Crumbs` `Segmented` `Subtab`
- **lifecycle.js** — `Stepper` (+ `stepIndexForStage`, the 7-stage→6-step map)
- **navigation.js** — `BrandMark` `BrandWord` `NavItem` `NavEyebrow` `UserCard`
  `SettingsPopover`
- **fleet.js** — `WorkerPoolMatrix` `Metric` `FleetTable` `FleetRow`
  `RepoFilterItem` `KanbanColumn` `KanbanCard`
- **cockpit.js** — `Dag` `DagNode` `DagEdge` `DagFieldNode` `DagLegend`
  `EpicZoneCard` `StoryListItem` `LiveActivityRow`
- **sessions.js** — `ChatBubble` `CodeChip` `RefChip` `ChatComposer`
  `TimelineRow` `SkillCard` `LinkedMini` `KVRow`
- **mobile.js** — `PhoneFrame` `PhoneStatus` `PhoneNav`

`ld-system.css` is the extracted visual layer: the design's component CSS
verbatim (token-driven) plus a "composites" section that promotes the screens'
repeated inline-styled patterns (`.metric`, `.pool-grid`, `.kanban-card`,
`.bubble`, `.skill-icon`, `.diffstat`, `.tl-dot`, `.phone-nav`, …) into named,
reusable classes.

---

## Scope

- **Done:** the three-concern contract; the extracted visual layer; the exhaustive
  factory set across all screens; the gallery (every component tagged with its
  three concerns); the design→app mapping. Factory DOM logic is verified in Node
  (every factory instantiates; full gallery renders without throwing).
- **Minimal markup — done across the board.** No factory sets a visual inline
  style; every factory emits minimal markup + class names, and CSS does the work.
  The only inline `style` left is **data-driven layout** carried as custom
  properties (`--x/--y` DAG positions, `--w/--h` canvas sizes, `--ava-sz`,
  `--dot-sz`). Sizes/colours/type live in CSS. State is composable modifier
  classes (`.on`, `.needs`, `.gated`, `.t-amber`, `.done/.here`).
- **CSS layout:** `ld-system.css` holds the shared visual layer (primitives +
  cross-screen composites); each screen's extra composite classes live in a
  per-screen file (`css/fleet.css`, `css/cockpit.css`, `css/sessions.css`,
  `css/mobile.css`) loaded after it. Navigation needed none (already class-driven).
- **Out of scope — Phase 4:** migrating the live screens (`fleet.js`,
  `repoDetail.js`, `issueDetail.js`, `entries.js`) to render via these factories.
  That migration must preserve the behavioural contract — the ~70 fixed element
  `id`s, the `table`/`tbody`/`thead th` structure, `data-fleet-view`, the tone
  maps, `hidden`-as-visibility, the modal focus-trap. The Storybook exists to
  make that swap safe; track it separately.

## Source

Design project (claude.ai/design) `31b3b279-…` — `loony-dev.html` (shell + all
component CSS), `ld-kit.jsx` (primitives + Stepper + sample data), `ld-fleet.jsx`
`ld-cockpit.jsx` `ld-sessions.jsx` `ld-mobile.jsx` (screens). Sample data
(`REPOS`/`WORKERS`/`SKILLS`/`EPIC`) is lifted verbatim; the bot/worker/operator
names in it are **illustrative only** — real accounts are per-install config and
are never hardcoded in a component.
