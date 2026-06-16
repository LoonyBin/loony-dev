"""On-disk contract for per-task worker session attach + steer (issue #164).

A worker that drives a task's Claude over a :class:`~loony_dev.agents.claude_session.ClaudeSession`
(issue #161) publishes a small registry so the *separate* web-dashboard process
can (a) bridge a websocket to the session's PTY via a Unix-domain socket and
(b) enqueue operator-injected turns. Like the remote-control connection file in
:mod:`loony_dev.supervisor`, this module is the single source of truth for the
layout; both the worker-side :class:`~loony_dev.agents.session_bridge.SessionBridge`
and the web :mod:`loony_dev.web.services` layer read/write through these helpers.

Layout, under each per-repo log dir ``<base>/.logs/<owner>/<repo>/``::

    sessions/<task-slug>/
        session.json     # {task_key, repo, session_id, socket, pid, started_at, status}
        attach.sock      # PTY-bridge Unix-domain socket (created by SessionBridge)
        injections/      # queue of operator-injected turns (one JSON file each)

``<task-slug>`` is a filesystem-safe, collision-resistant derivation of the
task key (which may contain ``/`` or other characters); the canonical key is
stored verbatim inside ``session.json`` and is what lookups match on.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR_NAME = "sessions"
SESSION_FILE_NAME = "session.json"
SOCKET_NAME = "attach.sock"
INJECTIONS_DIR_NAME = "injections"

# Provenance tag on operator-injected turns so logs/orchestrator can tell a
# human-steered turn from a bot-driven one (issue #164).
SOURCE_OPERATOR = "operator"


@dataclass(frozen=True)
class TaskSession:
    """A discovered per-task session (parsed ``session.json`` + its directory)."""

    task_key: str
    repo: str | None
    session_id: str | None
    socket: str | None
    pid: int | None
    started_at: str | None
    status: str | None
    dir: Path
    # On-demand interrogation plumbing (issue #199). ``worktree_path`` is the
    # exact cwd the session last wrote its transcript to — resuming a session id
    # in any *other* cwd makes the JSONL invisible (the #177 cross-worktree bug).
    # ``pipeline_key`` is the per-issue mutual-exclusion identity (``issue-N``),
    # and ``branch`` is the feature branch the worktree is checked out on, so a
    # torn-down worktree can be recreated at the canonical path before resuming.
    worktree_path: str | None = None
    pipeline_key: str | None = None
    branch: str | None = None


def task_slug(task_key: str) -> str:
    """Return a filesystem-safe, collision-resistant slug for *task_key*.

    Non-alphanumeric runs collapse to ``-`` (readable); a short digest of the
    original key is appended so distinct keys that sanitise alike (e.g.
    ``a/b`` vs ``a-b``) never share a directory.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "-", task_key).strip("-") or "task"
    digest = hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:10]
    return f"{safe}-{digest}"


def repo_log_dir(base_dir: Path, owner: str, repo: str) -> Path:
    return Path(base_dir) / ".logs" / owner / repo


def session_dir(base_dir: Path, owner: str, repo: str, task_key: str) -> Path:
    """Return the per-task session directory for ``owner/repo`` and *task_key*."""
    return repo_log_dir(base_dir, owner, repo) / SESSIONS_DIR_NAME / task_slug(task_key)


def socket_path(sess_dir: Path) -> Path:
    return Path(sess_dir) / SOCKET_NAME


def injections_dir(sess_dir: Path) -> Path:
    return Path(sess_dir) / INJECTIONS_DIR_NAME


def write_session_file(
    sess_dir: Path,
    *,
    task_key: str,
    repo: str,
    session_id: str,
    pid: int | None,
    started_at: str,
    status: str = "running",
    socket: str | None = None,
    worktree_path: str | None = None,
    pipeline_key: str | None = None,
    branch: str | None = None,
) -> Path:
    """Atomically write the canonical ``session.json`` for a task session.

    Writes to a temp file and renames so a reader never observes a partial file.
    *socket* defaults to the conventional ``attach.sock`` inside *sess_dir*.
    *worktree_path* / *pipeline_key* / *branch* carry the on-demand-interrogation
    plumbing (issue #199); they are omitted from the payload when ``None`` so an
    older reader (or the bridge path that does not set them) round-trips cleanly.
    """
    sess_dir = Path(sess_dir)
    sess_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_key": task_key,
        "repo": repo,
        "session_id": session_id,
        "socket": socket if socket is not None else str(socket_path(sess_dir)),
        "pid": pid,
        "status": status,
        "started_at": started_at,
    }
    if worktree_path is not None:
        payload["worktree_path"] = str(worktree_path)
    if pipeline_key is not None:
        payload["pipeline_key"] = pipeline_key
    if branch is not None:
        payload["branch"] = branch
    path = sess_dir / SESSION_FILE_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)
    return path


