"""Tests for the read-only web dashboard (issues #130, #131)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loony_dev.web import create_app
from loony_dev.web import services, streaming


def _make_worker(base: Path, owner: str, repo: str, pid: int, log_lines: list[str]) -> None:
    """Create a fake .logs/<owner>/<repo>/loony-worker.{pid,log} layout."""
    repo_dir = base / ".logs" / owner / repo
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / services.WORKER_PID_NAME).write_text(str(pid))
    (repo_dir / services.WORKER_LOG_NAME).write_text("\n".join(log_lines) + "\n")


class ServicesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_list_workers_discovers_tree_and_status_running(self) -> None:
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["line 1", "line 2"])
        workers = services.list_workers(self.base)
        self.assertEqual(len(workers), 1)
        w = workers[0]
        self.assertEqual(w.repo, "acme/widgets")
        self.assertEqual(w.pid, os.getpid())
        self.assertEqual(w.status, "running")  # our own PID is alive
        self.assertIsNone(w.exitcode)
        self.assertIsNotNone(w.started_at)

    def test_list_workers_dead_pid_is_stale(self) -> None:
        # PID 0x7FFFFFFF is almost certainly not a live process.
        _make_worker(self.base, "acme", "ghost", 0x7FFFFFFF, ["x"])
        workers = services.list_workers(self.base)
        self.assertEqual(workers[0].status, "stale")

    def test_list_workers_invalid_pid_is_stale(self) -> None:
        repo_dir = self.base / ".logs" / "acme" / "broken"
        repo_dir.mkdir(parents=True)
        (repo_dir / services.WORKER_PID_NAME).write_text("not-a-number")
        (repo_dir / services.WORKER_LOG_NAME).write_text("x\n")
        w = services.list_workers(self.base)[0]
        self.assertEqual(w.status, "stale")
        self.assertIsNone(w.pid)

    def test_list_workers_nonpositive_pid_is_stale(self) -> None:
        # A PID file containing 0 or a negative value must not be treated as a
        # live PID: os.kill(0/-N, 0) targets a process group, not a process.
        for bad_pid in ("0", "-1"):
            with self.subTest(pid=bad_pid):
                repo_dir = self.base / ".logs" / "acme" / f"pid{bad_pid}"
                repo_dir.mkdir(parents=True)
                (repo_dir / services.WORKER_PID_NAME).write_text(bad_pid)
                (repo_dir / services.WORKER_LOG_NAME).write_text("x\n")
        workers = {w.repo: w for w in services.list_workers(self.base)}
        for name in ("acme/pid0", "acme/pid-1"):
            self.assertEqual(workers[name].status, "stale")
            self.assertIsNone(workers[name].pid)

    def test_list_workers_skips_hidden_dirs(self) -> None:
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["x"])
        (self.base / ".logs" / services.SESSIONS_DIR_NAME).mkdir(parents=True)
        repos = [w.repo for w in services.list_workers(self.base)]
        self.assertEqual(repos, ["acme/widgets"])

    def test_list_workers_empty_when_no_logs(self) -> None:
        self.assertEqual(services.list_workers(self.base), [])

    def test_tail_log_returns_last_n_lines(self) -> None:
        lines = [f"log {i}" for i in range(20)]
        _make_worker(self.base, "acme", "widgets", os.getpid(), lines)
        tail = services.tail_log(self.base, "acme", "widgets", 5)
        self.assertEqual(tail, ["log 15", "log 16", "log 17", "log 18", "log 19"])

    def test_tail_log_missing_raises(self) -> None:
        with self.assertRaises(services.LogNotFoundError):
            services.tail_log(self.base, "acme", "nope", 10)

    def test_tail_log_rejects_traversal(self) -> None:
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["x"])
        for owner, repo in [("..", "etc"), ("acme", ".."), ("a/b", "c")]:
            with self.assertRaises(services.LogNotFoundError):
                services.tail_log(self.base, owner, repo, 10)

    def test_list_sessions_empty_without_dir(self) -> None:
        self.assertEqual(services.list_sessions(self.base), [])

    def test_list_sessions_parses_json_defensively(self) -> None:
        sdir = self.base / ".logs" / services.SESSIONS_DIR_NAME
        sdir.mkdir(parents=True)
        (sdir / "a.json").write_text('{"session_id": "uuid-1", "repo": "acme/x", "key": "issue:1", "extra": 9}')
        (sdir / "bad.json").write_text("{not json")
        sessions = services.list_sessions(self.base)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "uuid-1")
        self.assertEqual(sessions[0].repo, "acme/x")
        self.assertEqual(sessions[0].key, "issue:1")


class WebAppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["alpha", "beta", "gamma"])
        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))

    def test_index_served(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("loony-dev dashboard", resp.text)

    def test_workers_endpoint(self) -> None:
        resp = self.client.get("/api/workers")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["repo"], "acme/widgets")
        self.assertEqual(body[0]["status"], "running")
        self.assertIsNone(body[0]["exitcode"])

    def test_worktrees_endpoint(self) -> None:
        resp = self.client.get("/api/worktrees")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)  # no checkouts -> []

    def test_sessions_endpoint_empty(self) -> None:
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_log_tail_endpoint(self) -> None:
        resp = self.client.get("/api/logs/acme/widgets/tail?lines=5")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["repo"], "acme/widgets")
        self.assertEqual(body["lines"], ["alpha", "beta", "gamma"])
        self.assertEqual(body["count"], 3)

    def test_log_tail_default_lines(self) -> None:
        resp = self.client.get("/api/logs/acme/widgets/tail")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 3)

    def test_log_tail_unknown_repo_404(self) -> None:
        resp = self.client.get("/api/logs/acme/nope/tail")
        self.assertEqual(resp.status_code, 404)

    def test_log_tail_bad_lines_422(self) -> None:
        self.assertEqual(self.client.get("/api/logs/acme/widgets/tail?lines=0").status_code, 422)
        self.assertEqual(self.client.get("/api/logs/acme/widgets/tail?lines=99999").status_code, 422)
        self.assertEqual(self.client.get("/api/logs/acme/widgets/tail?lines=abc").status_code, 422)

    def test_log_tail_traversal_rejected(self) -> None:
        # Encoded traversal should not escape; FastAPI routing + our validation
        # must yield a 4xx, never a 200 with foreign file contents.
        resp = self.client.get("/api/logs/acme/widgets/tail", params={"lines": 5})
        self.assertEqual(resp.status_code, 200)  # sanity: the happy path works
        bad = self.client.get("/api/logs/%2e%2e/etc/tail")
        self.assertIn(bad.status_code, (404, 422))


async def _anext_with_timeout(gen, timeout=5.0):
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


class AsyncLogWatcherTestCase(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the async log tailer (issue #131, A2)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.log = self.base / "worker.log"

    async def test_backlog_then_new_lines(self) -> None:
        self.log.write_text("one\ntwo\n")
        gen = streaming.tail_lines(self.log, backlog=10, poll_interval=0.05)
        try:
            self.assertEqual(await _anext_with_timeout(gen), "one")
            self.assertEqual(await _anext_with_timeout(gen), "two")
            with self.log.open("a") as fh:
                fh.write("three\n")
                fh.flush()
            self.assertEqual(await _anext_with_timeout(gen), "three")
        finally:
            await gen.aclose()

    async def test_backlog_is_bounded(self) -> None:
        self.log.write_text("".join(f"line{i}\n" for i in range(50)))
        gen = streaming.tail_lines(self.log, backlog=3, poll_interval=0.05)
        try:
            got = [await _anext_with_timeout(gen) for _ in range(3)]
            self.assertEqual(got, ["line47", "line48", "line49"])
        finally:
            await gen.aclose()

    async def test_file_appears_late(self) -> None:
        gen = streaming.tail_lines(self.log, backlog=10, poll_interval=0.05)
        try:
            # No file yet; create it after the generator has started waiting.
            await asyncio.sleep(0.1)
            self.log.write_text("late\n")
            self.assertEqual(await _anext_with_timeout(gen), "late")
        finally:
            await gen.aclose()

    async def test_fallback_polling_path(self) -> None:
        # Force the non-inotify branch and confirm new lines still arrive.
        import unittest.mock as mock

        self.log.write_text("a\n")
        with mock.patch.object(streaming.inotify, "INOTIFY_AVAILABLE", False):
            gen = streaming.tail_lines(self.log, backlog=10, poll_interval=0.05)
            try:
                self.assertEqual(await _anext_with_timeout(gen), "a")
                with self.log.open("a") as fh:
                    fh.write("b\n")
                self.assertEqual(await _anext_with_timeout(gen), "b")
            finally:
                await gen.aclose()

    async def test_cleanup_releases_descriptors(self) -> None:
        self.log.write_text("x\n")
        watcher = streaming.AsyncLogWatcher(self.log, poll_interval=0.05)
        gen = watcher.lines(backlog=10)
        # Pull one line so the file + inotify watch are set up.
        await _anext_with_timeout(gen)
        await gen.aclose()
        self.assertIsNone(watcher._file)
        self.assertEqual(watcher._inotify_fd, -1)
        self.assertFalse(watcher._reader_registered)


class _SSEDriver:
    """Drive an ASGI app's SSE endpoint in-process.

    httpx's ASGITransport buffers the entire response body before returning, so
    it deadlocks on an unbounded ``text/event-stream``. This minimal driver
    speaks ASGI directly: it feeds one empty request body, collects sent body
    chunks, and can deliver an ``http.disconnect`` to exercise teardown.
    """

    def __init__(self, app, path: str) -> None:
        self._app = app
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"test")],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 12345),
            "root_path": "",
        }
        self._sent: asyncio.Queue = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task = None
        self.status = None
        self.headers: dict[str, str] = {}

    async def _receive(self):
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message) -> None:
        await self._sent.put(message)

    async def __aenter__(self) -> "_SSEDriver":
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        msg = await asyncio.wait_for(self._sent.get(), timeout=5)
        assert msg["type"] == "http.response.start", msg
        self.status = msg["status"]
        self.headers = {k.decode(): v.decode() for k, v in msg["headers"]}
        return self

    async def read_until(self, needle: str, timeout: float = 5.0) -> str:
        buf = ""
        while needle not in buf:
            msg = await asyncio.wait_for(self._sent.get(), timeout=timeout)
            if msg["type"] == "http.response.body":
                buf += msg.get("body", b"").decode()
        return buf

    async def __aexit__(self, *exc) -> None:
        self._disconnect.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5)


class SSEEndpointTestCase(unittest.IsolatedAsyncioTestCase):
    """Tests for the SSE live-log endpoint (issue #131, A3 — acceptance)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["alpha", "beta"])
        self.app = create_app(base_dir=self.base, supervisor_log=None)
        self.log = self.base / ".logs" / "acme" / "widgets" / services.WORKER_LOG_NAME

    async def test_stream_emits_backlog_and_new_line(self) -> None:
        async with _SSEDriver(self.app, "/api/logs/acme/widgets/stream") as drv:
            self.assertEqual(drv.status, 200)
            self.assertEqual(drv.headers.get("content-type"), "text/event-stream; charset=utf-8")
            self.assertEqual(drv.headers.get("cache-control"), "no-cache")
            self.assertEqual(drv.headers.get("x-accel-buffering"), "no")
            # Backlog is delivered first.
            backlog = await drv.read_until("data: beta")
            self.assertIn("data: alpha", backlog)
            # A freshly appended line is pushed live.
            with self.log.open("a") as fh:
                fh.write("gamma-live\n")
                fh.flush()
            tail = await drv.read_until("data: gamma-live")
            self.assertIn("data: gamma-live", tail)

    def test_stream_unknown_repo_404(self) -> None:
        client = TestClient(self.app)
        self.assertEqual(client.get("/api/logs/acme/nope/stream").status_code, 404)

    def test_stream_traversal_rejected(self) -> None:
        client = TestClient(self.app)
        bad = client.get("/api/logs/%2e%2e/etc/stream")
        self.assertIn(bad.status_code, (404, 422))

    async def test_no_fd_leak_across_reconnects(self) -> None:
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.exists():
            self.skipTest("/proc/self/fd unavailable on this platform")

        async def one_cycle() -> None:
            async with _SSEDriver(self.app, "/api/logs/acme/widgets/stream") as drv:
                await drv.read_until("data: beta")
                with self.log.open("a") as fh:
                    fh.write("ping\n")
                    fh.flush()
                await drv.read_until("data: ping")

        await one_cycle()  # warm up (lazy imports, etc.)
        before = len(os.listdir(fd_dir))
        for _ in range(25):
            await one_cycle()
        after = len(os.listdir(fd_dir))
        # Allow a tiny slack for unrelated runtime fds; a real leak would add ~25.
        self.assertLessEqual(after, before + 2, f"fd leak: {before} -> {after}")


if __name__ == "__main__":
    unittest.main()
