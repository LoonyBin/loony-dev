"""Async tail of a Claude JSONL transcript as structured observe events (#202).

The dashboard's JSONL-driven *observe* surface renders a session's conversation
straight from its on-disk transcript — no live ``claude`` process required.
This module is the backend half: it tails ``jsonl_path_for(cwd, session_id)``
incrementally and yields the structured events produced by
:func:`loony_dev.session_transcript.parse_entry`.

It reuses :class:`loony_dev.web.streaming.AsyncLogWatcher` for the file-watching
machinery (inotify on Linux, poll fallback, full-history backlog drained into
the same handle that then live-tails from EOF — so no entry is lost or
duplicated at the backlog/live boundary). Each complete JSONL line is parsed;
malformed lines are skipped (a partial final line is buffered by the
line-watcher until the rest arrives), mirroring the worker-side tailer.

The first events yielded are the parsed full history (backlog), then live
updates, so a client connecting mid-session immediately sees the whole
conversation and then near-real-time growth.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncIterator

from loony_dev import session_transcript
from loony_dev.web.streaming import POLL_INTERVAL, AsyncLogWatcher

logger = logging.getLogger(__name__)


async def tail_events(
    jsonl_path: Path | str,
    *,
    poll_interval: float = POLL_INTERVAL,
) -> AsyncIterator[dict]:
    """Yield observe events from *jsonl_path*: full backlog, then live tail.

    ``backlog=None`` replays the entire transcript first (required for
    reconnect idempotency — the client replays from zero and dedupes by event
    ``id``), then live updates stream as the file grows. Closing the returned
    async generator (or cancelling its consumer) releases the underlying
    inotify/file descriptors via :class:`AsyncLogWatcher`'s ``finally`` teardown.
    """
    watcher = AsyncLogWatcher(jsonl_path, poll_interval=poll_interval)
    async for line in watcher.lines(backlog=None):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A complete (newline-terminated) line should parse; if it does not,
            # skip it rather than wedging the tail (mirrors the worker tailer).
            logger.debug("Skipping unparseable JSONL line in %s", jsonl_path)
            continue
        for event in session_transcript.parse_entry(entry):
            yield event
