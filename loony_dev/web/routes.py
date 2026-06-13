"""Thin FastAPI handlers delegating to :mod:`loony_dev.web.services`.

The router is built by :func:`create_api_router` against a fixed ``base_dir`` so
the application factory can point it at any directory (e.g. a temp tree in tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse

from loony_dev.web import entries, services, streaming

# Default and maximum tail sizes mirror the TUI defaults (tui.py / cli.py).
DEFAULT_TAIL_LINES = 100
MAX_TAIL_LINES = 5000

# Seconds between SSE heartbeat comments: keeps proxies from idling the
# connection out and lets the server notice a vanished client.
SSE_HEARTBEAT_INTERVAL = 15.0

# Seconds between server-side state recomputes for the consolidated /events
# stream. The snapshot is cheap to gather, so a short poll + emit-on-change is
# enough to feel real-time without a change-notification bus (issue #155).
SSE_STATE_INTERVAL = 2.0

# Stuck-detection defaults (issue #132) — mirrored by the app factory / CLI.
DEFAULT_STUCK_AFTER_SECONDS = 300
DEFAULT_ACTIVITY_SAMPLE_SECONDS = 0.3
DEFAULT_KILL_GRACE_SECONDS = 5.0


def _format_sse(line: str) -> str:
    """Encode *line* as an SSE ``data:`` event (multi-line-safe)."""
    body = "".join(f"data: {part}\n" for part in line.split("\n"))
    return f"{body}\n"


def create_api_router(
    base_dir: Path,
    tail_lines: int = DEFAULT_TAIL_LINES,
    claude_home: Path | None = None,
    *,
    stuck_after_seconds: float = DEFAULT_STUCK_AFTER_SECONDS,
    activity_sample_seconds: float = DEFAULT_ACTIVITY_SAMPLE_SECONDS,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> APIRouter:
    """Return an ``/api`` router bound to *base_dir*.

    *tail_lines* is the default number of log lines returned by the log-tail
    endpoint when a request omits ``?lines=``. *claude_home* is the global
    ``~/.claude`` root used by the skills/commands endpoints (injectable so tests
    can point it at a temp tree); it defaults to ``~/.claude``. The remaining
    keyword arguments tune the stuck-process detector and the kill endpoint's
    SIGKILL escalation.
    """
    default_tail_lines = max(1, min(tail_lines, MAX_TAIL_LINES))
    global_root = Path(claude_home) if claude_home is not None else Path.home() / ".claude"
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

    def _state_snapshot() -> dict:
        """Gather the consolidated dashboard state in one shot.

        Mirrors the four per-resource GET endpoints so the streamed payload and
        the polling fallback never drift apart.
        """
        return {
            "workers": [asdict(w) for w in services.list_workers(base_dir)],
            "worktrees": [asdict(w) for w in services.list_worktrees(base_dir)],
            "sessions": [asdict(s) for s in services.list_sessions(base_dir)],
            "stuck": [
                asdict(s)
                for s in services.list_stuck(
                    base_dir,
                    threshold_seconds=stuck_after_seconds,
                    activity_sample_seconds=activity_sample_seconds,
                )
            ],
        }

    @router.get("/events")
    async def stream_events(request: Request) -> StreamingResponse:
        """Push a consolidated state snapshot, then updates as state changes.

        Replaces the frontend's 5s poll of the four per-resource endpoints. An
        initial snapshot is emitted on connect; thereafter the state is recomputed
        every ``SSE_STATE_INTERVAL`` seconds and re-emitted only when it differs.
        A heartbeat comment is sent during idle periods so proxies don't drop the
        connection, and ``request.is_disconnected()`` reaps vanished clients —
        the same teardown contract as the log-tail stream.
        """

        async def event_stream():
            loop = asyncio.get_running_loop()
            last_payload: str | None = None
            last_sent = loop.time()
            while True:
                if await request.is_disconnected():
                    break
                # The snapshot reads /proc and the worktree tree; run it off the
                # event loop so a slow gather never stalls other connections.
                snapshot = await asyncio.to_thread(_state_snapshot)
                payload = json.dumps(snapshot, sort_keys=True)
                now = loop.time()
                if payload != last_payload:
                    last_payload = payload
                    last_sent = now
                    yield _format_sse(payload)
                elif now - last_sent >= SSE_HEARTBEAT_INTERVAL:
                    last_sent = now
                    yield ": heartbeat\n\n"
                await asyncio.sleep(SSE_STATE_INTERVAL)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers=headers
        )

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

    _register_entry_routes(router, "skills", base_dir=base_dir, global_root=global_root)
    _register_entry_routes(router, "commands", base_dir=base_dir, global_root=global_root)

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


def _register_entry_routes(router: APIRouter, kind: str, *, base_dir: Path,
                           global_root: Path) -> None:
    """Register CRUD endpoints for one entry *kind* ("skills" / "commands").

    Two concrete prefixes are generated (``/api/skills``, ``/api/commands``) so
    ``{name}`` stays the only free path segment and OpenAPI/validation stays
    clean. ``EntryError`` maps to 400 and ``EntryNotFoundError`` to 404, mirroring
    the log-tail ``LogNotFoundError`` → 404 pattern.
    """
    scope_kwargs = lambda scope, owner, repo: dict(  # noqa: E731
        global_root=global_root, base_dir=base_dir, scope=scope, owner=owner, repo=repo,
    )

    @router.get(f"/{kind}")
    def list_kind(
        scope: str = "global",
        owner: str | None = None,
        repo: str | None = None,
    ) -> list[dict]:
        try:
            views = entries.list_entries(kind, **scope_kwargs(scope, owner, repo))
        except entries.EntryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [asdict(v) for v in views]

    @router.get(f"/{kind}/{{name}}")
    def read_kind(
        name: str,
        scope: str = "global",
        owner: str | None = None,
        repo: str | None = None,
    ) -> dict:
        try:
            content = entries.read_entry(kind, name, **scope_kwargs(scope, owner, repo))
        except entries.EntryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except entries.EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"name": name, "content": content}

    @router.put(f"/{kind}/{{name}}")
    async def write_kind(
        name: str,
        request: Request,
        scope: str = "global",
        owner: str | None = None,
        repo: str | None = None,
    ) -> dict:
        # Body is raw markdown (not a JSON wrapper) so frontmatter round-trips
        # verbatim — matches "paste a markdown file".
        try:
            content = (await request.body()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="Request body is not valid UTF-8"
            ) from exc
        try:
            view = entries.write_entry(kind, name, content, **scope_kwargs(scope, owner, repo))
        except entries.EntryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(view)

    @router.delete(f"/{kind}/{{name}}", status_code=204)
    def delete_kind(
        name: str,
        scope: str = "global",
        owner: str | None = None,
        repo: str | None = None,
    ) -> Response:
        try:
            entries.delete_entry(kind, name, **scope_kwargs(scope, owner, repo))
        except entries.EntryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except entries.EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)
