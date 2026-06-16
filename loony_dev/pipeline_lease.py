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
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
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


def _is_stale(lease: PipelineLease, *, now: float, stale_after_seconds: float) -> bool:
    """True if *lease* may be reclaimed (dead holder or aged past the backstop)."""
    if not _pid_alive(lease.pid):
        return True
    if lease.started_at is not None and (now - lease.started_at) >= stale_after_seconds:
        return True
    return False


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
) -> bool:
    """Atomically take the pipeline lease for *holder*; ``True`` iff acquired.

    Returns ``False`` when another live holder already owns the pipeline. A stale
    lease (dead holder pid, or aged past *stale_after_seconds*) is reclaimed and
    acquisition retried once.
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
                existing, now=now, stale_after_seconds=stale_after_seconds
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
    base_dir: Path, repo: str, pipeline_key: str, *, holder: str | None = None
) -> bool:
    """Release the pipeline lease; ``True`` iff a lease was removed.

    When *holder* is given, only a lease held by that holder is removed, so a
    late release cannot stomp a lease a different holder has since acquired.
    """
    path = lease_path(base_dir, repo, pipeline_key)
    if holder is not None:
        existing = _parse_lease(path)
        if existing is not None and existing.holder != holder:
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
