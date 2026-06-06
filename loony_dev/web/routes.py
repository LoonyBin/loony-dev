"""Thin FastAPI handlers delegating to :mod:`loony_dev.web.services`.

The router is built by :func:`create_api_router` against a fixed ``base_dir`` so
the application factory can point it at any directory (e.g. a temp tree in tests).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi import Path as PathParam

from loony_dev.web import services

# Default and maximum tail sizes mirror the TUI defaults (tui.py / cli.py).
DEFAULT_TAIL_LINES = 100
MAX_TAIL_LINES = 5000

# Stuck-detection defaults (issue #132) — mirrored by the app factory / CLI.
DEFAULT_STUCK_AFTER_SECONDS = 300
DEFAULT_ACTIVITY_SAMPLE_SECONDS = 0.3
DEFAULT_KILL_GRACE_SECONDS = 5.0


def create_api_router(
    base_dir: Path,
    tail_lines: int = DEFAULT_TAIL_LINES,
    *,
    stuck_after_seconds: float = DEFAULT_STUCK_AFTER_SECONDS,
    activity_sample_seconds: float = DEFAULT_ACTIVITY_SAMPLE_SECONDS,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> APIRouter:
    """Return an ``/api`` router bound to *base_dir*.

    *tail_lines* is the default number of log lines returned by the log-tail
    endpoint when a request omits ``?lines=``. The remaining keyword arguments
    tune the stuck-process detector and the kill endpoint's SIGKILL escalation.
    """
    default_tail_lines = max(1, min(tail_lines, MAX_TAIL_LINES))
    router = APIRouter(prefix="/api")

    @router.get("/workers")
    def get_workers() -> list[dict]:
        return [asdict(w) for w in services.list_workers(base_dir)]

    @router.get("/worktrees")
    def get_worktrees() -> list[dict]:
        return [asdict(w) for w in services.list_worktrees(base_dir)]

    @router.get("/sessions")
    def get_sessions() -> list[dict]:
        return [asdict(s) for s in services.list_sessions(base_dir)]

    @router.get("/logs/{owner}/{repo}/tail")
    def get_log_tail(
        owner: str,
        repo: str,
        lines: int = Query(default_tail_lines, ge=1, le=MAX_TAIL_LINES),
    ) -> dict:
        try:
            tail = services.tail_log(base_dir, owner, repo, lines)
        except services.LogNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"repo": f"{owner}/{repo}", "lines": tail, "count": len(tail)}

    @router.get("/stuck")
    def get_stuck() -> list[dict]:
        return [
            asdict(s)
            for s in services.list_stuck(
                base_dir,
                threshold_seconds=stuck_after_seconds,
                activity_sample_seconds=activity_sample_seconds,
            )
        ]

    @router.post("/processes/{pid}/kill")
    def kill_process(
        background_tasks: BackgroundTasks,
        pid: int = PathParam(..., gt=1, description="PID of the descendant to terminate"),
    ) -> dict:
        try:
            status = services.kill_descendant(base_dir, pid, grace_seconds=kill_grace_seconds)
        except services.NotADescendantError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if status.get("alive"):
            background_tasks.add_task(
                services.escalate_kill,
                base_dir,
                pid,
                kill_grace_seconds,
                status.get("starttime"),
            )
        return status

    return router
