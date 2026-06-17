"""FastAPI application factory for the read-only web dashboard.

The dashboard runs as a separate process from the supervisor and derives all
state from the supervisor's on-disk file layout (see
:mod:`loony_dev.web.services`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from loony_dev.web import services
from loony_dev.web.routes import create_api_router

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# How often the optional auto-interrupt monitor recomputes the stuck set, and
# how long it waits before ESC-ing the same session again (so a turn that takes
# a moment to abort is not hammered with repeated interrupts).
AUTO_INTERRUPT_POLL_INTERVAL = 5.0
AUTO_INTERRUPT_COOLDOWN = 60.0


async def _auto_interrupt_loop(
    base_dir: Path,
    *,
    stuck_after_seconds: float,
    activity_sample_seconds: float,
    auto_interrupt_after_seconds: float,
) -> None:
    """Periodically ESC sessions whose stuck turn exceeds the auto threshold.

    Runs only when ``auto_interrupt_after_seconds > 0``. A single instance lives
    in the (singleton) dashboard process, so there is no cross-process race; a
    per-session cooldown avoids re-interrupting a turn that is still aborting.
    SIGKILL is never auto-escalated — it stays a manual action.
    """
    loop = asyncio.get_running_loop()
    last_fired: dict[str, float] = {}
    while True:
        try:
            await asyncio.sleep(AUTO_INTERRUPT_POLL_INTERVAL)
            # Build the candidate set at the auto-interrupt age so turns younger
            # than ``stuck_after_seconds`` still qualify (e.g. --stuck-after 300
            # --auto-interrupt-after 60 must catch a 2-minute blocked turn).
            effective_threshold = min(stuck_after_seconds, auto_interrupt_after_seconds)
            stuck = await asyncio.to_thread(
                services.list_stuck,
                base_dir,
                threshold_seconds=effective_threshold,
                activity_sample_seconds=activity_sample_seconds,
            )
            candidates = services.auto_interrupt_candidates(
                stuck, auto_interrupt_after_seconds=auto_interrupt_after_seconds
            )
            now = loop.time()
            for session_id in candidates:
                if now - last_fired.get(session_id, float("-inf")) < AUTO_INTERRUPT_COOLDOWN:
                    continue
                last_fired[session_id] = now
                try:
                    result = await asyncio.to_thread(
                        services.interrupt_session, base_dir, session_id
                    )
                    logger.info(
                        "Auto-interrupted session %s: %s",
                        session_id, result.get("detail"),
                    )
                except services.SessionNotFoundError:
                    last_fired.pop(session_id, None)  # vanished; allow a later retry
                except services.SessionControlError as exc:
                    logger.warning("Auto-interrupt of %s failed: %s", session_id, exc)
        except asyncio.CancelledError:
            break
        except Exception:  # never let one bad pass kill the monitor
            logger.exception("Auto-interrupt monitor iteration failed")


def create_app(
    base_dir: Path,
    supervisor_log: Path | None = None,
    tail_lines: int = 100,
    claude_home: Path | None = None,
    *,
    stuck_after_seconds: float = 300,
    activity_sample_seconds: float = 0.3,
    kill_grace_seconds: float = 5.0,
    auto_interrupt_after_seconds: float = 0.0,
    github_state_enabled: bool = True,
    github_refresh_seconds: float = 60.0,
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
        stuck_after_seconds: Age a blocked Claude descendant must reach before it
            is considered stuck.
        activity_sample_seconds: Gap between the two CPU/IO samples used to decide
            a Claude subtree is idle.
        kill_grace_seconds: Grace period after SIGTERM before SIGKILL escalation.
        auto_interrupt_after_seconds: When > 0, a background monitor ESC-interrupts
            a session whose Claude turn has been stuck this long. Disabled (0) by
            default; SIGKILL is never auto-escalated.
        github_state_enabled: When True (default), the snapshot is enriched with
            partial GitHub state (#219) — per-pipeline title + label/PR stage and
            per-repo open counts. Disable to keep the dashboard filesystem-only
            (e.g. where ``gh`` is unauthenticated); the frontend then keeps its
            placeholders.
        github_refresh_seconds: TTL of the cached GitHub fetch; the 2s SSE poll
            reads the cache, so ``gh`` runs at most once per this many seconds.
    """
    base_dir = Path(base_dir)
    claude_home = Path(claude_home) if claude_home is not None else Path.home() / ".claude"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task | None = None
        if auto_interrupt_after_seconds and auto_interrupt_after_seconds > 0:
            task = asyncio.create_task(
                _auto_interrupt_loop(
                    base_dir,
                    stuck_after_seconds=stuck_after_seconds,
                    activity_sample_seconds=activity_sample_seconds,
                    auto_interrupt_after_seconds=auto_interrupt_after_seconds,
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="loony-dev dashboard",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.base_dir = base_dir
    app.state.supervisor_log = supervisor_log
    app.state.tail_lines = tail_lines
    app.state.claude_home = claude_home
    app.state.stuck_after_seconds = stuck_after_seconds
    app.state.activity_sample_seconds = activity_sample_seconds
    app.state.kill_grace_seconds = kill_grace_seconds
    app.state.auto_interrupt_after_seconds = auto_interrupt_after_seconds
    app.state.github_state_enabled = github_state_enabled
    app.state.github_refresh_seconds = github_refresh_seconds

    app.include_router(
        create_api_router(
            base_dir,
            tail_lines=tail_lines,
            claude_home=claude_home,
            stuck_after_seconds=stuck_after_seconds,
            activity_sample_seconds=activity_sample_seconds,
            kill_grace_seconds=kill_grace_seconds,
            github_state_enabled=github_state_enabled,
            github_refresh_seconds=github_refresh_seconds,
        )
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app
