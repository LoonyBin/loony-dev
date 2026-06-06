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
    claude_home: Path | None = None,
) -> FastAPI:
    """Build a dashboard app reading state from *base_dir*.

    Args:
        base_dir: Supervisor base directory holding ``.logs/`` and repo checkouts.
        supervisor_log: Optional supervisor log path (currently informational;
            reserved for a future supervisor-log endpoint).
        tail_lines: Default number of log lines returned by the log-tail endpoint
            when a request does not specify ``?lines=``.
        claude_home: Global ``~/.claude`` root used by the skills/commands
            endpoints (injectable for tests); defaults to ``~/.claude``.
    """
    base_dir = Path(base_dir)
    claude_home = Path(claude_home) if claude_home is not None else Path.home() / ".claude"

    app = FastAPI(title="loony-dev dashboard", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.base_dir = base_dir
    app.state.supervisor_log = supervisor_log
    app.state.tail_lines = tail_lines
    app.state.claude_home = claude_home

    app.include_router(create_api_router(base_dir, tail_lines=tail_lines, claude_home=claude_home))

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app
