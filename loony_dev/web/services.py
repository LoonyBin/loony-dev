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

import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loony_dev import execution_state, pipeline_lease, pipeline_log, session_registry
from loony_dev.agents import session_resume
from loony_dev.git import GitRepo

logger = logging.getLogger(__name__)

WORKER_LOG_NAME = "loony-worker.log"
WORKER_PID_NAME = "loony-worker.pid"

# Per-repo remote-control "connection file". The canonical schema and writer live
# in ``loony_dev.supervisor`` (see ``_write_connection_file``); this reader
# consumes ``{session_id, repo, key, mode, join_url}`` plus the file's mtime.
# Parsing is defensive: unknown keys are ignored and malformed/missing files are
# skipped. The sibling ``remote-control.pid`` file gives process liveness, read
# the same defensive way as the worker PID file.
REMOTE_CONTROL_CONN_NAME = "remote-control.json"
REMOTE_CONTROL_PID_NAME = "remote-control.pid"


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
class CommitView:
    """One commit from a repo's local ``git log`` (issue #224).

    Sourced from the persistent base checkout (``<base>/<owner>/<repo>``, which
    tracks ``main``), never from GitHub — the "Recent commits" sidebar panel
    shows what the bot has actually committed locally.
    """

    sha: str
    short_sha: str
    subject: str
    author: str
    date_iso: str  # committer date, ISO-8601 (``%cI``)
    rel_date: str  # committer date, relative (``%cr``), e.g. "2 hours ago"


@dataclass(frozen=True)
class SessionView:
    session_id: str
    repo: str | None
    key: str | None
    # The claude.ai join link the child emits once the remote-control session is
    # live (``null`` until it appears). Surfaced so the dashboard can render a
    # join link + QR for the per-repo session card.
    join_url: str | None = None
    mode: str | None = None  # e.g. "remote-control"
    updated_at: str | None = None  # ISO-8601 mtime of remote-control.json (staleness)
    alive: bool | None = None  # remote-control process running; null if no PID file
    control_socket: str | None = None


@dataclass(frozen=True)
class TaskSessionView:
    """A per-task worker session the dashboard can attach to / steer (issue #164).

    ``attachable`` reflects whether the PTY-bridge Unix socket is currently
    present, so the frontend can disable the Attach button for a session whose
    worker has gone away but left a stale ``session.json``.

    ``observable`` reflects whether the session can be rendered from its on-disk
    JSONL transcript (issue #202) — true whenever both ``cwd`` and ``session_id``
    are known, independent of whether a live PTY exists. This is what the
    frontend uses to pick the JSONL-driven observe surface as the default, with
    the raw-bytes attach terminal reserved for the live "drive" case.
    """

    task_key: str
    repo: str | None
    session_id: str | None
    status: str | None
    started_at: str | None
    attachable: bool
    observable: bool = False
    cwd: str | None = None
    # The per-pipeline mutual-exclusion identity (``issue-N`` / ``pr-P``). It may
    # equal ``task_key`` for a simple issue pipeline but is conceptually distinct
    # (``task_key`` = ``task.worktree_key``); the dashboard must use this — not
    # ``task_key`` — to address the ``/api/pipelines/{pipeline_key}/...`` routes
    # (issue #199). Surfaced read-only so the Issue ▸ PR detail view (#190) can
    # name the pipeline the upcoming #200 controls act on.
    pipeline_key: str | None = None


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


# Unit separator: a field delimiter that cannot appear inside a commit subject /
# author name, so the per-field split is unambiguous (newlines are the record
# delimiter). Mirrors the ``%x1f`` placeholders in the ``--format`` below.
_COMMIT_FIELD_SEP = "\x1f"
_COMMIT_FORMAT = _COMMIT_FIELD_SEP.join(["%H", "%h", "%s", "%an", "%cI", "%cr"])


class CheckoutNotFoundError(Exception):
    """Raised when a repo's base checkout is absent, not a git repo, or its path
    segments are invalid (the request can't resolve to a real checkout)."""


class GitCommandError(Exception):
    """Raised when the ``git log`` invocation itself fails (non-zero exit, OS
    error, or timeout) — a genuine failure, distinct from "no such checkout"."""


