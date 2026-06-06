"""Thin FastAPI handlers delegating to :mod:`loony_dev.web.services`.

The router is built by :func:`create_api_router` against a fixed ``base_dir`` so
the application factory can point it at any directory (e.g. a temp tree in tests).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from loony_dev.web import services, streaming

# Default and maximum tail sizes mirror the TUI defaults (tui.py / cli.py).
DEFAULT_TAIL_LINES = 100
MAX_TAIL_LINES = 5000

# Seconds between SSE heartbeat comments: keeps proxies from idling the
# connection out and lets the server notice a vanished client.
SSE_HEARTBEAT_INTERVAL = 15.0


def _format_sse(line: str) -> str:
    """Encode *line* as an SSE ``data:`` event (multi-line-safe)."""
    body = "".join(f"data: {part}\n" for part in line.split("\n"))
    return f"{body}\n"


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

    @router.get("/logs/{owner}/{repo}/stream")
    async def stream_log(owner: str, repo: str, request: Request) -> StreamingResponse:
        # Validate the path (rejects traversal) and require the log to exist now,
        # mirroring the 404 behaviour of the /tail endpoint.
        try:
            log_path = services._safe_repo_log_path(base_dir, owner, repo)
        except services.LogNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not log_path.exists():
            raise HTTPException(status_code=404, detail=f"no log for {owner}/{repo}")

        async def event_stream():
            gen = streaming.tail_lines(log_path, backlog=default_tail_lines)
            queue: asyncio.Queue = asyncio.Queue()

            async def pump() -> None:
                try:
                    async for line in gen:
                        await queue.put(("line", line))
                finally:
                    await queue.put(("eof", None))

            task = asyncio.create_task(pump())
            try:
                while True:
                    try:
                        kind, payload = await asyncio.wait_for(
                            queue.get(), timeout=SSE_HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": heartbeat\n\n"
                        continue
                    if kind == "eof":
                        break
                    if await request.is_disconnected():
                        break
                    yield _format_sse(payload)
            finally:
                # Disconnect / cancellation lands here: stop the pump (which
                # closes the watcher via its finally) and release the generator.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await gen.aclose()

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers=headers
        )

    return router
