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

## 0. Mental model — a human team

This contract models how a team of humans shares a backlog, because the other actors are (or
behave like) humans. Translate every rule back to this picture if in doubt:

- **A claim is an assignment.** You pick up a ticket by putting your name on it. It stays
  yours until you finish, you hand it back, or someone with authority reassigns it. It does
  **not** evaporate because you went quiet for an hour.
- **Liveness is observed, not broadcast.** Nobody re-asserts "I'm still on this" every ten
  minutes. Teammates infer whether work is alive from **out-of-band signals** — the last
  commit, the last comment, standup — not from a heartbeat the worker is obligated to emit.
- **A stuck worker comes back with fresh eyes.** If progress stalls, the *same* worker (its
  next session) resumes its *own still-assigned* ticket later. The assignment never changed.
- **Truly stuck → ask for help.** The worker escalates explicitly (`in-error`). It does not
  silently drop the ticket.
- **Persistent silence → a lead reassigns.** If a ticket neither progresses nor escalates,
  an **authority** (a human, or a designated supervisory actor) reassigns it. Peers do
  **not** seize each other's tickets on a timer. Every actor respects "not mine → not mine."

The machine-paced parts — fast crash recovery, heartbeating, in-flight dedupe among one
actor's own workers — live in the **local** scope, invisible to this contract.

---

## 1. Principles

1. **Advisory, not mutual exclusion.** A claim is a courtesy signal, not a lock. The
   protocol cannot *prevent* a non-conforming actor from acting concurrently; it makes
   concurrent action **visible, rare, and cheap to reconcile**.
2. **Optimistic.** Actors claim, then verify, then proceed — they do not acquire before
   acting. A lost race costs at most one wasted unit of work, never corrupted state.
3. **Idempotent effects.** Every externally visible side effect MUST be keyed to the work
   item so that a duplicate actor produces a no-op or a merge, never duplication.
4. **Detect-and-yield.** When an actor observes that another holds a claim, it MUST yield.
   Detection is mandatory; prevention is not possible.
5. **Durable ownership.** A claim does **not** self-expire on a clock. Ownership ends only by
   the owner completing or handing back the work, or by an authority reassigning it (§5).
6. **Liveness is observed, not heartbeated.** Whether a claim is still being worked is
   inferred from observable repo activity at **human pace** (hours, not minutes). There is no
   obligation to emit a fast keep-alive, and absence of one is **not** grounds for a peer
   takeover.
7. **Labels are a shared contract.** The label vocabulary and its transitions are a
   versioned interface owned by no single actor. Changing their meaning is a breaking change
   to *this contract*, not a local refactor.

---

## 2. Vocabulary

- **Actor** — an independent participant (bot or human) acting on the repo. Each automated
  actor has a stable **actor id** (its GitHub login, e.g. `trixy`) and SHOULD advertise its
  software/version.
- **Authority** — an actor permitted to reassign work it does not own: a human maintainer, or
  a supervisory actor the team designates. Ordinary peer actors are **not** authorities.
- **Work item** — an issue or pull request, identified by its number. Conventionally grouped
  by branch key `issue-<N>` (issue-originated) or `pr-<P>` (externally opened PR).
- **State** — the coarse lifecycle phase, carried by **labels** (§3).
- **Claim** — a durable assertion of ownership, carried by the **assignee** (§4).

---

## 3. State layer — labels (the shared contract)

State is carried by these labels. An actor MUST treat them as the canonical phase signal.

| Label | Meaning | Who may set |
| --- | --- | --- |
| `ready-for-planning` | Open for an actor to plan; awaiting/holding a plan for human approval | any actor; humans |
| `ready-for-development` | Plan approved, open for an actor to implement | any actor; humans |
| `in-progress` | An actor is actively working this item (see claim, §4) | the claiming actor |
| `in-error` | Terminal-until-human: the actor escalated for help; **do not auto-retry, do not seize** | the failing actor; humans |

Rules:

- An item is **available** when it carries an actionable state label (`ready-for-*`) and is
  **unassigned** (§4).
- An item carrying `in-progress` is **owned** by its assignee; another actor MUST NOT act on
  it. Only the owner (a later session of the same actor) resumes it.
- `in-error` is an explicit request for human help. A conforming actor MUST NOT claim,
  reassign, or auto-act on an `in-error` item. Only a human (or an authority's deliberate
  re-label/reassignment) returns it to an available state.
- An actor MUST NOT repurpose these labels for private bookkeeping, and MUST NOT remove a
  label it does not understand. Unknown labels MUST be preserved untouched.

---

## 4. Claim layer — ownership

A claim is **the GitHub assignee**. This is the human-legible, native, single-source answer
to "whose work is this," readable by every actor — bot or human — without any custom medium.

- To claim, an actor assigns the work item to its own account.
- The claim is **durable**: it persists across the claiming actor's crashes, restarts, and
  quiet periods. It does **not** carry or honor a machine TTL.
- An item assigned to actor *X* belongs to *X*. Another actor MUST NOT act on it, regardless
  of how long *X* has been silent. Reassignment is an authority's decision (§5.4), signalled
  by the assignee field **changing** — never by a peer's local timer.

**Optional activity marker (informational only).** An actor MAY maintain a single marker
comment to make its progress legible to humans and supervisory actors — the bot equivalent of
a standup update. It is an **activity log, not a lease**, and carries **no enforced expiry**:

~~~text
<!-- gcp:claim v=0.1 -->
```json
{
  "actor": "trixy",
  "software": "loony-dev/1.x",
  "work_item": "issue-197",
  "claimed_at": "2026-06-28T12:00:00Z",
  "last_active": "2026-06-28T15:30:00Z",
  "phase": "implement"
}
```
<!-- /gcp:claim -->
~~~

