"""Framework-agnostic data layer for the web dashboard.

These functions derive all state from the supervisor's on-disk file layout
under ``<base>/.logs/...`` (and repo checkouts under ``<base>/<owner>/<repo>``).
The web process runs separately from the supervisor and never shares any
in-memory state with it, so everything here is reconstructed from the
filesystem.

No FastAPI imports live in this module — the route layer is a thin wrapper
around these pure functions, which keeps them directly unit-testable.
"""

from __future__ import annotations

import os
import signal
import socket
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loony_dev.git import GitRepo

WORKER_LOG_NAME = "loony-worker.log"
WORKER_PID_NAME = "loony-worker.pid"

# Per-repo remote-control "connection file". The canonical schema and writer live
# in ``loony_dev.supervisor`` (see ``_write_connection_file``); this reader only
# consumes the first three keys ``{session_id, repo, key}``. Parsing is defensive:
# unknown keys are ignored and malformed/missing files are skipped.
REMOTE_CONTROL_CONN_NAME = "remote-control.json"


# ---------------------------------------------------------------------------
# Data views (plain dataclasses, JSON-serialisable via asdict)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerView:
    repo: str
    pid: int | None
    status: str  # "running" | "stale" | "unknown"
    started_at: str | None  # ISO-8601, approx (PID-file mtime)
    exitcode: None  # always null in v1 — unobservable cross-process
    log_path: str


@dataclass(frozen=True)
class WorktreeView:
    repo: str
    path: str
    branch: str | None
    head: str | None
    detached: bool
    bare: bool


@dataclass(frozen=True)
class SessionView:
    session_id: str
    repo: str | None
    key: str | None
    control_socket: str | None = None


@dataclass(frozen=True)
class StuckProcessView:
    worker_repo: str
    task_key: str | None
    pid: int
    cmdline: str
    age_seconds: int
    blocked_on: str
    # Globally-unique id of the ClaudeSession that owns this descendant, used to
    # address the ESC-interrupt endpoint (``None`` when no session is advertised
    # for the worker's repo).
    session_id: str | None = None


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------

def process_status(pid: int) -> str:
    """Map a PID to a worker status using the 3-state ``os.kill(pid, 0)`` probe.

    Returns:
        "running" — the process exists and we can signal it.
        "unknown" — the process exists but is owned by another user
                    (``PermissionError``).
        "stale"   — no such process (``ProcessLookupError``).
    """
    try:
        os.kill(pid, 0)
        return "running"
    except PermissionError:
        return "unknown"
    except (ProcessLookupError, OSError):
        return "stale"


