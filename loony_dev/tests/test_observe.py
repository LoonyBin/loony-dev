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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from unittest.mock import patch

from fastapi.testclient import TestClient

from loony_dev.agents.base import Agent
from loony_dev.agents.claude_quota import ClaudeQuotaMixin
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

    def test_record_session_worktree_sets_cwd_to_worktree(self) -> None:
        # A pipeline recorded at dispatch (before any -p turn registers its cwd)
        # must already be observable — #200 sets cwd to the worktree path, the
        # exact dir the turns run in, so observe can resolve the transcript.
        sr.record_session_worktree(
            self.base, "acme/widgets", pipeline_key="issue-8", task_key="issue-8",
            session_id="sid", worktree_path="/wt/issue-8",
        )
        got = sr.find_session(self.base, "issue-8")
        self.assertEqual(got.cwd, "/wt/issue-8")
        self.assertEqual(services.observe_jsonl_path(self.base, "issue-8"),
                         jsonl_path_for(Path("/wt/issue-8"), "sid"))

    def test_record_session_worktree_preserves_existing_cwd(self) -> None:
        # A re-dispatch must never wipe a cwd a -p turn already recorded, or the
        # entry would silently lose observability.
        sr.register_task_session(
            self.base, "acme/widgets", "issue-9", session_id="sid", cwd="/turn/cwd",
        )
        sr.record_session_worktree(
            self.base, "acme/widgets", pipeline_key="issue-9", task_key="issue-9",
            session_id="sid", worktree_path="/wt/issue-9",
        )
        got = sr.find_session(self.base, "issue-9")
        self.assertEqual(got.cwd, "/turn/cwd")


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


class LiveObserveJsonlPathTestCase(unittest.TestCase):
    """The base (remote-control) session JSONL resolver (#282, #294).

    ``--remote-control`` writes its transcript under a random UUID, not the
    deterministic relay id in ``remote-control.json``, so the resolver globs the
    checkout's project-slug dir and returns the newest ``*.jsonl`` (issue #294)
    rather than computing a filename from ``session_id``.
    """

    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.config_dir = Path(_tmpdir())
        self.cwd = Path(_tmpdir())  # the base checkout cwd (absolute)
        for d in (self.base, self.config_dir, self.cwd):
            self.addCleanup(lambda d=d: shutil.rmtree(d, ignore_errors=True))
        self.enterContext(mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)},
        ))
        self.conn_dir = self.base / ".logs" / "acme" / "widgets"
        self.conn_dir.mkdir(parents=True, exist_ok=True)
        self.conn_path = self.conn_dir / "remote-control.json"
        # The project-slug dir claude would write the base session's transcript to.
        self.project_dir = jsonl_path_for(self.cwd, "x").parent

    def _write_conn(self, data: object) -> None:
        self.conn_path.write_text(json.dumps(data) if not isinstance(data, str) else data)

    def _write_jsonl(self, name: str, mtime: float | None = None) -> Path:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        path = self.project_dir / name
        path.write_text("{}\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _good_conn(self) -> None:
        # session_id is the (unusable-as-filename) relay handle; cwd is what matters.
        self._write_conn({
            "session_id": "loony-acme-widgets-abc", "cwd": str(self.cwd),
            "key": "base", "mode": "remote-control",
        })

    def test_resolves_to_only_transcript(self) -> None:
        self._good_conn()
        only = self._write_jsonl("37997fbd-aaaa.jsonl")
        self.assertEqual(
            services.live_observe_jsonl_path(self.base, "acme", "widgets"), only,
        )

    def test_resolves_to_newest_transcript(self) -> None:
        # The on-disk filename is a random UUID, never the relay id; with several
        # present the resolver returns the most-recently-modified one.
        self._good_conn()
        self._write_jsonl("old.jsonl", mtime=1_000.0)
        newest = self._write_jsonl("newest.jsonl", mtime=3_000.0)
        self._write_jsonl("middle.jsonl", mtime=2_000.0)
        self.assertEqual(
            services.live_observe_jsonl_path(self.base, "acme", "widgets"), newest,
        )

    def test_excludes_stale_transcript_after_restart(self) -> None:
        # After a supervisor restart, the only on-disk transcript is the prior
        # session's (mtime < started_at) and the new session hasn't written yet.
        # The route tails the resolved path once and forever, so returning that
        # stale file would pin an observer to it — instead resolve to None (→
        # honest 4404) and let reconnect pick up the new transcript (#294 review).
        started = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
        self._write_conn({
            "session_id": "loony-acme-widgets-abc", "cwd": str(self.cwd),
            "key": "base", "mode": "remote-control",
            "started_at": started.isoformat(),
        })
        self._write_jsonl("prior-session.jsonl", mtime=started.timestamp() - 60)
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_resolves_current_transcript_after_restart(self) -> None:
        # Same restart scenario, but the new session has now written: the fresh
        # transcript (mtime >= started_at) is returned and the prior one ignored.
        started = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
        self._write_conn({
            "session_id": "loony-acme-widgets-abc", "cwd": str(self.cwd),
            "key": "base", "mode": "remote-control",
            "started_at": started.isoformat(),
        })
        self._write_jsonl("prior-session.jsonl", mtime=started.timestamp() - 60)
        current = self._write_jsonl("current-session.jsonl", mtime=started.timestamp() + 5)
        self.assertEqual(
            services.live_observe_jsonl_path(self.base, "acme", "widgets"), current,
        )

    def test_unparseable_started_at_falls_back_to_newest(self) -> None:
        # A connection file predating (or corrupting) started_at must not wedge
        # resolution: fall back to unfiltered newest-mtime selection.
        self._write_conn({
            "session_id": "loony-acme-widgets-abc", "cwd": str(self.cwd),
            "key": "base", "mode": "remote-control", "started_at": "not-a-time",
        })
        only = self._write_jsonl("37997fbd-aaaa.jsonl", mtime=1_000.0)
        self.assertEqual(
            services.live_observe_jsonl_path(self.base, "acme", "widgets"), only,
        )

    def test_none_when_no_transcript_yet(self) -> None:
        # Honest-empty: project dir exists but holds no transcript → None (→ 4404),
        # never a path to await (the #294 no-hang guarantee).
        self._good_conn()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_none_when_project_dir_absent(self) -> None:
        self._good_conn()  # no project dir created at all
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_none_when_missing(self) -> None:
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "nope"))

    def test_none_when_malformed(self) -> None:
        self._write_conn("{not json")
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_none_when_not_dict(self) -> None:
        self._write_conn([1, 2, 3])
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_none_when_missing_cwd(self) -> None:
        self._write_conn({"session_id": "sid"})  # no cwd
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_none_when_cwd_has_invalid_shape(self) -> None:
        # Present-but-malformed cwd values (non-string, empty, or relative) are
        # rejected rather than coerced into a fabricated transcript path.
        self._write_conn({"cwd": 123, "session_id": "sid"})
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))
        self._write_conn({"cwd": "", "session_id": "sid"})
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))
        self._write_conn({"cwd": "relative/path", "session_id": "sid"})
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "widgets"))

    def test_none_for_traversal_segment(self) -> None:
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "..", "widgets"))
        self.assertIsNone(services.live_observe_jsonl_path(self.base, "acme", "a/b"))


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


