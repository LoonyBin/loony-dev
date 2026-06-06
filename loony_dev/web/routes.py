"""Thin FastAPI handlers delegating to :mod:`loony_dev.web.services`.

The router is built by :func:`create_api_router` against a fixed ``base_dir`` so
the application factory can point it at any directory (e.g. a temp tree in tests).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from loony_dev.web import services

# Default and maximum tail sizes mirror the TUI defaults (tui.py / cli.py).
DEFAULT_TAIL_LINES = 100
MAX_TAIL_LINES = 5000


def create_api_router(base_dir: Path, tail_lines: int = DEFAULT_TAIL_LINES) -> APIRouter:
    """Return an ``/api`` router bound to *base_dir*.

    *tail_lines* is the default number of log lines returned by the log-tail
    endpoint when a request omits ``?lines=``.
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

    return router
