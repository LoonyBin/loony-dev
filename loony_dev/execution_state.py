"""Per-pipeline execution-state substrate: event log + live-state snapshot (issue #267).

This is the **worker-side write path** for the execution-state substrate (epic
#266), behind the narrow storage seam pinned by ADR 0002
(``docs/adr/0002-execution-state-storage.md``). It is **writer-only**: nothing
reads these artifacts yet — the dashboard / reliability children (#268–#270)
wire up later — so it ships standalone with no behaviour change.

Two instance-local artifacts per pipeline, co-located with the existing #220
per-pipeline log (same ``pipelines/`` dir, same :func:`session_registry.task_slug`
slugging, same forward-only ``.key`` sidecar)::

    .logs/<owner>/<repo>/pipelines/
        <slug>.log           # existing (#220)
        <slug>.key           # existing sidecar (raw pipeline key; forward-only)
        <slug>.events.jsonl  # NEW — append-only event log (one JSON object/line)
        <slug>.state.json    # NEW — live-state snapshot (atomically rewritten)

The storage seam (ADR decision 3) is **exactly six** public names —
:func:`append_event`, :func:`write_snapshot`, :func:`read_snapshot`,
:func:`list_active`, :func:`tail_events`, :func:`stream_events`. A heartbeat bump
is **not** a seventh: the read-modify-write that preserves the snapshot's other
fields lives in the private :func:`_bump_snapshot`. The path locators
(:func:`events_path`, :func:`snapshot_path`) mirror :mod:`pipeline_log`'s
forward-only contract and are support, not storage primitives.

The event log is **projection-grade** (ADR decision 2): complete, ordered, typed,
actor-stamped, and **time-mergeable across pipelines** — ``ts`` is ISO-8601 UTC,
so a cross-pipeline merge for the cockpit live-activity feed is a plain sort of
per-pipeline tails. The snapshot is **atomically rewritten** (temp + ``os.replace``,
mirroring :func:`session_registry.write_session_file`) so a concurrent reader
never observes a torn file.

All writes are **best-effort** at the call-sites: a substrate failure must never
break a task. This module raises on programmer error (closed-vocab violations at
construction) but the readers (:func:`read_snapshot`, :func:`tail_events`,
:func:`list_active`) parse defensively and never raise on a malformed file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from loony_dev import pipeline_log, session_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed vocabularies (ADR decision 2 — typed event schema; #218 read-model)
# ---------------------------------------------------------------------------

# Event ``type`` — the closed vocabulary the projection reads.
EVENT_TYPES = frozenset(
    {"dispatched", "phase_enter", "turn_start", "turn_complete", "error", "terminal"}
)
# Event ``state_tone`` — the frontend's colour/affordance hint.
STATE_TONES = frozenset({"active", "review", "blocked", "none"})
# Snapshot ``stage`` — the pipeline's lifecycle phase.
STAGES = frozenset(
    {"Inbox", "Planning", "Implementing", "PR Open", "In Review", "Conflicts", "Merged"}
)
# Snapshot ``state`` — the writer sets ``running`` / ``idle`` / ``failed`` only;
# ``crashed`` is reserved for the reliability layer (#268) to set when it finds a
# stale heartbeat. The full vocabulary is admitted here so a ``crashed`` snapshot
# round-trips through :func:`read_snapshot`.
STATES = frozenset({"running", "failed", "crashed", "idle"})

# File-name suffixes, siblings of the #220 ``.log`` / ``.key``.
EVENTS_SUFFIX = ".events.jsonl"
SNAPSHOT_SUFFIX = ".state.json"

# ---------------------------------------------------------------------------
# task_type → skill / stage maps (verified against loony_dev/tasks/*.py)
# ---------------------------------------------------------------------------
# ``current_skill`` reflects the **dispatched task's** skill. It is derived from
# ``task.task_type`` — never from ``task.command_name`` — because ``IssueTask``
# is multi-phase and leaves ``command_name = None`` (tasks/base.py), so the
# headline implement phase would record ``current_skill=None`` and violate
# "all fields populated".
SKILL_BY_TASK_TYPE: dict[str, str] = {
    "plan_issue": "plan-issue",
    "implement_issue": "implement-issue",
    "address_review": "address-reviews",
    "resolve_conflicts": "resolve-conflicts",
    "fix_ci": "fix-ci",
    "cleanup_stuck": "cleanup-stuck",
}

STAGE_BY_TASK_TYPE: dict[str, str] = {
    "plan_issue": "Planning",
    "implement_issue": "Implementing",
    "address_review": "In Review",
    "resolve_conflicts": "Conflicts",
    "fix_ci": "PR Open",
}


def skill_for_task_type(task_type: str) -> str | None:
    """Return the running skill for *task_type* (``current_skill``), or ``None``."""
    return SKILL_BY_TASK_TYPE.get(task_type)


def stage_for_task_type(task_type: str) -> str | None:
    """Return the lifecycle stage for *task_type*, or ``None`` if uninstrumented."""
    return STAGE_BY_TASK_TYPE.get(task_type)


# ---------------------------------------------------------------------------
# Actor resolution (config-resolved, never hardcoded — #218)
# ---------------------------------------------------------------------------

ACTOR_BOT = "bot"
ACTOR_CAPO = "capo"
ACTOR_HUMAN = "human"
ACTOR_SYSTEM = "system"


def resolve_actor(kind: str) -> str:
    """Resolve the *config-resolved* actor name for an event-emitting *kind*.

    Works **without a** :class:`~loony_dev.github.Repo` **instance** — the agents'
    ``self.repo`` is a plain ``str``, so resolving the bot identity via
    ``repo.bot_name`` at the turn-boundary site would raise and (best-effort) drop
    every agent heartbeat. The bot name is resolved independently of any ``Repo``:

    * ``bot`` — ``config.settings["bot_name"]`` falling back to the **static**
      :meth:`Repo.detect_bot_name` (the GitHub bot, e.g. ``trixy``).
    * ``capo`` — ``config.settings["capo_name"]`` (default ``"capo"``), the
      orchestrator/supervisor identity; even that is not a literal.
    * ``human`` — operator-injected turns (``session_registry.SOURCE_OPERATOR``).
    * ``system`` — infra/lifecycle events with no human/bot agency.

    An unrecognised *kind* raises :class:`ValueError`: a typo at a call-site is a
    programmer error and must surface (before any event is written) rather than
    silently mis-attributing the event to a default actor.
    """
    from loony_dev import config

    if kind == ACTOR_BOT:
        name = config.settings.get("bot_name")
        if name:
            return str(name)
        return _detect_bot_cached()
    if kind == ACTOR_CAPO:
        return str(config.settings.get("capo_name", "capo"))
    if kind == ACTOR_HUMAN:
        return "human"
    if kind == ACTOR_SYSTEM:
        return "system"
    raise ValueError(
        f"invalid actor kind {kind!r}; expected one of "
        f"{[ACTOR_BOT, ACTOR_CAPO, ACTOR_HUMAN, ACTOR_SYSTEM]}"
    )


# The detection fallback is cached **including failure** for the whole process:
# ``Repo.detect_bot_name`` is ``lru_cache``d on *success* only, so without this a
# gh-auth failure would re-spawn ``gh api user`` on every event. The config path
# above is intentionally *not* cached so a monkeypatched ``bot_name`` is honoured.
_detected_bot: str | None = None
_detected_bot_done = False


def _detect_bot_cached() -> str:
    global _detected_bot, _detected_bot_done
    if not _detected_bot_done:
        try:
            from loony_dev.github import Repo

            _detected_bot = Repo.detect_bot_name()
        except Exception:  # pragma: no cover - detection is best-effort
            logger.debug("Could not detect bot name; using 'bot'", exc_info=True)
            _detected_bot = None
        _detected_bot_done = True
    return _detected_bot or "bot"


# ---------------------------------------------------------------------------
# Data shapes (frozen, closed-vocab-validated at construction)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp — lexicographically sortable (cross-pipeline merge)."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExecutionEvent:
    """One projection-grade event: complete, ordered, typed, actor-stamped.

    ``ts`` is ISO-8601 UTC so a merge of per-pipeline tails is a plain string sort
    (the ADR live-activity-feed requirement). ``type`` / ``state_tone`` are
    validated against their closed vocabularies at construction, so the schema is
    enforced by the writer rather than by convention.
    """

    type: str
    what: str
    actor: str
    target: dict
    state_tone: str = "none"
    ts: str = ""

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"invalid event type {self.type!r}; expected one of {sorted(EVENT_TYPES)}")
        if self.state_tone not in STATE_TONES:
            raise ValueError(
                f"invalid state_tone {self.state_tone!r}; expected one of {sorted(STATE_TONES)}"
            )
        if not isinstance(self.target, dict):
            raise ValueError("event target must be a JSON object (dict)")
        if not self.ts:
            object.__setattr__(self, "ts", _utc_now_iso())

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "actor": self.actor,
            "type": self.type,
            "what": self.what,
            "target": self.target,
            "state_tone": self.state_tone,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ExecutionEvent:
        target = data.get("target")
        return cls(
            type=str(data["type"]),
            what=str(data.get("what", "")),
            actor=str(data.get("actor", "system")),
            target=target if isinstance(target, dict) else {},
            state_tone=str(data.get("state_tone", "none")),
            ts=str(data.get("ts", "")),
        )


@dataclass(frozen=True)
class LiveState:
    """The live-state snapshot — every field the #218 cockpit read-model consumes.

    Keyed on the **pipeline** (``issue-N``), not on a worker: the shared-pool
    worker-identity model is unresolved (#94/#218), so "worker" is simply
    whichever process currently holds the pipeline. ``needs_you`` is **derived**
    (see :func:`derive_needs_you`) — computed in one place, never set by hand.
    """

    pipeline_key: str
    repo: str
    stage: str
    current_skill: str | None
    state: str
    updated_at: str = ""
    last_heartbeat: str = ""
    needs_you: bool = False
    linked_pr: int | None = None
    worktree_path: str | None = None
    live: bool = False
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"invalid stage {self.stage!r}; expected one of {sorted(STAGES)}")
        if self.state not in STATES:
            raise ValueError(f"invalid state {self.state!r}; expected one of {sorted(STATES)}")
        now = _utc_now_iso()
        if not self.updated_at:
            object.__setattr__(self, "updated_at", now)
        if not self.last_heartbeat:
            object.__setattr__(self, "last_heartbeat", self.updated_at)
        # ``needs_you`` is always derived from (state, stage) so it can never drift
        # from them — a hand-passed value is overwritten on purpose.
        object.__setattr__(self, "needs_you", derive_needs_you(self.state, self.stage))

    def to_dict(self) -> dict:
        return {
            "pipeline_key": self.pipeline_key,
            "repo": self.repo,
            "stage": self.stage,
            "current_skill": self.current_skill,
            "updated_at": self.updated_at,
            "last_heartbeat": self.last_heartbeat,
            "needs_you": self.needs_you,
            "linked_pr": self.linked_pr,
            "worktree_path": self.worktree_path,
            "live": self.live,
            "attempt": self.attempt,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LiveState:
        pr = data.get("linked_pr")
        attempt = data.get("attempt", 1)
        # Strict bool only: a malformed ``live`` (e.g. the string "false", which
        # ``bool()`` would read as truthy) must never coerce an idle snapshot into
        # an active one. Anything non-bool defaults to ``False`` so a torn field
        # never inflates :func:`list_active`.
        live = data.get("live", False)
        return cls(
            pipeline_key=str(data["pipeline_key"]),
            repo=str(data.get("repo", "")),
            stage=str(data["stage"]),
            current_skill=_str_or_none(data.get("current_skill")),
            state=str(data["state"]),
            updated_at=str(data.get("updated_at", "")),
            last_heartbeat=str(data.get("last_heartbeat", "")),
            linked_pr=int(pr) if isinstance(pr, int) else None,
            worktree_path=_str_or_none(data.get("worktree_path")),
            live=live if isinstance(live, bool) else False,
            attempt=int(attempt) if isinstance(attempt, int) else 1,
        )


def target_for(repo: str, pipeline_key: str) -> dict:
    """Build an event ``target`` from a pipeline key: ``{repo, issue|pr}``.

    ``issue-N`` → ``{"repo": …, "issue": N}``; ``pr-P`` → ``{"repo": …, "pr": P}``.
    An unrecognised/un-parseable key yields just ``{"repo": …}`` rather than raising.
    """
    target: dict = {"repo": repo}
    try:
        if pipeline_key.startswith("issue-"):
            target["issue"] = int(pipeline_key[len("issue-"):])
        elif pipeline_key.startswith("pr-"):
            target["pr"] = int(pipeline_key[len("pr-"):])
    except ValueError:
        pass
    return target


def derive_needs_you(state: str, stage: str) -> bool:
    """Whether a pipeline needs a human, derived in one place (#218).

    True when the pipeline is **in-error** (``state`` failed/crashed), awaiting a
    human **review/merge** gate (``stage == "In Review"``), or **blocked** on a
    conflict the bot could not clear (``stage == "Conflicts"``).
    """
    return state in ("failed", "crashed") or stage in ("In Review", "Conflicts")


# ---------------------------------------------------------------------------
# Path locators (forward-only, mirroring pipeline_log.py)
# ---------------------------------------------------------------------------


def _slug_path(base_dir: Path, repo: str, pipeline_key: str, suffix: str) -> Path:
    """``.logs/<owner>/<repo>/pipelines/<slug><suffix>`` for *repo* = ``owner/name``."""
    owner, name = _split_repo(repo)
    return (
        pipeline_log.pipeline_logs_dir(Path(base_dir), owner, name)
        / f"{session_registry.task_slug(pipeline_key)}{suffix}"
    )


def events_path(base_dir: Path, repo: str, pipeline_key: str) -> Path:
    """Forward-only locator for a pipeline's append-only event log."""
    return _slug_path(base_dir, repo, pipeline_key, EVENTS_SUFFIX)


def snapshot_path(base_dir: Path, repo: str, pipeline_key: str) -> Path:
    """Forward-only locator for a pipeline's live-state snapshot."""
    return _slug_path(base_dir, repo, pipeline_key, SNAPSHOT_SUFFIX)


def _key_sidecar_path(base_dir: Path, repo: str, pipeline_key: str) -> Path:
    owner, name = _split_repo(repo)
    return pipeline_log.pipeline_key_sidecar_path(Path(base_dir), owner, name, pipeline_key)


# ---------------------------------------------------------------------------
# The storage seam — exactly six public names (ADR decision 3)
# ---------------------------------------------------------------------------


def append_event(base_dir: Path, repo: str, pipeline_key: str, event: ExecutionEvent) -> None:
    """Append *event* as one JSON line to the pipeline's event log.

    Contention-free per ADR (each pipeline writes its own file); a per-path lock
    still guards the rare same-process concurrent append. Writes the ``.key``
    sidecar on first append (``exists()``-guarded, identical to the #220 handler),
    so either module may be the first writer of a pipeline's dir.
    """
    path = events_path(base_dir, repo, pipeline_key)
    line = json.dumps(event.to_dict(), ensure_ascii=False)
    with _path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_sidecar(base_dir, repo, pipeline_key)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()


def write_snapshot(base_dir: Path, repo: str, pipeline_key: str, state: LiveState) -> None:
    """Atomically (re)write the pipeline's live-state snapshot.

    Temp file + :func:`os.replace` (mirrors
    :func:`session_registry.write_session_file`), so a concurrent reader never
    observes a torn/partial file. Also writes the ``.key`` sidecar so a snapshot
    can be the first artifact a pipeline produces.

    Fails fast if *state*'s identity (``repo`` / ``pipeline_key``) does not match
    the storage key it is being written under — that would desync
    :func:`snapshot_path`, :func:`read_snapshot`, and :func:`list_active` from the
    snapshot's own fields.
    """
    if state.repo != repo or state.pipeline_key != pipeline_key:
        raise ValueError(
            f"snapshot identity ({state.repo!r}, {state.pipeline_key!r}) does not "
            f"match storage key ({repo!r}, {pipeline_key!r})"
        )
    path = snapshot_path(base_dir, repo, pipeline_key)
    payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
    with _path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_sidecar(base_dir, repo, pipeline_key)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)


def read_snapshot(base_dir: Path, repo: str, pipeline_key: str) -> LiveState | None:
    """Parse the pipeline's snapshot; ``None`` if missing/malformed (never raises)."""
    path = snapshot_path(base_dir, repo, pipeline_key)
    return _read_snapshot_file(path)


def list_active(base_dir: Path) -> list[LiveState]:
    """Return every **live** snapshot under *base_dir* (``state == running`` or ``live``).

    Scans ``.logs/<owner>/<repo>/pipelines/*.state.json`` exactly as
    :func:`session_registry.iter_sessions` scans sessions — hidden owner dirs and
    unreadable/malformed files are skipped. Forward-only: it never reverses a slug
    back to a key (the key is read from inside the snapshot).
    """
    out: list[LiveState] = []
    logs_dir = Path(base_dir) / ".logs"
    if not logs_dir.is_dir():
        return out
    for owner_dir in _sorted_dirs(logs_dir):
        if owner_dir.name.startswith("."):
            continue
        for repo_dir in _sorted_dirs(owner_dir):
            pipelines_root = repo_dir / pipeline_log.PIPELINES_DIR_NAME
            if not pipelines_root.is_dir():
                continue
            for snap in sorted(pipelines_root.glob(f"*{SNAPSHOT_SUFFIX}")):
                state = _read_snapshot_file(snap)
                if state is not None and (state.state == "running" or state.live):
                    out.append(state)
    return out


def tail_events(base_dir: Path, repo: str, pipeline_key: str, n: int) -> list[ExecutionEvent]:
    """Return the last *n* parsed events (oldest→newest); malformed lines skipped.

    Never raises: a missing log is an empty list, a torn line is dropped.
    """
    if n <= 0:
        return []
    path = events_path(base_dir, repo, pipeline_key)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    # Scan backward collecting *n* **valid** events, so malformed lines near the
    # end never short the count (slicing the last n lines first could return
    # fewer than n parseable events).
    events: list[ExecutionEvent] = []
    for line in reversed(lines):
        event = _parse_event_line(line)
        if event is not None:
            events.append(event)
            if len(events) == n:
                break
    events.reverse()
    return events


def stream_events(
    base_dir: Path,
    repo: str,
    pipeline_key: str,
    *,
    poll_interval: float = 0.25,
) -> Iterator[ExecutionEvent]:
    """Follow a pipeline's event log, yielding each event as it is appended.

    Yields the existing events first, then blocks polling for new complete lines.
    Implemented minimally to honour the pinned storage interface; it has **no
    consumer yet** — inotify-backed dashboard streaming is a #270 reader concern.
    Only whole (newline-terminated) lines are yielded, so a half-written append is
    never parsed. The caller drives termination by ceasing to pull.
    """
    path = events_path(base_dir, repo, pipeline_key)
    pos = 0
    buffer = ""
    while True:
        chunk = ""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except OSError:
            pass  # not created yet — wait for the first append
        if chunk:
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                event = _parse_event_line(line)
                if event is not None:
                    yield event
        else:
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Private heartbeat bump (NOT a seventh seam name)
# ---------------------------------------------------------------------------


def _bump_snapshot(base_dir: Path, repo: str, pipeline_key: str, **fields: object) -> None:
    """Read-modify-write the snapshot, preserving every other field.

    The progress-driven heartbeat: bumps ``last_heartbeat`` (and ``updated_at``)
    to now and applies any *fields* overrides, leaving the rest of the snapshot
    intact. No-op when no snapshot exists yet (nothing to bump). Guarded by the
    per-path lock so the read and the write cannot interleave with a concurrent
    :func:`write_snapshot`.
    """
    path = snapshot_path(base_dir, repo, pipeline_key)
    with _path_lock(path):
        current = _read_snapshot_file(path)
        if current is None:
            return
        now = _utc_now_iso()
        updated = replace(current, last_heartbeat=now, updated_at=now, **fields)  # type: ignore[arg-type]
        payload = json.dumps(updated.to_dict(), indent=2, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

# Per-path locks: append + snapshot RMW for one pipeline serialise on the same
# lock so a heartbeat bump can never interleave with a full snapshot rewrite.
_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


def _path_lock(path: Path) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _locks[path] = lock
        return lock


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, (name or owner)


def _write_sidecar(base_dir: Path, repo: str, pipeline_key: str) -> None:
    sidecar = _key_sidecar_path(base_dir, repo, pipeline_key)
    if not sidecar.exists():
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(pipeline_key, encoding="utf-8")


def _read_snapshot_file(path: Path) -> LiveState | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LiveState.from_dict(data)
    except (KeyError, ValueError):
        return None


def _parse_event_line(line: str) -> ExecutionEvent | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ExecutionEvent.from_dict(data)
    except (KeyError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _sorted_dirs(parent: Path) -> list[Path]:
    try:
        return sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        return []
