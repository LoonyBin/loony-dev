"""Cross-process per-pipeline mutual exclusion (issue #199).

The scheduler dedupes concurrent work on one issue *in memory* via
``Orchestrator._inflight`` / ``_claimed_keys`` keyed on the pipeline identity
(``issue-N``). That is enough while every task runs inside the worker process.
On-demand interrogation breaks that assumption: a human **drive** session is
opened from the *web* process, so an in-memory dedupe in the worker cannot see
it (and vice versa). If both started a ``claude`` on the same ``(session,
worktree)`` they would race one transcript — the exact failure this lease
prevents.

The lease is therefore a small **on-disk** artifact, one file per pipeline under
the per-repo log dir::

    <base>/.logs/<owner>/<repo>/leases/<pipeline-slug>.json
        {holder, pid, pipeline_key, started_at}

Acquisition is atomic (``open(..., O_CREAT | O_EXCL)``): exactly one of two
racing acquirers creates the file; the loser sees ``FileExistsError``. A holder
that crashed without releasing is reclaimed when its ``pid`` is dead or the lease
is older than a generous TTL (mirrors the >12h stuck-item reset philosophy), so a
crash can never wedge a pipeline forever.

**Reclaim liveness ladder (issue #268).** A reclaim decision is a pure function
of three signals — never ``/proc`` CPU/IO sampling (that ``stuck``-forensics
guessing belongs to the ``web/services.list_stuck`` *read* path, not here):

===========  ============================================  ====================
 tier         signal                                        reclaim
===========  ============================================  ====================
 local fast   dead ``pid`` (:func:`_pid_alive`)             instant (crash)
 local new    stale ``last_heartbeat`` in the #267          ``heartbeat_stale_after``
              live-state snapshot                            (~tens of min)
 global       lease age ≥ ``stale_after_seconds``           12h backstop
===========  ============================================  ====================

The middle tier is the new one: a worker whose ``pid`` is alive but whose
``claude -p`` turn is wedged (a hung turn, a deadlock, an agent loop) no longer
sits for the full 12h. It is read from the snapshot the keystone (#267) writes
and is gated to **bot** holders with a ``running`` snapshot (a drive lease and an
``idle``/``failed`` snapshot never carry a progress heartbeat), so it can only
fire on a genuinely wedged automated turn. A reclaimed worker learns it lost the
lease at its next turn boundary via :func:`check_fence` and stands down.
"""
from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loony_dev.session_registry import repo_log_dir, task_slug

logger = logging.getLogger(__name__)

LEASES_DIR_NAME = "leases"

# A holder is the scheduler ("bot") or a human interrogation ("drive"). They are
# symmetric for exclusion: neither may run while the other holds the lease.
HOLDER_BOT = "bot"
HOLDER_DRIVE = "drive"

# Reclaim a lease whose holder process is dead, or — as a backstop for a holder
# on another host / a pid we cannot probe — one older than this. Mirrors the
# 12h stuck-item reset so a crashed holder never wedges the pipeline forever.
DEFAULT_STALE_AFTER_SECONDS = 12 * 60 * 60

# The middle reclaim tier (#268): treat a *bot* holder as stale when its #267
# snapshot heartbeat has not advanced for this long. Default 1h — comfortably
# above a single ``claude -p`` turn cap (``claude_turn_timeout_seconds``, default
# 1800s, *and* a long inter-turn CodeRabbit wait, since the heartbeat now also
# bumps at turn *start*), so a slow-but-real worker is never falsely reclaimed,
# yet far under the 12h global backstop. Correctness requires it stay strictly
# greater than the turn cap; read via :func:`_worker_setting`
# (``[worker] heartbeat_stale_after``, no CLI flag — a reliability tuning knob).
DEFAULT_HEARTBEAT_STALE_AFTER_SECONDS = 60 * 60


class LeaseFencedError(Exception):
    """A worker found its pipeline lease was reclaimed out from under it (#268).

    Raised by :func:`check_fence` at a turn boundary when the on-disk lease is
    gone or now belongs to a different holder than the one recorded in
    :data:`current_lease_token`. The worker must propagate it and **stand down**
    mid-task so a turn that unwedges *after* its lease was reclaimed never
    double-runs against the new holder.
    """


