# Global Claim Protocol (GCP) — v0.1

**Status:** Draft
**Scope:** Coordination of work-item ownership on a shared GitHub repository across
**independent, heterogeneous actors** — multiple automation agents (possibly built on
entirely different software) and human contributors.
**Audience:** anyone implementing an agent that operates on a repo `loony-dev` also operates on.

This is an **external contract**, not an internal design. It governs the *global* scope
only: who is allowed to act on a given work item at a given time, expressed through state
that every actor can read and write via the ordinary GitHub API. It says nothing about how
any single actor schedules its own internal workers — that is a private, local concern.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are used per
RFC 2119.

---

## 1. Principles

1. **Advisory, not mutual exclusion.** A claim is a courtesy signal, not a lock. The
   protocol cannot *prevent* a non-conforming actor from acting concurrently; it makes
   concurrent action **visible, rare, and cheap to reconcile**.
2. **Optimistic.** Actors claim, then verify, then proceed — they do not acquire before
   acting. A lost race costs at most one wasted unit of work, never corrupted state.
3. **Idempotent effects.** Every externally visible side effect MUST be keyed to the work
   item so that a duplicate actor produces a no-op or a merge, never duplication.
4. **Detect-and-yield.** When an actor observes that another holds a live claim, or that it
   lost a race, it MUST yield. Detection is mandatory; prevention is not possible.
5. **Self-expiring.** Every claim carries an explicit expiry. A crashed or vanished actor
   releases its work automatically once its claim lapses — no actor depends on another to
   clean up.
6. **Labels are a shared contract.** The label vocabulary and its transitions are a
   versioned interface owned by no single actor. Changing their meaning is a breaking change
   to *this contract*, not a local refactor.

---

## 2. Vocabulary

- **Actor** — an independent participant (bot or human) acting on the repo. Each automated
  actor has a stable **actor id** (its GitHub login, e.g. `trixy`) and SHOULD advertise its
  software/version.
- **Work item** — an issue or pull request, identified by its number. Conventionally grouped
  by branch key `issue-<N>` (issue-originated) or `pr-<P>` (externally opened PR).
- **State** — the coarse lifecycle phase, carried by **labels** (§3).
- **Claim** — a time-bounded assertion of ownership over a work item, carried by **assignee +
  a claim marker** (§4).

---

## 3. State layer — labels (the shared contract)

State is carried by these labels. An actor MUST treat them as the canonical phase signal.

| Label | Meaning | Who may set |
| --- | --- | --- |
| `ready-for-planning` | Open for an actor to plan; awaiting/holding a plan for human approval | any actor; humans |
| `ready-for-development` | Plan approved, open for an actor to implement | any actor; humans |
| `in-progress` | An actor is actively working this item (see claim, §4) | the claiming actor |
| `in-error` | Terminal-until-human: repeated/permanent failure, **do not auto-retry** | the failing actor; humans |

Rules:

- An item is **actionable** when it carries an actionable state label (`ready-for-*`) and has
  **no live claim** (§4), OR it carries `in-progress` with an **expired** claim (reclaimable).
- `in-error` is a terminal handoff to humans. A conforming actor MUST NOT claim or auto-act on
  an `in-error` item. Only a human (or explicit human-triggered re-label) returns it to an
  actionable state.
- An actor MUST NOT repurpose these labels for private bookkeeping, and MUST NOT remove a
  label it does not understand.
- Unknown labels MUST be preserved untouched.

---

## 4. Claim layer — ownership

A claim has two parts, both readable by any actor through standard GitHub:

1. **Owner identity — the GitHub assignee.** The claiming actor MUST assign the work item to
   its own account. The assignee is the human-legible, native, single-source answer to "who
   holds this." An item with a different live assignee is claimed.
2. **Claim marker — a single machine-readable comment.** Because GitHub has no native lease
   metadata or TTL, the claim's timing is carried in **one canonical marker comment** on the
   work item, identified by a stable sentinel so any actor can find and parse it:

```
<!-- gcp:claim v=0.1 -->
```json
{
  "actor": "trixy",
  "software": "loony-dev/1.x",
  "work_item": "issue-197",
  "claimed_at": "2026-06-28T12:00:00Z",
  "last_heartbeat": "2026-06-28T12:04:30Z",
  "expires_at": "2026-06-28T12:10:00Z"
}
```
<!-- /gcp:claim -->
```

Rules:

- There MUST be at most **one** claim marker per work item. An actor updating a claim MUST
  edit the existing marker comment in place, not post a new one.
- All timestamps MUST be UTC, RFC 3339.
- `expires_at` MUST be `last_heartbeat + lease_ttl`. The RECOMMENDED `lease_ttl` is **10
  minutes**; an actor MAY use a shorter TTL but SHOULD NOT exceed 30 minutes.
