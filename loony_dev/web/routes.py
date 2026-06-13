"""Thin FastAPI handlers delegating to :mod:`loony_dev.web.services`.

The router is built by :func:`create_api_router` against a fixed ``base_dir`` so
the application factory can point it at any directory (e.g. a temp tree in tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from dataclasses import asdict
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse

from loony_dev.agents.session_bridge import FRAME_CONTROL, FRAME_DATA, encode_frame
from loony_dev.web import entries, services, streaming

# Frame header shared with the worker-side bridge: 1-byte type + 4-byte BE length.
_FRAME_HEADER = struct.Struct(">BI")

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

    @router.get("/task-sessions")
    def get_task_sessions() -> list[dict]:
        """List per-task worker sessions the dashboard can attach to / steer."""
        return [asdict(s) for s in services.list_task_sessions(base_dir)]

    @router.post("/sessions/{task_key}/inject")
    async def inject_turn(task_key: str, request: Request) -> dict:
        """Enqueue a one-shot operator-steered turn (``source: "operator"``).

        A simpler alternative to the live terminal: the orchestrator runs the
        queued prompt as the session's next turn. Body is JSON ``{"prompt": ...}``.
        """
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="body must be JSON") from exc
        prompt = body.get("prompt") if isinstance(body, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(status_code=400, detail="'prompt' must be a non-empty string")
        try:
            return services.inject_turn(base_dir, task_key, prompt)
        except services.SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.websocket("/sessions/{task_key}/attach")
    async def attach_session(websocket: WebSocket, task_key: str) -> None:
        """Bridge a websocket to the worker-owned ``ClaudeSession`` PTY socket.

        The dashboard speaks binary frames (raw keystrokes / PTY bytes) and text
        frames (JSON control: ``resize`` outbound, ``mic`` status inbound). This
        handler is a transparent proxy onto the per-task Unix-domain socket; the
        worker side enforces the read-only-while-bot-has-the-mic contract.
        """
        await _attach_session(base_dir, websocket, task_key)

    def _state_snapshot() -> dict:
        """Gather the consolidated dashboard state in one shot.

        Mirrors the per-resource GET endpoints so the streamed payload and the
        polling fallback never drift apart.
        """
        return {
            "workers": [asdict(w) for w in services.list_workers(base_dir)],
            "worktrees": [asdict(w) for w in services.list_worktrees(base_dir)],
            "sessions": [asdict(s) for s in services.list_sessions(base_dir)],
            "task_sessions": [
                asdict(s) for s in services.list_task_sessions(base_dir)
            ],
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


async def _attach_session(base_dir: Path, websocket: WebSocket, task_key: str) -> None:
    """Proxy *websocket* onto the per-task PTY bridge Unix socket.

    Resolves *task_key* against the worker-published registry, dials the bridge
    socket, then runs two pumps until either side closes — translating the
    bridge's binary framing to/from websocket binary (PTY bytes) and text (JSON
    control) messages. Both the socket and the websocket are released on exit, so
    repeated attach/detach cycles leak neither fds nor PTY backlog handles.
    """
    session = services.find_task_session(base_dir, task_key)
    if session is None or not session.socket:
        # Reject the handshake (close before accept) with a distinct code the
        # client can tell apart from a generic network failure.
        await websocket.close(code=4404, reason="no such session")
        return
    try:
        reader, writer = await asyncio.open_unix_connection(session.socket)
    except OSError:
        await websocket.close(code=4503, reason="session bridge unavailable")
        return

    await websocket.accept()

    async def socket_to_ws() -> None:
        while True:
            try:
                head = await reader.readexactly(_FRAME_HEADER.size)
            except (asyncio.IncompleteReadError, OSError):
                break
            ftype, length = _FRAME_HEADER.unpack(head)
            try:
                payload = await reader.readexactly(length) if length else b""
            except (asyncio.IncompleteReadError, OSError):
                break
            try:
                if ftype == FRAME_CONTROL:
                    await websocket.send_text(payload.decode("utf-8", "replace"))
                else:
                    await websocket.send_bytes(payload)
            except (WebSocketDisconnect, RuntimeError):
                break

    async def ws_to_socket() -> None:
        while True:
            try:
                message = await websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                break
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            try:
                if data is not None:
                    writer.write(encode_frame(FRAME_DATA, data))
                    await writer.drain()
                elif text is not None:
                    writer.write(encode_frame(FRAME_CONTROL, text.encode("utf-8")))
                    await writer.drain()
            except OSError:
                break

    s2w = asyncio.create_task(socket_to_ws())
    w2s = asyncio.create_task(ws_to_socket())
    try:
        await asyncio.wait({s2w, w2s}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (s2w, w2s):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        writer.close()
        with contextlib.suppress(OSError, Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()


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
