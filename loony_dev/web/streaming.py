"""Async log tailing for the web dashboard's live-stream endpoint.

The web process needs an ``asyncio``-native log tailer that never blocks the
event loop. On Linux this registers the non-blocking inotify fd with the running
loop via ``loop.add_reader`` so new lines are delivered as soon as the file is
appended, with no sleep-polling. On platforms where inotify is unavailable it
falls back to an ``asyncio.sleep`` poll loop.

The public entry point is :func:`tail_lines`, an async generator that first emits
a bounded backlog (the last N lines) and then streams new lines forever until the
consumer closes it — at which point all file/inotify descriptors are released in
a ``finally`` block, guaranteeing no FD leak across reconnects.

Backlog and live tailing share a single file handle: the backlog is read by
draining the file into a bounded ``deque`` (which leaves the handle positioned at
EOF), and live tailing continues from exactly that position. This closes the
window in which lines appended between "read backlog" and "start watching" would
otherwise be lost.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import AsyncIterator

from loony_dev import inotify, pipeline_log

logger = logging.getLogger(__name__)

# Default backlog and poll cadence (the latter only used on the fallback path).
DEFAULT_BACKLOG = 100
POLL_INTERVAL = 0.5


class AsyncLogWatcher:
    """Yield the backlog then new lines appended to a log file.

    Instances are single-use: call :meth:`lines` once and iterate it. On exit
    (normal completion, consumer ``aclose()``, or cancellation) the inotify fd,
    its loop reader registration, and the open file are all released.
    """

    def __init__(self, log_path: Path | str, *, poll_interval: float = POLL_INTERVAL) -> None:
        self._path = Path(log_path)
        self._poll_interval = poll_interval
        self._file = None
        self._inotify_fd: int = -1
        self._inotify_wd: int = -1
        self._reader_registered = False
        self._data_ready = asyncio.Event()

    async def _open_when_available(self) -> None:
        """Open the log file, waiting (via polling) for it to appear."""
        while self._file is None:
            try:
                self._file = open(self._path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
            except FileNotFoundError:
                await asyncio.sleep(self._poll_interval)

    def _setup_inotify(self) -> bool:
        """Register an inotify watch + loop reader. Return False to use polling."""
        fd = inotify.init()
        if fd < 0:
            return False
        wd = inotify.add_watch(fd, str(self._path))
        if wd < 0:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        self._inotify_fd = fd
        self._inotify_wd = wd
        asyncio.get_running_loop().add_reader(fd, self._on_inotify)
        self._reader_registered = True
        return True

    def _on_inotify(self) -> None:
        """Loop reader callback: drain inotify events and wake the generator."""
        try:
            os.read(self._inotify_fd, 4096)
        except OSError:
            pass
        self._data_ready.set()

    def _drain(self) -> list[str]:
        """Read and return every line available since the last read."""
        out: list[str] = []
        if self._file is None:
            return out
        while True:
            line = self._file.readline()
            if not line:
                break
            out.append(line.rstrip("\n"))
        return out

    async def lines(self, *, backlog: int | None = DEFAULT_BACKLOG) -> AsyncIterator[str]:
        """Async-iterate the backlog, then new lines appended to the file.

        *backlog* is the number of pre-existing lines to replay first: ``0``
        replays none (live-only), a positive int replays the last N, and
        ``None`` replays the **entire** file (used by the JSONL observe tailer,
        which needs the full history so a reconnecting client renders an
        identical conversation, #202).
        """
        try:
            await self._open_when_available()
            # Drain the existing content into a deque for the backlog; this
            # leaves the handle at EOF so live tailing continues seamlessly. A
            # ``None`` maxlen keeps every line (full history).
            if backlog is None or backlog > 0:
                maxlen = None if backlog is None else backlog
                tail: deque[str] = deque(self._file, maxlen=maxlen)
                for line in tail:
                    yield line.rstrip("\n")
            else:
                self._file.seek(0, os.SEEK_END)

            use_inotify = self._setup_inotify()
            while True:
                if use_inotify:
                    # Clear *before* draining so an append that races the drain
                    # still leaves the event set, waking the next wait().
                    self._data_ready.clear()
                    for line in self._drain():
                        yield line
                    await self._data_ready.wait()
                else:
                    for line in self._drain():
                        yield line
                    await asyncio.sleep(self._poll_interval)
        finally:
            self.close()

    def close(self) -> None:
        """Release the inotify fd, its loop reader, and the open file."""
        if self._reader_registered and self._inotify_fd >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self._inotify_fd)
            except (RuntimeError, ValueError, OSError):
                pass
            self._reader_registered = False
        if self._inotify_fd >= 0:
            try:
                os.close(self._inotify_fd)
            except OSError:
                pass
            self._inotify_fd = -1
            self._inotify_wd = -1
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


class FleetEventWatcher:
    """Signal "the execution-state substrate changed" for the fleet SSE push (#270).

    Where :class:`AsyncLogWatcher` follows a *single* file's appended lines, the
    consolidated ``/api/events`` stream needs to react to *any* pipeline's
    ``.state.json`` / ``.events.jsonl`` across **every** repo. This is a
    directory-level inotify watcher over each
    ``.logs/<owner>/<repo>/pipelines/`` directory: it does not parse the event
    struct — it only raises a "something changed" edge so the SSE loop recomputes
    its (now cheap) snapshot and diffs. We watch the directory (not each file)
    with :data:`inotify.DIR_WATCH_MASK` because the snapshot is rewritten via
    ``mkstemp`` + ``os.replace`` (``IN_MOVED_TO``), which a per-file watch would
    miss; a brand-new pipeline file first appears as ``IN_CREATE``.

    New pipeline directories — which don't exist at startup, since they're created
    on a pipeline's first append — are reconciled **inline** on each
    :meth:`arm` (and once at start), so there is no background reconcile task to
    leak. On a platform without inotify (:data:`inotify.INOTIFY_AVAILABLE` false)
    it degrades to a poll: :meth:`wait_for_change` sleeps ``poll_interval`` and
    returns ``True`` (the SSE loop then recomputes + diffs, emitting only on a
    real change), mirroring :class:`AsyncLogWatcher`'s fallback.
    """

    def __init__(self, base_dir: Path | str, *, poll_interval: float = POLL_INTERVAL) -> None:
        self._base = Path(base_dir)
        self._poll_interval = poll_interval
        self._inotify_fd: int = -1
        self._reader_registered = False
        self._use_inotify = False
        self._started = False
        # Directories with a live watch, so reconciliation only adds new ones.
        self._watched: set[Path] = set()
        self._data_ready = asyncio.Event()

    def _pipeline_dirs(self) -> list[Path]:
        """Every existing ``.logs/<owner>/<repo>/pipelines/`` directory."""
        logs_dir = self._base / ".logs"
        out: list[Path] = []
        if not logs_dir.is_dir():
            return out
        try:
            owner_dirs = sorted(p for p in logs_dir.iterdir() if p.is_dir())
        except OSError:
            return out
        for owner_dir in owner_dirs:
            if owner_dir.name.startswith("."):
                continue
            try:
                repo_dirs = sorted(p for p in owner_dir.iterdir() if p.is_dir())
            except OSError:
                continue
            for repo_dir in repo_dirs:
                pipelines = repo_dir / pipeline_log.PIPELINES_DIR_NAME
                if pipelines.is_dir():
                    out.append(pipelines)
        return out

    def _start(self) -> None:
        """Lazily create the inotify fd + loop reader on first :meth:`arm`."""
        if self._started:
            return
        self._started = True
        fd = inotify.init()
        if fd < 0:
            self._use_inotify = False
            return
        self._inotify_fd = fd
        asyncio.get_running_loop().add_reader(fd, self._on_inotify)
        self._reader_registered = True
        self._use_inotify = True

    def _reconcile_watches(self) -> None:
        """Add a watch for any pipeline dir that has appeared since the last pass.

        A failed ``add_watch`` (e.g. the per-instance watch limit is hit) must not
        be swallowed: leaving ``_use_inotify`` True would let
        :meth:`wait_for_change` block on an inotify edge that can never fire for the
        unwatched dir, silently missing its updates. So we tear the watcher down and
        fall back to polling — which catches *every* dir's changes — surfacing the
        failure via a log rather than degrading invisibly.
        """
        if not self._use_inotify or self._inotify_fd < 0:
            return
        for d in self._pipeline_dirs():
            if d in self._watched:
                continue
            wd = inotify.add_watch(self._inotify_fd, str(d), inotify.DIR_WATCH_MASK)
            if wd < 0:
                logger.warning(
                    "inotify add_watch failed for %s; falling back to polling", d
                )
                self.close()
                self._use_inotify = False
                return
            self._watched.add(d)

    def _on_inotify(self) -> None:
        """Loop reader callback: drain inotify events and raise the change edge."""
        try:
            os.read(self._inotify_fd, 4096)
        except OSError:
            pass
        self._data_ready.set()

    def arm(self) -> None:
        """Reconcile watches and clear the change edge **before** the caller reads.

        Called at the top of each SSE loop iteration, *before* recomputing the
        snapshot, so a change that races the recompute leaves the edge set and the
        following :meth:`wait_for_change` returns immediately (no missed update).
        """
        self._start()
        self._reconcile_watches()
        self._data_ready.clear()

    async def wait_for_change(self, timeout: float) -> bool:
        """Block until the substrate changes or *timeout* elapses.

        Returns ``True`` if a change fired, ``False`` on timeout (the SSE loop
        uses the timeout as its heartbeat cadence). Without inotify it sleeps the
        poll interval and returns ``True`` so the caller recomputes + diffs.
        """
        self._start()
        if not self._use_inotify:
            await asyncio.sleep(min(self._poll_interval, timeout))
            return True
        try:
            await asyncio.wait_for(self._data_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def close(self) -> None:
        """Release the inotify fd and its loop reader (no watches/tasks leak)."""
        if self._reader_registered and self._inotify_fd >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self._inotify_fd)
            except (RuntimeError, ValueError, OSError):
                pass
            self._reader_registered = False
        if self._inotify_fd >= 0:
            try:
                os.close(self._inotify_fd)
            except OSError:
                pass
            self._inotify_fd = -1
        self._watched.clear()


async def tail_lines(
    log_path: Path | str,
    *,
    backlog: int | None = DEFAULT_BACKLOG,
    poll_interval: float = POLL_INTERVAL,
) -> AsyncIterator[str]:
    """Emit the last *backlog* lines, then stream new lines as they are appended.

    Closing the returned async generator (or cancelling its consumer) propagates
    into the underlying :class:`AsyncLogWatcher`, releasing all descriptors.
    """
    watcher = AsyncLogWatcher(log_path, poll_interval=poll_interval)
    async for line in watcher.lines(backlog=backlog):
        yield line
