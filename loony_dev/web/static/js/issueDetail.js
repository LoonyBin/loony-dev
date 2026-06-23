"use strict";

// Issue ▸ PR pipeline-detail view (issue #190). A per-pipeline page matching the
// mockup: breadcrumb + header, a lifecycle stepper, an inline "jump in & steer"
// panel (conversation + reply, reusing the existing inject/observe/attach
// paths), and a right sidebar (activity timeline, linked issue/PR cards,
// worktree info).
//
// Navigation is driven by the Alpine store: app.js's wireDetailViews() effect
// calls show(repo|null, taskKey|null) whenever the active view/pipeline changes
// (issue #239 moved this off a load-order-fragile inline x-effect). Consolidated
// state arrives via update(snapshot) from the app-shell orchestrator (the #155
// /api/events stream); we filter it to the current pipeline. Modeled on
// repoDetail.js.
//
// DATA SOURCES (issue #225). Two live feeds back this page:
//   1. The SSE snapshot's worker signals (workers / worktrees / sessions /
//      task_sessions / stuck) — the live PTY/worker picture.
//   2. The partial GitHub-state feed on the same snapshot (#219): `pipelines`
//      carries the real issue/PR title, the label/PR-derived `stage`, raw
//      `labels`, `pr_state`, and `mergeable`. `findPipelineView` matches it to
//      the open pipeline and it drives the header title, the stepper, the
//      resting state pill, and the linked cards.
// The Activity timeline reads the structured per-pipeline log feed (#220) via
// `GET /api/pipelines/{key}/activity`. GitHub fields with no feed (diff +/−,
// changed-files, review verdicts) still render as "—" placeholders, and the page
// degrades to the filesystem heuristic when the GitHub feed is absent (disabled
// / fetch failed / pipeline unseen) so it opens for any issue/PR.

import { formatAge, icon, goView, goRepo, dotTone, stageTone, avatarTone } from "./dom.js";
// Shared design-system components from the design-system lib (static/ds/) — the
// single source of truth backing both the Storybook and the app (#258 / Phase 4).
import { Stepper } from "/static/ds/components/lifecycle.js";
import { Crumbs, StatePill, Tag, Btn } from "/static/ds/components/primitives.js";
import { TimelineRow } from "/static/ds/components/sessions.js";
import { streamObserve } from "./observe.js";
import { injectTurn, openTerminal } from "./attach.js";
import { interruptSession } from "./overview.js";
import { streamLog } from "./logs.js";
import { apiText, getJSON } from "./api.js";

let current = null; // { repo, taskKey } currently displayed, or null
let currentKey = null; // `${repo} ${taskKey}` for idempotent show()
let lastState = null; // latest consolidated snapshot
let lastGhView = null; // latest PipelineGitHubView for the current pipeline, or null
let convStream = null; // observe stream controller for the inline conversation
let stopLog = null; // stop fn for the folded-in worker-log tail (#221)
let driveState = "idle"; // idle | observing | driving | busy (the #200 seam)
let drivePipeline = null; // { pipelineKey, repo } while a drive lease is held, else null
let timelineToken = 0; // bumped per timeline fetch so a stale response is dropped
let timelineLastFetch = 0; // throttle for snapshot-driven timeline refetches

// The lifecycle the stepper renders. Conflict is a detour off this spine.
const STAGES = ["Issue", "Plan", "Implement", "PR", "Review", "Merge"];

// The two ready-for-* entry labels (mirrors services.READY_LABELS / repo.py).
const READY_LABELS = ["ready-for-planning", "ready-for-development"];

// Map the GitHub feed's lifecycle `stage` (services.derive_stage) onto the
// stepper spine. Conflicts sit at Review with the conflict detour badge shown.
const STAGE_TO_STEP = {
  "Inbox": "Issue",
  "Planning": "Plan",
  "Implementing": "Implement",
  "PR Open": "PR",
  "In Review": "Review",
  "Conflicts": "Review",
};

// Tag tone for a linked-card / state pill, per GitHub stage.
// Stage → Tag tone is the canonical stageTone() in dom.js (one source of truth;
// this file's old copy had drifted).

// Shared tooltip for the steer affordances while no live PTY bridge exists. The
// PTY bridge is unreliable (see CLAUDE.md), so reply/Interrupt/Take over are
// greyed until a worker has actually attached one.
const STEER_DISABLED_TIP =
  "No live session bridge — reply/steer available once a worker PTY is attached";