def recent_commits(
    base_dir: Path, owner: str, name: str, limit: int = 5
) -> list[CommitView]:
    """Return the newest commits from ``owner/name``'s base checkout (issue #224).

    Targets the persistent main-branch checkout at ``<base>/<owner>/<name>`` and
    runs a single ``git log`` there — a *local* history (not a GitHub fetch), so
    it reflects exactly what the bot has committed on disk. *limit* is clamped to
    a small bound (1–20).

    Following the project's "raise on failure" convention (CLAUDE.md), this is the
    single place validation + failures live (the route is a thin mapper). It
    raises :class:`CheckoutNotFoundError` for an invalid path segment or a
    missing/non-git checkout, and :class:`GitCommandError` when ``git`` itself
    fails or emits a record that breaks the parse contract. A repo with a valid
    but empty log returns ``[]`` (a real, not-failed result).
    """
    for segment in (owner, name):
        if (
            not segment
            or segment in (".", "..")
            or "/" in segment
            or "\\" in segment
            or "\x00" in segment
        ):
            raise CheckoutNotFoundError(f"invalid path segment: {segment!r}")

    checkout = base_dir / owner / name
    if not (checkout / ".git").exists():
        raise CheckoutNotFoundError(f"no git checkout for {owner}/{name}")

    capped = max(1, min(int(limit), 20))
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(checkout), "log",
                "-n", str(capped),
                "--no-color",
                f"--format={_COMMIT_FORMAT}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise GitCommandError(f"git log failed for {owner}/{name}: {exc}") from exc

    commits: list[CommitView] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        parts = line.split(_COMMIT_FIELD_SEP)
        if len(parts) != 6:
            # A record with the wrong field count means our parse contract broke
            # (e.g. a subject smuggling the separator byte). Fail loudly per the
            # "raise on failure" convention rather than returning a partial list.
            raise GitCommandError(
                f"git log parse failed for {owner}/{name}: malformed record {line!r}"
            )
        sha, short_sha, subject, author, date_iso, rel_date = parts
        commits.append(
            CommitView(
                sha=sha,
                short_sha=short_sha,
                subject=subject,
                author=author,
                date_iso=date_iso,
                rel_date=rel_date,
            )
        )
    return commits


