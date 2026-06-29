"""Bounded reverse-block log tail reader.

A small, dependency-free reader that returns the last N lines of a file without
reading it from byte 0: it seeks to EOF (or a caller-supplied offset) and walks
backward in fixed-size blocks, stopping once enough newlines are seen. Memory and
IO stay proportional to ``lines x avg-line`` rather than file size, so a large
``loony-worker.log`` is never drained per request (issue #270).

Both the REST ``/tail`` path (``services.py``) and the SSE ``/stream`` backlog
(``streaming.py``, issue #286) share this single implementation.
"""

from __future__ import annotations

import os
from pathlib import Path

# Block size for the reverse tail reader. The reader walks the file backward in
# fixed-size blocks from EOF, so memory + IO are bounded by ``lines x avg-line``
# rather than file size — a large ``loony-worker.log`` is no longer read from
# byte 0 and discarded per request (issue #270).
_TAIL_BLOCK_SIZE = 8192

# Hard ceiling on how far the reverse reader scans backward in one page. Without
# it a log with too few ``\n`` separators (or a single huge final line) would
# walk all the way to byte 0 — reintroducing the whole-file read this reader
# exists to avoid. When the budget is hit before enough newlines are seen the
# page is bounded to (at most) the last ``_TAIL_MAX_BYTES`` bytes; an oversized
# trailing line is therefore truncated rather than read in full (issue #270).
_TAIL_MAX_BYTES = 8 * 1024 * 1024


def _read_tail_page(
    path: Path, lines: int, before_offset: int | None = None
) -> tuple[list[str], int | None]:
    """Read up to *lines* whole lines ending at *before_offset* (default EOF).

    Seeks to the window end (``before_offset`` or EOF) and reads fixed-size blocks
    **backward**, stopping once enough newlines are seen — never touching byte 0 of
    a large file. Returns ``(lines, next_offset)`` where ``next_offset`` is the
    byte position at which the first returned line begins — the cursor a client
    passes back as ``before_offset`` to page older lines — or ``None`` when the
    start of file was reached (no older lines remain). Decodes UTF-8 with
    ``errors="replace"`` and strips trailing newlines, matching the previous
    ``deque(fh)`` behaviour. Raises ``FileNotFoundError`` like ``open``.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        end = size if before_offset is None else max(0, min(before_offset, size))
        if lines <= 0 or end == 0:
            return [], (end if end > 0 else None)

        pos = end
        chunks: list[bytes] = []
        newline_count = 0
        # Read backward until we have one more newline than requested lines (so the
        # lines-th line from the end is bounded), we hit the start of file, or we
        # exhaust the byte budget (the guard against a pathological few-newline log
        # forcing a whole-file read).
        while pos > 0 and newline_count <= lines and (end - pos) < _TAIL_MAX_BYTES:
            read_size = min(_TAIL_BLOCK_SIZE, pos)
            pos -= read_size
            fh.seek(pos)
            block = fh.read(read_size)
            chunks.append(block)
            newline_count += block.count(b"\n")
        data = b"".join(reversed(chunks))  # bytes [pos, end)

    # Map each split fragment to the absolute byte offset where it begins.
    parts = data.split(b"\n")
    entries: list[tuple[int, bytes]] = []
    offset = pos
    for part in parts:
        entries.append((offset, part))
        offset += len(part) + 1  # +1 for the consumed '\n' separator

    # A trailing '\n' yields a final empty fragment that is not a line.
    if data.endswith(b"\n") and entries and entries[-1][1] == b"":
        entries.pop()
    # When we stopped mid-file because we found enough newline delimiters, the
    # first fragment began before `pos` and is incomplete — drop it (its start is
    # captured by the next page's window). But if the byte budget stopped the scan
    # first (a few-newline / oversized line), keep that sole bounded suffix so the
    # tail returns the line truncated rather than reporting nothing.
    budget_hit = pos > 0 and (end - pos) >= _TAIL_MAX_BYTES and newline_count <= lines
    if pos > 0 and entries and not budget_hit:
        entries.pop(0)

    selected = entries[-lines:]
    if not selected:
        return [], None
    start_offset = selected[0][0]
    next_offset = start_offset if start_offset > 0 else None
    return [content.decode("utf-8", errors="replace") for _, content in selected], next_offset


def _read_tail(path: Path, lines: int) -> list[str]:
    """Return the last *lines* lines of *path* without reading the whole file."""
    return _read_tail_page(path, lines)[0]
