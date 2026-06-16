"""Resume a parked pipeline's Claude session into a fresh PTY (issue #199).

A pipeline's Claude session is a durable on-disk artifact — a deterministic id
(``session_id_for(repo, session_key)``) plus the transcript JSONL — so once the
pipeline parks (waiting on plan approval, CI, or review) there is no live process
to attach to, but the conversation can still be *resumed* from disk on demand.

The sharp edge is the same one #181's incident (#177) hit: the transcript lives
under a project slug derived from the **cwd**, so ``claude --resume <id>`` in any
*other* cwd cannot see it and :class:`ClaudeSession` readiness times out. This
module therefore resumes in the **exact cwd the session last wrote to** — the
``worktree_path`` recorded in :mod:`loony_dev.session_registry` — recreating that
worktree at its canonical path first if it was torn down.

Two entry points mirror the issue's two modes:

* :func:`resume_session` — **drive**: recreate the worktree if needed, open a
  fresh PTY with ``--resume``, and serve it over the attach bridge. The caller is
  responsible for holding the pipeline lease (see
  :mod:`loony_dev.pipeline_lease`) around this — a drive mutates the session.
* :func:`observe_transcript_path` — **observe**: resolve the on-disk transcript
  path for a read-only tail. Takes no lease and starts no process.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from loony_dev import session_registry
from loony_dev.agents.claude_session import (
    ClaudeSession,
    jsonl_path_for,
    trust_directory,
)
from loony_dev.agents.session_bridge import SessionBridge, publish_session
from loony_dev.git import GitRepo
from loony_dev.session import session_id_for

logger = logging.getLogger(__name__)


class SessionResumeError(Exception):
    """Raised when a parked pipeline cannot be resumed (e.g. no branch to recreate)."""


@dataclass(frozen=True)
class PipelineCoordinates:
    """Everything needed to resume/observe a pipeline's session."""

    session_id: str
    worktree_path: Path
    branch: str | None
    task_key: str
    pipeline_key: str


@dataclass
class ResumedSession:
    """A live, resumed :class:`ClaudeSession` plus its serving bridge."""

    session: ClaudeSession
    bridge: SessionBridge
    coordinates: PipelineCoordinates

    def close(self) -> None:
        """Tear down the bridge and the resumed PTY (best-effort)."""
        try:
            self.bridge.close()
        finally:
            self.session.close()


def pipeline_session_key(pipeline_key: str) -> str:
    """Map a pipeline key (``issue-N`` / ``pr-P``) to its session key.

    Session keys use ``:`` (``issue:N``); pipeline/worktree keys use ``-``
    (``issue-N``). They differ only in that separator, so the first ``-`` becomes
    ``:`` to recover the session key the agent used for ``--resume`` continuity.
    """
    return pipeline_key.replace("-", ":", 1)


def canonical_worktree_path(git: GitRepo, repo: str, pipeline_key: str) -> Path:
    """Return the deterministic worktree path for ``repo``'s *pipeline_key*.

    Mirrors ``Orchestrator.worktree_root`` —
    ``<work_dir>/.worktrees/<owner>/<repo>/<pipeline_key>`` — so a torn-down
    worktree is recreated exactly where the scheduler would have put it.
    """
    owner, name = repo.split("/", 1)
    return git.work_dir / ".worktrees" / owner / name / pipeline_key


def resolve_pipeline_coordinates(
    base_dir: Path, git: GitRepo, repo: str, pipeline_key: str,
) -> PipelineCoordinates:
    """Resolve the session id, cwd, and branch for *pipeline_key*.

    Prefers the recorded registry entry (the authoritative ``session_id →
    worktree_path`` map). Falls back to recomputing everything from the
    deterministic keys when no record exists — e.g. a pipeline that parked before
    this feature shipped — so resume still works, just without a recorded cwd to
    cross-check.
    """
    record = session_registry.find_pipeline_session(base_dir, repo, pipeline_key)
    if record is not None and record.session_id and record.worktree_path:
        return PipelineCoordinates(
            session_id=record.session_id,
            worktree_path=Path(record.worktree_path),
            branch=record.branch,
            task_key=record.task_key,
            pipeline_key=pipeline_key,
        )
    session_id = session_id_for(repo, pipeline_session_key(pipeline_key))
    branch = git.find_branch_with_prefix(f"{pipeline_key}/")
    return PipelineCoordinates(
        session_id=session_id,
        worktree_path=canonical_worktree_path(git, repo, pipeline_key),
        branch=branch,
        task_key=pipeline_key,
        pipeline_key=pipeline_key,
    )


def ensure_worktree(git: GitRepo, coords: PipelineCoordinates) -> Path:
    """Ensure the pipeline's worktree exists at its recorded cwd; return it.

    A no-op when the worktree is still on disk (the common case once #198 retains
    it). When torn down, it is recreated on its feature branch at the canonical
    path so the resumed session lands in the *same* cwd it last wrote to (the
    #177 regression guard). The path is then pre-trusted so interactive
    ``claude`` does not block on the folder-trust dialog (#178).
    """
    path = coords.worktree_path
    if not path.exists():
        branch = coords.branch or git.find_branch_with_prefix(f"{coords.pipeline_key}/")
        if not branch:
            raise SessionResumeError(
                f"cannot resume {coords.pipeline_key}: worktree {path} is gone and no "
                f"feature branch was found to recreate it",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Recreating worktree for %s at %s (branch=%s)", coords.pipeline_key, path, branch)
        git.create_worktree(branch=branch, path=path, base=None)
    trust_directory(path)
    return path


def resume_session(
    base_dir: Path,
    git: GitRepo,
    repo: str,
    pipeline_key: str,
    *,
    backstop_seconds: float | None = None,
) -> ResumedSession:
    """Resume *pipeline_key*'s parked session into a fresh PTY and serve it.

    Steps (drive mode): resolve coordinates → ensure the canonical worktree
    exists → open ``claude --resume <id>`` in that exact cwd → publish the PTY
    over the attach bridge so the existing dashboard proxy can connect. The
    caller MUST hold the pipeline lease around this (a drive mutates the session).
    """
    coords = resolve_pipeline_coordinates(base_dir, git, repo, pipeline_key)
    worktree_path = ensure_worktree(git, coords)

    kwargs: dict = {
        "session_id": coords.session_id,
        "extra_args": ["--resume", coords.session_id],
    }
    if backstop_seconds is not None:
        kwargs["backstop_seconds"] = backstop_seconds
    session = ClaudeSession(cwd=worktree_path, **kwargs)
    session.open()
    bridge = publish_session(
        session,
        base_dir,
        repo,
        coords.task_key,
        worktree_path=str(worktree_path),
        pipeline_key=pipeline_key,
        branch=coords.branch,
        status="driving",
    )
    return ResumedSession(session=session, bridge=bridge, coordinates=coords)


def observe_transcript_path(
    base_dir: Path, git: GitRepo, repo: str, pipeline_key: str,
) -> Path:
    """Return the JSONL transcript path for a read-only observe of *pipeline_key*.

    Read-only: takes no lease and starts no process. The path is computed from
    the *recorded* cwd (``jsonl_path_for(worktree_path, session_id)``), so an
    observer reads the same transcript the session actually wrote — never a path
    derived from the wrong cwd (the #177 class). Does not require the worktree to
    exist on disk: the transcript lives under the Claude config dir, keyed only by
    the cwd slug, so it is readable even after the worktree is torn down.
    """
    coords = resolve_pipeline_coordinates(base_dir, git, repo, pipeline_key)
    return jsonl_path_for(coords.worktree_path, coords.session_id)