def _read_pid(pid_path: Path) -> int | None:
    """Read a bare integer PID from *pid_path*; return None if unreadable."""
    try:
        pid = int(pid_path.read_text().strip())
        return pid if pid > 0 else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _iso_mtime(path: Path) -> str | None:
    """Return *path*'s mtime as a UTC ISO-8601 string, or None if unavailable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_repos(base_dir: Path) -> list[tuple[str, str, Path]]:
    """Scan ``.logs/<owner>/<repo>/`` and return ``(owner, name, repo_log_dir)``.

    Hidden directories (those whose name starts with ``.``) are skipped.
    """
    logs_dir = base_dir / ".logs"
    found: list[tuple[str, str, Path]] = []
    if not logs_dir.exists():
        return found
    for owner_dir in sorted(logs_dir.iterdir()):
        if not owner_dir.is_dir() or owner_dir.name.startswith("."):
            continue
        for repo_dir in sorted(owner_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            found.append((owner_dir.name, repo_dir.name, repo_dir))
    return found


def list_workers(base_dir: Path) -> list[WorkerView]:
    """Return one :class:`WorkerView` per discovered worker log directory.

    ``repo`` is derived from the PID-file path. ``started_at`` is the PID file's
    mtime (written once at worker launch) used as an approximate launch time.
    ``exitcode`` is always ``None`` — a separate process cannot observe the exit
    code of the supervisor's ``multiprocessing.Process`` workers; the knowable
    information is conveyed via ``status`` instead.
    """
    workers: list[WorkerView] = []
    for owner, name, repo_dir in _discover_repos(base_dir):
        pid_path = repo_dir / WORKER_PID_NAME
        log_path = repo_dir / WORKER_LOG_NAME
        pid = _read_pid(pid_path)
        if pid is None:
            status = "stale"
            started_at = None
        else:
            status = process_status(pid)
            started_at = _iso_mtime(pid_path)
        workers.append(
            WorkerView(
                repo=f"{owner}/{name}",
                pid=pid,
                status=status,
                started_at=started_at,
                exitcode=None,
                log_path=str(log_path),
            )
        )
    return workers


def list_worktrees(base_dir: Path) -> list[WorktreeView]:
    """Flatten ``git worktree list`` across every checked-out repo.

    A repo is checked out at ``<base>/<owner>/<repo>`` (see
    ``supervisor.ensure_repo_checked_out``). For each repo discovered from the
    ``.logs`` scan that has a git checkout, list its worktrees and tag each with
    the owning ``repo``. Repos without a checkout (or that error) are skipped.
    """
    worktrees: list[WorktreeView] = []
    for owner, name, _repo_dir in _discover_repos(base_dir):
        checkout = base_dir / owner / name
        if not (checkout / ".git").exists():
            continue
        try:
            infos = GitRepo(work_dir=checkout).list_worktrees()
        except Exception:
            continue
        for info in infos:
            worktrees.append(
                WorktreeView(
                    repo=f"{owner}/{name}",
                    path=str(info.path),
                    branch=info.branch,
                    head=info.head,
                    detached=info.detached,
                    bare=info.bare,
                )
            )
    return worktrees


def list_sessions(base_dir: Path) -> list[SessionView]:
    """Read each repo's ``remote-control.json`` connection file.

    Discovers repos via the same ``.logs/<owner>/<repo>/`` scan as the workers
    and reads ``<repo_log_dir>/remote-control.json`` (written by the supervisor;
    canonical schema in :mod:`loony_dev.supervisor`). Parsing is defensive:
    malformed/missing files are skipped, unknown keys are ignored, and
    ``session_id`` falls back to ``owner/repo`` when absent.
    """
    import json

    sessions: list[SessionView] = []
    for owner, name, repo_dir in _discover_repos(base_dir):
        conn_path = repo_dir / REMOTE_CONTROL_CONN_NAME
        try:
            data = json.loads(conn_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        repo = data.get("repo") or f"{owner}/{name}"
        session_id = data.get("session_id") or repo
        key = data.get("key")
        control_socket = data.get("control_socket")
        sessions.append(
            SessionView(
                session_id=str(session_id),
                repo=str(repo) if repo is not None else None,
                key=str(key) if key is not None else None,
                control_socket=str(control_socket) if control_socket else None,
            )
        )
    return sessions


# ---------------------------------------------------------------------------
# Session interrupt (issue #163)
#
# ESC is the *primary* intervention for a wedged Claude turn: it aborts the
# in-flight turn but leaves the persistent session alive and steerable, unlike
# the SIGTERM/SIGKILL path. The web process does not own the session's PTY, so
# it reaches it over a Unix-domain control socket the session advertises in its
# connection file (``control_socket``). Sessions are addressed by their
# globally-unique ``session_id`` rather than a bare task key, which is ambiguous
# across repos.
# ---------------------------------------------------------------------------

# Seconds to wait on a control-socket round trip before giving up.
SESSION_CONTROL_TIMEOUT = 5.0


class SessionNotFoundError(Exception):
    """Raised when no advertised session matches a requested ``session_id``."""


class SessionControlError(Exception):
    """Raised when a session's control channel is missing or unreachable."""