def read_session(sess_dir: Path) -> TaskSession | None:
    """Parse ``session.json`` in *sess_dir*; ``None`` if missing/malformed.

    Parsing is defensive: a missing ``task_key`` falls back to the directory
    name so a malformed entry is skipped rather than crashing discovery.
    """
    sess_dir = Path(sess_dir)
    try:
        data = json.loads((sess_dir / SESSION_FILE_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    task_key = data.get("task_key")
    if not task_key:
        return None
    pid = data.get("pid")
    return TaskSession(
        task_key=str(task_key),
        repo=_str_or_none(data.get("repo")),
        session_id=_str_or_none(data.get("session_id")),
        socket=_str_or_none(data.get("socket")),
        pid=int(pid) if isinstance(pid, int) else None,
        started_at=_str_or_none(data.get("started_at")),
        status=_str_or_none(data.get("status")),
        dir=sess_dir,
        worktree_path=_str_or_none(data.get("worktree_path")),
        pipeline_key=_str_or_none(data.get("pipeline_key")),
        branch=_str_or_none(data.get("branch")),
    )


def iter_sessions(base_dir: Path) -> Iterator[TaskSession]:
    """Yield every valid per-task session discovered under *base_dir*.

    Scans ``.logs/<owner>/<repo>/sessions/<slug>/session.json``. Hidden owner
    dirs and unreadable/malformed entries are skipped.
    """
    logs_dir = Path(base_dir) / ".logs"
    if not logs_dir.is_dir():
        return
    for owner_dir in _sorted_dirs(logs_dir):
        if owner_dir.name.startswith("."):
            continue
        for repo_dir in _sorted_dirs(owner_dir):
            sessions_root = repo_dir / SESSIONS_DIR_NAME
            if not sessions_root.is_dir():
                continue
            for sess_dir in _sorted_dirs(sessions_root):
                session = read_session(sess_dir)
                if session is not None:
                    yield session


def find_session(base_dir: Path, task_key: str) -> TaskSession | None:
    """Return the discovered session whose canonical ``task_key`` matches.

    Matches the *stored* key (never builds a path from the raw argument), so an
    attacker-controlled ``task_key`` URL segment cannot traverse the filesystem.
    """
    for session in iter_sessions(base_dir):
        if session.task_key == task_key:
            return session
    return None


def find_pipeline_session(base_dir: Path, repo: str, pipeline_key: str) -> TaskSession | None:
    """Return the recorded session for ``repo``'s *pipeline_key*, or ``None``.

    On-demand interrogation (issue #199) addresses a *parked* pipeline by its
    per-issue key (``issue-N``), not a task key. Like :func:`find_session` this
    matches the value *stored* in ``session.json`` (never builds a path from the
    raw argument), so a caller may pass a URL segment straight through without
    risking path traversal. The repo is matched too so two repos that happen to
    share a pipeline key never cross over.
    """
    for session in iter_sessions(base_dir):
        if session.pipeline_key == pipeline_key and session.repo == repo:
            return session
    return None


def record_session_worktree(
    base_dir: Path,
    repo: str,
    *,
    pipeline_key: str,
    task_key: str,
    session_id: str,
    worktree_path: str | os.PathLike,
    branch: str | None = None,
    status: str = "parked",
) -> Path:
    """Record the ``(session_id → worktree_path)`` mapping for a pipeline.

    This is the durable map :mod:`loony_dev.agents.session_resume` reads to resume
    a parked pipeline into a fresh PTY. Unlike :func:`write_session_file` it does
    not assume a live :class:`~loony_dev.agents.session_bridge.SessionBridge`: real
    turns run via ``claude -p`` (not the persistent PTY), so the worktree path is
    recorded at the point a turn is about to write the transcript, independent of
    the bridge. An existing live entry's ``socket``/``pid``/``status`` is preserved
    so this never downgrades a session that a bridge is actively serving.
    """
    owner, name = repo.split("/", 1)
    sess_dir = session_dir(base_dir, owner, name, task_key)
    existing = read_session(sess_dir)
    return write_session_file(
        sess_dir,
        task_key=task_key,
        repo=repo,
        session_id=session_id,
        pid=existing.pid if existing is not None else None,
        started_at=(
            existing.started_at
            if existing is not None and existing.started_at
            else datetime.now(timezone.utc).isoformat()
        ),
        status=existing.status if existing is not None and existing.status else status,
        socket=existing.socket if existing is not None else None,
        worktree_path=str(worktree_path),
        pipeline_key=pipeline_key,
        branch=branch if branch is not None else (existing.branch if existing else None),
    )


def remove_session_dir(sess_dir: Path) -> None:
    """Best-effort teardown of a task session directory (socket, queue, file)."""
    import shutil

    try:
        shutil.rmtree(sess_dir)
    except OSError:
        pass


def enqueue_injection(
    sess_dir: Path,
    prompt: str,
    *,
    source: str = SOURCE_OPERATOR,
) -> Path:
    """Append an operator-injected turn to the session's queue.

    Each turn is its own JSON file named ``<nanos>-<uuid>.json`` so the
    orchestrator can drain them in arrival order. Provenance is tagged with
    *source* (``"operator"`` by default) so a human-steered turn is
    distinguishable from a bot-driven one.
    """
    qdir = injections_dir(sess_dir)
    qdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "prompt": prompt,
        "source": source,
        "enqueued_at": time.time(),
    }
    name = f"{time.time_ns()}-{uuid.uuid4().hex}.json"
    path = qdir / name
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)
    return path


def drain_injections(sess_dir: Path) -> list[dict]:
    """Return queued injected turns (arrival order) and remove them.

    Intended for the orchestrator tick: it pops every pending injection and runs
    them as the session's next turns. Malformed files are discarded.
    """
    qdir = injections_dir(sess_dir)
    if not qdir.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(qdir.iterdir()):
        if entry.suffix != ".json":
            continue
        # Claim the file with an atomic rename before reading: if two drainers
        # race, only one rename succeeds and the loser skips the file rather than
        # processing it twice.
        claimed = entry.with_suffix(entry.suffix + ".processing")
        try:
            entry.rename(claimed)
        except OSError:
            continue  # already claimed (or vanished) — another consumer has it
        try:
            data = json.loads(claimed.read_text())
        except (OSError, ValueError):
            data = None
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
        if isinstance(data, dict):
            out.append(data)
    return out


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _sorted_dirs(parent: Path) -> list[Path]:
    try:
        return sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        return []
