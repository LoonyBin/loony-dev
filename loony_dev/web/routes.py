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
from loony_dev.web import entries, services, streaming, transcript_stream

# Frame header shared with the worker-side bridge: 1-byte type + 4-byte BE length.
_FRAME_HEADER = struct.Struct(">BI")

# Default and maximum tail sizes mirror the `loony-dev web --tail-lines` default (cli.py).
DEFAULT_TAIL_LINES = 100
MAX_TAIL_LINES = 5000

# Seconds between SSE heartbeat comments: keeps proxies from idling the
# connection out and lets the server notice a vanished client. It also doubles as
# the consolidated /events stream's wake cadence — the FleetEventWatcher push is
# the primary signal (sub-second), and this is just the idle fallback (issue #270).
SSE_HEARTBEAT_INTERVAL = 15.0

# Stuck-detection defaults (issue #132) — mirrored by the app factory / CLI.
DEFAULT_STUCK_AFTER_SECONDS = 300
DEFAULT_KILL_GRACE_SECONDS = 5.0

# Partial-GitHub-state defaults (issue #219) — mirrored by the app factory / CLI.
DEFAULT_GITHUB_STATE_ENABLED = True
DEFAULT_GITHUB_REFRESH_SECONDS = 60.0


def _format_sse(line: str) -> str:
    """Encode *line* as an SSE ``data:`` event (multi-line-safe)."""
    body = "".join(f"data: {part}\n" for part in line.split("\n"))
    return f"{body}\n"


def _sse_tail_response(
    log_path: Path, request: Request, *, backlog: int
) -> StreamingResponse:
    """Stream a file's tail (backlog + appended lines) as Server-Sent Events.

    Shared by every log-stream route (worker-scope and per-pipeline, #220): a
    queue-based pump drains :func:`streaming.tail_lines` while the consumer loop
    emits each line, sends a heartbeat comment during idle gaps so proxies don't
    drop the connection, and reaps a vanished client via
    ``request.is_disconnected()``. On disconnect/cancellation the pump is
    cancelled and the underlying generator closed, releasing all descriptors.
    Callers validate (and 404) the path before calling this.
    """

    async def event_stream():
        gen = streaming.tail_lines(log_path, backlog=backlog)
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
            # Disconnect / cancellation lands here: stop the pump (which closes
            # the watcher via its finally) and release the generator.
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