- A claim is **live** iff `now < expires_at`; otherwise it is **expired** and the item is
  reclaimable by any actor.

---

## 5. Lifecycle

### 5.1 Discover
List candidate work items by state label (§3). For each, read the assignee and parse the
claim marker (§4). Filter to **actionable** items (§3).

### 5.2 Claim (optimistic, with verification)
To claim an actionable item, an actor MUST, in order:

1. Set the assignee to itself and set state to `in-progress`.
2. Write/replace the claim marker with a fresh `claimed_at`, `last_heartbeat`, and
   `expires_at`.
3. **Wait a short randomized jitter** (RECOMMENDED 2–5 s), then **re-read** the assignee and
   claim marker.
4. If the re-read shows a competing claim within the contention window, apply the tiebreak
   (§6). If the actor is not the winner, it MUST yield (§5.5) and abandon the item.

An actor MUST complete step 3 before performing any irreversible side effect (pushing a
branch, opening a PR, posting substantive output).

### 5.3 Heartbeat
While working, the actor MUST refresh the claim marker (`last_heartbeat`, recomputed
`expires_at`) at an interval comfortably shorter than `lease_ttl` (RECOMMENDED ≤ ⅓ TTL). An
actor that cannot heartbeat (crash, network loss) lets its claim lapse automatically.

### 5.4 Reclaim (expired claims)
Any actor MAY claim an item whose marker is expired by following §5.2. The new claim
supersedes the stale one. The reclaiming actor SHOULD note the supersession in the marker
(e.g. a `superseded` field) but is not required to coordinate with the prior owner.

### 5.5 Release / Yield
- On completion, an actor advances state via the normal transitions (e.g. open the PR, drop
  `in-progress`) and MUST clear its claim (unassign and mark the claim released or remove the
  marker).
- On yield (lost race, voluntary abort), an actor MUST revert any state it set during the
  failed claim that another actor now owns — specifically it MUST NOT leave itself assigned or
  the marker pointing at itself if it did not win.
- On permanent failure, an actor sets `in-error` (§3) and clears its claim.

---

## 6. Conflict resolution

Two actors MAY claim within the jitter window before either sees the other. Resolution MUST
be **deterministic and computable independently by both**, so each reaches the same verdict
without further negotiation:

1. **Earlier `claimed_at` wins.**
2. **Tie on `claimed_at` → lexicographically smallest `actor` id wins.**

The non-winner MUST yield (§5.5). Because the rule is a pure function of values both actors
can read, no lock or exchange is needed. Clock skew only widens the contention window
slightly; the jitter (§5.2) MUST exceed expected skew (RECOMMENDED treat skew as ≤ 2 s).

---

## 7. Idempotency requirements

Conformance to §5–§6 makes duplicate work **rare**; idempotency makes it **harmless**. An
actor MUST:

- Use a deterministic, work-item-keyed branch (`issue-<N>` / `feat-…`) so a racing duplicate
  pushes to the same ref rather than forking a parallel branch.
- Check for an existing open PR for the work item before opening one; reuse it if present.
- Treat comment/label writes as upserts where the contract defines a single canonical artifact
  (e.g. the claim marker, a plan comment).

A lost race that nonetheless produced a side effect MUST converge to the same artifact the
winner produces, not a second copy.

---

## 8. Conformance levels

- **Full** — implements claim + verify + heartbeat + reclaim + tiebreak + idempotency. Safe to
  run alongside other Full actors with near-zero duplicated work.
- **Cooperative** — honors labels and assignee, reads claims and yields to live ones, but does
  not heartbeat (e.g. a human, or a simple bot). Safe, at the cost of coarser handoff. Such an
  actor SHOULD set `in-progress` + assignee while working and clear them when done.
- **Non-conforming** — ignores the contract. The protocol cannot constrain it; it can only be
  detected after the fact via idempotent convergence (§7). Out of scope for safety guarantees.

A Full actor MUST interoperate safely with Cooperative actors and MUST degrade safely (lose a
claim, never corrupt state) in the presence of Non-conforming ones.

---

## 9. Non-goals

- **Not** mutual exclusion. Concurrent action is possible; the contract bounds its cost, not
  its occurrence.
- **Not** a resource/quota coordinator (e.g. shared API quotas across actors). Out of scope.
- **Not** an intra-actor scheduler. How one actor divides work among its own
  workers/processes is private and invisible to this contract.
- **Not** a security boundary. It assumes cooperative actors; it does not defend against a
  hostile one.

---

## 10. Versioning

This contract is versioned (`gcp:claim v=…` and this document's header). The label vocabulary
(§3) and the claim marker schema (§4) are the breaking surface. Additive fields in the marker
JSON are non-breaking and MUST be ignored by actors that don't understand them. Removing or
redefining a label, or changing claim semantics, requires a major version bump and coordinated
rollout across actors.
