"""FastAPI application factory for the read-only web dashboard.

The dashboard is the first step toward replacing the Textual TUI. It runs as a
separate process from the supervisor and derives all state from the supervisor's
on-disk file layout (see :mod:`loony_dev.web.services`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from loony_dev.web.routes import create_api_router

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    base_dir: Path,
    supervisor_log: Path | None = None,
    tail_lines: int = 100,
    *,
    stuck_after_seconds: float = 300,
    activity_sample_seconds: float = 0.3,
    kill_grace_seconds: float = 5.0,
) -> FastAPI:
    """Build a dashboard app reading state from *base_dir*.

    Args:
        base_dir: Supervisor base directory holding ``.logs/`` and repo checkouts.
        supervisor_log: Optional supervisor log path (currently informational;
            reserved for a future supervisor-log endpoint).
        tail_lines: Default number of log lines returned by the log-tail endpoint
            when a request does not specify ``?lines=``.
        stuck_after_seconds: Age a blocked Claude descendant must reach before it
            is considered stuck.
        activity_sample_seconds: Gap between the two CPU/IO samples used to decide
            a Claude subtree is idle.
        kill_grace_seconds: Grace period after SIGTERM before SIGKILL escalation.
    """
    base_dir = Path(base_dir)

    app = FastAPI(title="loony-dev dashboard", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.base_dir = base_dir
    app.state.supervisor_log = supervisor_log
    app.state.tail_lines = tail_lines
    app.state.stuck_after_seconds = stuck_after_seconds
    app.state.activity_sample_seconds = activity_sample_seconds
    app.state.kill_grace_seconds = kill_grace_seconds

    app.include_router(
        create_api_router(
            base_dir,
            tail_lines=tail_lines,
            stuck_after_seconds=stuck_after_seconds,
            activity_sample_seconds=activity_sample_seconds,
            kill_grace_seconds=kill_grace_seconds,
        )
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app
