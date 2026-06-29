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

Backlog and live tailing are pinned to a single captured EOF cursor so no line
is lost or duplicated across the "read backlog → start watching" boundary. For a
positive backlog the watcher seeks its own handle to EOF, reads the last N lines
with the bounded reverse-block reader (:func:`loony_dev.web.log_tail._read_tail_page`,
bounded to that cursor so it never sees bytes appended meanwhile), then restores
the handle to the captured cursor; live tailing resumes from exactly there. This
keeps initial-backlog cost proportional to N rather than file size (issue #286)
while preserving the gap-free hand-off. A ``None`` backlog (the JSONL transcript
tailer) still drains the whole file, since it needs the full history.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import AsyncIterator

from loony_dev import inotify, pipeline_log
from loony_dev.web.log_tail import _read_tail_page

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
        replays none (live-only), a positive int replays the last N via a bounded
        reverse-block read (proportional to N, not file size — issue #286), and
        ``None`` replays the **entire** file (used by the JSONL observe tailer,
        which needs the full history so a reconnecting client renders an
        identical conversation, #202). A negative *backlog* is invalid and raises
        :class:`ValueError` rather than being silently treated as live-only.
        """
        if backlog is not None and backlog < 0:
            raise ValueError(f"backlog must be >= 0 or None, got {backlog}")
        try:
            await self._open_when_available()
            if backlog is None:
                # Full history: drain the whole file into the backlog, leaving the
                # handle at EOF so live tailing continues seamlessly. Only the
                # JSONL transcript tailer takes this path (it needs every line).
                tail: deque[str] = deque(self._file)
                for line in tail:
                    yield line.rstrip("\n")
            elif backlog > 0:
                # Bounded backlog: pin both the replayed window and the live-tail
                # start to one captured EOF cursor. ``seek`` returns the new
                # position; reading the tail bounded to ``end`` means the reverse
                # reader never sees bytes appended after we captured it, so the
                # backlog can't overlap the live ``_drain()`` — no line is lost or
                # duplicated at the hand-off. The reader uses its own handle; we
                # then restore ours to ``end`` so live tailing resumes from there.
                end = self._file.seek(0, os.SEEK_END)
                for line in _read_tail_page(self._path, backlog, before_offset=end)[0]:
                    yield line
                self._file.seek(end)
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
    leak. To make that reconcile *prompt* rather than heartbeat-delayed, the
    ancestor dirs (base, ``.logs``, each ``<owner>``/``<repo>``) are also watched
    with :data:`inotify.PARENT_WATCH_MASK`, so creating a brand-new
    ``pipelines/`` subtree fires an ``IN_CREATE`` edge that wakes the loop and the
    next :meth:`arm` arms the new dir (see :meth:`_watch_targets`).
    On a platform without inotify (:data:`inotify.INOTIFY_AVAILABLE` false)
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

    def _watch_targets(self) -> dict[Path, int]:
        """Every directory to watch, mapped to its inotify mask.

        Each existing ``.logs/<owner>/<repo>/pipelines/`` directory gets the full
        :data:`inotify.DIR_WATCH_MASK` (file appends + atomic snapshot replaces).
        Their *ancestors* — the base dir, ``.logs``, and each ``<owner>``/
        ``<repo>`` — get the creation-only :data:`inotify.PARENT_WATCH_MASK` so a
        pipeline whose ``pipelines/`` subtree is created *after* connect still
        fires an edge (``IN_CREATE`` on the parent): the next :meth:`arm`
        reconciles the new dir and the recomputed snapshot already reflects the
        first append. Without this an SSE client that connects before any pipeline
        exists would sleep until the heartbeat, missing the sub-second target.

        Raises :class:`OSError` if a directory can't be enumerated: returning a
        *partial* target set would leave ``_use_inotify`` on while some dirs go
        unwatched, so :meth:`wait_for_change` would block on edges that never
        arrive for them. The caller catches this and falls back to polling.
        """
        targets: dict[Path, int] = {}
        # Watch the base dir so the very first ``.logs`` creation is caught too.
        if self._base.is_dir():
            targets[self._base] = inotify.PARENT_WATCH_MASK
        logs_dir = self._base / ".logs"
        if not logs_dir.is_dir():
            return targets
        targets[logs_dir] = inotify.PARENT_WATCH_MASK
        for owner_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
            if owner_dir.name.startswith("."):
                continue
            targets[owner_dir] = inotify.PARENT_WATCH_MASK
            for repo_dir in sorted(p for p in owner_dir.iterdir() if p.is_dir()):
                targets[repo_dir] = inotify.PARENT_WATCH_MASK
                pipelines = repo_dir / pipeline_log.PIPELINES_DIR_NAME
                if pipelines.is_dir():
                    targets[pipelines] = inotify.DIR_WATCH_MASK
        return targets

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

        A failed enumeration (:meth:`_watch_targets` raising) or ``add_watch``
        (e.g. the per-instance watch limit is hit) must not be swallowed: leaving
        ``_use_inotify`` True would let :meth:`wait_for_change` block on an inotify
        edge that can never fire for the unwatched dir, silently missing its
        updates. So in either case we tear the watcher down and fall back to
        polling — which catches *every* dir's changes — surfacing the failure via a
        log rather than degrading invisibly.
        """
        if not self._use_inotify or self._inotify_fd < 0:
            return
        try:
            targets = self._watch_targets()
        except OSError:
            logger.warning(
                "inotify watch enumeration failed; falling back to polling",
                exc_info=True,
            )
            self.close()
            self._use_inotify = False
            return
        for d, mask in targets.items():
            if d in self._watched:
                continue
            wd = inotify.add_watch(self._inotify_fd, str(d), mask)
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