# The fence token of the lease the current task holds: the lease's ``started_at``
# (unique per acquisition), set by the orchestrator in the pool thread before the
# agent runs. ``None`` outside a fenced dispatch (tests / drive / no-worktree
# tasks) — :func:`check_fence` is then a no-op. A ``ContextVar`` so it is
# per-task even though the thread pool reuses worker threads.
current_lease_token: ContextVar[float | None] = ContextVar(
    "loony_current_lease_token", default=None
)


@dataclass(frozen=True)
class PipelineLease:
    """A parsed lease file plus its on-disk path."""

    pipeline_key: str | None
    holder: str | None
    pid: int | None
    started_at: float | None
    path: Path


def leases_dir(base_dir: Path, owner: str, repo: str) -> Path:
    return repo_log_dir(base_dir, owner, repo) / LEASES_DIR_NAME


def lease_path(base_dir: Path, repo: str, pipeline_key: str) -> Path:
    """Return the lease-file path for ``repo``'s *pipeline_key*.

    The filename is a filesystem-safe, collision-resistant slug of the pipeline
    key (the same scheme task sessions use), so an unusual key can never escape
    the leases directory.
    """
    owner, name = repo.split("/", 1)
    return leases_dir(base_dir, owner, name) / f"{task_slug(pipeline_key)}.json"


def _worker_setting(key: str, default: object) -> object:
    """Read *key* from the ``[worker]`` config section (flat fallback).

    Mirrors ``loony_dev.execution_state._worker_setting`` /
    ``loony_dev.agents.coding._worker_setting``: the worker exposes its settings
    both as a nested ``[worker]`` dict and (for registered CLI options) as a
    flattened top-level key, so a robust read checks the section first.
    """
    from loony_dev import config

    worker_cfg = config.settings.get("worker")
    if isinstance(worker_cfg, dict) and key in worker_cfg:
        return worker_cfg[key]
    return config.settings.get(key, default)


def heartbeat_stale_after_seconds() -> float:
    """The configured middle-tier (#268) heartbeat-staleness window, in seconds.

    Fails loudly on bad config rather than silently falling back to the default:
    a non-numeric value, one that is zero/negative (which would make *every* bot
    lease instantly reclaimable), or one at/below the ``claude -p`` turn cap
    (``claude_turn_timeout_seconds``) — which would let the middle tier reclaim a
    healthy long-running turn before it reaches its next heartbeat boundary — is
    operator error and must surface, not be masked into a default that hides the
    misconfiguration. The strict ``> turn cap`` lower bound is the correctness
    invariant documented on :data:`DEFAULT_HEARTBEAT_STALE_AFTER_SECONDS`.
    """
    raw = _worker_setting("heartbeat_stale_after", DEFAULT_HEARTBEAT_STALE_AFTER_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"worker.heartbeat_stale_after must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError(
            f"worker.heartbeat_stale_after must be positive, got {value!r}"
        )
    turn_cap_raw = _worker_setting("claude_turn_timeout_seconds", 30 * 60)
    try:
        turn_cap = float(turn_cap_raw)
    except (TypeError, ValueError):
        turn_cap = float(30 * 60)
    if value <= turn_cap:
        raise ValueError(
            "worker.heartbeat_stale_after must be greater than "
            f"worker.claude_turn_timeout_seconds ({turn_cap!r}), got {value!r}"
        )
    return value