- There MUST be at most **one** marker per work item; updates edit it in place.
- Timestamps MUST be UTC, RFC 3339. `last_active` is advisory — it informs a human or
  authority's judgement (§5.4), and MUST NOT be interpreted by a peer as a license to take
  over.

---

## 5. Lifecycle

### 5.1 Discover
List candidate work items by state label (§3) and read the assignee. Filter to **available**
items: actionable label **and** unassigned (§3–§4). Skip anything assigned to another actor.

### 5.2 Claim (optimistic, with verification)
To claim an available item, an actor MUST, in order:

1. Assign the item to itself and set state to `in-progress`.
2. **Wait a short randomized jitter** (RECOMMENDED 2–5 s), then **re-read** the assignee.
3. If the re-read shows a different/additional assignee, apply the tiebreak (§6). If the
   actor is not the winner, it MUST yield (§5.5) and abandon the item.

An actor MUST complete step 2 before performing any irreversible side effect (pushing a
branch, opening a PR, posting substantive output). It MAY then post/update the activity
marker (§4).

### 5.3 Work (no heartbeat obligation)
While working, the actor SHOULD update the activity marker's `last_active`/`phase` at natural
boundaries (per phase or turn) so humans and supervisory actors can see progress. This is
**not** a keep-alive: missing it does not forfeit the claim, and no peer may act on its
absence.

### 5.4 Resumption and reassignment
- **Owner resumption ("fresh eyes").** A later session of the *owning* actor MAY resume its
  own still-assigned item after a stall. The assignee does **not** change; to peers, nothing
  happened. The cadence of this retry is the owner's **local** policy (loony-dev's default is
  a coarse, human-paced ~12 h), out of scope for this contract.
- **Authority reassignment.** Only an **authority** (§2) may transfer an item away from a
  silent owner, by **changing the assignee**. This is the "team lead reassigns" path. It is a
  deliberate act informed by observable signals (last commit/comment, the activity marker),
  not an automated expiry. Once the assignee changes, the new owner claims per §5.2 and the
  prior owner, on noticing it is no longer assigned, MUST stand down.
- **Peers never reassign.** An ordinary actor MUST NOT reassign or act on another actor's
  item, no matter how stale it looks.

### 5.5 Release / Yield / Escalate
- **Complete:** advance state via the normal transitions (open the PR, drop `in-progress`) and
  unassign (or let PR-merge close it out).
- **Yield** (lost race, voluntary hand-back): revert any state set during the failed claim and
  remove itself as assignee so the item returns to **available**.
- **Escalate:** on a failure it cannot resolve, set `in-error` (§3) — the explicit "I need
  help" signal. The actor SHOULD keep itself assigned (the ticket is still its escalation) but
  MUST stop auto-acting; an authority takes it from here.

---

## 6. Conflict resolution

Two actors MAY claim a previously-unassigned item within the jitter window before either sees
the other (GitHub assignment is last-writer-wins and permits multiple assignees). Resolution
MUST be **deterministic and computable independently by both**, so each reaches the same
verdict without negotiation:

1. **Earlier `claimed_at` wins** (from the activity marker, when present).
2. **No marker / tie on `claimed_at` → lexicographically smallest `actor` id wins.**

The non-winner MUST yield (§5.5) — remove itself as assignee and abandon the item. Because the
rule is a pure function of values both actors can read, no lock or exchange is needed.

---

## 7. Idempotency requirements

Conformance to §5–§6 makes duplicate work **rare**; idempotency makes it **harmless**. An
actor MUST:

- Use a deterministic, work-item-keyed branch (`issue-<N>` / `feat-…`) so a racing duplicate
  pushes to the same ref rather than forking a parallel branch.
- Check for an existing open PR for the work item before opening one; reuse it if present.
- Treat comment/label writes as upserts where the contract defines a single canonical artifact
  (e.g. the activity marker, a plan comment).

A lost race that nonetheless produced a side effect MUST converge to the same artifact the
winner produces, not a second copy.

---

## 8. Conformance levels

- **Full** — implements claim + verify + tiebreak + idempotency, maintains the activity marker,
  and honors authority reassignment. Safe to run alongside other Full actors with near-zero
  duplicated work.
- **Cooperative** — honors labels and assignee, claims by assigning itself and clears it when
  done, yields to any item already assigned to another — but does not maintain a marker (e.g. a
  human, or a simple bot). Safe, at the cost of coarser observability.
- **Non-conforming** — ignores the contract. The protocol cannot constrain it; it can only be
  detected after the fact via idempotent convergence (§7). Out of scope for safety guarantees.

A Full actor MUST interoperate safely with Cooperative actors and MUST degrade safely (yield a
claim, never corrupt state) in the presence of Non-conforming ones.

---

## 9. Non-goals

- **Not** mutual exclusion. Concurrent action is possible; the contract bounds its cost, not
  its occurrence.
- **Not** a machine lease. There is no fast TTL and no automatic peer takeover on silence;
  reassignment is an authority's deliberate act.
- **Not** a crash-recovery mechanism. How fast an owner's own next session resumes a stalled
  item is **local** policy, invisible here.
- **Not** a resource/quota coordinator (e.g. shared API quotas across actors). Out of scope.
- **Not** an intra-actor scheduler. How one actor divides work among its own
  workers/processes is private and invisible to this contract.
- **Not** a security boundary. It assumes cooperative actors; it does not defend against a
  hostile one.

---

## 10. Versioning

This contract is versioned (`gcp:claim v=…` and this document's header). The label vocabulary
(§3) and the assignee semantics (§4) are the breaking surface. Additive fields in the marker
JSON are non-breaking and MUST be ignored by actors that don't understand them. Removing or
redefining a label, or changing claim/assignment semantics, requires a major version bump and
coordinated rollout across actors.
