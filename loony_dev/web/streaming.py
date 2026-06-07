"""Async log tailing for the web dashboard's live-stream endpoint.

The synchronous TUI uses :class:`loony_dev.tui.LogWatcher`; the web process needs
an ``asyncio``-native equivalent that never blocks the event loop. On Linux this
registers the non-blocking inotify fd with the running loop via
``loop.add_reader`` so new lines are delivered as soon as the file is appended,
with no sleep-polling. On platforms where inotify is unavailable it falls back to
an ``asyncio.sleep`` poll loop, mirroring :class:`LogWatcher`'s fallback.

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
import os
from collections import deque
from pathlib import Path
from typing import AsyncIterator

from loony_dev import inotify

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

    async def lines(self, *, backlog: int = DEFAULT_BACKLOG) -> AsyncIterator[str]:
        """Async-iterate the backlog, then new lines appended to the file."""
        try:
            await self._open_when_available()
            # Drain the existing content into a bounded deque for the backlog;
            # this leaves the handle at EOF so live tailing continues seamlessly.
            if backlog > 0:
                tail: deque[str] = deque(self._file, maxlen=backlog)
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


async def tail_lines(
    log_path: Path | str,
    *,
    backlog: int = DEFAULT_BACKLOG,
    poll_interval: float = POLL_INTERVAL,
) -> AsyncIterator[str]:
    """Emit the last *backlog* lines, then stream new lines as they are appended.

    Closing the returned async generator (or cancelling its consumer) propagates
    into the underlying :class:`AsyncLogWatcher`, releasing all descriptors.
    """
    watcher = AsyncLogWatcher(log_path, poll_interval=poll_interval)
    async for line in watcher.lines(backlog=backlog):
        yield line