class LiveObserveWebSocketTestCase(unittest.TestCase):
    """The base-session ``/repos/{owner}/{repo}/live/observe`` route (#282).

    Seeds a ``remote-control.json`` connection file and a JSONL transcript at the
    computed path, then drives the route through the same shared ``_pump_transcript``
    backlog→live core the per-task observe uses.
    """

    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.config_dir = Path(_tmpdir())
        self.cwd = Path(_tmpdir())
        for d in (self.base, self.config_dir, self.cwd):
            self.addCleanup(lambda d=d: shutil.rmtree(d, ignore_errors=True))
        self.enterContext(mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)},
        ))
        self.session_id = "loony-acme-widgets-base"
        conn_dir = self.base / ".logs" / "acme" / "widgets"
        conn_dir.mkdir(parents=True, exist_ok=True)
        (conn_dir / "remote-control.json").write_text(json.dumps({
            "session_id": self.session_id, "cwd": str(self.cwd),
            "key": "base", "mode": "remote-control",
        }))
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
        self._write(_user("u1", "Say Hi"), _assistant("a1", "Hi 👋"))
        with self.client.websocket_connect(
            "/api/repos/acme/widgets/live/observe"
        ) as ws:
            self.assertEqual(ws.receive_json()["text"], "Say Hi")
            self.assertEqual(ws.receive_json()["text"], "Hi 👋")
            self.assertEqual(ws.receive_json()["kind"], "stop")
            self._write(_user("u2", "again"))
            self.assertEqual(ws.receive_json()["text"], "again")

    def test_no_base_session_closes_4404(self) -> None:
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(
                "/api/repos/acme/ghost/live/observe"
            ) as ws:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 4404)


class _DummyObserveAgent(ClaudeQuotaMixin, Agent):
    """Concrete mixin agent for exercising the observe-registration helpers."""

    name = "dummy_observe"

    def _can_handle_task(self, task):  # noqa: ANN001
        return True

    def execute(self, task):  # noqa: ANN001
        raise NotImplementedError


class ObserveRegistrationBaseDirTestCase(unittest.TestCase):
    """The agent registers observe sessions under its threaded base_dir (#285).

    Acceptance regression: registration must land under the *same* base-dir the
    web reads, even when ``config.settings`` carries no ``base_dir`` (the
    supervisor runs the worker spawn without a ``[worker] base_dir`` key). The
    pre-#285 code read ``config.settings.base_dir`` — which raises when unset —
    so this would have silently dropped the entry.
    """

    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def _task(self, worktree_key: str):
        task = mock.MagicMock()
        task.worktree_key = worktree_key
        return task

    def test_registration_lands_under_threaded_base_dir(self) -> None:
        from loony_dev import config
        from loony_dev.config._settings import Settings

        agent = _DummyObserveAgent()
        agent.repo = "acme/widgets"
        agent.base_dir = self.base  # threaded by the orchestrator
        work_dir = self.base / "acme" / "widgets" / ".worktrees" / "issue-9"

        # config.settings has NO base_dir — the property would raise if touched.
        with patch.object(config, "settings", Settings({})):
            agent._register_observe_session(
                self._task("issue-9"), work_dir, session_id="sid-9",
            )

        view = next(v for v in services.list_task_sessions(self.base)
                    if v.task_key == "issue-9")
        self.assertTrue(view.observable)
        self.assertEqual(view.cwd, str(work_dir))

    def test_no_threaded_base_dir_is_silent_noop(self) -> None:
        from loony_dev import config
        from loony_dev.config._settings import Settings

        agent = _DummyObserveAgent()
        agent.repo = "acme/widgets"
        agent.base_dir = None  # a bare/test agent

        with patch.object(config, "settings", Settings({})):
            # Must not raise even though config.settings.base_dir is unavailable.
            agent._register_observe_session(
                self._task("issue-9"), self.base / "wt", session_id="sid-9",
            )

        self.assertEqual(list(services.list_task_sessions(self.base)), [])


if __name__ == "__main__":
    unittest.main()
