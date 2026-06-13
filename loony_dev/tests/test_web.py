"""Tests for the read-only web dashboard (issues #130, #131, #132)."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from loony_dev.web import create_app
from loony_dev.web import entries
from loony_dev.web import routes, services, streaming

_HAS_PROC = sys.platform.startswith("linux") and Path("/proc/self/stat").exists()


def _proc_info(pid: int, *, ppid: int, cmdline: list[str], state: str = "S",
               starttime: int = 0, cpu_ticks: int = 0, wchan: str = "",
               io_bytes: int | None = 0) -> services.ProcInfo:
    """Build a synthetic :class:`~loony_dev.web.services.ProcInfo` for tests."""
    return services.ProcInfo(
        pid=pid,
        ppid=ppid,
        state=state,
        starttime=starttime,
        cpu_ticks=cpu_ticks,
        cmdline=cmdline,
        cmdline_str=" ".join(cmdline),
        wchan=wchan,
        io_bytes=io_bytes,
    )


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
        (self.base / ".logs" / ".hidden").mkdir(parents=True)
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

    def test_list_sessions_empty_without_files(self) -> None:
        self.assertEqual(services.list_sessions(self.base), [])

    def _write_conn(self, owner: str, repo: str, body: str, *, pid: int | None = None) -> None:
        repo_dir = self.base / ".logs" / owner / repo
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / services.REMOTE_CONTROL_CONN_NAME).write_text(body)
        if pid is not None:
            (repo_dir / services.REMOTE_CONTROL_PID_NAME).write_text(str(pid))

    def test_list_sessions_reads_remote_control_json(self) -> None:
        self._write_conn(
            "acme", "x",
            '{"session_id": "loony-acme-x", "repo": "acme/x", "key": "base", "extra": 9}',
        )
        sessions = services.list_sessions(self.base)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "loony-acme-x")
        self.assertEqual(sessions[0].repo, "acme/x")
        self.assertEqual(sessions[0].key, "base")

    def test_list_sessions_round_trips_join_url_and_mode(self) -> None:
        # A connection file as written by the supervisor's _write_connection_file:
        # the join_url and mode must round-trip through to the SessionView.
        self._write_conn(
            "acme", "x",
            '{"session_id": "loony-acme-x", "repo": "acme/x", "key": "base", '
            '"mode": "remote-control", "join_url": "https://claude.ai/c/abc123"}',
            pid=os.getpid(),
        )
        s = services.list_sessions(self.base)[0]
        self.assertEqual(s.join_url, "https://claude.ai/c/abc123")
        self.assertEqual(s.mode, "remote-control")
        self.assertIsNotNone(s.updated_at)
        self.assertTrue(s.alive)  # our own PID is alive

    def test_list_sessions_join_url_null_when_absent(self) -> None:
        # No join_url yet (Claude hasn't emitted the deep-link) must yield null,
        # not an error. With no PID file, alive falls back to null.
        self._write_conn("acme", "x", '{"session_id": "loony-acme-x", "repo": "acme/x", "key": "base"}')
        s = services.list_sessions(self.base)[0]
        self.assertIsNone(s.join_url)
        self.assertIsNone(s.mode)
        self.assertIsNone(s.alive)

    def test_list_sessions_alive_false_for_dead_pid(self) -> None:
        self._write_conn(
            "acme", "x",
            '{"session_id": "loony-acme-x", "repo": "acme/x", "key": "base"}',
            pid=0x7FFFFFFF,  # almost certainly not a live process
        )
        self.assertFalse(services.list_sessions(self.base)[0].alive)

    def test_list_sessions_skips_malformed_and_missing(self) -> None:
        # Good file for one repo, malformed JSON for another, and a third repo
        # with a worker but no connection file at all.
        self._write_conn("acme", "good", '{"session_id": "loony-acme-good", "repo": "acme/good", "key": "base"}')
        self._write_conn("acme", "bad", "{not json")
        _make_worker(self.base, "acme", "noconn", os.getpid(), ["x"])
        sessions = services.list_sessions(self.base)
        self.assertEqual([s.session_id for s in sessions], ["loony-acme-good"])

    def test_list_sessions_falls_back_to_repo_when_id_absent(self) -> None:
        self._write_conn("acme", "x", '{"repo": "acme/x", "key": "base"}')
        sessions = services.list_sessions(self.base)
        self.assertEqual(sessions[0].session_id, "acme/x")
        self.assertEqual(sessions[0].repo, "acme/x")

    def test_list_sessions_exposes_join_url(self) -> None:
        # The per-repo session card (#158) renders the join link from this field.
        self._write_conn(
            "acme", "x",
            '{"session_id": "loony-acme-x", "repo": "acme/x", "key": "base",'
            ' "join_url": "https://claude.ai/remote/abc"}',
        )
        sessions = services.list_sessions(self.base)
        self.assertEqual(sessions[0].join_url, "https://claude.ai/remote/abc")

    def test_list_sessions_join_url_absent_is_none(self) -> None:
        self._write_conn("acme", "x", '{"session_id": "loony-acme-x", "repo": "acme/x", "key": "base"}')
        sessions = services.list_sessions(self.base)
        self.assertIsNone(sessions[0].join_url)


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

    def test_static_assets_reachable(self) -> None:
        # The app shell loads its stylesheet and ES modules from /static.
        for path in ("/static/app.css", "/static/js/app.js", "/static/js/attach.js"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)

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

    def test_sessions_endpoint_surfaces_join_url_and_liveness(self) -> None:
        repo_dir = self.base / ".logs" / "acme" / "widgets"
        (repo_dir / services.REMOTE_CONTROL_CONN_NAME).write_text(
            '{"session_id": "loony-acme-widgets", "repo": "acme/widgets", "key": "base", '
            '"mode": "remote-control", "join_url": "https://claude.ai/c/xyz"}'
        )
        (repo_dir / services.REMOTE_CONTROL_PID_NAME).write_text(str(os.getpid()))
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        s = body[0]
        self.assertEqual(s["join_url"], "https://claude.ai/c/xyz")
        self.assertEqual(s["mode"], "remote-control")
        self.assertTrue(s["alive"])
        self.assertIsNotNone(s["updated_at"])

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


class EntriesTestCase(unittest.TestCase):
    """Unit tests for the skills/commands data layer (no HTTP)."""

    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory()
        self._base_tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._home_tmp.name)
        self.base = Path(self._base_tmp.name)
        self.addCleanup(self._home_tmp.cleanup)
        self.addCleanup(self._base_tmp.cleanup)

    def _kw(self, **over):
        kw = dict(global_root=self.home, base_dir=self.base, scope="global")
        kw.update(over)
        return kw

    def test_global_skill_write_creates_skill_md(self) -> None:
        view = entries.write_entry("skills", "deploy", "# Deploy\nbody\n", **self._kw())
        expected = self.home / "skills" / "deploy" / "SKILL.md"
        self.assertTrue(expected.is_file())
        self.assertEqual(expected.read_text(), "# Deploy\nbody\n")
        self.assertEqual(view.name, "deploy")
        self.assertEqual(view.path, str(expected))
        self.assertEqual(view.size, len("# Deploy\nbody\n".encode()))
        self.assertIsNotNone(view.modified_at)
        listed = entries.list_entries("skills", **self._kw())
        self.assertEqual([e.name for e in listed], ["deploy"])
        self.assertEqual(listed[0].size, view.size)

    def test_global_command_write_creates_md_file(self) -> None:
        entries.write_entry("commands", "ship", "do it\n", **self._kw())
        expected = self.home / "commands" / "ship.md"
        self.assertTrue(expected.is_file())
        self.assertEqual(expected.read_text(), "do it\n")
        self.assertEqual([e.name for e in entries.list_entries("commands", **self._kw())], ["ship"])

    def test_per_repo_write_lands_in_checkout_claude_dir(self) -> None:
        entries.write_entry(
            "skills", "lint", "x\n",
            **self._kw(scope="repo", owner="acme", repo="widgets"),
        )
        expected = self.base / "acme" / "widgets" / ".claude" / "skills" / "lint" / "SKILL.md"
        self.assertTrue(expected.is_file())
        self.assertEqual(expected.read_text(), "x\n")

    def test_overwrite_is_idempotent(self) -> None:
        entries.write_entry("commands", "ship", "v1\n", **self._kw())
        entries.write_entry("commands", "ship", "v2\n", **self._kw())
        self.assertEqual((self.home / "commands" / "ship.md").read_text(), "v2\n")
        self.assertEqual(len(entries.list_entries("commands", **self._kw())), 1)

    def test_read_entry_returns_content(self) -> None:
        entries.write_entry("skills", "deploy", "hello\n", **self._kw())
        self.assertEqual(entries.read_entry("skills", "deploy", **self._kw()), "hello\n")

    def test_read_missing_raises_not_found(self) -> None:
        with self.assertRaises(entries.EntryNotFoundError):
            entries.read_entry("skills", "ghost", **self._kw())

    def test_delete_skill_removes_directory(self) -> None:
        entries.write_entry("skills", "deploy", "x\n", **self._kw())
        skill_dir = self.home / "skills" / "deploy"
        self.assertTrue(skill_dir.is_dir())
        entries.delete_entry("skills", "deploy", **self._kw())
        self.assertFalse(skill_dir.exists())

    def test_delete_command_unlinks_file(self) -> None:
        entries.write_entry("commands", "ship", "x\n", **self._kw())
        entries.delete_entry("commands", "ship", **self._kw())
        self.assertFalse((self.home / "commands" / "ship.md").exists())

    def test_delete_missing_raises_not_found(self) -> None:
        for kind in ("skills", "commands"):
            with self.subTest(kind=kind), self.assertRaises(entries.EntryNotFoundError):
                entries.delete_entry(kind, "ghost", **self._kw())

    def test_validate_name_rejects_traversal(self) -> None:
        for bad in ("..", "a/b", ".", "", "a\\b", "a\x00b"):
            with self.subTest(name=bad), self.assertRaises(entries.EntryError):
                entries.write_entry("skills", bad, "x", **self._kw())

    def test_per_repo_traversal_in_owner_repo_rejected(self) -> None:
        for owner, repo in [("..", "x"), ("acme", ".."), ("a/b", "c")]:
            with self.subTest(owner=owner, repo=repo), self.assertRaises(entries.EntryError):
                entries.write_entry("skills", "n", "x", **self._kw(scope="repo", owner=owner, repo=repo))

    def test_repo_scope_requires_owner_and_repo(self) -> None:
        with self.assertRaises(entries.EntryError):
            entries.list_entries("skills", **self._kw(scope="repo"))

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(entries.EntryError):
            entries.list_entries("widgets", **self._kw())

    def test_invalid_scope_rejected(self) -> None:
        with self.assertRaises(entries.EntryError):
            entries.list_entries("skills", **self._kw(scope="bogus"))

    def test_list_empty_when_no_container(self) -> None:
        self.assertEqual(entries.list_entries("skills", **self._kw()), [])

    def test_containment_check_rejects_symlink_escape(self) -> None:
        # A symlinked skills/ container pointing outside <claude-dir> must not let
        # a write escape, even though the name itself is valid.
        outside = self.base / "outside"
        outside.mkdir()
        skills = self.home / "skills"
        skills.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(entries.EntryError):
            entries.write_entry("skills", "evil", "x", **self._kw())

    def test_list_omits_symlinked_entry_escaping_root(self) -> None:
        # A per-entry symlink whose SKILL.md resolves outside <claude-dir> must
        # not leak its off-root path/metadata via listing.
        entries.write_entry("skills", "real", "x\n", **self._kw())
        outside = self.base / "off_root"
        (outside / "SKILL.md").parent.mkdir(parents=True)
        (outside / "SKILL.md").write_text("secret\n")
        (self.home / "skills" / "evil").symlink_to(outside, target_is_directory=True)
        listed = entries.list_entries("skills", **self._kw())
        self.assertEqual([e.name for e in listed], ["real"])

    def test_delete_skill_without_skill_md_is_not_found(self) -> None:
        # A directory under skills/ that lacks SKILL.md is not a canonical skill;
        # deleting it must 404 rather than rmtree unrelated data.
        stray = self.home / "skills" / "stray"
        stray.mkdir(parents=True)
        (stray / "keepme.txt").write_text("important\n")
        with self.assertRaises(entries.EntryNotFoundError):
            entries.delete_entry("skills", "stray", **self._kw())
        self.assertTrue((stray / "keepme.txt").is_file())


class EntriesApiTestCase(unittest.TestCase):
    """Integration tests for the skills/commands endpoints via TestClient."""

    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory()
        self._base_tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._home_tmp.name)
        self.base = Path(self._base_tmp.name)
        self.addCleanup(self._home_tmp.cleanup)
        self.addCleanup(self._base_tmp.cleanup)
        self.client = TestClient(create_app(base_dir=self.base, claude_home=self.home))
        self.addCleanup(self.client.close)

    def test_put_then_list_and_on_disk(self) -> None:
        resp = self.client.put("/api/skills/deploy", content="# Deploy\n")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "deploy")
        # Available to a freshly-spawned session => correct on-disk location.
        on_disk = self.home / "skills" / "deploy" / "SKILL.md"
        self.assertTrue(on_disk.is_file())
        self.assertEqual(on_disk.read_text(), "# Deploy\n")
        listed = self.client.get("/api/skills").json()
        self.assertEqual([e["name"] for e in listed], ["deploy"])

    def test_raw_markdown_frontmatter_roundtrips(self) -> None:
        md = "---\nname: deploy\ndescription: ship it\n---\n\n# Body\n- a\n- b\n"
        self.client.put("/api/skills/deploy", content=md)
        got = self.client.get("/api/skills/deploy").json()
        self.assertEqual(got["content"], md)

    def test_commands_crud(self) -> None:
        self.assertEqual(self.client.put("/api/commands/ship", content="go\n").status_code, 200)
        self.assertTrue((self.home / "commands" / "ship.md").is_file())
        self.assertEqual(self.client.get("/api/commands/ship").json()["content"], "go\n")
        self.assertEqual(self.client.delete("/api/commands/ship").status_code, 204)
        self.assertFalse((self.home / "commands" / "ship.md").exists())

    def test_traversal_in_name_rejected(self) -> None:
        # Literal ".." is collapsed by URL normalisation (routing 404); encoded
        # "%2e%2e" reaches our validator. Neither must ever yield a 200/escape.
        self.assertIn(self.client.put("/api/skills/..", content="x").status_code, (400, 404, 422))
        bad = self.client.put("/api/skills/%2e%2e", content="x")
        self.assertIn(bad.status_code, (400, 404, 422))
        self.assertNotEqual(bad.status_code, 200)
        # Embedded separators never escape: routing splits the path so no foreign
        # file is created (segment-level rejection is unit-tested via _validate_name).
        bad_sep = self.client.put("/api/commands/a%2Fb", content="x")
        self.assertNotEqual(bad_sep.status_code, 200)
        self.assertEqual(self.client.get("/api/commands").json(), [])

    def test_per_repo_scope_lands_under_checkout(self) -> None:
        resp = self.client.put(
            "/api/skills/lint",
            params={"scope": "repo", "owner": "acme", "repo": "widgets"},
            content="x\n",
        )
        self.assertEqual(resp.status_code, 200)
        on_disk = self.base / "acme" / "widgets" / ".claude" / "skills" / "lint" / "SKILL.md"
        self.assertTrue(on_disk.is_file())
        listed = self.client.get(
            "/api/skills", params={"scope": "repo", "owner": "acme", "repo": "widgets"}
        ).json()
        self.assertEqual([e["name"] for e in listed], ["lint"])

    def test_delete_unknown_404(self) -> None:
        self.assertEqual(self.client.delete("/api/skills/ghost").status_code, 404)

    def test_read_unknown_404(self) -> None:
        self.assertEqual(self.client.get("/api/skills/ghost").status_code, 404)


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


async def _read_first_sse_event(drv: "_SSEDriver") -> str:
    """Return the body of the first complete SSE event (a ``data:`` payload)."""
    raw = await drv.read_until("\n\n")
    event = raw.split("\n\n", 1)[0]
    return "".join(
        line[len("data: "):] for line in event.split("\n") if line.startswith("data: ")
    )


class StateEventsEndpointTestCase(unittest.IsolatedAsyncioTestCase):
    """Tests for the consolidated state SSE endpoint ``/api/events`` (issue #155)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["alpha", "beta"])
        self.app = create_app(base_dir=self.base, supervisor_log=None)

    async def test_events_emits_initial_snapshot_with_all_keys(self) -> None:
        async with _SSEDriver(self.app, "/api/events") as drv:
            self.assertEqual(drv.status, 200)
            self.assertEqual(
                drv.headers.get("content-type"), "text/event-stream; charset=utf-8"
            )
            self.assertEqual(drv.headers.get("cache-control"), "no-cache")
            self.assertEqual(drv.headers.get("x-accel-buffering"), "no")
            snapshot = json.loads(await _read_first_sse_event(drv))
        self.assertEqual(
            set(snapshot),
            {"workers", "worktrees", "sessions", "task_sessions", "stuck"},
        )
        # The snapshot mirrors the per-resource endpoints: the seeded worker shows.
        self.assertEqual([w["repo"] for w in snapshot["workers"]], ["acme/widgets"])

    async def test_events_heartbeat_during_idle(self) -> None:
        # Shrink the cadences so an idle period elapses in test time; the state is
        # static, so after the initial snapshot only heartbeats should arrive.
        with mock.patch.object(routes, "SSE_STATE_INTERVAL", 0.02), \
                mock.patch.object(routes, "SSE_HEARTBEAT_INTERVAL", 0.02):
            async with _SSEDriver(self.app, "/api/events") as drv:
                await _read_first_sse_event(drv)
                heartbeat = await drv.read_until(": heartbeat")
                self.assertIn(": heartbeat", heartbeat)

    async def test_events_disconnect_tears_down_cleanly(self) -> None:
        # Entering reads the initial snapshot; exiting delivers http.disconnect and
        # awaits the ASGI task with a timeout — a leaked/wedged stream would hang.
        async with _SSEDriver(self.app, "/api/events") as drv:
            snapshot = json.loads(await _read_first_sse_event(drv))
            self.assertIn("workers", snapshot)


class StuckHeuristicTestCase(unittest.TestCase):
    """Unit tests for ``list_stuck`` with synthetic process trees.

    The OS layer (``_proc_snapshot`` / ``_descendants`` / ``_subtree_activity``)
    is monkeypatched so no real ``/proc`` is required and CPU/IO deltas are fully
    controlled. Tree: worker W -> claude C -> sleep S.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Worker PID is our own (so process_status -> "running").
        self.worker_pid = os.getpid()
        _make_worker(self.base, "acme", "widgets", self.worker_pid, ["x"])

        self.claude_pid = 100100
        self.sleep_pid = 100200
        self.tree = {self.worker_pid: [self.claude_pid], self.claude_pid: [self.sleep_pid]}
        self.infos = {
            self.worker_pid: _proc_info(self.worker_pid, ppid=1, cmdline=["python", "-m", "loony_dev"]),
            self.claude_pid: _proc_info(self.claude_pid, ppid=self.worker_pid, cmdline=["claude", "-p"]),
            self.sleep_pid: _proc_info(
                self.sleep_pid, ppid=self.claude_pid, cmdline=["sleep", "99999"],
                wchan="hrtimer_nanosleep",
            ),
        }

    def _descendants(self, pid: int):
        out: list[int] = []
        queue = list(self.tree.get(pid, []))
        while queue:
            child = queue.pop(0)
            out.append(child)
            queue.extend(self.tree.get(child, []))
        return iter(out)

    def _patch(self, activity_samples):
        """Patch the OS layer; *activity_samples* is an iterable of ActivitySample."""
        it = iter(activity_samples)
        return mock.patch.multiple(
            services,
            _proc_snapshot=lambda pid: self.infos.get(pid),
            _descendants=self._descendants,
            _subtree_activity=lambda root: next(it),
            _proc_age_seconds=lambda st: 10_000.0,  # always "old"
        )

    @staticmethod
    def _sample(cpu, io=0, io_available=True, ts=0.0) -> services.ActivitySample:
        return services.ActivitySample(cpu_ticks=cpu, io_bytes=io,
                                       io_available=io_available, timestamp=ts)

    def test_flags_idle_sleep_subtree(self) -> None:
        # Two identical samples -> no CPU/IO progress -> stuck.
        with self._patch([self._sample(5), self._sample(5)]):
            stuck = services.list_stuck(self.base, threshold_seconds=300,
                                        activity_sample_seconds=0)
        self.assertEqual(len(stuck), 1)
        s = stuck[0]
        self.assertEqual(s.pid, self.sleep_pid)
        self.assertEqual(s.worker_repo, "acme/widgets")
        self.assertEqual(s.cmdline, "sleep 99999")
        self.assertEqual(s.blocked_on, "hrtimer_nanosleep")
        self.assertGreaterEqual(s.age_seconds, 300)

    def test_no_false_positive_when_cpu_advances(self) -> None:
        # The reviewer's scenario: subtree CPU advances between samples (Claude is
        # actively working even though a child sleeps) -> NOT stuck.
        with self._patch([self._sample(5), self._sample(42)]):
            stuck = services.list_stuck(self.base, threshold_seconds=300,
                                        activity_sample_seconds=0)
        self.assertEqual(stuck, [])

    def test_no_false_positive_when_io_advances(self) -> None:
        with self._patch([self._sample(5, io=1000), self._sample(5, io=9000)]):
            stuck = services.list_stuck(self.base, threshold_seconds=300,
                                        activity_sample_seconds=0)
        self.assertEqual(stuck, [])

    def test_no_flag_when_younger_than_threshold(self) -> None:
        with mock.patch.multiple(
            services,
            _proc_snapshot=lambda pid: self.infos.get(pid),
            _descendants=self._descendants,
            _subtree_activity=lambda root: self._sample(5),
            _proc_age_seconds=lambda st: 5.0,  # too young
        ):
            stuck = services.list_stuck(self.base, threshold_seconds=300,
                                        activity_sample_seconds=0)
        self.assertEqual(stuck, [])

    def test_no_flag_for_running_state(self) -> None:
        # A descendant burning CPU (state R, e.g. running tests) is excluded at
        # the blocked-syscall stage — no activity sample is even taken.
        self.infos[self.sleep_pid] = _proc_info(
            self.sleep_pid, ppid=self.claude_pid, cmdline=["pytest"],
            state="R", wchan="",
        )

        def _boom(root):
            raise AssertionError("activity sampling should not run without a candidate")

        with mock.patch.multiple(
            services,
            _proc_snapshot=lambda pid: self.infos.get(pid),
            _descendants=self._descendants,
            _subtree_activity=_boom,
            _proc_age_seconds=lambda st: 10_000.0,
        ):
            stuck = services.list_stuck(self.base, threshold_seconds=300,
                                        activity_sample_seconds=0)
        self.assertEqual(stuck, [])

    def test_skips_non_running_worker(self) -> None:
        # A worker whose PID is dead is never inspected.
        base2 = Path(self._tmp.name) / "dead"
        _make_worker(base2, "acme", "ghost", 0x7FFFFFFF, ["x"])
        with self._patch([self._sample(5), self._sample(5)]):
            stuck = services.list_stuck(base2, threshold_seconds=0,
                                        activity_sample_seconds=0)
        self.assertEqual(stuck, [])

    def test_stuck_view_carries_owning_session(self) -> None:
        # When the worker's repo advertises a session, the stuck descendant is
        # tagged with that session's id/key so the ESC endpoint can address it.
        conn = self.base / ".logs" / "acme" / "widgets" / services.REMOTE_CONTROL_CONN_NAME
        conn.write_text(json.dumps(
            {"session_id": "loony-acme-widgets", "repo": "acme/widgets", "key": "base"}
        ))
        with self._patch([self._sample(5), self._sample(5)]):
            stuck = services.list_stuck(self.base, threshold_seconds=300,
                                        activity_sample_seconds=0)
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0].session_id, "loony-acme-widgets")
        self.assertEqual(stuck[0].task_key, "base")


@unittest.skipUnless(_HAS_PROC, "requires a Linux /proc filesystem")
class StuckRealProcessTestCase(unittest.TestCase):
    """Acceptance reproducer against real child processes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Use this test process as the "worker"; spawned children are descendants.
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["x"])
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._children:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    def _spawn(self, args: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children.append(proc)
        return proc

    def test_sleep_child_is_flagged(self) -> None:
        proc = self._spawn(["sleep", "99999"])
        time.sleep(0.2)  # let it settle into nanosleep
        stuck = services.list_stuck(self.base, threshold_seconds=0,
                                    activity_sample_seconds=0.2)
        pids = {s.pid for s in stuck}
        self.assertIn(proc.pid, pids)
        view = next(s for s in stuck if s.pid == proc.pid)
        self.assertIn("sleep", view.cmdline)

    def test_sleep_child_flagged_via_api(self) -> None:
        proc = self._spawn(["sleep", "99999"])
        time.sleep(0.2)
        client = TestClient(create_app(
            base_dir=self.base, supervisor_log=None,
            stuck_after_seconds=0, activity_sample_seconds=0.2,
        ))
        resp = client.get("/api/stuck")
        self.assertEqual(resp.status_code, 200)
        pids = {row["pid"] for row in resp.json()}
        self.assertIn(proc.pid, pids)

    def test_busy_child_not_flagged(self) -> None:
        # A CPU-burning child (running tests analogue) is state R, not blocked,
        # so it is never reported — guards the legitimate-long-op false positive.
        proc = self._spawn([sys.executable, "-c", "while True: pass"])
        time.sleep(0.2)
        stuck = services.list_stuck(self.base, threshold_seconds=0,
                                    activity_sample_seconds=0.2)
        self.assertNotIn(proc.pid, {s.pid for s in stuck})


@unittest.skipUnless(_HAS_PROC, "requires a Linux /proc filesystem")
class KillDescendantTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["x"])
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._children:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    def _spawn_sleep(self) -> subprocess.Popen:
        proc = subprocess.Popen(["sleep", "99999"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children.append(proc)
        time.sleep(0.1)
        return proc

    def test_kill_terminates_only_target(self) -> None:
        proc = self._spawn_sleep()
        self.assertTrue(services.is_worker_descendant(self.base, proc.pid))
        status = services.kill_descendant(self.base, proc.pid, grace_seconds=2.0)
        self.assertEqual(status["pid"], proc.pid)
        self.assertEqual(status["signal_sent"], "SIGTERM")
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())  # the sleep is gone
        os.kill(os.getpid(), 0)  # the "worker" (this process) is untouched

    def test_kill_rejects_non_descendant(self) -> None:
        # PID 1 (init) is never a descendant and is rejected outright.
        self.assertFalse(services.is_worker_descendant(self.base, 1))
        with self.assertRaises(services.NotADescendantError):
            services.kill_descendant(self.base, 1)

    def test_kill_rejects_ancestor(self) -> None:
        parent = os.getppid()
        if parent > 1:
            self.assertFalse(services.is_worker_descendant(self.base, parent))

    def test_api_kill_pid_le_1_rejected(self) -> None:
        client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        self.assertEqual(client.post("/api/processes/1/kill").status_code, 422)
        self.assertEqual(client.post("/api/processes/0/kill").status_code, 422)

    def test_api_kill_non_descendant_404(self) -> None:
        client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        # A high, almost-certainly-nonexistent PID is not a descendant.
        resp = client.post("/api/processes/2147483646/kill")
        self.assertEqual(resp.status_code, 404)

    def test_api_kill_descendant_ok(self) -> None:
        proc = self._spawn_sleep()
        client = TestClient(create_app(
            base_dir=self.base, supervisor_log=None, kill_grace_seconds=2.0,
        ))
        resp = client.post(f"/api/processes/{proc.pid}/kill")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["pid"], proc.pid)
        self.assertEqual(body["signal_sent"], "SIGTERM")
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())