def list_sessions(base_dir: Path) -> list[SessionView]:
    """Read each repo's ``remote-control.json`` connection file.

    Discovers repos via the same ``.logs/<owner>/<repo>/`` scan as the workers
    and reads ``<repo_log_dir>/remote-control.json`` (written by the supervisor;
    canonical schema in :mod:`loony_dev.supervisor`). Parsing is defensive:
    malformed/missing files are skipped, unknown keys are ignored, and
    ``session_id`` falls back to ``owner/repo`` when absent.

    Beyond the identity fields it surfaces the ``join_url`` (the claude.ai
    deep-link, ``null`` until Claude emits it), the ``mode``, the connection
    file's mtime as ``updated_at`` (so the UI can show staleness), and ``alive``
    — read from the sibling ``remote-control.pid`` file the same defensive way
    :func:`list_workers` reads the worker PID, falling back to ``None`` when no
    PID file is present.
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
        join_url = data.get("join_url")
        mode = data.get("mode")
        control_socket = data.get("control_socket")

        pid = _read_pid(repo_dir / REMOTE_CONTROL_PID_NAME)
        # "unknown" means the process exists but is owned by another user
        # (we lack permission to signal it) — it is alive, not dead.
        alive = process_status(pid) in ("running", "unknown") if pid is not None else None

        sessions.append(
            SessionView(
                session_id=str(session_id),
                repo=str(repo) if repo is not None else None,
                key=str(key) if key is not None else None,
                join_url=str(join_url) if join_url is not None else None,
                mode=str(mode) if mode is not None else None,
                updated_at=_iso_mtime(conn_path),
                alive=alive,
                control_socket=str(control_socket) if control_socket else None,
            )
        )
    return sessions


# ---------------------------------------------------------------------------
# Partial GitHub state (issue #219)
#
# The web process is otherwise filesystem-only; this is the single seam where it
# reaches GitHub (via the same ``loony_dev.github`` wrappers the workers use) to
# enrich the snapshot with the *cheap* lifecycle fields the reworked screens need
# but cannot derive from disk: per-pipeline issue/PR title, a label/PR-derived
# lifecycle stage, and per-repo open issue/PR counts. Everything here degrades
# gracefully — any ``gh`` failure falls back to today's placeholders, never an
# error (a failed repo yields ``RepoGitHubView(ok=False)`` with ``None`` counts
# and no pipelines; the others are unaffected). The (occasional) fetch sits
# behind a TTL cache so the 2s SSE poll never triggers a ``gh`` call.
# ---------------------------------------------------------------------------

# The lifecycle stages the Fleet frontend renders (see ``static/js/fleet.js``
# ``STAGES``). ``derive_stage`` maps GitHub label / PR state into exactly these.
STAGE_INBOX = "Inbox"
STAGE_PLANNING = "Planning"
STAGE_IMPLEMENTING = "Implementing"
STAGE_PR_OPEN = "PR Open"
STAGE_IN_REVIEW = "In Review"
STAGE_CONFLICTS = "Conflicts"


@dataclass(frozen=True)
class PipelineGitHubView:
    """GitHub-derived state for one pipeline (``issue-N`` / ``pr-P``).

    ``labels`` carries the raw issue labels (PR labels for an issue-less PR
    pipeline) so the frontend can filter on e.g. ``in-error`` — which is *not* a
    distinct stage (the board has no Error column), only a raw label. ``Merged``
    is likewise not reachable here: a merged PR is excluded from ``list_open`` and
    the pipeline is reclaimed quickly, so chasing it would cost a per-PR
    closed-state query for no practical gain.
    """

    pipeline_key: str        # "issue-N" / "pr-P"
    repo: str                # "owner/name"
    kind: str                # "issue" | "pr"
    number: int
    title: str | None
    stage: str               # one of the Fleet STAGES (see derive_stage)
    labels: list[str]        # raw issue labels (frontend filters / future use)
    pr_state: str | None     # "open" for the PRs we surface; None when no PR
    mergeable: str | None    # PR mergeable state (e.g. "CONFLICTING"), or None
    updated_at: str | None   # ISO-8601, newest of the issue/PR facets


@dataclass(frozen=True)
class RepoGitHubView:
    """Per-repo open counts. ``ok`` is False (and counts None) on a fetch error."""

    repo: str
    open_issues: int | None  # None = this repo's GitHub fetch failed
    open_prs: int | None
    ok: bool


@dataclass(frozen=True)
class LiveStateView:
    """The consumer-facing slice of a pipeline's live-state snapshot (#269).

    Mirrors the projection fields the Fleet board / stat strip and the
    ``/api/pipelines`` live-overlay read off :class:`execution_state.LiveState` —
    the **authoritative** ``stage`` / ``current_skill`` / ``attempt`` / ``state``
    the dashboard binds to instead of guessing the phase from GitHub labels. The
    snapshot substrate (#267) guarantees these reads never raise; the worker-only
    ``last_heartbeat`` is omitted (the board has no use for it yet).
    """

    pipeline_key: str
    repo: str
    stage: str
    current_skill: str | None
    state: str
    attempt: int
    needs_you: bool
    live: bool
    linked_pr: int | None
    updated_at: str
    worktree_path: str | None

    @classmethod
    def from_state(cls, s: "execution_state.LiveState") -> "LiveStateView":
        return cls(
            pipeline_key=s.pipeline_key,
            repo=s.repo,
            stage=s.stage,
            current_skill=s.current_skill,
            state=s.state,
            attempt=s.attempt,
            needs_you=s.needs_you,
            live=s.live,
            linked_pr=s.linked_pr,
            updated_at=s.updated_at,
            worktree_path=s.worktree_path,
        )


def list_live_states(base_dir: Path) -> list[LiveStateView]:
    """Every live snapshot under *base_dir* (the Fleet board / stat-strip source).

    This is the "reduce over the active snapshot set" feed (ADR 0002 read-model):
    :func:`execution_state.list_active` returns every running/live snapshot,
    skipping missing/malformed files, so this never raises and yields an empty
    list when ``.logs`` is absent.
    """
    return [LiveStateView.from_state(s) for s in execution_state.list_active(base_dir)]


def pipeline_live_overlay(base_dir: Path, repo: str, pipeline_key: str) -> dict | None:
    """The per-pipeline live overlay for ``/api/pipelines`` and the #218 DAG.

    Reads the pipeline's snapshot (authoritative ``stage`` / ``current_skill`` /
    ``attempt`` / ``state`` / ``needs_you`` / ``live`` / ``updated_at``) and its
    drive-lease ``holder`` (#199) — the only place the holder is read off the
    drive path. Returns ``None`` when neither a snapshot nor a lease exists (an
    idle, never-dispatched pipeline), so the caller can emit a ``null`` overlay
    and the UI keeps its GitHub/coarse stage fallback. Snapshot-derived fields are
    ``None`` when only a lease exists.
    """
    state = execution_state.read_snapshot(base_dir, repo, pipeline_key)
    lease = pipeline_lease.read_pipeline_lease(base_dir, repo, pipeline_key)
    holder = lease.holder if lease else None
    if state is None and holder is None:
        return None
    return {
        "stage": state.stage if state else None,
        "current_skill": state.current_skill if state else None,
        "attempt": state.attempt if state else None,
        "state": state.state if state else None,
        "needs_you": state.needs_you if state else None,
        "live": state.live if state else None,
        "updated_at": state.updated_at if state else None,
        "holder": holder,
    }


def derive_stage(issue_labels: list[str], pr) -> str:
    """Map an issue's labels + its PR facet into one Fleet lifecycle stage.

    A pure, unit-testable function. Once a PR exists it dominates (its conflict /
    review state is the live picture); otherwise the issue's loony-dev label
    drives the stage. ``in-error`` is intentionally not a stage — it rides the
    raw ``labels`` list — so an errored issue still shows its underlying stage.
    """
    if pr is not None:
        if (getattr(pr, "mergeable", None) or "").upper() == "CONFLICTING":
            return STAGE_CONFLICTS
        return STAGE_IN_REVIEW if getattr(pr, "reviews", None) else STAGE_PR_OPEN
    labels = set(issue_labels or [])
    if "in-progress" in labels:
        return STAGE_IMPLEMENTING
    if "ready-for-planning" in labels:
        return STAGE_PLANNING
    # ready-for-development (awaiting/ready to implement) and anything else read
    # as Inbox — the entry column.
    return STAGE_INBOX


def _parse_pipeline_key(pipeline_key: str) -> tuple[str, int]:
    """Split a ``issue-N`` / ``pr-P`` key into ``(kind, number)``."""
    m = re.match(r"^(issue|pr)-(\d+)$", pipeline_key)
    if m:
        return m.group(1), int(m.group(2))
    # Defensive: keys are always issue-N/pr-P, but never raise from a snapshot.
    kind = pipeline_key.split("-", 1)[0] or "issue"
    return kind, 0


def _latest_iso(*facets) -> str | None:
    """Return the newest ``updated_at`` across *facets* as ISO-8601, or None."""
    stamps = [
        f.updated_at for f in facets
        if f is not None and getattr(f, "updated_at", None) is not None
    ]
    if not stamps:
        return None
    return max(stamps).isoformat()


def _pipeline_view(pipeline, repo: str) -> PipelineGitHubView:
    """Build a :class:`PipelineGitHubView` from a :class:`~loony_dev.pipeline.Pipeline`."""
    issue = pipeline.issue
    pr = pipeline.pr
    kind, number = _parse_pipeline_key(pipeline.pipeline_key)

    # Title: prefer the issue facet, fall back to the PR facet. Both are Content.
    title = None
    if issue is not None and str(issue.title):
        title = str(issue.title)
    elif pr is not None and str(pr.title):
        title = str(pr.title)

    issue_labels = list(issue.labels) if issue is not None else []
    # Surface PR labels for an issue-less PR pipeline so e.g. in-error still shows.
    labels = issue_labels if issue is not None else (list(pr.labels) if pr is not None else [])

    return PipelineGitHubView(
        pipeline_key=pipeline.pipeline_key,
        repo=repo,
        kind=kind,
        number=number,
        title=title,
        stage=derive_stage(issue_labels, pr),
        labels=labels,
        pr_state="open" if pr is not None else None,
        mergeable=getattr(pr, "mergeable", None) if pr is not None else None,
        updated_at=_latest_iso(issue, pr),
    )


def _repo_for(owner: str, name: str, checkout: Path):
    """Construct a :class:`~loony_dev.github.Repo` for ``owner/name``'s checkout.

    A module-level seam so tests can stub repo construction without a real ``gh``
    auth / network round trip.
    """
    from loony_dev.github import Repo

    return Repo(f"{owner}/{name}", cwd=str(checkout))


def _count_open_issues(repo) -> int:
    """Return the repo's open-issue count via a single cheap ``gh issue list``.

    Raises ``ValueError`` on an unexpected (non-list) payload rather than
    silently counting it as zero — a malformed response is a fetch failure, and
    the per-repo ``try/except`` in ``_fetch_github_state`` turns it into an
    ``ok=False`` view, not a fabricated "0 open issues".
    """
    data = repo.client.gh_json(
        "issue", "list", "--state", "open", "--json", "number", "-L", "500",
    )
    if not isinstance(data, list):
        raise ValueError(f"Unexpected open-issue payload for {repo.name!r}: {data!r}")
    return len(data)


def _fetch_github_state(
    base_dir: Path,
) -> tuple[list[PipelineGitHubView], list[RepoGitHubView]]:
    """Fetch GitHub-derived snapshot state across every checked-out repo.

    Reuses :meth:`Pipeline.discover` (one enumeration of open issues + PRs, the
    same grouping the orchestrator uses) so the dashboard's pipeline view never
    drifts from the worker's. Per-repo ``try/except`` keeps one bad repo from
    dropping the others — the failed repo yields ``RepoGitHubView(ok=False)``.
    """
    from loony_dev.github import PullRequest
    from loony_dev.pipeline import Pipeline

    pipelines: list[PipelineGitHubView] = []
    repos: list[RepoGitHubView] = []
    for owner, name, _repo_dir in _discover_repos(base_dir):
        checkout = base_dir / owner / name
        if not (checkout / ".git").exists():
            continue
        repo_name = f"{owner}/{name}"
        try:
            repo = _repo_for(owner, name, checkout)
            # Stage rows in a per-repo local so a later counting failure (which
            # marks the repo ok=False) never leaks half a repo's pipelines into
            # the response — per-repo success is all-or-nothing.
            repo_pipelines = [
                _pipeline_view(pipeline, repo_name)
                for pipeline in Pipeline.discover(repo)
            ]
            # ``discover`` already populated the tick-cached open-PR list, so this
            # is a cache hit — no extra ``gh`` call.
            open_prs = len(PullRequest.list_open(repo=repo))
            open_issues = _count_open_issues(repo)
            pipelines.extend(repo_pipelines)
            repos.append(
                RepoGitHubView(
                    repo=repo_name, open_issues=open_issues, open_prs=open_prs, ok=True,
                )
            )
        except Exception:
            logger.warning("GitHub state fetch failed for %s", repo_name, exc_info=True)
            repos.append(
                RepoGitHubView(repo=repo_name, open_issues=None, open_prs=None, ok=False)
            )
    return pipelines, repos


# TTL cache in front of the (occasional) GitHub fetch, keyed by base_dir so the
# 2s SSE poll re-reads the last result and ``gh`` runs at most once per
# ``refresh_seconds``. A single in-flight refresh holds the lock; concurrent
# callers get the last value (possibly stale) rather than blocking — except the
# very first call, which has nothing to return and so blocks once.
_GH_CACHE: dict[Path, tuple[float, tuple[list[PipelineGitHubView], list[RepoGitHubView]]]] = {}
_GH_LOCK = threading.Lock()


def github_state(
    base_dir: Path,
    *,
    enabled: bool = True,
    refresh_seconds: float = 60.0,
    fetch_fn=None,
) -> tuple[list[PipelineGitHubView], list[RepoGitHubView]]:
    """Return cached GitHub-derived snapshot state, refreshing past the TTL.

    *fetch_fn* is the network seam (defaults to :func:`_fetch_github_state`),
    injectable for tests. With ``enabled=False`` this is a no-op returning
    ``([], [])`` and makes zero ``gh`` calls, so the frontend keeps its existing
    placeholders. A fetch that raises on a warm cache returns the last good value
    rather than propagating — graceful degradation, never a dashboard error.
    """
    if not enabled:
        return [], []
    fetch = fetch_fn or _fetch_github_state
    now = time.monotonic()
    cached = _GH_CACHE.get(base_dir)
    if cached is not None and now - cached[0] < refresh_seconds:
        return cached[1]
    # Only the first-ever call (no cache to fall back on) blocks for the lock.
    if not _GH_LOCK.acquire(blocking=cached is None):
        return cached[1] if cached is not None else ([], [])
    try:
        # Re-check under the lock: a waiter (e.g. a second cold-start call) may
        # have refreshed the cache while we blocked, so don't fetch again within
        # the same TTL window.
        now = time.monotonic()
        cached = _GH_CACHE.get(base_dir)
        if cached is not None and now - cached[0] < refresh_seconds:
            return cached[1]
        result = fetch(base_dir)
        # Stamp with completion time so a slow fetch doesn't look pre-aged.
        _GH_CACHE[base_dir] = (time.monotonic(), result)
        return result
    except Exception:
        logger.warning("GitHub snapshot refresh failed", exc_info=True)
        return cached[1] if cached is not None else ([], [])
    finally:
        _GH_LOCK.release()


# ---------------------------------------------------------------------------
# ready-for-* label controls (issue #225) — the moved "Assign issue"
#
# The Issue ▸ PR detail page lets a human set the two entry labels that drive
# the lifecycle state machine (planning → development). They are mutually
# exclusive, so setting one removes its sibling. Only issue pipelines carry
# these labels; a pr-P pipeline is rejected.
# ---------------------------------------------------------------------------

# The two ready-for-* entry labels (loony_dev.github.repo.REQUIRED_LABELS),
# mutually exclusive: setting one clears the other.
READY_LABELS = ("ready-for-planning", "ready-for-development")


class LabelControlError(Exception):
    """Raised when a label-control request is invalid (bad label / pr pipeline)."""


def set_pipeline_label(
    base_dir: Path, pipeline_key: str, label: str, repo: str,
) -> dict:
    """Set a ready-for-* entry label on an issue pipeline, clearing its sibling.

    Validates *label* against :data:`READY_LABELS` and restricts the operation to
    ``issue-N`` pipelines (these are issue-lifecycle labels). Raises
    :class:`LabelControlError` for a bad label / non-issue key / malformed repo
    (→ 422), :class:`SessionNotFoundError` for an unknown checkout (→ 404), and
    re-raises a failed ``add_label`` *or* a failed sibling ``remove_label`` as
    :class:`LabelControlError` (so a partial mutation is never reported as
    success). Returns the resulting entry-label set so the caller can confirm
    the new state.
    """
    if label not in READY_LABELS:
        raise LabelControlError(
            f"'label' must be one of {', '.join(READY_LABELS)}"
        )
    if not isinstance(repo, str) or "/" not in repo:
        raise LabelControlError("'repo' must be 'owner/repo'")
    owner, name = repo.split("/", 1)
    if not owner or not name or "/" in name:
        raise LabelControlError("'repo' must be 'owner/repo'")
    kind, number = _parse_pipeline_key(pipeline_key)
    if kind != "issue" or number <= 0:
        raise LabelControlError(
            "ready-for-* labels apply to issue pipelines only"
        )
    checkout = base_dir / owner / name
    if not (checkout / ".git").exists():
        raise SessionNotFoundError(f"no checkout for {repo!r}")

    from loony_dev.github import Issue

    gh_repo = _repo_for(owner, name, checkout)
    issue = Issue(number=number, _repo=gh_repo)
    if not issue.add_label(label):
        raise LabelControlError(f"failed to add label {label!r} to #{number}")
    # Keep the two entry labels mutually exclusive: drop the sibling. Raise if the
    # removal fails — returning success here would claim a mutually-exclusive
    # state we didn't achieve (the issue could keep both ready-for-* labels), and
    # the repo guideline is to raise on failure rather than report a false result.
    sibling = next(other for other in READY_LABELS if other != label)
    if not issue.remove_label(sibling):
        raise LabelControlError(f"failed to remove sibling label {sibling!r} from #{number}")
    return {
        "pipeline_key": pipeline_key,
        "repo": repo,
        "label": label,
        "labels": [label],
    }


# ---------------------------------------------------------------------------
# Per-task attach + steer sessions (issue #164)
# ---------------------------------------------------------------------------

class SessionNotFoundError(Exception):
    """Raised when no live session matches a requested task key or session id."""


def list_task_sessions(base_dir: Path) -> list[TaskSessionView]:
    """Return one :class:`TaskSessionView` per discovered per-task session.

    Reads the worker-published registry (see :mod:`loony_dev.session_registry`).
    ``attachable`` is true only when the bridge socket file is present, so a
    crashed worker's leftover ``session.json`` is surfaced but not joinable.
    """
    views: list[TaskSessionView] = []
    for session in session_registry.iter_sessions(base_dir):
        attachable = bool(session.socket) and Path(session.socket).exists()
        observable = bool(session.cwd) and bool(session.session_id)
        views.append(
            TaskSessionView(
                task_key=session.task_key,
                repo=session.repo,
                session_id=session.session_id,
                status=session.status,
                started_at=session.started_at,
                attachable=attachable,
                observable=observable,
                cwd=session.cwd,
                pipeline_key=session.pipeline_key,
            )
        )
    return views


def find_task_session(base_dir: Path, task_key: str) -> session_registry.TaskSession | None:
    """Return the registry record for *task_key*, or ``None`` if absent.

    Matches the canonical key stored in ``session.json`` (never builds a path
    from *task_key*), so the value is safe to pass straight from a URL segment.
    """
    return session_registry.find_session(base_dir, task_key)


# ---------------------------------------------------------------------------
# On-demand pipeline interrogation (issue #199)
#
# A parked pipeline (waiting on plan approval, CI, or review) has no live
# process, but its Claude session is a durable on-disk artifact, so it can be
# resumed on demand. Two modes:
#   * observe — read-only; tails the recorded transcript (or reuses a live attach
#     socket). No lease.
#   * drive   — resumes the session into a fresh PTY and serves it for attach.
#     Holds the per-pipeline lease so a bot task and a human drive never co-run
#     on one pipeline (the lease is cross-process; the scheduler honours it via
#     ``Orchestrator._claimed_keys``).
# ---------------------------------------------------------------------------

INTERROGATE_OBSERVE = "observe"
INTERROGATE_DRIVE = "drive"

# Live drive sessions this web process owns, keyed by ``(repo, pipeline_key)``.
# A drive holds a real PTY for the duration of the attach, so — unlike the rest
# of this module — it is genuine in-memory state; it is torn down (and its lease
# released) via :func:`stop_drive`.
_DRIVE_SESSIONS: dict[tuple[str, str], "session_resume.ResumedSession"] = {}


class PipelineBusyError(Exception):
    """Raised when a drive is requested for a pipeline an automated task holds."""


def _git_for_repo(base_dir: Path, repo: str) -> GitRepo:
    """Return a :class:`GitRepo` for ``repo``'s base checkout under *base_dir*."""
    if not isinstance(repo, str) or "/" not in repo:
        raise SessionNotFoundError(f"invalid repo {repo!r}: expected 'owner/repo'")
    owner, name = repo.split("/", 1)
    return GitRepo(work_dir=base_dir / owner / name)


def _resolve_pipeline_repo(base_dir: Path, pipeline_key: str, repo: str | None) -> str:
    """Determine the ``owner/repo`` a *pipeline_key* belongs to.

    Prefers an explicit *repo* (disambiguates a key shared across repos and lets
    a pre-feature pipeline with no record still be addressed); otherwise resolves
    it from the recorded session. Raises :class:`SessionNotFoundError` when
    neither is available.
    """
    if repo:
        return repo
    for session in session_registry.iter_sessions(base_dir):
        if session.pipeline_key == pipeline_key and session.repo:
            return session.repo
    raise SessionNotFoundError(
        f"no recorded session for pipeline {pipeline_key!r}; pass an explicit repo",
    )


def interrogate_pipeline(
    base_dir: Path,
    pipeline_key: str,
    mode: str,
    *,
    repo: str | None = None,
    resume_fn=None,
) -> dict:
    """Start an observe or drive interrogation of a parked pipeline.

    *resume_fn* is injectable for tests; it defaults to
    :func:`loony_dev.agents.session_resume.resume_session`. Returns a small status
    dict; for drive it includes the ``attach_url`` the dashboard connects to
    (reusing the existing ``WS /api/sessions/{task_key}/attach`` proxy).

    Raises :class:`SessionNotFoundError` (unknown pipeline) or
    :class:`PipelineBusyError` (drive requested while a bot task holds the lease).
    """
    resolved_repo = _resolve_pipeline_repo(base_dir, pipeline_key, repo)

    if mode == INTERROGATE_OBSERVE:
        return _observe_pipeline(base_dir, resolved_repo, pipeline_key)
    if mode == INTERROGATE_DRIVE:
        return _drive_pipeline(base_dir, resolved_repo, pipeline_key, resume_fn)
    raise ValueError(f"mode must be {INTERROGATE_OBSERVE!r} or {INTERROGATE_DRIVE!r}")


def _observe_pipeline(base_dir: Path, repo: str, pipeline_key: str) -> dict:
    """Read-only observe: no lease, no process. Reuse a live attach if present."""
    git = _git_for_repo(base_dir, repo)
    transcript = session_resume.observe_transcript_path(base_dir, git, repo, pipeline_key)
    record = session_registry.find_pipeline_session(base_dir, repo, pipeline_key)
    # If a task is actively running, its bridge socket exists — observe can reuse
    # the live attach socket (read-only, the bot holds the mic) instead of tailing.
    attach_url = None
    if record is not None and record.socket and Path(record.socket).exists():
        attach_url = f"/api/sessions/{record.task_key}/attach"
    return {
        "mode": INTERROGATE_OBSERVE,
        "pipeline_key": pipeline_key,
        "repo": repo,
        "lease": False,
        "transcript": str(transcript),
        "attach_url": attach_url,
    }


def _drive_pipeline(base_dir: Path, repo: str, pipeline_key: str, resume_fn) -> dict:
    """Drive: take the pipeline lease, resume into a PTY, return the attach URL."""
    resume_fn = session_resume.resume_session if resume_fn is None else resume_fn
    if not pipeline_lease.acquire_pipeline_lease(
        base_dir, repo, pipeline_key, holder=pipeline_lease.HOLDER_DRIVE,
    ):
        held = pipeline_lease.read_pipeline_lease(base_dir, repo, pipeline_key)
        holder = held.holder if held else "another task"
        raise PipelineBusyError(
            f"pipeline {pipeline_key!r} is held by {holder}; cannot drive",
        )
    try:
        git = _git_for_repo(base_dir, repo)
        resumed = resume_fn(base_dir, git, repo, pipeline_key)
        # Track the live session and build the response *inside* the try so a
        # failure storing the session (or reading its coordinates) still releases
        # the lease — otherwise the pipeline would wedge holding a useless lease.
        _DRIVE_SESSIONS[(repo, pipeline_key)] = resumed
        return {
            "mode": INTERROGATE_DRIVE,
            "pipeline_key": pipeline_key,
            "repo": repo,
            "lease": True,
            "attach_url": f"/api/sessions/{resumed.coordinates.task_key}/attach",
        }
    except Exception:
        # Resume failed — never leave the lease dangling, or the pipeline wedges.
        pipeline_lease.release_pipeline_lease(
            base_dir, repo, pipeline_key, holder=pipeline_lease.HOLDER_DRIVE,
        )
        raise


def stop_drive(base_dir: Path, pipeline_key: str, *, repo: str | None = None) -> dict:
    """Tear down a live drive session and release its pipeline lease.

    Idempotent: stopping a pipeline with no live drive still releases any lease
    this process holds and reports ``stopped: False``.
    """
    resolved_repo = _resolve_pipeline_repo(base_dir, pipeline_key, repo)
    resumed = _DRIVE_SESSIONS.pop((resolved_repo, pipeline_key), None)
    stopped = False
    if resumed is not None:
        try:
            resumed.close()
        except Exception:
            pass
        stopped = True
    pipeline_lease.release_pipeline_lease(
        base_dir, resolved_repo, pipeline_key, holder=pipeline_lease.HOLDER_DRIVE,
    )
    return {"pipeline_key": pipeline_key, "repo": resolved_repo, "stopped": stopped}


def observe_jsonl_path(base_dir: Path, task_key: str) -> Path | None:
    """Return the JSONL transcript path the observe surface should tail (#202).

    Resolves *task_key* against the registry and computes
    ``jsonl_path_for(cwd, session_id)`` from its recorded ``cwd`` + ``session_id``
    (see :mod:`loony_dev.session`). Returns ``None`` when no session matches or
    the entry predates #202 (no ``cwd``/``session_id``), so it isn't observable.
    The path may not exist yet (a registered session whose first turn has not
    written the transcript); the tailer waits for it to appear.
    """
    from loony_dev.session import jsonl_path_for

    session = session_registry.find_session(base_dir, task_key)
    if session is None or not session.cwd or not session.session_id:
        return None
    return jsonl_path_for(Path(session.cwd), session.session_id)


def inject_turn(base_dir: Path, task_key: str, prompt: str) -> dict:
    """Enqueue an operator-injected turn for *task_key*'s session.

    Raises :class:`SessionNotFoundError` when no session matches. Returns a small
    status dict naming the queued file and its provenance.
    """
    session = session_registry.find_session(base_dir, task_key)
    if session is None:
        raise SessionNotFoundError(f"no session for task {task_key!r}")
    path = session_registry.enqueue_injection(
        session.dir, prompt, source=session_registry.SOURCE_OPERATOR,
    )
    return {
        "task_key": task_key,
        "repo": session.repo,
        "source": session_registry.SOURCE_OPERATOR,
        "queued": path.name,
    }


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
# Per-pipeline log tail / list / activity (issue #220)
#
# A worker now writes a second, per-pipeline log (keyed by ``issue-N`` / ``pr-P``)
# alongside the universal worker log — see :mod:`loony_dev.pipeline_log`. The path
# is computed forward only (slug the key); the raw key is recovered from a
# ``<slug>.key`` sidecar, never by reversing the irreversible slug.
# ---------------------------------------------------------------------------

def _safe_pipeline_log_path(
    base_dir: Path, owner: str, repo: str, pipeline_key: str
) -> Path:
    """Resolve a pipeline log path for ``owner/repo``, rejecting traversal.

    Mirrors :func:`_safe_repo_log_path`: rejects any segment (including
    *pipeline_key*) containing a path separator, ``.``/``..``, or NUL, then
    confirms the forward-computed path stays within the repo's ``pipelines/`` dir.
    """
    for segment in (owner, repo, pipeline_key):
        if not segment or segment in (".", "..") or "/" in segment or "\\" in segment or "\x00" in segment:
            raise LogNotFoundError(f"invalid path segment: {segment!r}")

    pipelines_root = pipeline_log.pipeline_logs_dir(base_dir, owner, repo).resolve()
    candidate = pipeline_log.pipeline_log_path(base_dir, owner, repo, pipeline_key).resolve()
    if pipelines_root not in candidate.parents:
        raise LogNotFoundError("resolved path escapes pipelines directory")
    return candidate


def tail_pipeline_log(
    base_dir: Path, owner: str, repo: str, pipeline_key: str, lines: int
) -> list[str]:
    """Return up to the last *lines* lines of a pipeline's log.

    Raises :class:`LogNotFoundError` for invalid segments or a missing log file.
    Keeps only the tail in a bounded ``deque`` so memory stays proportional to
    *lines* rather than file size (mirrors :func:`tail_log`).
    """
    log_path = _safe_pipeline_log_path(base_dir, owner, repo, pipeline_key)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            tail: deque[str] = deque(fh, maxlen=max(lines, 0))
    except FileNotFoundError as exc:
        raise LogNotFoundError(
            f"no pipeline log for {owner}/{repo}:{pipeline_key}"
        ) from exc
    return [line.rstrip("\n") for line in tail]


def list_pipeline_logs(base_dir: Path, owner: str, repo: str) -> list[str]:
    """Return the raw pipeline keys that have a log under ``owner/repo``.

    Enumerates the ``pipelines/<slug>.key`` sidecars (the forward-only contract)
    rather than reversing a ``*.log`` stem, which the irreversible slug makes
    impossible. A log whose sidecar is missing is skipped defensively. Returns
    keys sorted for a stable scope-picker order.
    """
    pipelines_dir = pipeline_log.pipeline_logs_dir(base_dir, owner, repo)
    if not pipelines_dir.is_dir():
        return []
    keys: list[str] = []
    for sidecar in sorted(pipelines_dir.glob("*.key")):
        try:
            key = sidecar.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        # Only surface a key whose log file actually exists (the sidecar is
        # written at the same moment, but stay defensive against partial state).
        if key and sidecar.with_suffix(".log").exists():
            keys.append(key)
    return keys


def pipeline_activity(
    base_dir: Path, owner: str, repo: str, pipeline_key: str, lines: int
) -> list[dict]:
    """Tail the pipeline's structured event log into activity-timeline events (#269).

    Reads the **structured event store** (``execution_state.tail_events``) the
    #267 substrate writes — no regex-parsing of the freeform ``.log``. Each event
    is returned as ``{ts, actor, type, what, target, state_tone}`` (oldest→newest,
    malformed lines skipped by the substrate). ``actor`` is the event's
    config-resolved attribution, not a message/logger heuristic. This is the
    structured feed the #225 Activity timeline consumes.

    Never raises: a pipeline with no event log yields ``[]`` (the substrate's
    read-side guarantee), so the old log-missing 404 path no longer fires.
    """
    events = execution_state.tail_events(base_dir, f"{owner}/{repo}", pipeline_key, lines)
    return [e.to_dict() for e in events]


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
