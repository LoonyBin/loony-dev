"""Tests for the read-only web dashboard (issue #130)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loony_dev.web import create_app
from loony_dev.web import entries
from loony_dev.web import services


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
        self.client.put("/api/commands/a%2Fb", content="x")
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


if __name__ == "__main__":
    unittest.main()