def create_api_router(
    base_dir: Path,
    tail_lines: int = DEFAULT_TAIL_LINES,
    claude_home: Path | None = None,
    *,
    stuck_after_seconds: float = DEFAULT_STUCK_AFTER_SECONDS,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
    github_state_enabled: bool = DEFAULT_GITHUB_STATE_ENABLED,
    github_refresh_seconds: float = DEFAULT_GITHUB_REFRESH_SECONDS,
) -> APIRouter:
    """Return an ``/api`` router bound to *base_dir*.

    *tail_lines* is the default number of log lines returned by the log-tail
    endpoint when a request omits ``?lines=``. *claude_home* is the global
    ``~/.claude`` root used by the skills/commands endpoints (injectable so tests
    can point it at a temp tree); it defaults to ``~/.claude``. The remaining
    keyword arguments tune the heartbeat-derived stuck threshold and the kill
    endpoint's SIGKILL escalation, plus the partial-GitHub-state enrichment
    (#219): *github_state_enabled* toggles the per-snapshot ``gh`` fetch, and
    *github_refresh_seconds* is its TTL (the event-driven SSE reads the cache).
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

    def _pipelines_payload(pipelines) -> list[dict]:
        """Serialise GitHub pipelines, merging each one's live-state overlay (#269).

        The single source of the pipeline payload shape: the ``/api/pipelines``
        GET and the ``/api/events`` SSE snapshot both build their ``pipelines``
        list through here, so the polling and streamed shapes never drift (the
        ``_state_snapshot`` parity contract). Each entry is the GitHub-derived
        dict plus a nested ``live`` overlay (or ``null`` — see :func:`get_pipelines`).
        """
        out = []
        for p in pipelines:
            d = asdict(p)
            d["live"] = services.pipeline_live_overlay(base_dir, p.repo, p.pipeline_key)
            out.append(d)
        return out

    @router.get("/pipelines")
    def get_pipelines() -> list[dict]:
        """Per-pipeline state: GitHub facets + the live-state overlay (#219/#269).

        Each entry carries the GitHub-derived ``title`` / label-or-PR ``stage`` /
        ``labels`` plus a nested ``live`` object — the authoritative
        ``stage`` / ``current_skill`` / ``attempt`` / ``state`` / ``needs_you`` /
        ``live`` / ``updated_at`` from the execution-state snapshot (#267) and the
        drive-lease ``holder`` (#199). ``live`` is ``null`` for an idle,
        never-dispatched pipeline, so the UI falls back to the GitHub/coarse stage.
        The ``live`` object is also the #218 cockpit DAG's per-node live-overlay
        source (its dependency *edges* come from GitHub sub-issue links, not here).

        Distinct from ``POST /pipelines/{pipeline_key}/interrogate``. Returns an
        empty list when GitHub state is disabled or every fetch failed.
        """
        pipelines, _ = services.github_state(
            base_dir,
            enabled=github_state_enabled,
            refresh_seconds=github_refresh_seconds,
        )
        return _pipelines_payload(pipelines)

    @router.get("/repos")
    def get_repos() -> list[dict]:
        """Per-repo open issue / open PR counts (#219); empty if disabled/failed."""
        _, repos = services.github_state(
            base_dir,
            enabled=github_state_enabled,
            refresh_seconds=github_refresh_seconds,
        )
        return [asdict(r) for r in repos]

    @router.get("/repos/{owner}/{repo}/commits")
    def get_repo_commits(
        owner: str,
        repo: str,
        n: int = Query(5, ge=1, le=20),
    ) -> dict:
        """Recent local commits from ``owner/repo``'s base checkout (issue #224).

        A real ``git log`` from the persistent main-branch checkout on disk (not
        a GitHub fetch), powering the Live screen's "Recent commits" panel. All
        validation lives in :func:`services.recent_commits`; this handler just
        maps its failures — an absent/invalid checkout to 404 and a ``git``
        failure to 503 (the frontend treats either as "history unavailable").
        """
        try:
            commits = services.recent_commits(base_dir, owner, repo, n)
        except services.CheckoutNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except services.GitCommandError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "repo": f"{owner}/{repo}",
            "commits": [asdict(c) for c in commits],
            "count": len(commits),
        }

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

    @router.post("/pipelines/{pipeline_key}/interrogate")
    async def interrogate_pipeline(pipeline_key: str, request: Request) -> dict:
        """Start on-demand interrogation of a parked pipeline (issue #199).

        Body is JSON ``{"mode": "observe"|"drive", "repo"?: "owner/repo"}``.
        ``drive`` resumes the session into a fresh PTY and returns the attach URL
        (``409`` when an automated task holds the pipeline lease); ``observe`` is
        read-only and takes no lease. ``repo`` is optional — resolved from the
        recorded session when omitted, required for a pipeline with no record yet.
        """
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="body must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        mode = body.get("mode")
        if mode not in (services.INTERROGATE_OBSERVE, services.INTERROGATE_DRIVE):
            raise HTTPException(
                status_code=400,
                detail="'mode' must be 'observe' or 'drive'",
            )
        repo = body.get("repo")
        if repo is not None and not isinstance(repo, str):
            raise HTTPException(status_code=400, detail="'repo' must be a string or null")
        try:
            return await asyncio.to_thread(
                services.interrogate_pipeline, base_dir, pipeline_key, mode, repo=repo,
            )
        except services.SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except services.PipelineBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/pipelines/{pipeline_key}/release")
    async def release_pipeline(pipeline_key: str, request: Request) -> dict:
        """Tear down a live drive session and release its pipeline lease (#199)."""
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        repo = body.get("repo") if isinstance(body, dict) else None
        if repo is not None and not isinstance(repo, str):
            raise HTTPException(status_code=400, detail="'repo' must be a string or null")
        try:
            return await asyncio.to_thread(
                services.stop_drive, base_dir, pipeline_key, repo=repo,
            )
        except services.SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/pipelines/{pipeline_key}/labels")
    async def set_pipeline_label(pipeline_key: str, request: Request) -> dict:
        """Set a ready-for-* entry label on an issue pipeline (issue #225).

        The moved "Assign issue" control: body is JSON
        ``{"label": "ready-for-planning"|"ready-for-development", "repo": "owner/repo"}``.
        ``repo`` is required (a label change targets a specific issue and there is
        no session-resolution fallback). Setting one entry label clears its
        sibling (they are mutually exclusive). ``400`` for a malformed body /
        missing field, ``422`` for a bad label / non-issue pipeline / malformed
        repo, ``404`` for an unknown checkout. The next SSE snapshot reflects it.
        """
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="body must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        label = body.get("label")
        repo = body.get("repo")
        # Structural validation → 400 (matches inject_turn / interrogate_pipeline);
        # semantic validation (label not in the allowed set, pr pipeline, malformed
        # repo) is the service's job and maps to 422 below.
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(status_code=400, detail="'label' must be a non-empty string")
        if not isinstance(repo, str):
            raise HTTPException(status_code=400, detail="'repo' must be a string")
        try:
            return await asyncio.to_thread(
                services.set_pipeline_label, base_dir, pipeline_key, label, repo,
            )
        except services.LabelControlError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    @router.websocket("/sessions/{task_key}/observe")
    async def observe_session(websocket: WebSocket, task_key: str) -> None:
        """Stream a session's conversation from its JSONL transcript (issue #202).

        The JSONL-driven default observe surface: it renders the session from
        the on-disk transcript alone — no live ``claude`` process required, so a
        parked session between turns is observable identically to an active one.
        Sends structured JSON events (``user`` / ``assistant`` / ``thinking`` /
        ``tool_use`` / ``tool_result`` / ``stop`` / ``interrupt``): the full
        backlog first, then live updates as the transcript grows. Independent of
        the raw-bytes ``/attach`` PTY path, which stays for the live drive case.
        """
        await _observe_session(base_dir, websocket, task_key)

    def _state_snapshot() -> dict:
        """Gather the consolidated dashboard state in one shot.

        Mirrors the per-resource GET endpoints so the streamed payload and the
        polling fallback never drift apart. The GitHub-derived ``pipelines`` /
        ``repos`` (#219) ride the cache, so they add no ``gh`` call most ticks
        and fall back to empty lists (today's placeholders) on any failure.
        """
        gh_pipelines, gh_repos = services.github_state(
            base_dir,
            enabled=github_state_enabled,
            refresh_seconds=github_refresh_seconds,
        )
        return {
            "workers": [asdict(w) for w in services.list_workers(base_dir)],
            "worktrees": [asdict(w) for w in services.list_worktrees(base_dir)],
            "sessions": [asdict(s) for s in services.list_sessions(base_dir)],
            "task_sessions": [
                asdict(s) for s in services.list_task_sessions(base_dir)
            ],
            # "Stuck" is now heartbeat-age from the snapshot (#270) — a small read
            # per active pipeline, no /proc CPU/IO sampling and no blocking sleep
            # on the request path.
            "stuck": [
                asdict(s)
                for s in services.list_stuck_by_heartbeat(
                    base_dir, threshold_seconds=stuck_after_seconds
                )
            ],
            # Built through the same helper as ``GET /api/pipelines`` so the
            # streamed and polled pipeline payloads (incl. the nested ``live``
            # overlay) stay identical — the parity contract above.
            "pipelines": _pipelines_payload(gh_pipelines),
            "repos": [asdict(r) for r in gh_repos],
            # The live-state snapshot set (#269): real stage / current_skill /
            # attempt / state per running pipeline, the Fleet board + stat-strip
            # source. Filesystem-derived, so (like workers / sessions / stuck) it
            # stays live even when GitHub state is disabled / fetch-failed and the
            # ``pipelines`` list above is empty. Pushed event-driven off the
            # FleetEventWatcher inotify edge (#270), no fixed timer.
            "live_states": [asdict(s) for s in services.list_live_states(base_dir)],
        }

    @router.get("/events")
    async def stream_events(request: Request) -> StreamingResponse:
        """Push a consolidated state snapshot, then updates as state changes (#270).

        Event-driven: an initial snapshot is emitted on connect, then a
        :class:`~loony_dev.web.streaming.FleetEventWatcher` inotify watch over the
        execution-state substrate wakes the loop the instant any pipeline's
        ``.state.json`` / ``.events.jsonl`` changes — sub-second, no 2s timer. On
        each wake the (now cheap, heartbeat-derived) snapshot is recomputed and
        re-emitted only when it differs. A heartbeat comment is sent during idle
        periods (the watcher's wait timeout) so proxies don't drop the connection,
        and ``request.is_disconnected()`` reaps vanished clients. The watcher is
        closed in a ``finally`` so no inotify fd / loop reader leaks across
        reconnects — the same teardown contract as the log-tail stream.
        """

        async def event_stream():
            loop = asyncio.get_running_loop()
            watcher = streaming.FleetEventWatcher(base_dir)
            last_payload: str | None = None
            last_sent = loop.time()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    # Arm (reconcile watches + clear the edge) *before* the read so a
                    # change racing the recompute leaves the edge set for the next wait.
                    watcher.arm()
                    # Even cheapened, the snapshot occasionally touches the FS; run it
                    # off the event loop so a slow gather never stalls other connections.
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
                    # Block until the substrate changes or the heartbeat interval
                    # elapses (the latter drives the idle heartbeat above).
                    await watcher.wait_for_change(timeout=SSE_HEARTBEAT_INTERVAL)
            finally:
                watcher.close()

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
        before_offset: int | None = Query(
            None, ge=0, description="Byte cursor: page lines older than this offset"
        ),
    ) -> dict:
        """Offset-paginated tail of a repo's worker log (issue #270).

        Reads the tail backward from EOF (or from *before_offset*) so a large log
        is never read from byte 0. The additive ``next_offset`` byte cursor lets a
        client page older lines: pass it back as ``before_offset`` until it is
        ``null`` (start of file). ``lines`` / ``count`` stay backward-compatible.
        """
        try:
            page = services.tail_log_page(base_dir, owner, repo, lines, before_offset)
        except services.LogNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "repo": f"{owner}/{repo}",
            "lines": page["lines"],
            "count": len(page["lines"]),
            "next_offset": page["next_offset"],
        }

    @router.get("/logs/{owner}/{repo}/pipelines")
    def list_pipeline_logs(owner: str, repo: str) -> dict:
        """List the pipeline keys (``issue-N`` / ``pr-P``) with a log for the repo.

        Powers the folded Logs view's per-scope picker (worker-scope is the
        default; these are the additional pipeline scopes, issue #220).
        """
        keys = services.list_pipeline_logs(base_dir, owner, repo)
        return {"repo": f"{owner}/{repo}", "pipelines": keys, "count": len(keys)}

    @router.get("/logs/{owner}/{repo}/pipelines/{pipeline_key}/tail")
    def get_pipeline_log_tail(
        owner: str,
        repo: str,
        pipeline_key: str,
        lines: int = Query(default_tail_lines, ge=1, le=MAX_TAIL_LINES),
    ) -> dict:
        try:
            tail = services.tail_pipeline_log(base_dir, owner, repo, pipeline_key, lines)
        except services.LogNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "repo": f"{owner}/{repo}",
            "pipeline_key": pipeline_key,
            "lines": tail,
            "count": len(tail),
        }

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
        return _sse_tail_response(log_path, request, backlog=default_tail_lines)

    @router.get("/logs/{owner}/{repo}/pipelines/{pipeline_key}/stream")
    async def stream_pipeline_log(
        owner: str, repo: str, pipeline_key: str, request: Request
    ) -> StreamingResponse:
        """SSE live-tail of a single pipeline's log (issue #220).

        A clone of :func:`stream_log` targeting the per-pipeline file: validates
        the path (rejecting traversal on every segment, including *pipeline_key*),
        404s when the log doesn't exist yet, then streams backlog + new lines with
        the same heartbeat/disconnect handling.
        """
        try:
            log_path = services._safe_pipeline_log_path(base_dir, owner, repo, pipeline_key)
        except services.LogNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not log_path.exists():
            raise HTTPException(
                status_code=404, detail=f"no pipeline log for {owner}/{repo}:{pipeline_key}"
            )
        return _sse_tail_response(log_path, request, backlog=default_tail_lines)

    @router.get("/pipelines/{pipeline_key}/activity")
    def get_pipeline_activity(
        pipeline_key: str,
        repo: str = Query(..., description="owner/repo the pipeline belongs to"),
        lines: int = Query(default_tail_lines, ge=1, le=MAX_TAIL_LINES),
    ) -> dict:
        """Structured activity timeline from the pipeline's event log (#269).

        This is the endpoint the Issue ▸ PR activity timeline (#225) consumes. It
        tails the #267 structured event store directly — no log-regex. ``repo`` is
        required and names the ``owner/repo`` the pipeline belongs to, matching the
        front-end's pipeline-key-centric addressing. A pipeline with no events
        returns ``{events: [], count: 0}`` (200): the substrate's read side
        yields ``[]`` for a missing log. A traversal-bearing ``repo``/``key``
        (rejected by the service's path gate) 404s like the log-tail endpoints.
        """
        if "/" not in repo:
            raise HTTPException(status_code=422, detail="'repo' must be 'owner/repo'")
        owner, name = repo.split("/", 1)
        # Reject empty halves ("/", "owner/", "/repo") and a nested slash so the
        # repo resolves to a single ``owner/repo`` pair before path validation.
        if not owner or not name or "/" in name:
            raise HTTPException(status_code=422, detail="'repo' must be 'owner/repo'")
        try:
            events = services.pipeline_activity(base_dir, owner, name, pipeline_key, lines)
        except services.LogNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "repo": repo,
            "pipeline_key": pipeline_key,
            "events": events,
            "count": len(events),
        }

    @router.get("/activity")
    def get_activity(
        lines: int = Query(default_tail_lines, ge=1, le=MAX_TAIL_LINES),
    ) -> dict:
        """Cross-fleet "Live activity" backlog: a bounded recent-tail merge (#270).

        A time-ordered merge of the last *lines* events across the **active set**
        (≤ pool size) — not a historical/aggregate scan. Each event carries its
        ``repo`` / ``pipeline_key`` so the UI can attribute the line. Mirror of
        :func:`get_pipeline_activity` but fleet-wide; see ``/activity/stream`` for
        the live push.
        """
        events = services.fleet_activity(base_dir, lines)
        return {"events": events, "count": len(events)}

    @router.get("/activity/stream")
    async def stream_activity(
        request: Request,
        lines: int = Query(default_tail_lines, ge=1, le=MAX_TAIL_LINES),
    ) -> StreamingResponse:
        """SSE push of the cross-fleet activity feed (#270).

        Emits the recent-tail backlog, then on each FleetEventWatcher change
        recomputes the merged tail and emits only events past a tracked cursor
        (the last emitted ISO ``ts`` plus a small dedupe set for equal-``ts``
        ties), one JSON event per ``data:`` line. Same heartbeat / disconnect /
        teardown contract as ``stream_events`` — the watcher is closed in a
        ``finally`` so nothing leaks across reconnects.
        """

        def _key(event: dict) -> str:
            # Canonical full-payload key: keying on only a few fields would let two
            # distinct same-``ts`` events collide and silently drop one from the
            # live feed, so serialise every field.
            return json.dumps(event, sort_keys=True, separators=(",", ":"))

        async def event_stream():
            loop = asyncio.get_running_loop()
            watcher = streaming.FleetEventWatcher(base_dir)
            cursor_ts = ""
            ties: set[str] = set()
            last_sent = loop.time()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    watcher.arm()
                    merged = await asyncio.to_thread(services.fleet_activity, base_dir, lines)
                    emitted_any = False
                    for event in merged:  # ascending by ts
                        ts = event.get("ts", "")
                        if ts < cursor_ts:
                            continue
                        if ts == cursor_ts and _key(event) in ties:
                            continue
                        if ts > cursor_ts:
                            cursor_ts = ts
                            ties = {_key(event)}
                        else:
                            ties.add(_key(event))
                        emitted_any = True
                        yield _format_sse(json.dumps(event, sort_keys=True))
                    now = loop.time()
                    if emitted_any:
                        last_sent = now
                    elif now - last_sent >= SSE_HEARTBEAT_INTERVAL:
                        last_sent = now
                        yield ": heartbeat\n\n"
                    await watcher.wait_for_change(timeout=SSE_HEARTBEAT_INTERVAL)
            finally:
                watcher.close()

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
        """Heartbeat-derived stuck pipelines (#270) — no ``/proc`` on this path.

        Repointed from the ``/proc`` sampler to :func:`list_stuck_by_heartbeat` so
        it returns the same payload shape (minus the unavailable ``pid`` /
        ``cmdline``) without CPU/IO sampling or a blocking sleep. Kept (rather than
        removed) so any external consumer of ``/api/stuck`` keeps a 200.
        """
        return [
            asdict(s)
            for s in services.list_stuck_by_heartbeat(
                base_dir, threshold_seconds=stuck_after_seconds
            )
        ]

    @router.post("/sessions/{session_id}/interrupt")
    def interrupt_session(
        session_id: str = PathParam(..., min_length=1, description="Session id to interrupt"),
    ) -> dict:
        """Send an ESC interrupt to a session's in-flight turn (it stays alive).

        This is the primary, reversible intervention for a wedged Claude turn;
        the SIGTERM/SIGKILL ``/processes/{pid}/kill`` path remains the escalation.
        """
        try:
            return services.interrupt_session(base_dir, session_id)
        except services.SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except services.SessionControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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


