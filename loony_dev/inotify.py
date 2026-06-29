"""Shared ctypes-based inotify plumbing (Linux only).

The async web log tailer in :mod:`loony_dev.web.streaming` watches log files for
appends. To avoid duplicating the ctypes glue, the low-level bits live here:

- :data:`IN_MODIFY` / :data:`IN_CLOSE_WRITE` / :data:`IN_CREATE` /
  :data:`IN_MOVED_TO` event-mask constants,
- :data:`INOTIFY_AVAILABLE` — whether ``libc`` exposes the inotify syscalls,
- :func:`init` — create a non-blocking, close-on-exec inotify instance,
- :func:`add_watch` — register a watch on a path.

Everything degrades gracefully (returns ``-1`` / ``False``) on platforms where
inotify is unavailable, so callers can fall back to polling.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os

# inotify event-mask bits (from <sys/inotify.h>)
IN_MODIFY = 0x00000002  # File was modified
IN_MOVED_TO = 0x00000080  # File moved into the watched directory
IN_CLOSE_WRITE = 0x00000008  # Writable file was closed
IN_CREATE = 0x00000100  # File/dir created in the watched directory

# Directory-level mask for the fleet event watcher (#270): a pipeline's snapshot
# is rewritten via ``mkstemp`` + ``os.replace`` (``IN_MOVED_TO`` on the dir) and
# its event log is appended to (``IN_MODIFY``/``IN_CLOSE_WRITE``); a brand-new
# pipeline file first appears as ``IN_CREATE``. Watching the *directory* with all
# four bits catches every substrate change, where a per-file watch would miss the
# replaced inode entirely.
DIR_WATCH_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_CREATE | IN_MOVED_TO

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    INOTIFY_AVAILABLE = (
        hasattr(_libc, "inotify_init1")
        and hasattr(_libc, "inotify_add_watch")
        and hasattr(_libc, "inotify_rm_watch")
    )
except OSError:
    _libc = None  # type: ignore[assignment]
    INOTIFY_AVAILABLE = False


def init() -> int:
    """Create a non-blocking, close-on-exec inotify instance.

    Returns the inotify file descriptor, or ``-1`` if inotify is unavailable or
    the syscall fails.
    """
    if not INOTIFY_AVAILABLE:
        return -1
    try:
        fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return -1
    return fd if fd >= 0 else -1


def add_watch(inotify_fd: int, path: str, mask: int = IN_MODIFY | IN_CLOSE_WRITE) -> int:
    """Register an inotify watch on *path*; return the watch descriptor or ``-1``."""
    if not INOTIFY_AVAILABLE or inotify_fd < 0:
        return -1
    try:
        wd = _libc.inotify_add_watch(inotify_fd, str(path).encode(), mask)
    except OSError:
        return -1
    return wd if wd >= 0 else -1
