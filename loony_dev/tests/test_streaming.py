"""Tests for the web dashboard's async streaming primitives (issue #270).

Covers :class:`loony_dev.web.streaming.FleetEventWatcher` — the directory-level
inotify watcher that drives the event-driven ``/api/events`` push: it must fire on
an event-log append, fire on an ``os.replace`` snapshot rewrite (the
``IN_MOVED_TO`` case a per-file watch would miss), pick up a newly-created pipeline
directory on the next reconcile, release all descriptors on ``close()``, and
degrade to a poll when inotify is unavailable.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loony_dev import execution_state, inotify, pipeline_log
from loony_dev.web import streaming


def _event(what: str) -> execution_state.ExecutionEvent:
    return execution_state.ExecutionEvent(
        type="turn_start", what=what, actor="bot", target={}
    )


def _live(key: str, repo: str) -> execution_state.LiveState:
    return execution_state.LiveState(
        pipeline_key=key, repo=repo, stage="Implementing",
        current_skill="implement-issue", state="running", attempt=1, live=True,
    )


class FleetEventWatcherTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    async def test_fires_on_event_append(self) -> None:
        # Seed a pipeline dir so the watch is registered at arm().
        execution_state.append_event(self.base, "acme/widgets", "issue-1", _event("a"))
        watcher = streaming.FleetEventWatcher(self.base, poll_interval=0.1)
        try:
            watcher.arm()
            execution_state.append_event(self.base, "acme/widgets", "issue-1", _event("b"))
            self.assertTrue(await watcher.wait_for_change(timeout=2.0))
        finally:
            watcher.close()

    async def test_fires_on_snapshot_replace(self) -> None:
        # write_snapshot is mkstemp + os.replace → IN_MOVED_TO on the directory.
        execution_state.write_snapshot(
            self.base, "acme/widgets", "issue-1", _live("issue-1", "acme/widgets")
        )
        watcher = streaming.FleetEventWatcher(self.base, poll_interval=0.1)
        try:
            watcher.arm()
            execution_state.write_snapshot(
                self.base, "acme/widgets", "issue-1", _live("issue-1", "acme/widgets")
            )
            self.assertTrue(await watcher.wait_for_change(timeout=2.0))
        finally:
            watcher.close()

    async def test_reconciles_newly_created_dir(self) -> None:
        watcher = streaming.FleetEventWatcher(self.base, poll_interval=0.1)
        try:
            watcher.arm()  # no pipeline dirs exist yet
            # A pipeline appears after connect (first-ever append creates the dir).
            execution_state.append_event(self.base, "acme/widgets", "issue-1", _event("a"))
            watcher.arm()  # reconcile picks up the new dir
            if inotify.INOTIFY_AVAILABLE:
                pipelines = pipeline_log.pipeline_logs_dir(self.base, "acme", "widgets")
                self.assertIn(pipelines, watcher._watched)
        finally:
            watcher.close()

    async def test_poll_fallback_returns_true(self) -> None:
        with mock.patch.object(inotify, "INOTIFY_AVAILABLE", False):
            watcher = streaming.FleetEventWatcher(self.base, poll_interval=0.01)
            try:
                watcher.arm()
                self.assertFalse(watcher._use_inotify)
                self.assertTrue(await watcher.wait_for_change(timeout=1.0))
            finally:
                watcher.close()

    async def test_add_watch_failure_falls_back_to_polling(self) -> None:
        if not inotify.INOTIFY_AVAILABLE:
            self.skipTest("inotify unavailable; reconcile never calls add_watch")
        execution_state.append_event(self.base, "acme/widgets", "issue-1", _event("a"))
        watcher = streaming.FleetEventWatcher(self.base, poll_interval=0.01)
        try:
            # A failed registration must not leave inotify "on" (which would make
            # wait_for_change block on an edge that can never fire) — it tears down
            # and falls back to polling, which still catches every change.
            with mock.patch.object(inotify, "add_watch", return_value=-1):
                watcher.arm()
            self.assertFalse(watcher._use_inotify)
            self.assertEqual(watcher._inotify_fd, -1)
            self.assertEqual(watcher._watched, set())
            self.assertTrue(await watcher.wait_for_change(timeout=1.0))
        finally:
            watcher.close()

    async def test_close_releases_descriptors(self) -> None:
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.exists():
            self.skipTest("/proc/self/fd unavailable on this platform")
        execution_state.append_event(self.base, "acme/widgets", "issue-1", _event("a"))

        async def one_cycle() -> None:
            watcher = streaming.FleetEventWatcher(self.base, poll_interval=0.01)
            watcher.arm()
            await watcher.wait_for_change(timeout=0.05)
            watcher.close()

        await one_cycle()  # warm up
        before = len(os.listdir(fd_dir))
        for _ in range(25):
            await one_cycle()
        after = len(os.listdir(fd_dir))
        self.assertLessEqual(after, before + 2, f"fd leak: {before} -> {after}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