async def _observe_session(base_dir: Path, websocket: WebSocket, task_key: str) -> None:
    """Stream JSONL-derived conversation events for *task_key* (issue #202).

    Resolves the task's transcript path from the registry, accepts the socket,
    then pumps :func:`transcript_stream.tail_events` (backlog then live) as JSON
    messages until the client disconnects. The generator is closed in a
    ``finally`` so its inotify/file descriptors are released on every detach,
    leaking nothing across reconnects.
    """
    jsonl_path = services.observe_jsonl_path(base_dir, task_key)
    if jsonl_path is None:
        # Reject before accept with the same distinct code as /attach so the
        # client can tell "no such observable session" apart from a net failure.
        await websocket.close(code=4404, reason="no such session")
        return

    await websocket.accept()
    gen = transcript_stream.tail_events(jsonl_path)

    async def pump_events() -> None:
        async for event in gen:
            await websocket.send_json(event)

    async def watch_disconnect() -> None:
        # The stream is server→client only; draining inbound messages lets us
        # notice a client going away promptly (between live events) so the
        # tailer is torn down instead of lingering until the next append.
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

    sender = asyncio.create_task(pump_events())
    watcher = asyncio.create_task(watch_disconnect())
    try:
        await asyncio.wait({sender, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sender, watcher):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await gen.aclose()
        with contextlib.suppress(Exception):
            await websocket.close()


def _resolve_bot_name() -> str | None:
    """Best-effort GitHub bot login for owner resolution, or ``None``.

    The read-only dashboard must never 500 because ``gh`` is unavailable, so any
    failure resolving the login degrades to ``None`` (managed entries without an
    explicit ``owner`` frontmatter simply list with no owner). Resolution itself
    is ``lru_cache``d on ``Repo.detect_bot_name``.
    """
    from loony_dev.github.repo import Repo

    try:
        return Repo.detect_bot_name() or None
    except Exception:
        return None


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
            views = entries.list_entries(
                kind, **scope_kwargs(scope, owner, repo), bot_name=_resolve_bot_name()
            )
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