function muted(text) {
  const p = document.createElement("p");
  p.className = "muted";
  p.textContent = text;
  return p;
}

function kvRow(dl, label, value, valueClass) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value == null || value === "" ? "—" : String(value);
  if (valueClass) dd.className = valueClass;
  dl.appendChild(dt);
  dl.appendChild(dd);
}

// Parse a pipeline/task key into a display identity. `issue-190` → an issue
// numbered 190; `pr-210` → an externally-opened PR numbered 210; anything else
// falls back to the raw key.
function parseKey(key) {
  const k = String(key || "");
  let m = /^issue-(\d+)/.exec(k);
  if (m) return { kind: "issue", num: m[1], label: `#${m[1]}` };
  m = /^pr-(\d+)/.exec(k);
  if (m) return { kind: "pr", num: m[1], label: `PR #${m[1]}` };
  return { kind: "other", num: null, label: k || "pipeline" };
}

function findRow(state, repo, taskKey) {
  const rows = (state && state.task_sessions) || [];
  return rows.find((r) => r.task_key === taskKey && r.repo === repo) || null;
}

// The worktree backing this pipeline: a branch named for the key (issue-N is the
// branch *prefix* `issue-N/<slug>`). Best-effort — pr-P pipelines have no local
// branch we can name from the snapshot.
function findWorktree(state, repo, taskKey) {
  const wts = (state && state.worktrees) || [];
  return wts.find((w) =>
    w.repo === repo && w.branch &&
    (w.branch === taskKey || w.branch.startsWith(taskKey + "/")),
  ) || null;
}

function findStuck(state, repo, taskKey) {
  const stuck = (state && state.stuck) || [];
  return stuck.filter((s) => s.worker_repo === repo && s.task_key === taskKey);
}

// The GitHub-derived view (#219) for this pipeline: title, label/PR-derived
// stage, raw labels, pr_state, mergeable. The Fleet deep-link passes the
// pipeline key (issue-N / pr-P), which equals PipelineGitHubView.pipeline_key.
// Null when GitHub state is disabled / the fetch failed / the pipeline is unseen.
function findPipelineView(state, repo, taskKey) {
  const views = (state && state.pipelines) || [];
  return views.find((v) => v.pipeline_key === taskKey && v.repo === repo) || null;
}

function ageSeconds(iso) {
  if (!iso) return 0;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 0;
  return Math.max(0, (Date.now() - t) / 1000);
}

// Stage derivation, GitHub-first (issue #225). When the GitHub feed has a view
// for this pipeline its label/PR-derived `stage` is authoritative (mapped onto
// the stepper spine via STAGE_TO_STEP), with the conflict detour driven by the
// `Conflicts` stage or an explicit `CONFLICTING` mergeable. Returns
// {stage, conflict, knownFromGitHub}. Centralized so the source is explicit.
//
// Without a GitHub view (disabled / fetch failed / pipeline unseen) it falls
// back to the snapshot-only heuristic: the conflict detour is best-effort from
// stuck/worktree signals (it can under-report a mid-rebase that left no stuck
// process), and an externally-opened PR pipeline is assumed to be at Review.
export function deriveStage(taskKey, row, worktree, stuckEntries, ghView) {
  if (ghView && ghView.stage && STAGE_TO_STEP[ghView.stage]) {
    const conflict = ghView.stage === "Conflicts" ||
      String(ghView.mergeable || "").toUpperCase() === "CONFLICTING";
    return { stage: STAGE_TO_STEP[ghView.stage], conflict, knownFromGitHub: true };
  }
  const key = parseKey(taskKey);
  let stage;
  if (key.kind === "pr") {
    stage = "Review"; // externally-opened PR: the bot reviews it.
  } else if (worktree) {
    stage = "Implement"; // a checked-out branch means coding is underway.
  } else {
    stage = "Plan"; // no worktree yet — still planning / awaiting approval.
  }
  const conflict = (stuckEntries || []).some((s) => {
    const blob = `${s.blocked_on || ""} ${s.cmdline || ""}`.toLowerCase();
    return blob.includes("conflict") || blob.includes("rebase") ||
      blob.includes("merge");
  });
  return { stage, conflict, knownFromGitHub: false };
}

// --- Renderers --------------------------------------------------------------