class _FakeControlServer:
    """A tiny Unix-socket server standing in for a ClaudeSession control channel.

    Records the command it received and replies with a canned line, so the
    services/route layer can be tested without a real session.
    """

    def __init__(self, path: Path, reply: bytes = b"interrupted\n") -> None:
        self.path = path
        self.reply = reply
        self.received: list[str] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(4)
        self._sock.settimeout(5.0)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._closed = False
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                if self._closed:
                    break
                continue
            with conn:
                try:
                    data = conn.recv(256)
                    self.received.append(data.decode("utf-8").strip())
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class SessionInterruptServiceTestCase(unittest.TestCase):
    """Unit tests for ``interrupt_session`` and the auto-interrupt selector."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write_conn(self, owner: str, repo: str, body: dict) -> Path:
        repo_dir = self.base / ".logs" / owner / repo
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / services.REMOTE_CONTROL_CONN_NAME).write_text(json.dumps(body))
        return repo_dir

    def test_list_sessions_reads_control_socket(self) -> None:
        self._write_conn("acme", "x", {
            "session_id": "s1", "repo": "acme/x", "key": "base",
            "control_socket": "/tmp/s1.sock",
        })
        sessions = services.list_sessions(self.base)
        self.assertEqual(sessions[0].control_socket, "/tmp/s1.sock")

    def test_interrupt_unknown_session_raises(self) -> None:
        with self.assertRaises(services.SessionNotFoundError):
            services.interrupt_session(self.base, "nope")

    def test_interrupt_session_without_control_channel_raises(self) -> None:
        self._write_conn("acme", "x", {"session_id": "s1", "repo": "acme/x", "key": "base"})
        with self.assertRaises(services.SessionControlError):
            services.interrupt_session(self.base, "s1")

    def test_interrupt_session_round_trips_over_socket(self) -> None:
        repo_dir = self._write_conn("acme", "x", {"session_id": "s1", "repo": "acme/x", "key": "base"})
        sock_path = repo_dir / "ctl.sock"
        server = _FakeControlServer(sock_path, reply=b"interrupted\n")
        self.addCleanup(server.close)
        # Re-point the connection file at the live socket.
        self._write_conn("acme", "x", {
            "session_id": "s1", "repo": "acme/x", "key": "base",
            "control_socket": str(sock_path),
        })
        result = services.interrupt_session(self.base, "s1")
        self.assertTrue(result["interrupted"])
        self.assertEqual(result["repo"], "acme/x")
        self.assertEqual(server.received, ["interrupt"])

    def test_interrupt_session_idle_reply(self) -> None:
        repo_dir = self._write_conn("acme", "x", {"session_id": "s1", "repo": "acme/x", "key": "base"})
        sock_path = repo_dir / "ctl.sock"
        server = _FakeControlServer(sock_path, reply=b"idle\n")
        self.addCleanup(server.close)
        self._write_conn("acme", "x", {
            "session_id": "s1", "repo": "acme/x", "key": "base",
            "control_socket": str(sock_path),
        })
        result = services.interrupt_session(self.base, "s1")
        self.assertFalse(result["interrupted"])

    def test_interrupt_session_unreachable_socket_raises(self) -> None:
        self._write_conn("acme", "x", {
            "session_id": "s1", "repo": "acme/x", "key": "base",
            "control_socket": str(self.base / "missing.sock"),
        })
        with self.assertRaises(services.SessionControlError):
            services.interrupt_session(self.base, "s1")

    def test_auto_interrupt_candidates_selects_old_sessions(self) -> None:
        def view(session_id, age):
            return services.StuckProcessView(
                worker_repo="acme/x", task_key="base", pid=1, cmdline="sleep",
                age_seconds=age, blocked_on="nanosleep", session_id=session_id,
            )
        stuck = [view("s1", 1000), view("s1", 50), view("s2", 50), view(None, 9999)]
        # Disabled by default.
        self.assertEqual(
            services.auto_interrupt_candidates(stuck, auto_interrupt_after_seconds=0), [])
        # Only s1 (>= 600) qualifies; deduped; the None-session row is ignored.
        self.assertEqual(
            services.auto_interrupt_candidates(stuck, auto_interrupt_after_seconds=600),
            ["s1"])


@unittest.skipUnless(_HAS_PROC, "Unix-domain control sockets require a POSIX platform")
class SessionInterruptApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))

    def _write_conn(self, owner: str, repo: str, body: dict) -> Path:
        repo_dir = self.base / ".logs" / owner / repo
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / services.REMOTE_CONTROL_CONN_NAME).write_text(json.dumps(body))
        return repo_dir

    def test_api_interrupt_unknown_session_404(self) -> None:
        resp = self.client.post("/api/sessions/nope/interrupt")
        self.assertEqual(resp.status_code, 404)

    def test_api_interrupt_without_control_channel_409(self) -> None:
        self._write_conn("acme", "x", {"session_id": "s1", "repo": "acme/x", "key": "base"})
        resp = self.client.post("/api/sessions/s1/interrupt")
        self.assertEqual(resp.status_code, 409)

    def test_api_interrupt_ok(self) -> None:
        repo_dir = self._write_conn("acme", "x", {"session_id": "s1", "repo": "acme/x", "key": "base"})
        sock_path = repo_dir / "ctl.sock"
        server = _FakeControlServer(sock_path, reply=b"interrupted\n")
        self.addCleanup(server.close)
        self._write_conn("acme", "x", {
            "session_id": "s1", "repo": "acme/x", "key": "base",
            "control_socket": str(sock_path),
        })
        resp = self.client.post("/api/sessions/s1/interrupt")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["session_id"], "s1")
        self.assertTrue(body["interrupted"])


@unittest.skipUnless(_HAS_PROC, "ClaudeSession PTY/socket reproducer requires POSIX")
class SessionInterruptReproducerTestCase(unittest.TestCase):
    """Acceptance reproducer (issue #163): a long turn is ESC'd via the HTTP API
    and the session returns to idle without process death."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cwd = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # CLAUDE_CONFIG_DIR must be set before the session is constructed: the
        # JSONL path is computed at __init__ from the environment.
        self.enterContext(mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)}))

    def test_long_turn_interrupted_via_api(self) -> None:
        from loony_dev.agents.claude_session import ClaudeSession, _is_interrupt

        stub = Path(__file__).parent / "_claude_stub.py"
        os.chmod(stub, 0o755)

        repo_dir = self.base / ".logs" / "acme" / "widgets"
        repo_dir.mkdir(parents=True, exist_ok=True)
        sock_path = repo_dir / "ctl.sock"

        session = ClaudeSession(
            self.cwd, session_id="repro-163", binary=str(stub),
            readiness_timeout=10.0, debounce=0.2, control_socket=sock_path,
            env={"STUB_LONGTURN_SECS": "20"},
        )
        session.open()
        self.addCleanup(session.close)

        # Advertise the session (with its control socket) the way the supervisor
        # would, so the dashboard can resolve and reach it.
        (repo_dir / services.REMOTE_CONTROL_CONN_NAME).write_text(json.dumps({
            "session_id": "repro-163", "repo": "acme/widgets", "key": "base",
            "control_socket": str(sock_path),
        }))

        pid = session.pid
        result: dict[str, object] = {}

        def run_long() -> None:
            result["turn"] = session.send_turn("LONGTURN please", timeout=20.0)

        t = threading.Thread(target=run_long)
        t.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and b"LONGTURN" not in session.recent_output():
            time.sleep(0.05)
        # Hard barrier: fail at setup if the turn never started, rather than
        # intermittently at the interrupt assertions below.
        self.assertIn(b"LONGTURN", session.recent_output())

        client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        resp = client.post("/api/sessions/repro-163/interrupt")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["interrupted"])

        t.join(timeout=10.0)
        self.assertFalse(t.is_alive())
        self.assertTrue(result["turn"].was_interrupted)

        # The JSONL records the interrupt, and the session is alive and steerable.
        entries = [json.loads(line) for line in
                   session.jsonl_path.read_text().splitlines() if line.strip()]
        self.assertTrue(any(_is_interrupt(e) for e in entries))
        follow_up = session.send_turn("after interrupt", timeout=10.0)
        self.assertFalse(follow_up.was_interrupted)
        self.assertEqual(session.pid, pid)


if __name__ == "__main__":
    unittest.main()