def _pid_alive(pid: int | None) -> bool:
    """True if *pid* names a live process (or one we lack permission to signal)."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by another user
    except (ProcessLookupError, OSError):
        return False
    return True


def _parse_lease(path: Path) -> PipelineLease | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    started_at = data.get("started_at")
    return PipelineLease(
        pipeline_key=data.get("pipeline_key"),
        holder=data.get("holder"),
        pid=int(pid) if isinstance(pid, int) else None,
        started_at=float(started_at) if isinstance(started_at, (int, float)) else None,
        path=path,
    )


def _heartbeat_stale(
    lease: PipelineLease,
    base_dir: Path,
    repo: str,
    now: float,
    heartbeat_stale_after_seconds: float,
) -> bool:
    """True if *lease*'s #267 snapshot heartbeat has not advanced for too long.

    The middle reclaim tier. Reads the live-state snapshot the keystone (#267)
    writes; returns ``False`` (not stale) unless the snapshot exists, is
    ``running``, and carries a parseable ``last_heartbeat`` older than
    *heartbeat_stale_after_seconds*. A malformed/absent timestamp is treated as
    *not* stale on purpose — the 12h backstop is the net for those, so a torn
    snapshot read can never trigger a spurious reclaim. ``execution_state`` is
    imported lazily (no import cycle: it never imports this module).

    The staleness baseline is **clamped to the lease's own ``started_at``**: a
    ``running`` snapshot left over from a *previous* holder can carry a heartbeat
    older than this lease's acquisition, which would otherwise make a brand-new
    lease look instantly stale. A heartbeat cannot logically precede the lease
    meant to be producing it, so the later of (heartbeat, lease start) is used —
    giving every fresh lease the full window before it can be reclaimed.
    """
    if not lease.pipeline_key:
        return False
    from loony_dev import execution_state

    snapshot = execution_state.read_snapshot(base_dir, repo, lease.pipeline_key)
    if snapshot is None or snapshot.state != "running" or not snapshot.last_heartbeat:
        return False
    try:
        ts = datetime.fromisoformat(snapshot.last_heartbeat)
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    heartbeat_at = ts.timestamp()
    if lease.started_at is not None:
        heartbeat_at = max(heartbeat_at, lease.started_at)
    return (now - heartbeat_at) >= heartbeat_stale_after_seconds


def _is_stale(
    lease: PipelineLease,
    *,
    now: float,
    stale_after_seconds: float,
    base_dir: Path | None = None,
    repo: str | None = None,
    heartbeat_stale_after_seconds: float | None = None,
) -> bool:
    """True if *lease* may be reclaimed — the three-tier liveness ladder (#268).

    Tiers, cheapest first: a **dead holder pid** (instant crash recovery), a
    **stale snapshot heartbeat** (the wedged-but-alive middle tier, only when
    *base_dir*/*repo*/*heartbeat_stale_after_seconds* are supplied and the holder
    is a ``bot`` — a drive lease carries no progress heartbeat), and the lease
    **aged past** *stale_after_seconds* (the 12h cross-host backstop). The
    decision never CPU-samples ``/proc``; it reads pid-liveness, the #267
    snapshot, and lease age only.
    """
    if not _pid_alive(lease.pid):
        return True
    if lease.started_at is not None and (now - lease.started_at) >= stale_after_seconds:
        return True
    if (
        base_dir is not None
        and repo
        and heartbeat_stale_after_seconds is not None
        and lease.holder == HOLDER_BOT
        and lease.pipeline_key
    ):
        if _heartbeat_stale(lease, base_dir, repo, now, heartbeat_stale_after_seconds):
            return True
    return False


def check_fence(base_dir: Path, repo: str, pipeline_key: str) -> None:
    """Raise :class:`LeaseFencedError` if our pipeline lease was reclaimed (#268).

    The fence token is ``(pid, lease.started_at)``: ``pid`` alone is insufficient
    (a restarted/other worker may reuse a pid), so it is paired with the lease's
    ``started_at``, which is unique per acquisition. A no-op when no token is set
    (:data:`current_lease_token` is ``None`` — untracked/test/drive path). The
    lease read is defensive (:func:`read_pipeline_lease` never raises), so the
    only exception this can raise is :class:`LeaseFencedError`.
    """
    token = current_lease_token.get()
    if token is None:
        return
    lease = read_pipeline_lease(base_dir, repo, pipeline_key)
    if lease is None or lease.pid != os.getpid() or lease.started_at != token:
        raise LeaseFencedError(
            f"pipeline lease for {pipeline_key!r} was reclaimed "
            f"(ours: pid={os.getpid()} started_at={token}; "
            f"now: {None if lease is None else (lease.pid, lease.started_at)})"
        )


def read_pipeline_lease(base_dir: Path, repo: str, pipeline_key: str) -> PipelineLease | None:
    """Return the current lease for *pipeline_key*, or ``None`` if unheld/malformed."""
    return _parse_lease(lease_path(base_dir, repo, pipeline_key))


def acquire_pipeline_lease(
    base_dir: Path,
    repo: str,
    pipeline_key: str,
    *,
    holder: str,
    pid: int | None = None,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_stale_after_seconds: float | None = None,
) -> bool:
    """Atomically take the pipeline lease for *holder*; ``True`` iff acquired.

    Returns ``False`` when another live holder already owns the pipeline. A stale
    lease is reclaimed and acquisition retried once — stale meaning a dead holder
    pid, a lease aged past *stale_after_seconds*, or (when
    *heartbeat_stale_after_seconds* is given and the holder is a ``bot``) a #267
    snapshot heartbeat that has not advanced for that long (the wedged-but-alive
    middle tier, issue #268).
    """
    import time

    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    path = lease_path(base_dir, repo, pipeline_key)
    payload = json.dumps(
        {"holder": holder, "pid": pid, "pipeline_key": pipeline_key, "started_at": now}
    ).encode("utf-8")

    for attempt in (0, 1):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = _parse_lease(path)
            if existing is None or _is_stale(
                existing,
                now=now,
                stale_after_seconds=stale_after_seconds,
                base_dir=base_dir,
                repo=repo,
                heartbeat_stale_after_seconds=heartbeat_stale_after_seconds,
            ):
                # Reclaim a crashed/aged holder, then retry the exclusive create.
                if attempt == 0:
                    logger.info(
                        "Reclaiming stale pipeline lease %s (holder=%s pid=%s)",
                        pipeline_key,
                        existing.holder if existing else None,
                        existing.pid if existing else None,
                    )
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
            return False
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    return False


def release_pipeline_lease(
    base_dir: Path,
    repo: str,
    pipeline_key: str,
    *,
    holder: str | None = None,
    expected_started_at: float | None = None,
) -> bool:
    """Release the pipeline lease; ``True`` iff a lease was removed.

    When *holder* is given, only a lease held by that holder is removed, so a
    late release cannot stomp a lease a different holder has since acquired.

    When *expected_started_at* is given, only the lease from *that* acquisition
    (matched on its ``started_at`` fence token) is removed. The holder check
    alone is insufficient on the bot terminal-finish path: a reclaim that lands
    after the last :func:`check_fence` but before the release would install a new
    *bot* lease, which the ``holder == HOLDER_BOT`` check still matches — so a
    holder-only release would unlink the new holder's lease (#268). Pairing the
    token (unique per acquisition) closes that window.
    """
    path = lease_path(base_dir, repo, pipeline_key)
    if holder is not None or expected_started_at is not None:
        existing = _parse_lease(path)
        if existing is not None:
            if holder is not None and existing.holder != holder:
                return False
            if expected_started_at is not None and existing.started_at != expected_started_at:
                return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def active_drive_pipeline_keys(
    base_dir: Path,
    repo: str,
    *,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> set[str]:
    """Return pipeline keys currently held by a live **drive** lease for *repo*.

    The scheduler unions these into ``_claimed_keys`` so it never dispatches an
    automated task onto a pipeline a human is driving. Stale drive leases are
    ignored (a crashed drive must not block the bot forever).
    """
    import time

    now = time.time() if now is None else now
    owner, name = repo.split("/", 1)
    ldir = leases_dir(base_dir, owner, name)
    keys: set[str] = set()
    try:
        entries = sorted(ldir.iterdir())
    except OSError:
        return keys
    for entry in entries:
        if entry.suffix != ".json":
            continue
        lease = _parse_lease(entry)
        if lease is None or lease.holder != HOLDER_DRIVE or not lease.pipeline_key:
            continue
        if _is_stale(lease, now=now, stale_after_seconds=stale_after_seconds):
            continue
        keys.add(lease.pipeline_key)
    return keys