function renderBreadcrumb(repo, key) {
  const nav = document.getElementById("pipeline-breadcrumb");
  if (!nav) return;
  nav.innerHTML = "";
  // Shared Crumbs factory: items with onClick are links, the last (no onClick) is
  // the current segment. aria-current is set on it for parity with the old markup.
  const crumbs = Crumbs({ items: [
    { label: "Fleet", onClick: () => goView("fleet") },
    { label: repo, onClick: () => goRepo(repo) },
    { label: key.label },
  ] });
  const cur = crumbs.querySelector(".crumb-cur");
  if (cur) cur.setAttribute("aria-current", "page");
  nav.appendChild(crumbs);
}

function renderHeader(repo, key) {
  const repoEl = document.getElementById("pipeline-detail-repo");
  if (repoEl) repoEl.textContent = repo || "—";
  const titleEl = document.getElementById("pipeline-detail-title");
  if (titleEl) {
    // Prefer the real GitHub title from the feed; fall back to the pipeline
    // label as the honest stand-in when the feed is absent.
    titleEl.textContent = (lastGhView && lastGhView.title) ? lastGhView.title : key.label;
  }
  renderState();
}

// Map a GitHub stage onto an .sdot tone for the resting state pill.
function stageDot(stage) {
  if (stage === "Conflicts") return "blocked";
  if (stage === "In Review" || stage === "Planning") return "review";
  if (stage === "Implementing" || stage === "PR Open") return "active";
  return "gated";
}

// Statepill reflects the live status, overlaid by the drive-state seam (#200):
// observing/driving/busy take precedence over the parked/working baseline. With
// no live row, the GitHub stage (#225) is the resting text instead of bare "idle".
function renderState() {
  const el = document.getElementById("pipeline-detail-state");
  if (!el) return;
  const row = current ? findRow(lastState, current.repo, current.taskKey) : null;
  let dotClass = "gated";
  let text = "idle";
  if (driveState === "busy") { dotClass = "blocked"; text = "busy — bot is working"; }
  else if (driveState === "driving") { dotClass = "active"; text = "driving"; }
  else if (driveState === "observing") { dotClass = "review"; text = "observing"; }
  else if (row && row.status === "running") { dotClass = "active"; text = "working"; }
  else if (row && row.status) { text = row.status; }
  else if (lastGhView && lastGhView.stage) {
    dotClass = stageDot(lastGhView.stage);
    text = lastGhView.stage.toLowerCase();
  }
  el.innerHTML = "";
  // Shared StatePill (visual). The drive-state (observing/driving/busy) is a
  // BEHAVIOURAL runtime modifier the app injects via the class seam; dotClass (a
  // lifecycle state) maps to a visual dot colour via dotTone().
  el.appendChild(StatePill({
    tone: dotTone(dotClass),
    label: text,
    class: driveState !== "idle" ? driveState : undefined,
  }));
}

// Documented hook for #200: flip the header/stepper into a drive-state. A 409
// from interrogate (an automated task holds the lease) sets 'busy'. No-op-ish
// today beyond the rendering it reserves.
export function setDriveState(state) {
  driveState = state || "idle";
  renderState();
}

// Numbered-circle stepper matching the design: each step is a 22px round circle
// holding its 1-based number (or a check glyph when done), joined by connector
// bars; done/current/upcoming styling lives in app.css. The conflict detour
// hangs off the spine after Implement.
function renderStepper(staging) {
  const host = document.getElementById("pipeline-stepper");
  if (!host) return;
  host.innerHTML = "";
  // `staging.stage` is already a spine label (STAGE_TO_STEP maps the GitHub feed
  // onto STAGES). The shared Stepper factory renders the <ol>; we mark the active
  // step with aria-current for parity with the previous a11y.
  const current = STAGES.indexOf(staging.stage);
  const ol = Stepper({
    current,
    conflict: !!staging.conflict,
    steps: STAGES,
    attrs: { "aria-label": "Lifecycle" },
  });
  const here = ol.querySelector(".here");
  if (here) here.setAttribute("aria-current", "step");
  host.appendChild(ol);
}

// Map a log level to an .sdot tone for the timeline dot.
function levelTone(level) {
  const l = String(level || "").toUpperCase();
  if (l === "ERROR" || l === "CRITICAL") return "blocked";
  if (l === "WARNING") return "review";
  return "active"; // INFO / DEBUG / neutral
}

// Best-effort skill chip from the emitting logger (e.g. agents.coding →
// implement, agents.planning → plan). Omitted (null) when not derivable.
function skillFor(logger) {
  const l = String(logger || "").toLowerCase();
  if (l.includes("agents.coding")) return "implement";
  if (l.includes("agents.planning")) return "plan";
  if (l.includes("coderabbit")) return "review";
  return null;
}

