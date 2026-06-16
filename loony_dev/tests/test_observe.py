"""Tests for the JSONL-driven dashboard observe surface (issue #202).

Covers the async transcript tailer, the ``cwd`` registry round-trip + ``-p``
registration helpers, the ``observe_jsonl_path`` resolver, and the ``/observe``
WebSocket endpoint (backlog→live, 4404, reconnect idempotency, no live PTY).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from loony_dev import session_registry as sr
from loony_dev.session import jsonl_path_for
from loony_dev.web import create_app, services
from loony_dev.web import transcript_stream as ts


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="loony-observe-")


def _user(uuid: str, text: str) -> str:
    return json.dumps({"type": "user", "uuid": uuid, "timestamp": "t",
                       "message": {"content": text}})


def _assistant(uuid: str, text: str, stop: str = "end_turn") -> str:
    return json.dumps({"type": "assistant", "uuid": uuid, "timestamp": "t",
                       "message": {"stop_reason": stop,
                                   "content": [{"type": "text", "text": text}]}})


async def _anext(gen, timeout=5.0):
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


class TailEventsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.path = self.dir / "session.jsonl"

    async def test_backlog_then_live_no_dup_or_gap(self) -> None:
        self.path.write_text(_user("u1", "hello") + "\n" + _assistant("a1", "hi") + "\n")
        gen = ts.tail_events(self.path, poll_interval=0.05)
        try:
            # Backlog: user, then assistant + its stop.
            e1 = await _anext(gen)
            self.assertEqual(e1["kind"], "user")
            self.assertEqual(e1["text"], "hello")
            e2 = await _anext(gen)
            self.assertEqual(e2["kind"], "assistant")
            e3 = await _anext(gen)
            self.assertEqual(e3["kind"], "stop")

            # Live append continues from EOF with no missed/duplicated entry.
            with self.path.open("a") as fh:
                fh.write(_user("u2", "again") + "\n")
                fh.flush()
            e4 = await _anext(gen)
            self.assertEqual(e4["kind"], "user")
            self.assertEqual(e4["text"], "again")
            self.assertEqual(e4["id"], "u2#0")
        finally:
            await gen.aclose()

    async def test_malformed_line_is_skipped(self) -> None:
        self.path.write_text("{ not json\n" + _user("u1", "ok") + "\n")
        gen = ts.tail_events(self.path, poll_interval=0.05)
        try:
            ev = await _anext(gen)
            self.assertEqual(ev["kind"], "user")
            self.assertEqual(ev["text"], "ok")
        finally:
            await gen.aclose()

    async def test_file_appears_late(self) -> None:
        gen = ts.tail_events(self.path, poll_interval=0.05)
        try:
            await asyncio.sleep(0.1)
            self.path.write_text(_assistant("a1", "ready") + "\n")
            ev = await _anext(gen)
            self.assertEqual(ev["kind"], "assistant")
            self.assertEqual(ev["text"], "ready")
        finally:
            await gen.aclose()


class RegistryCwdTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def test_cwd_round_trips(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-1")
        sr.write_session_file(
            sess_dir, task_key="issue-1", repo="acme/widgets",
            session_id="sid", pid=1, started_at="t", cwd="/work/dir",
        )
        got = sr.read_session(sess_dir)
        self.assertEqual(got.cwd, "/work/dir")
        self.assertEqual(got.session_id, "sid")

    def test_legacy_entry_without_cwd_reads_none(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-2")
        sess_dir.mkdir(parents=True)
        (sess_dir / sr.SESSION_FILE_NAME).write_text(json.dumps({
            "task_key": "issue-2", "repo": "acme/widgets", "session_id": "x",
        }))
        got = sr.read_session(sess_dir)
        self.assertIsNone(got.cwd)

    def test_register_task_session_writes_cwd_and_no_socket(self) -> None:
        sr.register_task_session(
            self.base, "acme/widgets", "issue-3",
            session_id="sid", cwd="/cwd", pid=99,
        )
        got = sr.find_session(self.base, "issue-3")
        self.assertIsNotNone(got)
        self.assertEqual(got.cwd, "/cwd")
        self.assertEqual(got.session_id, "sid")
        self.assertEqual(got.status, "running")
        # No PTY bridge socket for a -p session → not attachable.
        self.assertFalse(got.socket)

    def test_set_status_preserves_cwd_and_session_id(self) -> None:
        sr.register_task_session(
            self.base, "acme/widgets", "issue-4", session_id="sid", cwd="/cwd",
        )
        sr.set_task_session_status(self.base, "acme/widgets", "issue-4", "idle")
        got = sr.find_session(self.base, "issue-4")
        self.assertEqual(got.status, "idle")
        self.assertEqual(got.cwd, "/cwd")
        self.assertEqual(got.session_id, "sid")

    def test_set_status_missing_entry_is_noop(self) -> None:
        # Must not raise when no entry exists.
        sr.set_task_session_status(self.base, "acme/widgets", "ghost", "idle")
        self.assertIsNone(sr.find_session(self.base, "ghost"))


class ObservableViewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def test_observable_true_with_cwd_and_session_id(self) -> None:
        sr.register_task_session(
            self.base, "acme/widgets", "issue-5", session_id="sid", cwd="/cwd",
        )
        view = next(v for v in services.list_task_sessions(self.base)
                    if v.task_key == "issue-5")
        self.assertTrue(view.observable)
        self.assertFalse(view.attachable)
        self.assertEqual(view.cwd, "/cwd")

    def test_observable_false_without_cwd(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-6")
        sr.write_session_file(
            sess_dir, task_key="issue-6", repo="acme/widgets",
            session_id="sid", pid=1, started_at="t",  # no cwd
        )
        view = next(v for v in services.list_task_sessions(self.base)
                    if v.task_key == "issue-6")
        self.assertFalse(view.observable)

    def test_observe_jsonl_path_resolves(self) -> None:
        sr.register_task_session(
            self.base, "acme/widgets", "issue-7", session_id="sid", cwd="/work/x",
        )
        path = services.observe_jsonl_path(self.base, "issue-7")
        self.assertEqual(path, jsonl_path_for(Path("/work/x"), "sid"))

    def test_observe_jsonl_path_none_for_unknown(self) -> None:
        self.assertIsNone(services.observe_jsonl_path(self.base, "ghost"))


class ObserveWebSocketTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.config_dir = Path(_tmpdir())
        self.cwd = Path(_tmpdir())
        for d in (self.base, self.config_dir, self.cwd):
            self.addCleanup(lambda d=d: shutil.rmtree(d, ignore_errors=True))
        self.enterContext(mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)},
        ))
        self.session_id = "sess-observe"
        # Register a -p style session (no live PTY/socket) pointing at our cwd.
        sr.register_task_session(
            self.base, "acme/widgets", "issue-3",
            session_id=self.session_id, cwd=str(self.cwd), status="idle",
        )
        self.jsonl = jsonl_path_for(self.cwd, self.session_id)
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        self.addCleanup(self.client.close)

    def _write(self, *lines: str) -> None:
        with self.jsonl.open("a") as fh:
            for line in lines:
                fh.write(line + "\n")
            fh.flush()

    def test_backlog_then_live(self) -> None:
        self._write(_user("u1", "hello"), _assistant("a1", "hi"))
        with self.client.websocket_connect("/api/sessions/issue-3/observe") as ws:
            self.assertEqual(ws.receive_json()["kind"], "user")
            self.assertEqual(ws.receive_json()["kind"], "assistant")
            self.assertEqual(ws.receive_json()["kind"], "stop")
            # Live append (no live process anywhere) still streams through.
            self._write(_user("u2", "again"))
            live = ws.receive_json()
            self.assertEqual(live["kind"], "user")
            self.assertEqual(live["text"], "again")

    def test_reconnect_is_idempotent(self) -> None:
        self._write(_user("u1", "hello"), _assistant("a1", "hi"))

        def collect() -> list[dict]:
            out = []
            with self.client.websocket_connect("/api/sessions/issue-3/observe") as ws:
                for _ in range(3):
                    out.append(ws.receive_json())
            return out

        first = collect()
        second = collect()
        # Same JSONL → identical event sequence (ids included) every reconnect.
        self.assertEqual([e["id"] for e in first], [e["id"] for e in second])
        self.assertEqual([e["kind"] for e in first], ["user", "assistant", "stop"])

    def test_unknown_session_closes_4404(self) -> None:
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/api/sessions/ghost/observe") as ws:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 4404)


if __name__ == "__main__":
    unittest.main()