def _find_session(base_dir: Path, session_id: str) -> SessionView:
    """Return the advertised session whose id is *session_id*.

    Looking the session up among the on-disk connection files is the security
    gate: only a socket the supervisor itself advertised can be addressed, never
    an arbitrary path supplied by the caller.
    """
    for session in list_sessions(base_dir):
        if session.session_id == session_id:
            return session
    raise SessionNotFoundError(f"no session with id {session_id!r}")


def _send_control_command(socket_path: Path, command: str, *, timeout: float) -> str:
    """Send a one-line *command* to a session control socket and return its reply."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        sock.sendall(command.encode("utf-8") + b"\n")
        chunks: list[bytes] = []
        while True:
            data = sock.recv(256)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    except OSError as exc:
        raise SessionControlError(
            f"control socket {socket_path} unreachable: {exc}"
        ) from exc
    finally:
        sock.close()
    return b"".join(chunks).decode("utf-8", "replace").strip()


def interrupt_session(
    base_dir: Path, session_id: str, *, timeout: float = SESSION_CONTROL_TIMEOUT
) -> dict:
    """Send an ESC interrupt to the ClaudeSession identified by *session_id*.

    Resolves the session from the on-disk connection files, connects to its
    control socket, and asks it to interrupt the in-flight turn. The session
    process survives; only the current turn aborts (its ``on_failure`` runs as
    usual). Returns ``{session_id, repo, interrupted, detail}``.

    Raises :class:`SessionNotFoundError` when no session matches, or
    :class:`SessionControlError` when the session advertises no control channel
    or the socket cannot be reached.
    """
    session = _find_session(base_dir, session_id)
    if not session.control_socket:
        raise SessionControlError(f"session {session_id!r} has no control channel")
    reply = _send_control_command(
        Path(session.control_socket), "interrupt", timeout=timeout
    )
    if reply not in {"interrupted", "idle"}:
        # A stale/mismatched control server (e.g. "error: ..." or an empty
        # reply) is a control failure, not a successful idle interrupt.
        raise SessionControlError(
            f"control socket {session.control_socket} returned invalid reply: {reply!r}"
        )
    return {
        "session_id": session_id,
        "repo": session.repo,
        "interrupted": reply == "interrupted",
        "detail": reply,
    }


def auto_interrupt_candidates(
    stuck: list[StuckProcessView], *, auto_interrupt_after_seconds: float
) -> list[str]:
    """Return the distinct session ids eligible for automatic ESC interrupt.

    A session qualifies when one of its stuck descendants has been wedged for at
    least *auto_interrupt_after_seconds*. Returns an empty list when the feature
    is disabled (``auto_interrupt_after_seconds <= 0``), so auto-intervention is
    strictly opt-in; SIGKILL is never auto-escalated.
    """
    if auto_interrupt_after_seconds <= 0:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for view in stuck:
        sid = view.session_id
        if not sid or sid in seen:
            continue
        if view.age_seconds >= auto_interrupt_after_seconds:
            seen.add(sid)
            out.append(sid)
    return out


# ---------------------------------------------------------------------------
# Log tail
# ---------------------------------------------------------------------------

class LogNotFoundError(Exception):
    """Raised when a requested worker log path is invalid or does not exist."""


def _safe_repo_log_path(base_dir: Path, owner: str, repo: str) -> Path:
    """Resolve the worker log path for ``owner/repo``, rejecting traversal.

    Rejects any ``owner``/``repo`` segment containing path separators or ``..``
    and confirms the resolved log path stays within ``<base>/.logs``.
    """
    for segment in (owner, repo):
        if not segment or segment in (".", "..") or "/" in segment or "\\" in segment or "\x00" in segment:
            raise LogNotFoundError(f"invalid path segment: {segment!r}")

    logs_root = (base_dir / ".logs").resolve()
    candidate = (logs_root / owner / repo / WORKER_LOG_NAME).resolve()
    if logs_root not in candidate.parents:
        raise LogNotFoundError("resolved path escapes logs directory")
    return candidate


def tail_log(base_dir: Path, owner: str, repo: str, lines: int) -> list[str]:
    """Return up to the last *lines* lines of ``owner/repo``'s worker log.

    Raises :class:`LogNotFoundError` for invalid segments or a missing log file.
    Reads the whole file but keeps only the tail in a bounded ``deque`` so memory
    stays proportional to *lines* rather than file size.
    """
    log_path = _safe_repo_log_path(base_dir, owner, repo)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            tail: deque[str] = deque(fh, maxlen=max(lines, 0))
    except FileNotFoundError as exc:
        raise LogNotFoundError(f"no log for {owner}/{repo}") from exc
    return [line.rstrip("\n") for line in tail]


# ---------------------------------------------------------------------------
# Stuck-process detection (issue #132)
#
# A worker can wedge when Claude (a descendant of the worker PID) blocks
# indefinitely — e.g. a runaway ``sleep 99999`` keeps the stdout pipe open so
# ``communicate()`` never returns. We detect this by walking ``/proc`` down from
# each running worker PID, flagging descendants that have been parked in a
# non-returning syscall for a while AND whose Claude subtree is burning no CPU/IO.
#
# Worker-log mtime is deliberately NOT used as the activity signal: Claude's
# output is drained into the *agent* process's pipe buffer by ``communicate()``,
# not the worker log, so the log can sit idle during a legitimate long Claude
# call. The activity signal is sourced from /proc CPU/IO counters instead.
#
# The OS-introspection helpers below are thin and side-effect-free so tests can
# monkeypatch ``_proc_snapshot`` / ``_descendants`` / ``_subtree_activity`` with
# synthetic process trees, with no dependency on a real ``/proc``.
# ---------------------------------------------------------------------------

PROC_ROOT = Path("/proc")

# wchan substrings indicating a thread parked in a non-returning kernel wait.
# ``*nanosleep*`` covers ``sleep``; ``pipe_read``/``pipe_wait`` cover a blocked
# read on Claude's stdout/stderr pipe.
_BLOCKING_WCHAN_TOKENS = ("nanosleep", "pipe_read", "pipe_wait")

# A process is a candidate only if quiescently blocked (sleeping/uninterruptible),
# never if it is actively running (``R``) or a zombie (``Z``).
_QUIESCENT_STATES = frozenset({"S", "D"})


def _sysconf_clk_tck() -> int:
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        return ticks if ticks and ticks > 0 else 100
    except (ValueError, OSError, AttributeError):
        return 100


def _read_btime() -> float | None:
    """Return system boot time (epoch seconds) from ``/proc/stat``'s ``btime``."""
    try:
        with open(PROC_ROOT / "stat", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


# Cached at import time — boot time and the clock tick rate do not change.
_CLK_TCK = _sysconf_clk_tck()
_BTIME = _read_btime()

# Reading ``/proc/<pid>/io`` (and any /proc file) inflates the *reading*
# process's own rchar/CPU counters. The dashboard runs as a separate process
# from every worker, so it is never legitimately part of a worker's subtree;
# excluding our own PID from activity aggregation keeps the act of measuring
# from perturbing the measurement.
_SELF_PID = os.getpid()


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    state: str
    starttime: int  # clock ticks since boot (stat field 22)
    cpu_ticks: int  # utime + stime (stat fields 14 + 15)
    cmdline: list[str]
    cmdline_str: str
    wchan: str
    io_bytes: int | None  # rchar + wchar, or None if unreadable


@dataclass(frozen=True)
class ActivitySample:
    cpu_ticks: int
    io_bytes: int
    io_available: bool
    timestamp: float


def _proc_snapshot(pid: int) -> ProcInfo | None:
    """Read a point-in-time snapshot of ``/proc/<pid>``; ``None`` if it vanished.

    Robust to the process disappearing mid-read (returns ``None`` on any
    ``OSError``). The ``io`` counters are best-effort — readable for same-user
    processes — and degrade to ``None`` when permission is denied or absent.
    """
    proc = PROC_ROOT / str(pid)
    try:
        stat_raw = (proc / "stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None

    # stat field 2 (comm) is wrapped in parens and may itself contain spaces or
    # parens, so split on the LAST ')' to locate the remaining space-delimited
    # fields. After the split, rest[0] is field 3 (state), rest[k] is field 3+k.
    try:
        rparen = stat_raw.rindex(")")
    except ValueError:
        return None
    rest = stat_raw[rparen + 1:].split()
    try:
        state = rest[0]              # field 3
        ppid = int(rest[1])          # field 4
        utime = int(rest[11])        # field 14
        stime = int(rest[12])        # field 15
        starttime = int(rest[19])    # field 22
    except (IndexError, ValueError):
        return None

    try:
        cmdline_raw = (proc / "cmdline").read_text(encoding="utf-8", errors="replace")
    except OSError:
        cmdline_raw = ""
    cmdline = [part for part in cmdline_raw.split("\x00") if part]
    cmdline_str = " ".join(cmdline)

    try:
        wchan = (proc / "wchan").read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        wchan = ""

    io_bytes: int | None = None
    try:
        rchar = wchar = 0
        for line in (proc / "io").read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("rchar:"):
                rchar = int(line.split()[1])
            elif line.startswith("wchar:"):
                wchar = int(line.split()[1])
        io_bytes = rchar + wchar
    except (OSError, ValueError, IndexError):
        io_bytes = None

    return ProcInfo(
        pid=pid,
        ppid=ppid,
        state=state,
        starttime=starttime,
        cpu_ticks=utime + stime,
        cmdline=cmdline,
        cmdline_str=cmdline_str,
        wchan=wchan,
        io_bytes=io_bytes,
    )


def _read_children(pid: int) -> list[int]:
    """Return immediate child PIDs of *pid* via ``/proc/<pid>/task/*/children``."""
    children: list[int] = []
    task_dir = PROC_ROOT / str(pid) / "task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return children
    for tid in tids:
        try:
            data = (task_dir / tid / "children").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for token in data.split():
            try:
                children.append(int(token))
            except ValueError:
                continue
    return children


def _descendants(pid: int) -> Iterator[int]:
    """Yield every descendant PID of *pid*, breadth-first.

    Robust to processes appearing/disappearing mid-walk and to cycles (a PID is
    never yielded twice). The root *pid* itself is not yielded.
    """
    seen: set[int] = set()
    queue: deque[int] = deque(_read_children(pid))
    while queue:
        child = queue.popleft()
        if child in seen or child == pid:
            continue
        seen.add(child)
        yield child
        queue.extend(_read_children(child))


def _proc_age_seconds(starttime_ticks: int) -> float:
    """Age (seconds) of a process whose stat ``starttime`` is *starttime_ticks*."""
    if _BTIME is None:
        return 0.0
    start_epoch = _BTIME + (starttime_ticks / _CLK_TCK)
    return max(0.0, time.time() - start_epoch)


def _subtree_activity(root_pid: int) -> ActivitySample:
    """Aggregate CPU ticks and I/O bytes across *root_pid* and all descendants.

    ``io_available`` is ``False`` if any process in the subtree had unreadable
    I/O counters, in which case the I/O signal must be ignored by the caller and
    the decision falls back to CPU only.
    """
    cpu_total = 0
    io_total = 0
    io_available = True
    for pid in (root_pid, *_descendants(root_pid)):
        if pid == _SELF_PID:
            continue  # never count the measuring process (see _SELF_PID note)
        info = _proc_snapshot(pid)
        if info is None:
            continue
        cpu_total += info.cpu_ticks
        if info.io_bytes is None:
            io_available = False
        else:
            io_total += info.io_bytes
    return ActivitySample(
        cpu_ticks=cpu_total,
        io_bytes=io_total,
        io_available=io_available,
        timestamp=time.monotonic(),
    )


def _is_blocking_wchan(wchan: str) -> bool:
    lowered = wchan.lower()
    return any(token in lowered for token in _BLOCKING_WCHAN_TOKENS)


def _cmd_basename(info: ProcInfo) -> str:
    return os.path.basename(info.cmdline[0]) if info.cmdline else ""


def _is_blocked_candidate(info: ProcInfo, threshold_seconds: float) -> bool:
    """True if *info* is an old, quiescently-blocked stuck candidate.

    The blocked-syscall requirement is what excludes the legitimate-long-op
    false positive (e.g. a ``pytest`` descendant is ``R``/CPU-busy, not parked in
    ``nanosleep``).
    """
    if info.state not in _QUIESCENT_STATES:
        return False
    if _proc_age_seconds(info.starttime) < threshold_seconds:
        return False
    return _is_blocking_wchan(info.wchan) or _cmd_basename(info) == "sleep"


def _blocked_on_label(info: ProcInfo) -> str:
    if info.wchan and info.wchan not in ("0", ""):
        return info.wchan
    if _cmd_basename(info) == "sleep":
        return "sleep"
    return info.wchan or "?"


def _nearest_claude_root(
    info: ProcInfo, by_pid: dict[int, ProcInfo], worker_pid: int
) -> int:
    """Walk parent links from *info* to the nearest ``claude`` ancestor.

    Falls back to *worker_pid* when no ``claude`` ancestor is found within the
    captured subtree snapshot.
    """
    current: ProcInfo | None = info
    guard = 0
    while current is not None and guard < 1000:
        if _cmd_basename(current) == "claude":
            return current.pid
        if current.pid == worker_pid:
            break
        current = by_pid.get(current.ppid)
        guard += 1
    return worker_pid


def _subtree_is_idle(root_pid: int, activity_sample_seconds: float) -> bool:
    """True if *root_pid*'s subtree makes no CPU/IO progress across two samples.

    Sampled twice ``activity_sample_seconds`` apart. A process parked in
    ``sleep`` accrues zero CPU for its whole life, so a zero delta here combined
    with "blocked in nanosleep ≥ threshold" (checked by the caller) is a strong,
    correct equivalent of "no activity in the window".
    """
    first = _subtree_activity(root_pid)
    if activity_sample_seconds > 0:
        time.sleep(activity_sample_seconds)
    second = _subtree_activity(root_pid)
    cpu_advanced = second.cpu_ticks > first.cpu_ticks
    io_advanced = (
        first.io_available and second.io_available and second.io_bytes > first.io_bytes
    )
    return not (cpu_advanced or io_advanced)


def list_stuck(
    base_dir: Path,
    *,
    threshold_seconds: float = 300,
    activity_sample_seconds: float = 0.3,
) -> list[StuckProcessView]:
    """Return stuck Claude descendants across all running workers.

    For each running worker, descendants are flagged when they are (1) old and
    parked in a non-returning syscall, AND (2) part of a Claude subtree doing no
    CPU/IO work. The (potentially blocking) activity sample is taken only when at
    least one old, blocked candidate exists, so the common case pays no latency.
    """
    # Map each repo to its advertised session so a stuck descendant can be tied
    # back to the ClaudeSession that owns it (for the ESC-interrupt endpoint).
    sessions = list_sessions(base_dir)
    repo_to_session = {s.repo: s for s in sessions if s.repo}

    stuck: list[StuckProcessView] = []
    for worker in list_workers(base_dir):
        if worker.status != "running" or worker.pid is None:
            continue
        worker_pid = worker.pid

        # Snapshot the whole subtree once for this worker (per-request, no TTL).
        by_pid: dict[int, ProcInfo] = {}
        for pid in _descendants(worker_pid):
            info = _proc_snapshot(pid)
            if info is not None:
                by_pid[pid] = info
        root_info = _proc_snapshot(worker_pid)
        if root_info is not None:
            by_pid[worker_pid] = root_info

        candidates = [
            info for pid, info in by_pid.items()
            if pid != worker_pid and _is_blocked_candidate(info, threshold_seconds)
        ]
        if not candidates:
            continue

        # Evaluate the activity signal once per distinct Claude subtree root.
        idle_cache: dict[int, bool] = {}
        for info in candidates:
            root = _nearest_claude_root(info, by_pid, worker_pid)
            if root not in idle_cache:
                idle_cache[root] = _subtree_is_idle(root, activity_sample_seconds)
            if not idle_cache[root]:
                continue  # Claude is actively progressing — not stuck.
            session = repo_to_session.get(worker.repo)
            stuck.append(
                StuckProcessView(
                    worker_repo=worker.repo,
                    task_key=session.key if session else None,
                    pid=info.pid,
                    cmdline=info.cmdline_str,
                    age_seconds=int(_proc_age_seconds(info.starttime)),
                    blocked_on=_blocked_on_label(info),
                    session_id=session.session_id if session else None,
                )
            )
    return stuck


# ---------------------------------------------------------------------------
# Descendant validation + kill (issue #132)
# ---------------------------------------------------------------------------

class NotADescendantError(Exception):
    """Raised when a kill target is not a live descendant of any worker PID."""


def is_worker_descendant(base_dir: Path, pid: int) -> bool:
    """True if *pid* is a descendant of some running worker PID.

    Security gate: the kill endpoint must never signal an arbitrary host
    process. PID ``<= 1`` is rejected outright (1 is init; ``0``/negative target
    process groups).
    """
    if pid <= 1:
        return False
    for worker in list_workers(base_dir):
        if worker.status == "running" and worker.pid:
            if pid in set(_descendants(worker.pid)):
                return True
    return False


def kill_descendant(base_dir: Path, pid: int, *, grace_seconds: float = 5.0) -> dict:
    """Send ``SIGTERM`` to *pid* after validating it is a worker descendant.

    Returns a status dict ``{pid, signal_sent, escalated, alive, starttime}``.
    Does NOT block for the SIGKILL escalation — the caller schedules
    :func:`escalate_kill` as a background task when ``alive`` is true. Always
    signals a single positive PID, never a process group.

    Raises :class:`NotADescendantError` when *pid* is not a current descendant.
    """
    if not is_worker_descendant(base_dir, pid):
        raise NotADescendantError(f"PID {pid} is not a known worker descendant")

    snapshot = _proc_snapshot(pid)
    starttime = snapshot.starttime if snapshot is not None else None

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"pid": pid, "signal_sent": "SIGTERM", "escalated": False,
                "alive": False, "starttime": starttime}
    return {"pid": pid, "signal_sent": "SIGTERM", "escalated": False,
            "alive": True, "starttime": starttime}


def escalate_kill(
    base_dir: Path,
    pid: int,
    grace_seconds: float,
    starttime: int | None = None,
) -> bool:
    """Background step: ``SIGKILL`` *pid* if still alive after *grace_seconds*.

    Re-validates descendant membership and (if known) the original ``starttime``
    immediately before signalling, guarding against TOCTOU / PID reuse. Returns
    ``True`` iff a ``SIGKILL`` was actually delivered.
    """
    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return False  # exited cleanly within the grace period
        time.sleep(0.1)

    try:
        os.kill(pid, 0)
    except OSError:
        return False  # exited right at the deadline

    if not is_worker_descendant(base_dir, pid):
        return False  # no longer ours — refuse to signal
    snapshot = _proc_snapshot(pid)
    if starttime is not None and (snapshot is None or snapshot.starttime != starttime):
        return False  # PID was reused for a different process

    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False