// Two-letter avatar glyph for an activity actor role.
function actorGlyph(actor) {
  if (actor === "operator") return "OP";
  if (actor === "system") return "SY";
  return "TX"; // trixy (the bot account)
}

// Parse a pipeline-log timestamp ("2026-06-17 12:00:00,123") into a relative age
// in seconds, or null when unparseable.
function tsAgeSeconds(ts) {
  if (!ts) return null;
  const t = Date.parse(String(ts).replace(",", ".").replace(" ", "T"));
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

// Fetch the structured activity feed (#220) and render it, falling back to the
// snapshot-derived rows when the feed is empty or unavailable (404 — no pipeline
// log yet, common for GitHub-only pipelines). Token-guarded so a slow response
// for a pipeline we've navigated away from is dropped.
async function loadTimeline(repo, taskKey, row, stuckEntries) {
  const host = document.getElementById("pipeline-timeline");
  if (!host) return;
  const token = ++timelineToken;
  timelineLastFetch = Date.now();
  let events = null;
  try {
    const data = await getJSON(
      `/api/pipelines/${encodeURIComponent(taskKey)}/activity` +
      `?repo=${encodeURIComponent(repo)}&lines=50`,
    );
    events = (data && Array.isArray(data.events)) ? data.events : [];
  } catch (_) {
    events = null; // 404 / fetch error → fall back to snapshot rows
  }
  // Drop a stale response (navigated away or a newer fetch superseded us).
  if (token !== timelineToken || !current ||
      current.repo !== repo || current.taskKey !== taskKey) {
    return;
  }
  renderTimeline(host, events, row, stuckEntries);
}

// Pure renderer: the GitHub/pipeline-log activity feed (tone dot + actor avatar +
// message + skill chip + relative time) when present, else the snapshot-derived
// fallback rows (session started / status / stuck).
function renderTimeline(host, events, row, stuckEntries) {
  host.innerHTML = "";
  const rows = [];
  if (Array.isArray(events) && events.length) {
    for (const ev of events) {
      rows.push({
        tone: levelTone(ev.level),
        actor: ev.actor || "system",
        text: ev.message || "",
        skill: skillFor(ev.logger),
        age: tsAgeSeconds(ev.ts),
      });
    }
  } else {
    // Snapshot fallback — worker signals only (the feed is empty / unavailable).
    if (row && row.started_at) {
      rows.push({ tone: "active", actor: "trixy", text: "Session started", skill: null, age: ageSeconds(row.started_at) });
    }
    if (row && row.status) {
      rows.push({ tone: "active", actor: "trixy", text: `Status: ${row.status}`, skill: null, age: row.started_at ? ageSeconds(row.started_at) : null });
    }
    for (const s of (stuckEntries || [])) {
      rows.push({ tone: "blocked", actor: "operator", text: `Stuck — ${s.blocked_on || "blocked"}`, skill: null, age: Number(s.age_seconds) || 0 });
    }
  }
  if (!rows.length) {
    host.appendChild(muted("No activity recorded yet."));
    return;
  }
  for (const r of rows) {
    // The shared TimelineRow factory (SoT), flat shape: dot (state→dotTone) +
    // actor avatar + message (+ skill chip) + trailing relative time. `r.tone`
    // is already a dotTone lifecycle key (active / review / blocked).
    const line = TimelineRow({
      rail: false,
      whenAlign: "right",
      state: r.tone,
      avatar: { tone: avatarTone(r.actor), glyph: actorGlyph(r.actor) },
      title: r.text,
      chip: r.skill || null,
      when: r.age == null ? "" : formatAge(r.age),
    });
    // The dot + avatar are decorative; hide them from the a11y tree.
    line.querySelectorAll(".tl-dot, .ava").forEach((n) => n.setAttribute("aria-hidden", "true"));
    host.appendChild(line);
  }
}

function linkedCard(title, stateText, stateClass, dl) {
  const card = document.createElement("div");
  card.className = "linked-card";
  const head = document.createElement("div");
  head.className = "linked-head";
  const h = document.createElement("span");
  h.className = "linked-title";
  h.textContent = title;
  head.appendChild(h);
  head.appendChild(Tag({ tone: stateClass, label: stateText }));
  card.appendChild(head);
  card.appendChild(dl);
  return card;
}

// A row of label chips appended below a linked card's kv body.
function labelChips(labels) {
  const wrap = document.createElement("div");
  wrap.className = "linked-labels";
  for (const name of labels) {
    wrap.appendChild(Tag({ tone: "neutral", label: name }));
  }
  return wrap;
}

function renderLinked(repo, key, worktree) {
  const host = document.getElementById("pipeline-linked");
  if (!host) return;
  host.innerHTML = "";
  const gh = lastGhView;

  // Issue card — lit from the GitHub feed (#225): number + real title + stage +
  // label chips. Falls back to the key/placeholder when the feed is absent.
  const issueDl = document.createElement("dl");
  issueDl.className = "kv";
  kvRow(issueDl, "Issue", key.kind === "issue" ? key.label : "—");
  if (gh && gh.title) kvRow(issueDl, "Title", gh.title);
  const issueState = gh ? (gh.stage || "—").toLowerCase() : "no GitHub data";
  const issueTone = gh ? stageTone(gh.stage) : "ghost";
  const issueCard = linkedCard("Issue", issueState, issueTone, issueDl);
  if (gh && Array.isArray(gh.labels) && gh.labels.length) {
    issueCard.appendChild(labelChips(gh.labels));
  }
  host.appendChild(issueCard);

  // PR card — pr_state / mergeable from the feed as the state tag; branch from
  // the worktree. Diff +/−, files and reviews stay "—" placeholders (out of
  // scope per the issue: those GitHub-only fields land in a later pass).
  const prDl = document.createElement("dl");
  prDl.className = "kv";
  kvRow(prDl, "PR", key.kind === "pr" ? key.label : "—");
  kvRow(prDl, "Branch", worktree ? worktree.branch : "—");
  kvRow(prDl, "Diff", "—");
  kvRow(prDl, "Files", "—");
  kvRow(prDl, "Reviews", "—");
  let prState = "no GitHub data";
  let prTone = "ghost";
  if (gh && gh.pr_state) {
    const conflicting = String(gh.mergeable || "").toUpperCase() === "CONFLICTING";
    prState = conflicting ? "conflicts" : gh.pr_state;
    prTone = conflicting ? "red" : "green";
  } else if (gh) {
    prState = "no PR yet";
  }
  const prCard = linkedCard("Pull request", prState, prTone, prDl);
  host.appendChild(prCard);
}

function renderWorktree(worktree) {
  const host = document.getElementById("pipeline-worktree");
  if (!host) return;
  host.innerHTML = "";
  if (!worktree) {
    host.appendChild(muted("No worktree — pipeline not checked out yet."));
    return;
  }
  const dl = document.createElement("dl");
  dl.className = "kv";
  kvRow(dl, "Branch", worktree.detached ? "(detached)" : worktree.branch);
  kvRow(dl, "HEAD", worktree.head ? worktree.head.slice(0, 10) : null);
  kvRow(dl, "Path", worktree.path);
  host.appendChild(dl);
  host.appendChild(muted("isolated · auto-GC on merge"));
}

// Centralized control row — the single seam #200 fills in (issue #190). Each
// control routes through one documented function with a clear pipeline_key
// argument, rather than scattered disabled buttons.
function renderControls(row, pipelineKey) {
  const host = document.getElementById("pipeline-controls");
  if (!host) return;
  host.innerHTML = "";

  const sessionId = row && row.session_id;
  const attachable = row && row.attachable;
  const taskKey = row && row.task_key;

  const btn = (text, opts = {}) => {
    const b = Btn({
      variant: opts.variant || "ghost",   // steer-toolbar default (matches the design)
      size: "sm",
      label: text,
      onClick: opts.onClick || undefined,
      attrs: opts.disabled ? { disabled: "" } : undefined,
    });
    if (opts.title) b.title = opts.title;
    host.appendChild(b);
    return b;
  };

  // Interrupt (live): ESC the in-flight turn. Disabled without a control channel.
  btn("Interrupt", {
    variant: "outline",
    disabled: !sessionId,
    title: sessionId
      ? "Send ESC to abort the in-flight turn (session stays alive)"
      : STEER_DISABLED_TIP,
    onClick: sessionId ? () => interruptSession(sessionId) : null,
  });

  // Open live terminal — attaches to the session's *local* PTY socket
  // (/api/sessions/{taskKey}/attach), so it genuinely requires a live bridge and
  // stays gated on `attachable`: with no PTY there is no terminal to open.
  btn("Open live terminal", {
    variant: "soft",
    disabled: !attachable,
    title: attachable
      ? "Open the live PTY terminal for this session"
      : STEER_DISABLED_TIP,
    onClick: attachable && taskKey ? () => openTerminal(taskKey) : null,
  });

  // Take over (drive) — #200: resume & drive the pipeline via the #199
  // interrogate(drive) endpoint. This is the hosted-relay drive path: it resumes
  // the session into a *fresh* PTY server-side and opens the returned attach_url,
  // so it does NOT depend on a pre-existing local PTY (relay-drive ≠
  // local-PTY-steer). Gating it on `attachable` would regress #200's
  // parked-pipeline drive, so it stays enabled; driveControl() handles a 409
  // (lease held by an automated task) via the busy pill.
  btn("Take over", {
    title: "Resume the pipeline into a live terminal and drive it (acquires the lease; release returns control to the bot)",
    onClick: () => driveControl(pipelineKey, row && row.repo),
  });

  // Pause / Reassign: no endpoints exist — a separate functional issue.
  btn("Pause", { disabled: true, title: "Pause-on-demand is a separate functional issue" });
  btn("Reassign", { disabled: true, title: "Reassign-on-demand is a separate functional issue" });
}

// Gate the reply input + Send on a live PTY bridge (#225). The PTY bridge is
// unreliable, so reply/steer is greyed (with an explanatory tooltip) until a
// worker has actually attached one; the inject path stays wired behind the gate.
function renderSteerGate(row) {
  const attachable = !!(row && row.attachable);
  const text = document.getElementById("pipeline-reply-text");
  const send = document.getElementById("pipeline-reply-send");
  if (text) {
    text.disabled = !attachable;
    text.title = attachable ? "" : STEER_DISABLED_TIP;
  }
  if (send) {
    send.disabled = !attachable;
    send.title = attachable ? "" : STEER_DISABLED_TIP;
  }
}

// ready-for-* label controls (#225) — the moved "Assign issue". Two buttons set
// the mutually-exclusive entry labels on an issue pipeline; the currently-set
// label is highlighted and disabled. Hidden for pr-/other pipelines (these are
// issue-lifecycle labels). Reflects ghView.labels; POSTs the new label endpoint.
function renderLifecycle(repo, key) {
  const host = document.getElementById("pipeline-lifecycle");
  if (!host) return;
  host.innerHTML = "";
  const err = document.getElementById("pipeline-lifecycle-error");
  if (err) err.textContent = "";
  if (key.kind !== "issue") {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const labels = (lastGhView && Array.isArray(lastGhView.labels)) ? lastGhView.labels : [];
  const caption = document.createElement("span");
  caption.className = "lifecycle-caption";
  caption.textContent = "Set lifecycle:";
  host.appendChild(caption);
  const mk = (text, value) => {
    const active = labels.includes(value);
    const b = Btn({
      variant: active ? "primary" : "outline",
      size: "sm",
      label: text,
      onClick: active ? undefined : () => submitLabel(repo, value),
      attrs: active ? { disabled: "", "aria-pressed": "true" } : undefined,
    });
    b.title = active ? `Already ${value}` : `Set ${value} on ${key.label}`;
    host.appendChild(b);
  };
  mk("Mark ready for planning", "ready-for-planning");
  mk("Mark ready for development", "ready-for-development");
}

// POST the ready-for-* label endpoint (#225). Disables the group across the
// in-flight request (double-submit guard), optimistically reflects the new label
// (the next SSE snapshot confirms), and surfaces an error inline on failure.
async function submitLabel(repo, label) {
  if (!current) return;
  const host = document.getElementById("pipeline-lifecycle");
  const err = document.getElementById("pipeline-lifecycle-error");
  if (err) err.textContent = "";
  if (host) host.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    await apiText(`/api/pipelines/${encodeURIComponent(current.taskKey)}/labels`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, repo }),
    });
    // Optimistic: drop both entry labels then add the chosen one so the buttons
    // re-render in the new state immediately; the next snapshot is authoritative.
    if (lastGhView) {
      const kept = (lastGhView.labels || []).filter((l) => !READY_LABELS.includes(l));
      lastGhView.labels = [...kept, label];
    }
    renderLifecycle(current.repo, parseKey(current.taskKey));
    renderLinked(current.repo, parseKey(current.taskKey), findWorktree(lastState, current.repo, current.taskKey));
  } catch (e) {
    if (err) err.textContent = String((e && e.message) || e);
    renderLifecycle(current.repo, parseKey(current.taskKey)); // re-enable buttons
  }
}

