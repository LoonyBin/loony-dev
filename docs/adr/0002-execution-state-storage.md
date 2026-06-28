# ADR 0002 — Execution-state storage: filesystem artifacts now, a database deferred to a derived projection

- **Status:** Accepted (2026-06-28)
- **Scope:** the local execution-state substrate (epic #266) — the per-pipeline **event log** + **live-state snapshot** that workers emit and that both the reliability layer and the web dashboard read.

## Context

Workers, the supervisor, and the web dashboard coordinate **through the filesystem only**; the single rich signal a worker emits today is a freeform **log**. Logs are write-optimised for a human reading after the fact — not read-optimised for a live dashboard, and useless as a basis for reliability decisions. #266 fixes this by making the worker emit a structured execution substrate (event log + live-state snapshot), with reliability and observability as co-equal readers.

That raised an unavoidable storage question, and we decided to settle it **before** the emission contract (#267) is built, because the choice is baked into that contract:

> Keep the substrate as filesystem artifacts under `<workspace>/.logs`, or stand up a database (sqlite/postgres, behind a switchable driver)?

## The two workloads pull opposite ways

The substrate is not one workload. It is two, with opposite ideal backends:

| Workload | Shape | Best fit |
|---|---|---|
| Append event | hot, per-pipeline, **no cross-pipeline write contention** | filesystem (append) |
| Update live-state snapshot | hot, per-pipeline, every turn | filesystem (atomic `os.replace`) |
| Stream events → dashboard | push, sub-second | filesystem (inotify) |
| "What's running now" | reduce over the *active* set (small: ≤ pool size) | wash |
| Historical / relational / time-windowed aggregates | append-once, query-many | database |

The hot path and live streaming — the entire point of #266 — favour the filesystem. A database wins only on **historical/relational aggregation**.

### Why sqlite specifically is the wrong rung here

sqlite reads as the "easy default" and is not:

- **Multi-process writers.** The supervisor runs **worker-per-repo** — many processes writing execution state frequently. sqlite is single-writer (one DB lock; WAL helps readers, not writers). Workers that today contend on *nothing* (each writes its own pipeline file) would serialise on a global write lock and surface `database is locked`. We would be *adding* contention the filesystem does not have.
- **Loses streaming.** sqlite has no `LISTEN/NOTIFY`, so the dashboard would fall back to **polling** — the exact problem #270 exists to delete. inotify-on-append is free; a DB throws it away unless it is postgres.

So if a DB ever lands it is **postgres, not sqlite** — which means real operational weight (a service to run, back up, monitor), the threat the original SWOT flagged.

## Evidence: what the dashboard actually reads

The decision turned on what the views actually consume, not on a generic DB-vs-FS preference. We extracted the read-model from the #218 cockpit hi-fi mock (the L Space prototypes attached to #228 — `ld-cockpit.jsx`, `ld-fleet.jsx`, `ld-sessions.jsx`, `ld-kit.jsx`):

| View | Source of its data |
|---|---|
| Fleet board / kanban / stat strip | GitHub (titles, PRs) + **live-state snapshot** (stage, current skill, updated, needs-you) + reduce over the small active set |
| Live (per-repo) | GitHub (open issues/PRs) + git status + snapshot (workers-here) |
| Issue ▸ PR stepper + activity timeline | snapshot (phase) + **event log tail** (one file) + GitHub (diff/reviews) |
| Cockpit DAG | GitHub **sub-issues = the dependency edges** + label/PR state + snapshot live-overlay |
| Cockpit live-activity feed | **merge of recent event-log tails** across the active set (small N) |
| Skills "used N× today" / Run log | **the only surface that wants cross-fleet, time-windowed aggregation** |

Two findings fell out:

1. **The DAG edges are already materialised in GitHub.** The cockpit's dependency graph is epic→issues + parent/child links — i.e. GitHub **sub-issues**, exactly the structure used to file #266 → #267–270. #218's graph structure is in GitHub by construction; the cockpit reads it, it is not a new store.
2. **The only analytical surface is peripheral.** Everything on the "dashboard live + useful" critical path is **live-overlay (snapshot) + GitHub-graph + event tails (per-pipeline or recent-merge)**. The single read that wants relational/historical aggregation — the Skills run-log/usage counts — is off the critical path and satisfiable by a bounded time-window scan of event logs or a small rolling counter.

## Decision

1. **Filesystem under `<workspace>/.logs`.** The substrate is instance-private filesystem artifacts. **Workers never write to a database** — in any scenario (the worker-per-repo write contention and the loss of inotify streaming make a worker→DB write path wrong regardless).
2. **The event log is projection-grade:** complete, ordered, typed, actor-stamped, and **time-mergeable across pipelines** (the cockpit live-activity feed requires the cross-pipeline merge). It is a real event stream, not a throwaway tail buffer for the activity panel.
3. **Writes go through a narrow storage interface** (`append_event` / `write_snapshot` / `read_snapshot` / `list_active` / `tail_events` / `stream_events`). #267 implements the filesystem backend; the orchestrator and agent call-sites only ever touch this seam.
4. **A database is deferred to a derived read-model projection**, triggered **only** when a fleet-analytics / skill-run-log / historical-metrics need becomes real (not part of #266, and not the headline of #218 — whose DAG is GitHub-graph + live-overlay). Even then it is **postgres** (with `LISTEN/NOTIFY` for streaming), built by **tailing the event log**, read *alongside* the live filesystem feed — never a worker write target. **sqlite is explicitly rejected** for this substrate.

## Rationale

- The hot path (per-pipeline append + snapshot rewrite) and live streaming (inotify push) are what #266 is *for*; the filesystem is the better engineering fit and a DB actively hurts both.
- Zero new infrastructure or operational surface, consistent with the existing process model and the codebase's "coordinate through the filesystem" ethos.
- Operator debuggability is preserved: `tail` the event log, `cd` into the worktree — the same inspection philosophy retention was designed around (#198).
- Event-sourcing keeps the filesystem log as the **source of truth** and makes any future database a **derived index**, not a second master — so the decision is a strict prefix of the eventual shape, with no rework.
- The read-model evidence shows the analytical surface that would justify a DB is peripheral and defer- able (YAGNI); the storage seam makes adding it later cheap.

## Consequences

- **+** Dashboard reads a small snapshot + tails events; reliability reads `last_heartbeat` from the snapshot — which lets #268/#270 delete the `/proc` liveness forensics (the 300ms-sleep sampling) entirely.
- **+** No broker, no DB, no migration/backup surface introduced now.
- **−** Cross-cutting historical queries (skill run-log, usage-over-time) require either a bounded log scan or a later postgres projection; they are not free.
- **−** Append-only event logs grow and need retention/rotation (the existing `.log` files have the same need).
- The storage seam (decision 3) and the projection-grade event schema (decision 2) are now **constraints on #267**.

## Out of scope

- The **global** cross-actor coordination contract — `docs/global-claim-protocol.md` (GitHub labels/assignee) — is a different layer and unaffected.
- Full FastAPI / background-job-queue conversion (rejected separately: node-local worktrees + resumable `claude -p` sessions are hostile to a stateless distributed model).