// Take over (#200): POST /api/pipelines/{pipeline_key}/interrogate {mode:"drive"}
// → acquire the pipeline lease, resume the session into a fresh PTY, and open its
// returned attach_url in the existing terminal. The terminal's one-shot onClose
// releases the lease (close, WS drop, or navigate-away). A 409 means an automated
// task already holds the lease → surface the 'busy' pill instead of driving.
async function driveControl(pipelineKey, repo) {
  if (drivePipeline) return; // a drive is already live; ignore re-entry
  setDriveState("driving"); // optimistic; reverts on error
  let resp;
  try {
    resp = await apiText(`/api/pipelines/${encodeURIComponent(pipelineKey)}/interrogate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "drive", repo: repo || null }),
    });
  } catch (err) {
    // 409 = PipelineBusyError: the bot (or another task) holds the lease.
    setDriveState(err && err.status === 409 ? "busy" : "idle");
    return;
  }
  let attachUrl = null;
  try { attachUrl = (await resp.json()).attach_url; } catch (_) { /* malformed */ }
  // The lease is held the moment interrogate returned 2xx, so from here every
  // exit must release it. Mark the drive live first so releaseDrive is armed.
  drivePipeline = { pipelineKey, repo };
  if (!attachUrl) {
    releaseDrive(pipelineKey, repo); // no terminal to own the release
    return;
  }
  openTerminal(pipelineKey, {
    attachUrl,
    title: `Driving: ${pipelineKey}`,
    onClose: () => releaseDrive(pipelineKey, repo),
  });
}

// Release a live drive lease: POST /api/pipelines/{pipeline_key}/release. Single-
// shot via the `drivePipeline` guard so the terminal's onClose, a navigate-away,
// and a pagehide can all call it without double-releasing. Best-effort: the
// backend lease self-heals on a dead PID, so a failed POST is non-fatal.
function releaseDrive(pipelineKey, repo) {
  if (!drivePipeline) return; // already released
  drivePipeline = null;
  setDriveState("idle");
  apiText(`/api/pipelines/${encodeURIComponent(pipelineKey)}/release`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo: repo || null }),
  }).catch(() => { /* best-effort; lease self-heals */ });
}

function renderAll() {
  if (!current) return;
  const { repo, taskKey } = current;
  const key = parseKey(taskKey);
  const row = findRow(lastState, repo, taskKey);
  const worktree = findWorktree(lastState, repo, taskKey);
  const stuckEntries = findStuck(lastState, repo, taskKey);
  // Refresh the GitHub view first — header/stepper/state/linked/lifecycle all
  // read it through the module-level lastGhView.
  lastGhView = findPipelineView(lastState, repo, taskKey);
  const staging = deriveStage(taskKey, row, worktree, stuckEntries, lastGhView);

  renderBreadcrumb(repo, key);
  renderHeader(repo, key);
  renderStepper(staging);
  renderControls(row, row ? row.pipeline_key || taskKey : taskKey);
  renderSteerGate(row);
  renderLifecycle(repo, key);
  renderLinked(repo, key, worktree);
  renderWorktree(worktree);
}

// Start the inline conversation stream once, if the row is observable. Idempotent:
// a no-op while a stream is already live, so an incoming snapshot never restarts
// (and so reconnects) the conversation.
function startConvStream() {
  if (convStream || !current) return;
  const conv = document.getElementById("pipeline-conv");
  if (!conv) return;
  const row = findRow(lastState, current.repo, current.taskKey);
  if (row && row.observable) {
    convStream = streamObserve(current.taskKey, conv, onConvStatus);
  } else {
    conv.innerHTML = "";
    conv.appendChild(muted("No transcript recorded for this session yet."));
  }
}

// Folded-in worker-log tail (#221): stream the pipeline's repo log into the
// detail view's log pane. Idempotent — a re-render never restarts the tail
// (mirrors repoDetail's stopLog discipline); show() owns start/stop.
function startLogStream() {
  if (stopLog || !current) return;
  const pre = document.getElementById("pipeline-log");
  if (!pre) return;
  const title = document.getElementById("pipeline-log-title");
  if (title) title.textContent = `— ${current.repo} (live)`;
  stopLog = streamLog(current.repo, pre);
}

// Reflect the read-only inline observe stream in the state pill (#200). Observe
// is the lowest-precedence drive state, so only overlay/clear it when no higher
// state (driving/busy) is active — driving must not be clobbered by the inline
// observe stream that runs alongside it.
function onConvStatus(_text, kind) {
  if (kind === "live") {
    if (driveState === "idle") setDriveState("observing");
  } else if (kind === "off") {
    if (driveState === "observing") setDriveState("idle");
  }
}

// Called by app.js's wireDetailViews() effect when the active view/pipeline
// changes. Manages the (single) conversation stream and triggers a render.
// Idempotent for the same pipeline so a re-render never restarts the stream.
function show(repo, taskKey) {
  const key = repo && taskKey ? `${repo} ${taskKey}` : null;
  if (key === currentKey) return;
  if (convStream) {
    convStream.close();
    convStream = null;
  }
  if (stopLog) {
    stopLog();
    stopLog = null;
  }
  // Release any live drive lease before leaving this pipeline — a navigated-away
  // drive must not keep the bot paused. The terminal's onClose is single-shot
  // with this, so calling it here is safe.
  if (drivePipeline) releaseDrive(drivePipeline.pipelineKey, drivePipeline.repo);
  driveState = "idle";
  lastGhView = null; // cleared so a stale view never bleeds into the new page
  current = key ? { repo, taskKey } : null;
  currentKey = key;
  timelineLastFetch = 0; // force a fresh timeline fetch for the new pipeline
  if (!current) return; // navigated away

  renderAll();
  refreshTimeline();
  startConvStream();
  startLogStream();
}

// Refresh the Activity timeline from the pipeline-log feed (#220/#225). The log
// isn't in the SSE payload, so it is fetched separately — throttled so a fast
// snapshot cadence doesn't hammer the endpoint.
const TIMELINE_REFRESH_MS = 4000;
function refreshTimeline() {
  if (!current) return;
  const { repo, taskKey } = current;
  const row = findRow(lastState, repo, taskKey);
  const stuckEntries = findStuck(lastState, repo, taskKey);
  loadTimeline(repo, taskKey, row, stuckEntries);
}

// Fed the consolidated snapshot by the orchestrator; re-render only while a
// pipeline detail page is open. Never restarts a live conversation stream, but
// will start one once the row becomes observable.
export function update(snapshot) {
  lastState = snapshot;
  if (current) {
    renderAll();
    if (Date.now() - timelineLastFetch >= TIMELINE_REFRESH_MS) refreshTimeline();
    startConvStream();
  }
}

export function init() {
  // Exposed on window so app.js's wireDetailViews() effect can drive show(),
  // and so #200 can reach setDriveState without re-importing the module.
  window.issueDetail = { show, setDriveState };

  const send = document.getElementById("pipeline-reply-send");
  if (send) send.addEventListener("click", submitReply);

  // Best-effort lease release if the tab closes mid-drive. The backend lease
  // self-heals on a dead PID, so this is a promptness optimization, not a
  // correctness guarantee (a closing tab may not flush the request).
  window.addEventListener("pagehide", () => {
    if (drivePipeline) releaseDrive(drivePipeline.pipelineKey, drivePipeline.repo);
  });

  // Honour a deep link (#pipeline/owner/name/key) if Alpine has already booted.
  const store = window.Alpine && window.Alpine.store("app");
  if (store && store.view === "pipeline" && store.repo && store.pipelineRoute) {
    show(store.repo, store.pipelineRoute);
  }
}

// Reply → inject (live): POST an operator turn the orchestrator runs next. The
// send button is disabled across the in-flight POST so a double-click can't
// enqueue duplicate operator turns (mirrors attach.js submitSteer).
async function submitReply() {
  const text = document.getElementById("pipeline-reply-text");
  const err = document.getElementById("pipeline-reply-error");
  const send = document.getElementById("pipeline-reply-send");
  if (!text || !current) return;
  const prompt = text.value.trim();
  if (!prompt) {
    if (err) err.textContent = "Reply can't be empty.";
    text.setAttribute("aria-invalid", "true");
    text.setAttribute("aria-errormessage", "pipeline-reply-error");
    text.focus();
    return;
  }
  text.removeAttribute("aria-invalid");
  text.removeAttribute("aria-errormessage");
  if (err) err.textContent = "";
  if (send) send.disabled = true;
  try {
    await injectTurn(current.taskKey, prompt);
    text.value = "";
  } catch (e) {
    if (err) {
      err.textContent = String(e.message || e);
      text.setAttribute("aria-invalid", "true");
      text.setAttribute("aria-errormessage", "pipeline-reply-error");
    }
  } finally {
    if (send) send.disabled = false;
  }
}
