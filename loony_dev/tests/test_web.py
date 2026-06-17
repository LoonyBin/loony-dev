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
from typing import ClassVar
from unittest import mock

from fastapi.testclient import TestClient

from loony_dev import session_registry
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


def _make_pipeline_log(
    base: Path, owner: str, repo: str, key: str, log_lines: list[str], *, sidecar: bool = True
) -> None:
    """Create a fake pipelines/<slug>.{log,key} for one pipeline key (issue #220)."""
    from loony_dev import pipeline_log

    log_path = pipeline_log.pipeline_log_path(base, owner, repo, key)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(log_lines) + "\n")
    if sidecar:
        pipeline_log.pipeline_key_sidecar_path(base, owner, repo, key).write_text(key)


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

    # ── Per-pipeline logs (issue #220) ──────────────────────────────────────

    def test_tail_pipeline_log_returns_last_n_lines(self) -> None:
        lines = [f"log {i}" for i in range(20)]
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", lines)
        tail = services.tail_pipeline_log(self.base, "acme", "widgets", "issue-5", 3)
        self.assertEqual(tail, ["log 17", "log 18", "log 19"])

    def test_tail_pipeline_log_missing_raises(self) -> None:
        with self.assertRaises(services.LogNotFoundError):
            services.tail_pipeline_log(self.base, "acme", "widgets", "issue-404", 10)

    def test_tail_pipeline_log_rejects_traversal(self) -> None:
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", ["x"])
        for key in ("..", "a/b", "with\x00nul", "."):
            with self.assertRaises(services.LogNotFoundError):
                services.tail_pipeline_log(self.base, "acme", "widgets", key, 10)

    def test_list_pipeline_logs_recovers_keys_from_sidecars(self) -> None:
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", ["a"])
        _make_pipeline_log(self.base, "acme", "widgets", "pr-9", ["b"])
        keys = services.list_pipeline_logs(self.base, "acme", "widgets")
        self.assertEqual(sorted(keys), ["issue-5", "pr-9"])

    def test_list_pipeline_logs_skips_log_without_sidecar(self) -> None:
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", ["a"], sidecar=False)
        self.assertEqual(services.list_pipeline_logs(self.base, "acme", "widgets"), [])

    def test_list_pipeline_logs_empty_when_no_dir(self) -> None:
        self.assertEqual(services.list_pipeline_logs(self.base, "acme", "widgets"), [])

    def test_pipeline_activity_parses_well_formed_and_falls_back(self) -> None:
        _make_pipeline_log(
            self.base, "acme", "widgets", "issue-5",
            [
                "2026-06-17 12:00:00,123 [INFO] loony_dev.agents.coding: implementing",
                "2026-06-17 12:00:01,000 [INFO] loony_dev.orchestrator: Dispatching task",
                "    File \"x.py\", line 1, in f",  # a wrapped traceback continuation
            ],
        )
        events = services.pipeline_activity(self.base, "acme", "widgets", "issue-5", 100)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["level"], "INFO")
        self.assertEqual(events[0]["logger"], "loony_dev.agents.coding")
        self.assertEqual(events[0]["message"], "implementing")
        self.assertEqual(events[0]["actor"], "trixy")
        self.assertEqual(events[1]["actor"], "system")
        # The non-matching continuation line is kept verbatim, not dropped.
        self.assertIsNone(events[2]["ts"])
        self.assertIn("x.py", events[2]["message"])

    def test_pipeline_activity_attributes_operator(self) -> None:
        _make_pipeline_log(
            self.base, "acme", "widgets", "issue-5",
            ["2026-06-17 12:00:00,123 [INFO] loony_dev.orchestrator: running operator-injected turn"],
        )
        events = services.pipeline_activity(self.base, "acme", "widgets", "issue-5", 100)
        self.assertEqual(events[0]["actor"], "operator")

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


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "trixy", "GIT_AUTHOR_EMAIL": "trixy@example.com",
    "GIT_COMMITTER_NAME": "trixy", "GIT_COMMITTER_EMAIL": "trixy@example.com",
}


def _git_repo_with_commits(checkout: Path, subjects: list[str]) -> None:
    """Create a real git checkout at *checkout* with one commit per subject.

    Commits are made oldest-first, so the newest subject is the last in the list.
    """
    checkout.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_GIT_ENV}

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True, env=env)

    run("init", "-q", "-b", "main")
    for i, subject in enumerate(subjects):
        (checkout / "f.txt").write_text(f"{i}\n")
        run("add", "-A")
        run("commit", "-q", "-m", subject)


class RecentCommitsTestCase(unittest.TestCase):
    """`recent_commits` reads a real local git log (issue #224)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_returns_commits_newest_first_with_fields(self) -> None:
        _git_repo_with_commits(
            self.base / "acme" / "widgets",
            ["feat: one", "fix: two", "chore: three"],
        )
        commits = services.recent_commits(self.base, "acme", "widgets", limit=5)
        # Newest first, and all three present.
        self.assertEqual(
            [c.subject for c in commits], ["chore: three", "fix: two", "feat: one"]
        )
        top = commits[0]
        self.assertEqual(top.author, "trixy")
        self.assertEqual(len(top.sha), 40)
        self.assertTrue(top.sha.startswith(top.short_sha))
        self.assertTrue(top.date_iso)  # ISO-8601 committer date
        self.assertTrue(top.rel_date)  # relative ("… ago")

    def test_limit_clamps_count(self) -> None:
        _git_repo_with_commits(
            self.base / "acme" / "widgets", [f"c{i}" for i in range(8)]
        )
        self.assertEqual(len(services.recent_commits(self.base, "acme", "widgets", limit=3)), 3)
        # Below/above the 1–20 bound is clamped, never raised.
        self.assertEqual(len(services.recent_commits(self.base, "acme", "widgets", limit=0)), 1)
        self.assertEqual(len(services.recent_commits(self.base, "acme", "widgets", limit=999)), 8)

    def test_non_git_checkout_raises_checkout_not_found(self) -> None:
        (self.base / "acme" / "widgets").mkdir(parents=True)
        with self.assertRaises(services.CheckoutNotFoundError):
            services.recent_commits(self.base, "acme", "widgets")

    def test_missing_checkout_raises_checkout_not_found(self) -> None:
        with self.assertRaises(services.CheckoutNotFoundError):
            services.recent_commits(self.base, "acme", "nope")

    def test_invalid_segment_raises_checkout_not_found(self) -> None:
        for owner, name in [("..", "widgets"), ("acme", ".."), ("a/b", "c"), ("", "x")]:
            with self.subTest(owner=owner, name=name):
                with self.assertRaises(services.CheckoutNotFoundError):
                    services.recent_commits(self.base, owner, name)

    def test_git_failure_raises_git_command_error(self) -> None:
        # A checkout whose .git exists but is corrupt: git log exits non-zero,
        # which must surface as GitCommandError (raise on failure, per CLAUDE.md)
        # rather than silently degrading to an empty list.
        checkout = self.base / "acme" / "broken"
        checkout.mkdir(parents=True)
        (checkout / ".git").write_text("not a git dir\n")
        with self.assertRaises(services.GitCommandError):
            services.recent_commits(self.base, "acme", "broken")


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
        for path in (
            "/static/app.css",
            "/static/js/app.js",
            # sessions.js / logs.js stay reachable after the Sessions + Logs
            # fold-in (#221): repoDetail imports renderSessionCard + streamLog
            # from them, and issueDetail imports streamLog from logs.js.
            "/static/js/sessions.js",
            "/static/js/logs.js",
            "/static/js/repoDetail.js",
            "/static/js/attach.js",
            "/static/js/issueDetail.js",
            # fleet.js drives the Fleet worklist + the #227 mobile triage list.
            "/static/js/fleet.js",
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)

    def test_index_folds_session_surface_into_live(self) -> None:
        # The remote-control session surface moved into the Live screen (#221):
        # the shell still loads the client-side QR library, but the standalone
        # Sessions grid is gone — the join URL + QR card now renders into the Live
        # repo-detail session panel (repoDetail embeds renderSessionCard there).
        body = self.client.get("/").text
        self.assertIn('qrcode-generator', body)
        self.assertIn('id="repo-session-body"', body)
        # The standalone Sessions grid + task-sessions table are folded in.
        self.assertNotIn('id="sessions"', body)
        self.assertNotIn('class="session-grid"', body)
        self.assertNotIn('id="task-sessions"', body)

    def test_index_live_has_greyed_steer_bar_and_diagnostics(self) -> None:
        # The Live screen (#224): a disabled chat + Send steer bar below the
        # session body, and the worktrees/stuck/log blocks collapsed into a
        # <details> Diagnostics section (their IDs preserved for the JS).
        body = self.client.get("/").text
        # Greyed steer bar: a disabled input + a disabled Send button.
        self.assertIn('class="live-steer-bar"', body)
        self.assertIn('class="live-steer-input"', body)
        self.assertIn('placeholder=\'Ask about the repo, or "open an issue to…"\'', body)
        # Diagnostics <details> wraps the preserved worktrees / stuck / log IDs.
        self.assertIn('class="diagnostics-section"', body)
        self.assertIn("<summary>Diagnostics</summary>", body)
        for preserved in ('id="repo-worktrees"', 'id="repo-stuck-section"',
                          'id="repo-stuck"', 'id="repo-log"', 'id="repo-log-title"'):
            self.assertIn(preserved, body)
        # The Diagnostics wrapper precedes those blocks (it encloses them).
        self.assertLess(body.index('class="diagnostics-section"'), body.index('id="repo-worktrees"'))
        self.assertLess(body.index('class="diagnostics-section"'), body.index('id="repo-log"'))

    def test_index_wires_pipeline_view(self) -> None:
        # The Issue ▸ PR detail view (#190): the shell must expose the section,
        # the stepper / timeline / linked / worktree containers, the breadcrumb,
        # the inline conversation + reply, and the centralized control row. The
        # control buttons themselves are rendered client-side by renderControls()
        # (#200 wires Take over live; Pause / Reassign remain disabled stubs).
        body = self.client.get("/").text
        self.assertIn('class="view pipeline-detail"', body)
        self.assertIn("$store.app.view === 'pipeline'", body)
        self.assertIn('id="pipeline-breadcrumb"', body)
        self.assertIn('id="pipeline-stepper"', body)
        self.assertIn('id="pipeline-conv"', body)
        self.assertIn('id="pipeline-reply-send"', body)
        self.assertIn('id="pipeline-controls"', body)
        self.assertIn('id="pipeline-timeline"', body)
        self.assertIn('id="pipeline-linked"', body)
        self.assertIn('id="pipeline-worktree"', body)
        # The ready-for-* lifecycle control group + its error slot (#225).
        self.assertIn('id="pipeline-lifecycle"', body)
        self.assertIn('id="pipeline-lifecycle-error"', body)
        # The detail module is loaded as part of the app shell's module graph.
        self.assertEqual(self.client.get("/static/js/issueDetail.js").status_code, 200)

    def test_issue_detail_module_wires_label_and_steer_gate(self) -> None:
        # #225: the detail module consumes the GitHub feed, posts the label
        # endpoint, gates steer on a live PTY, and reads the activity feed.
        js = self.client.get("/static/js/issueDetail.js").text
        self.assertIn("findPipelineView", js)              # consumes snapshot.pipelines
        self.assertIn("/labels", js)                        # POSTs the label endpoint
        self.assertIn("ready-for-planning", js)             # ready-for-* controls
        self.assertIn("ready-for-development", js)
        self.assertIn("/activity", js)                      # activity-timeline feed
        self.assertIn("STEER_DISABLED_TIP", js)             # disabled-until-PTY tooltip

    def test_detail_view_dispatch_is_driven_from_app_js(self) -> None:
        # #239: the Live and Issue ▸ PR detail screens rendered empty because the
        # view → module-show() dispatch lived in inline x-effects that guarded on
        # `window.repoDetail && …` / `window.issueDetail && …`. Alpine evaluated
        # those once at startup before app.js assigned the globals, so they
        # short-circuited, tracked no dependencies, and never re-ran — show() was
        # effectively never called. The dispatch now lives in app.js, after the
        # init() calls set the globals and with Alpine already started.
        #
        # NOTE: these are string-level guards on the served assets, not rendered-
        # DOM assertions — there is no JS/DOM harness in this repo (no
        # package.json), which is the same gap that let the original bug merge.
        # Closing that fully (a headless-JS test runner) is a separate infra issue.
        body = self.client.get("/").text
        # The fragile inline-effect pattern is gone from the shell markup.
        self.assertNotIn("window.repoDetail && window.repoDetail.show", body)
        self.assertNotIn("window.issueDetail && window.issueDetail.show", body)
        # No inline x-effect on the two detail-view <section> tags (where the
        # short-circuiting dispatch lived). Scoped to those tags rather than the
        # whole body so a legitimate x-effect elsewhere can't trip this guard.
        def section_open_tag(cls: str) -> str:
            start = body.index(f'<section class="{cls}"')
            return body[start:body.index(">", start) + 1]
        self.assertNotIn("x-effect", section_open_tag("view repo-detail"))
        self.assertNotIn("x-effect", section_open_tag("view pipeline-detail"))
        # The view-gating x-show on each detail section survives.
        self.assertIn("$store.app.view === 'live'", body)
        self.assertIn("$store.app.view === 'pipeline'", body)
        # app.js drives the dispatch via an Alpine.effect that calls both modules'
        # show() gated on the live / pipeline view.
        js = self.client.get("/static/js/app.js").text
        self.assertIn("Alpine.effect", js)
        self.assertIn("window.repoDetail.show", js)
        self.assertIn("window.issueDetail.show", js)
        self.assertIn('view === "live"', js)
        self.assertIn('view === "pipeline"', js)

    def test_index_primary_nav_is_fleet_and_live(self) -> None:
        # The design IA (#221): primary nav reads Operate / Fleet · Live, with no
        # standalone Sessions or Logs destinations. Skills stays in the gear menu.
        body = self.client.get("/").text
        # The "Operate" eyebrow sits above the rail nav.
        self.assertIn('class="rail-nav-eyebrow"', body)
        self.assertIn(">Operate<", body)
        # Fleet + Live nav entries (the NAV array the rail renders).
        self.assertIn('{ id: "fleet", label: "Fleet", icon: "dashboard" }', body)
        self.assertIn('{ id: "live", label: "Live", icon: "sensors" }', body)
        # The Fleet + Live view sections are wired to the renamed view ids.
        self.assertIn("$store.app.view === 'fleet'", body)
        self.assertIn("$store.app.view === 'live'", body)
        # The old Overview/Sessions/Logs top-level destinations are gone.
        self.assertNotIn("$store.app.view === 'overview'", body)
        self.assertNotIn("$store.app.view === 'sessions'", body)
        self.assertNotIn("$store.app.view === 'logs'", body)
        self.assertNotIn('id="log-repo"', body)
        # Skills stays reachable from the gear flyout.
        self.assertIn("$store.app.go('skills')", body)

    def test_index_hash_migration_maps_legacy_routes(self) -> None:
        # Back/forward + old bookmarks must still resolve (#221 AC): the served
        # shell's parseHash carries the legacy-alias map and the live/ + repo/
        # prefix branches.
        body = self.client.get("/").text
        self.assertIn('const ALIASES = { overview: "fleet", sessions: "live", logs: "live" };', body)
        self.assertIn('h.startsWith("live/") || h.startsWith("repo/")', body)

    def test_index_pipeline_detail_wires_log_pane(self) -> None:
        # Logs fold-in (#221): the Issue ▸ PR detail view gained a worker-log tail
        # pane so log tailing is reachable from the detail surface.
        body = self.client.get("/").text
        self.assertIn('id="pipeline-log"', body)

    def test_index_screen_head_restores_per_screen_subtitles(self) -> None:
        # The shell fidelity pass (#222) reintroduces the design's ScreenHead
        # pattern (title + descriptive subtitle + right-aligned controls slot) on
        # every primary screen, restoring the intro copy the rework dropped.
        body = self.client.get("/").text
        # The shared head + subtitle classes ship and replace the old bespoke
        # .live-head / .skills-head / .pipeline-header heads.
        self.assertIn('class="screen-head"', body)
        self.assertIn('class="screen-head-sub"', body)
        self.assertNotIn('class="live-head"', body)
        self.assertNotIn('class="skills-head"', body)
        self.assertNotIn('class="pipeline-header"', body)
        # A distinctive slice of each restored subtitle pins the copy in place.
        self.assertIn("Click a metric to filter the worklist", body)  # Fleet
        self.assertIn("Watch and steer this repo", body)         # Live
        self.assertIn("through to a merged PR", body)            # Issue ▸ PR
        self.assertIn("runs at a stage of the lifecycle", body)  # Skills library (#226)
        # The IDs the JS modules read/write survive the head refactor.
        self.assertIn('id="repo-detail-title"', body)
        self.assertIn('id="repo-quick-actions"', body)
        self.assertIn('id="pipeline-detail-title"', body)
        self.assertIn('id="pipeline-detail-repo"', body)
        self.assertIn('id="pipeline-detail-state"', body)
        self.assertIn('id="entry-new"', body)

    def test_index_screen_titles_match_design_casing(self) -> None:
        # The mock's ScreenHead `title` props are title/sentence-case display
        # headings (ld-*.jsx): "Fleet", "Skills library" — not lowercased.
        body = self.client.get("/").text
        self.assertIn("<h2>Fleet</h2>", body)
        # Skills is titled "Skills library" per the design mock (#226) — a
        # sentence-case display label, not the lowercased convention.
        self.assertIn("<h2>Skills library</h2>", body)
        # The lowercased deviation is gone.
        self.assertNotIn("<h2>fleet</h2>", body)
        self.assertNotIn("<h2>skills &amp; commands</h2>", body)

    def test_index_rail_brand_mark_and_tagline(self) -> None:
        # Rail/brand polish (#222): a 3×3 dot-grid brand mark, the "agent console"
        # tagline (not "dashboard"), and a hover collapse chevron.
        body = self.client.get("/").text
        self.assertIn('class="rail-mark"', body)
        self.assertIn('class="brand-sub">agent console<', body)
        # The old Material Symbols logo + "dashboard" tagline are gone from the rail.
        self.assertNotIn(">deployed_code<", body)
        self.assertNotIn('class="brand-sub">dashboard<', body)
        # The collapse chevron is bound to the collapsed state.
        self.assertIn('class="rail-collapse-chevron', body)
        self.assertIn("$store.app.collapsed ? 'chevron_right' : 'chevron_left'", body)

    def test_index_density_defaults_to_compact(self) -> None:
        # Tweaks (#222): the document defaults to compact density (a stored
        # preference still wins via the pre-paint pin), and the Alpine store seeds
        # its fallback from the same default.
        body = self.client.get("/").text
        self.assertIn('data-density="compact"', body)
        self.assertIn('root.getAttribute("data-density") || "compact"', body)

    def test_index_gear_menu_has_settings_cap(self) -> None:
        # Tweaks (#222): the gear flyout gains a non-interactive "Settings" cap
        # header above its items.
        body = self.client.get("/").text
        self.assertIn('class="menu-cap"', body)
        self.assertIn('class="menu-cap" role="presentation">Settings<', body)

    def test_app_css_ships_screen_head_and_brand_rules(self) -> None:
        # The shell polish (#222) is CSS-dominant; assert the structural hooks
        # ship and the folded-away bespoke head rules are gone.
        css = self.client.get("/static/app.css").text
        self.assertIn(".screen-head", css)
        # Screen titles render at the display type scale, not body size.
        # Anchor the property to its selector: --fs-display is shared with
        # other rules (e.g. .fleet-pool-count), so a bare substring would
        # still pass if .screen-head-text h2 regressed.
        self.assertIn(".screen-head-text h2 { margin: 0; font-size: var(--fs-display);", css)
        self.assertIn(".screen-head-sub", css)
        self.assertIn(".rail-mark", css)
        self.assertIn(".rail-collapse-chevron", css)
        self.assertIn(".menu-cap", css)
        # The three bespoke head selectors folded into .screen-head.
        self.assertNotIn(".live-head {", css)
        self.assertNotIn(".skills-head {", css)
        self.assertNotIn(".pipeline-header {", css)

    def test_index_declares_responsive_viewport(self) -> None:
        # The mobile companion pass (#192) needs the responsive viewport meta so
        # phones lay out at device width instead of a zoomed-out desktop page.
        body = self.client.get("/").text
        self.assertIn('name="viewport"', body)
        self.assertIn("width=device-width", body)

    def test_app_css_ships_mobile_companion_rules(self) -> None:
        # The responsive layout (#192) is CSS-dominant; assert the structural
        # hooks ship: a phone breakpoint, safe-area insets for the bottom tab
        # bar / sticky steer reply, and the full-bleed overlay treatment.
        css = self.client.get("/static/app.css").text
        self.assertIn("Mobile companion surfaces (#192)", css)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("env(safe-area-inset-bottom)", css)
        # Full-bleed overlays make the modal card fill the phone screen.
        self.assertIn("max-height: 100%", css)

    def test_app_css_ships_mobile_triage_rules(self) -> None:
        # The #227 "Needs your call" triage list is hidden by default (desktop)
        # and flips on inside the phone breakpoint; its card recipe ships too.
        css = self.client.get("/static/app.css").text
        self.assertIn(".fleet-triage { display: none; }", css)
        self.assertIn(".triage-card", css)
        # The display:block flip lives inside the phone media query.
        phone = css.split("@media (max-width: 720px)", 1)
        self.assertEqual(len(phone), 2, "expected a 720px breakpoint")
        self.assertIn(".fleet-triage { display: block;", phone[1])

    def test_index_ships_fleet_triage_container(self) -> None:
        # The mobile triage list renders into a labelled container in Fleet.
        body = self.client.get("/").text
        self.assertIn('id="fleet-triage"', body)
        self.assertIn('aria-label="Needs your call"', body)

    def test_fleet_js_renders_needs_you_triage(self) -> None:
        # fleet.js defines the triage renderer, reuses the existing `needsYou`
        # predicate (not a re-definition), and taps through via goPipeline.
        js = self.client.get("/static/js/fleet.js").text
        self.assertIn("renderTriage", js)
        self.assertIn("filter(needsYou)", js)
        self.assertIn("goPipeline", js)
        # Exactly one `needsYou` definition survives (the triage list reuses it).
        self.assertEqual(js.count("function needsYou"), 1)

    def test_task_sessions_endpoint_surfaces_pipeline_key(self) -> None:
        # The Issue ▸ PR detail view (#190) addresses the #199 pipeline routes by
        # pipeline_key, so the read path must surface it (read-only). It rides the
        # existing /api/task-sessions GET via asdict.
        sess_dir = session_registry.session_dir(self.base, "acme", "widgets", "issue-9")
        session_registry.write_session_file(
            sess_dir, task_key="issue-9", repo="acme/widgets", session_id="sid",
            pid=1, started_at="t", cwd="/cwd", pipeline_key="issue-9",
        )
        # Service layer.
        view = next(v for v in services.list_task_sessions(self.base)
                    if v.task_key == "issue-9")
        self.assertEqual(view.pipeline_key, "issue-9")
        # HTTP endpoint.
        resp = self.client.get("/api/task-sessions")
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.json() if r["task_key"] == "issue-9")
        self.assertEqual(row["pipeline_key"], "issue-9")

    def test_task_sessions_endpoint_pipeline_key_absent_is_none(self) -> None:
        # A pre-#199 entry (no pipeline_key) round-trips as null, not an error.
        sess_dir = session_registry.session_dir(self.base, "acme", "widgets", "issue-10")
        session_registry.write_session_file(
            sess_dir, task_key="issue-10", repo="acme/widgets", session_id="sid",
            pid=1, started_at="t", cwd="/cwd",  # no pipeline_key
        )
        row = next(r for r in self.client.get("/api/task-sessions").json()
                   if r["task_key"] == "issue-10")
        self.assertIsNone(row["pipeline_key"])

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

    # ── Per-pipeline log routes (issue #220) ────────────────────────────────

    def test_pipeline_log_tail_endpoint(self) -> None:
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", ["one", "two", "three"])
        resp = self.client.get("/api/logs/acme/widgets/pipelines/issue-5/tail?lines=2")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["repo"], "acme/widgets")
        self.assertEqual(body["pipeline_key"], "issue-5")
        self.assertEqual(body["lines"], ["two", "three"])
        self.assertEqual(body["count"], 2)

    def test_pipeline_log_tail_unknown_404(self) -> None:
        resp = self.client.get("/api/logs/acme/widgets/pipelines/issue-404/tail")
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_log_tail_bad_lines_422(self) -> None:
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", ["x"])
        self.assertEqual(
            self.client.get("/api/logs/acme/widgets/pipelines/issue-5/tail?lines=0").status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/logs/acme/widgets/pipelines/issue-5/tail?lines=99999").status_code, 422
        )

    def test_pipeline_log_list_endpoint(self) -> None:
        _make_pipeline_log(self.base, "acme", "widgets", "issue-5", ["a"])
        _make_pipeline_log(self.base, "acme", "widgets", "pr-9", ["b"])
        resp = self.client.get("/api/logs/acme/widgets/pipelines")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(sorted(body["pipelines"]), ["issue-5", "pr-9"])
        self.assertEqual(body["count"], 2)

    def test_pipeline_activity_endpoint(self) -> None:
        _make_pipeline_log(
            self.base, "acme", "widgets", "issue-5",
            ["2026-06-17 12:00:00,123 [INFO] loony_dev.agents.coding: doing work"],
        )
        resp = self.client.get("/api/pipelines/issue-5/activity?repo=acme/widgets")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["pipeline_key"], "issue-5")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["actor"], "trixy")

    def test_pipeline_activity_unknown_404(self) -> None:
        resp = self.client.get("/api/pipelines/issue-404/activity?repo=acme/widgets")
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_activity_bad_repo_422(self) -> None:
        # Missing repo query param, no slash, empty halves, and a nested slash
        # must all be rejected before path resolution (issue #220 review).
        self.assertEqual(
            self.client.get("/api/pipelines/issue-5/activity").status_code, 422
        )
        for repo in ("noslash", "/", "owner/", "/repo", "a/b/c"):
            with self.subTest(repo=repo):
                resp = self.client.get(
                    "/api/pipelines/issue-5/activity", params={"repo": repo}
                )
                self.assertEqual(resp.status_code, 422)

    def test_pipeline_log_traversal_rejected(self) -> None:
        bad = self.client.get("/api/logs/acme/widgets/pipelines/%2e%2e/tail")
        self.assertIn(bad.status_code, (404, 422))

    # ── Recent-commits route (issue #224) ───────────────────────────────────

    def test_commits_endpoint_happy_path(self) -> None:
        _git_repo_with_commits(self.base / "acme" / "widgets", ["feat: a", "fix: b"])
        resp = self.client.get("/api/repos/acme/widgets/commits")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["repo"], "acme/widgets")
        self.assertEqual(body["count"], 2)
        self.assertEqual([c["subject"] for c in body["commits"]], ["fix: b", "feat: a"])
        self.assertIn("short_sha", body["commits"][0])
        self.assertIn("rel_date", body["commits"][0])

    def test_commits_endpoint_404_for_non_git_repo(self) -> None:
        # The worker dir exists (acme/widgets) but has no git checkout: the
        # service raises CheckoutNotFoundError → 404 (raise on failure, not a
        # silent empty 200).
        resp = self.client.get("/api/repos/acme/widgets/commits")
        self.assertEqual(resp.status_code, 404)

    def test_commits_endpoint_503_on_git_failure(self) -> None:
        # A corrupt .git makes git log fail → GitCommandError → 503.
        checkout = self.base / "acme" / "broken"
        checkout.mkdir(parents=True)
        (checkout / ".git").write_text("not a git dir\n")
        resp = self.client.get("/api/repos/acme/broken/commits")
        self.assertEqual(resp.status_code, 503)

    def test_commits_endpoint_clamps_n_via_query_bounds(self) -> None:
        # Out-of-range n is a 422 (same Query(ge,le) contract as the log routes).
        self.assertEqual(self.client.get("/api/repos/acme/widgets/commits?n=0").status_code, 422)
        self.assertEqual(self.client.get("/api/repos/acme/widgets/commits?n=999").status_code, 422)
        self.assertEqual(self.client.get("/api/repos/acme/widgets/commits?n=abc").status_code, 422)

    def test_commits_endpoint_traversal_rejected(self) -> None:
        bad = self.client.get("/api/repos/%2e%2e/etc/commits")
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

    # ---- Card metadata (#191) -------------------------------------------

    def test_list_surfaces_description_owner_and_trigger(self) -> None:
        # A hand-authored skill with a frontmatter description + explicit trigger.
        body = (
            "---\n"
            "description: Deploy the service\n"
            "trigger: a release tag is pushed\n"
            "---\n\n# Deploy\n"
        )
        entries.write_entry("skills", "deploy", body, **self._kw())
        (view,) = entries.list_entries("skills", **self._kw())
        self.assertEqual(view.description, "Deploy the service")
        self.assertIsNone(view.owner)  # no managed marker, no owner fm => unknown
        self.assertFalse(view.managed)
        self.assertEqual(view.trigger, "a release tag is pushed")

    def test_managed_marker_sets_managed_and_owner_to_bot_name(self) -> None:
        body = (
            "---\ndescription: do it\n---\n"
            f"{entries.MANAGED_MARKER}\n\nbody\n"
        )
        entries.write_entry("commands", "ship", body, **self._kw())
        (view,) = entries.list_entries("commands", **self._kw(), bot_name="trixy")
        self.assertTrue(view.managed)
        self.assertEqual(view.owner, "trixy")

    def test_managed_owner_unresolved_without_bot_name(self) -> None:
        body = f"{entries.MANAGED_MARKER}\n\nbody\n"
        entries.write_entry("commands", "ship", body, **self._kw())
        (view,) = entries.list_entries("commands", **self._kw())
        self.assertTrue(view.managed)
        self.assertIsNone(view.owner)  # bot_name not supplied

    def test_explicit_owner_frontmatter_wins(self) -> None:
        # An explicit `owner:` beats both the bot name and the marker default.
        body = f"---\nowner: capo\n---\n{entries.MANAGED_MARKER}\n\nbody\n"
        entries.write_entry("commands", "ship", body, **self._kw())
        (view,) = entries.list_entries("commands", **self._kw(), bot_name="trixy")
        self.assertTrue(view.managed)
        self.assertEqual(view.owner, "capo")

    def test_phase_from_known_command_mapping(self) -> None:
        entries.write_entry("commands", "plan-issue", "no frontmatter\n", **self._kw())
        entries.write_entry("commands", "fix-ci", "no frontmatter\n", **self._kw())
        by_name = {v.name: v for v in entries.list_entries("commands", **self._kw())}
        self.assertEqual(by_name["plan-issue"].phase, "planning")
        self.assertEqual(by_name["fix-ci"].phase, "ci")

    def test_phase_mapping_does_not_apply_to_unmanaged_skills(self) -> None:
        # A *hand-authored* skill that merely shares a known command's name must
        # not inherit that command's phase chip — only managed entries map (#240).
        entries.write_entry("skills", "plan-issue", "no frontmatter\n", **self._kw())
        (view,) = entries.list_entries("skills", **self._kw())
        self.assertIsNone(view.phase)

    def test_phase_mapping_applies_to_managed_skills(self) -> None:
        # A *managed* skill named after a known command genuinely is that agent,
        # so the phase chip is real and surfaces on the skills tab (#240).
        body = f"{entries.MANAGED_MARKER}\n\nno frontmatter\n"
        entries.write_entry("skills", "plan-issue", body, **self._kw())
        (view,) = entries.list_entries("skills", **self._kw())
        self.assertTrue(view.managed)
        self.assertEqual(view.phase, "planning")

    def test_trigger_extracted_from_use_when_clause(self) -> None:
        body = "---\ndescription: A tool. Use when the build breaks.\n---\n"
        entries.write_entry("skills", "rescue", body, **self._kw())
        (view,) = entries.list_entries("skills", **self._kw())
        self.assertEqual(view.trigger, "the build breaks.")

    def test_frontmatterless_entry_lists_with_none_metadata(self) -> None:
        # A plain markdown file (no fence, unknown name) must list without raising
        # and surface None metadata — except owner, which is always concrete.
        entries.write_entry("skills", "plain", "# Just a heading\nbody\n", **self._kw())
        (view,) = entries.list_entries("skills", **self._kw())
        self.assertIsNone(view.description)
        self.assertIsNone(view.trigger)
        self.assertIsNone(view.phase)
        self.assertIsNone(view.owner)
        self.assertFalse(view.managed)

    def test_write_round_trips_frontmatter_verbatim(self) -> None:
        # Editing in the drawer must never strip metadata: read-back equals write.
        body = (
            "---\ndescription: keep me\nargument-hint: <x>\n---\n\n# Body\n"
        )
        entries.write_entry("skills", "verbatim", body, **self._kw())
        self.assertEqual(entries.read_entry("skills", "verbatim", **self._kw()), body)


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

    def test_list_serializes_managed_and_resolved_owner(self) -> None:
        # A managed entry surfaces managed=true and the resolved bot login; the
        # bot-name resolver is stubbed so the read-only route never touches `gh`.
        self.client.put("/api/commands/ship", content=f"{entries.MANAGED_MARKER}\nbody\n")
        with mock.patch.object(routes, "_resolve_bot_name", return_value="trixy"):
            listed = self.client.get("/api/commands").json()
        (entry,) = listed
        self.assertTrue(entry["managed"])
        self.assertEqual(entry["owner"], "trixy")

    def test_list_tolerates_bot_name_resolution_failure(self) -> None:
        # If `gh` is unavailable the resolver yields None and the list still 200s.
        self.client.put("/api/commands/ship", content=f"{entries.MANAGED_MARKER}\nbody\n")
        with mock.patch.object(routes, "_resolve_bot_name", return_value=None):
            resp = self.client.get("/api/commands")
        self.assertEqual(resp.status_code, 200)
        (entry,) = resp.json()
        self.assertTrue(entry["managed"])
        self.assertIsNone(entry["owner"])

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
            {"workers", "worktrees", "sessions", "task_sessions", "stuck",
             "pipelines", "repos"},
        )
        # The snapshot mirrors the per-resource endpoints: the seeded worker shows.
        self.assertEqual([w["repo"] for w in snapshot["workers"]], ["acme/widgets"])

    async def test_events_snapshot_carries_task_session_pipeline_key(self) -> None:
        # The pipeline-detail view (#190) reads pipeline_key off the SSE snapshot's
        # task_sessions rows, so it must ride the consolidated stream.
        sess_dir = session_registry.session_dir(self.base, "acme", "widgets", "issue-11")
        session_registry.write_session_file(
            sess_dir, task_key="issue-11", repo="acme/widgets", session_id="sid",
            pid=1, started_at="t", cwd="/cwd", pipeline_key="issue-11",
        )
        async with _SSEDriver(self.app, "/api/events") as drv:
            snapshot = json.loads(await _read_first_sse_event(drv))
        row = next(r for r in snapshot["task_sessions"] if r["task_key"] == "issue-11")
        self.assertEqual(row["pipeline_key"], "issue-11")

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
            backstop_seconds=20.0, debounce=0.2, control_socket=sock_path,
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


# ---------------------------------------------------------------------------
# Partial GitHub state (issue #219)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402


class _FakeFacet:
    """Minimal stand-in for an Issue/PR facet used by the GitHub-state layer."""

    def __init__(self, *, title="", labels=None, updated_at=None,
                 mergeable=None, reviews=None):
        self.title = title
        self.labels = labels or []
        self.updated_at = updated_at
        self.mergeable = mergeable
        self.reviews = reviews or []


class _FakePipeline:
    def __init__(self, key, *, issue=None, pr=None):
        self.pipeline_key = key
        self.issue = issue
        self.pr = pr


class DeriveStageTestCase(unittest.TestCase):
    """`derive_stage` maps label / PR state into the Fleet stages (pure)."""

    def test_issue_labels_map_to_stages(self) -> None:
        self.assertEqual(services.derive_stage(["ready-for-planning"], None), "Planning")
        self.assertEqual(services.derive_stage(["in-progress"], None), "Implementing")
        self.assertEqual(services.derive_stage(["ready-for-development"], None), "Inbox")
        self.assertEqual(services.derive_stage([], None), "Inbox")

    def test_in_error_is_not_a_stage(self) -> None:
        # in-error rides the raw labels list; the stage still reflects the rest.
        self.assertEqual(
            services.derive_stage(["in-error", "in-progress"], None), "Implementing"
        )
        self.assertEqual(services.derive_stage(["in-error"], None), "Inbox")

    def test_pr_state_dominates(self) -> None:
        conflicting = _FakeFacet(mergeable="CONFLICTING")
        self.assertEqual(services.derive_stage(["in-progress"], conflicting), "Conflicts")
        reviewed = _FakeFacet(reviews=[object()])
        self.assertEqual(services.derive_stage([], reviewed), "In Review")
        clean = _FakeFacet(mergeable="MERGEABLE")
        self.assertEqual(services.derive_stage([], clean), "PR Open")


class FetchGitHubStateTestCase(unittest.TestCase):
    """`_fetch_github_state` builds views and isolates per-repo failures."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _make_checkout(self, owner: str, name: str) -> None:
        (self.base / ".logs" / owner / name).mkdir(parents=True, exist_ok=True)
        (self.base / owner / name / ".git").mkdir(parents=True, exist_ok=True)

    def test_builds_views_with_title_precedence_and_counts(self) -> None:
        self._make_checkout("acme", "widgets")
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        issue_pipe = _FakePipeline(
            "issue-7",
            issue=_FakeFacet(title="Real issue title", labels=["ready-for-planning"],
                             updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        )
        pr_pipe = _FakePipeline(
            "pr-9",
            pr=_FakeFacet(title="PR only title", labels=["bug"], mergeable="CONFLICTING",
                          updated_at=ts),
        )

        from loony_dev.github import PullRequest
        from loony_dev.pipeline import Pipeline

        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch.object(Pipeline, "discover",
                                  lambda repo: iter([issue_pipe, pr_pipe])), \
                mock.patch.object(PullRequest, "list_open",
                                  lambda repo=None: [object(), object()]), \
                mock.patch.object(services, "_count_open_issues", lambda repo: 5):
            pipelines, repos = services._fetch_github_state(self.base)

        self.assertEqual(len(pipelines), 2)
        by_key = {p.pipeline_key: p for p in pipelines}
        self.assertEqual(by_key["issue-7"].title, "Real issue title")
        self.assertEqual(by_key["issue-7"].kind, "issue")
        self.assertEqual(by_key["issue-7"].number, 7)
        self.assertEqual(by_key["issue-7"].stage, "Planning")
        self.assertIsNone(by_key["issue-7"].pr_state)
        # PR facet supplies the title when there is no issue facet; PR dominates.
        self.assertEqual(by_key["pr-9"].title, "PR only title")
        self.assertEqual(by_key["pr-9"].stage, "Conflicts")
        self.assertEqual(by_key["pr-9"].pr_state, "open")
        self.assertEqual(by_key["pr-9"].mergeable, "CONFLICTING")
        self.assertEqual(by_key["pr-9"].updated_at, ts.isoformat())

        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0].repo, "acme/widgets")
        self.assertTrue(repos[0].ok)
        self.assertEqual(repos[0].open_prs, 2)
        self.assertEqual(repos[0].open_issues, 5)

    def test_skips_repos_without_checkout(self) -> None:
        # .logs entry but no git checkout -> never touched, no gh call.
        (self.base / ".logs" / "acme" / "nope").mkdir(parents=True)
        with mock.patch.object(services, "_repo_for",
                               side_effect=AssertionError("should not construct")):
            pipelines, repos = services._fetch_github_state(self.base)
        self.assertEqual((pipelines, repos), ([], []))

    def test_one_repo_failure_is_isolated(self) -> None:
        self._make_checkout("acme", "good")
        self._make_checkout("acme", "bad")
        good_pipe = _FakePipeline("issue-1", issue=_FakeFacet(title="ok", labels=[]))

        from loony_dev.github import PullRequest
        from loony_dev.pipeline import Pipeline

        def fake_discover(repo):
            if getattr(repo, "broken", False):
                raise RuntimeError("gh exploded")
            return iter([good_pipe])

        def fake_repo_for(owner, name, checkout):
            r = object.__new__(type("R", (), {}))
            r.broken = (name == "bad")
            return r

        with mock.patch.object(services, "_repo_for", fake_repo_for), \
                mock.patch.object(Pipeline, "discover", fake_discover), \
                mock.patch.object(PullRequest, "list_open", lambda repo=None: []), \
                mock.patch.object(services, "_count_open_issues", lambda repo: 0):
            pipelines, repos = services._fetch_github_state(self.base)

        # The good repo still yields its pipeline + an ok view; the bad one is
        # surfaced as ok=False with null counts and contributes no pipelines.
        self.assertEqual([p.pipeline_key for p in pipelines], ["issue-1"])
        by_repo = {r.repo: r for r in repos}
        self.assertTrue(by_repo["acme/good"].ok)
        self.assertFalse(by_repo["acme/bad"].ok)
        self.assertIsNone(by_repo["acme/bad"].open_prs)
        self.assertIsNone(by_repo["acme/bad"].open_issues)

    def test_count_failure_drops_the_repos_pipelines(self) -> None:
        # discover() succeeds (yields a pipeline) but the later count fails. The
        # repo must be ok=False AND contribute zero pipelines — per-repo success
        # is all-or-nothing, never half a repo's rows.
        self._make_checkout("acme", "widgets")
        pipe = _FakePipeline("issue-3", issue=_FakeFacet(title="t", labels=[]))

        from loony_dev.github import PullRequest
        from loony_dev.pipeline import Pipeline

        def boom(repo):
            raise RuntimeError("gh count exploded")

        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch.object(Pipeline, "discover", lambda repo: iter([pipe])), \
                mock.patch.object(PullRequest, "list_open", lambda repo=None: []), \
                mock.patch.object(services, "_count_open_issues", boom):
            pipelines, repos = services._fetch_github_state(self.base)

        self.assertEqual(pipelines, [])
        self.assertEqual(len(repos), 1)
        self.assertFalse(repos[0].ok)


class GitHubStateCacheTestCase(unittest.TestCase):
    """`github_state` TTL cache: refresh past the window, degrade on error."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        services._GH_CACHE.pop(self.base, None)
        self.addCleanup(services._GH_CACHE.pop, self.base, None)

    def test_disabled_short_circuits_with_no_fetch(self) -> None:
        calls = []
        result = services.github_state(
            self.base, enabled=False, fetch_fn=lambda b: calls.append(b) or ([], []),
        )
        self.assertEqual(result, ([], []))
        self.assertEqual(calls, [])

    def test_caches_within_ttl_and_refetches_after(self) -> None:
        calls = []

        def fetch(base):
            calls.append(base)
            return ([f"call-{len(calls)}"], [])

        first = services.github_state(self.base, refresh_seconds=60.0, fetch_fn=fetch)
        second = services.github_state(self.base, refresh_seconds=60.0, fetch_fn=fetch)
        self.assertEqual(first, second)  # cache hit, same value
        self.assertEqual(len(calls), 1)

        # Age the cache entry past the TTL -> next call refetches.
        ts, value = services._GH_CACHE[self.base]
        services._GH_CACHE[self.base] = (ts - 1000.0, value)
        third = services.github_state(self.base, refresh_seconds=60.0, fetch_fn=fetch)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(third, first)

    def test_error_on_warm_cache_returns_last_good(self) -> None:
        good = (["good"], [])
        services.github_state(self.base, refresh_seconds=0.0, fetch_fn=lambda b: good)
        # refresh_seconds=0 forces a re-fetch; the fetch now raises.
        def boom(base):
            raise RuntimeError("gh down")
        result = services.github_state(self.base, refresh_seconds=0.0, fetch_fn=boom)
        self.assertEqual(result, good)  # last good value, not an exception

    def test_concurrent_cold_start_fetches_once(self) -> None:
        # Two cold-start callers both block for the lock (no cache to fall back
        # on). The one that loses the race must re-check the now-warm cache under
        # the lock and reuse it, not run a duplicate fetch in the same window.
        import threading

        calls = []
        started = threading.Event()
        release = threading.Event()

        def fetch(base):
            calls.append(base)
            started.set()
            release.wait(2.0)  # hold the lock so the second caller queues
            return ([f"call-{len(calls)}"], [])

        results: dict[str, object] = {}

        def run(tag):
            results[tag] = services.github_state(
                self.base, refresh_seconds=60.0, fetch_fn=fetch,
            )

        t1 = threading.Thread(target=run, args=("a",))
        t2 = threading.Thread(target=run, args=("b",))
        t1.start()
        self.assertTrue(started.wait(2.0))  # t1 is inside fetch, holding the lock
        t2.start()
        release.set()
        t1.join(2.0)
        t2.join(2.0)

        self.assertEqual(len(calls), 1)  # only one fetch ran
        self.assertEqual(results["a"], results["b"])  # both saw the same value


class GitHubStateApiTestCase(unittest.TestCase):
    """`/api/pipelines`, `/api/repos`, and the snapshot expose GitHub state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["x"])
        self.app = create_app(base_dir=self.base, supervisor_log=None)
        self.client = TestClient(self.app)

    def _fake_state(self):
        pipeline = services.PipelineGitHubView(
            pipeline_key="issue-3", repo="acme/widgets", kind="issue", number=3,
            title="Add a thing", stage="Planning", labels=["ready-for-planning"],
            pr_state=None, mergeable=None, updated_at="2024-01-01T00:00:00+00:00",
        )
        repo = services.RepoGitHubView(
            repo="acme/widgets", open_issues=4, open_prs=2, ok=True,
        )
        return [pipeline], [repo]

    def test_pipelines_endpoint(self) -> None:
        with mock.patch.object(services, "github_state",
                               return_value=self._fake_state()):
            resp = self.client.get("/api/pipelines")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["pipeline_key"], "issue-3")
        self.assertEqual(body[0]["title"], "Add a thing")
        self.assertEqual(body[0]["stage"], "Planning")

    def test_repos_endpoint(self) -> None:
        with mock.patch.object(services, "github_state",
                               return_value=self._fake_state()):
            resp = self.client.get("/api/repos")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body, [
            {"repo": "acme/widgets", "open_issues": 4, "open_prs": 2, "ok": True},
        ])

    def test_endpoints_empty_when_disabled(self) -> None:
        # The app default has github state enabled, but with no checkout there is
        # nothing to fetch, so both endpoints degrade to empty lists (no error).
        self.assertEqual(self.client.get("/api/pipelines").json(), [])
        self.assertEqual(self.client.get("/api/repos").json(), [])

    def test_snapshot_failure_falls_back_to_empty_not_500(self) -> None:
        # A raising fetch must not surface as a dashboard error: github_state
        # swallows it and the endpoints still return 200 with empty lists.
        services._GH_CACHE.pop(self.base, None)
        self.addCleanup(services._GH_CACHE.pop, self.base, None)
        with mock.patch.object(services, "_fetch_github_state",
                               side_effect=RuntimeError("gh down")):
            resp = self.client.get("/api/pipelines")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


class _FakeIssueRecorder:
    """An ``Issue`` stand-in recording add_label / remove_label calls (#225)."""

    instances: ClassVar[list["_FakeIssueRecorder"]] = []
    add_result: ClassVar[bool] = True
    remove_result: ClassVar[bool] = True

    def __init__(self, *, number, _repo):
        self.number = number
        self.added: list[str] = []
        self.removed: list[str] = []
        _FakeIssueRecorder.instances.append(self)

    def add_label(self, label):
        self.added.append(label)
        return _FakeIssueRecorder.add_result

    def remove_label(self, label):
        self.removed.append(label)
        return _FakeIssueRecorder.remove_result


class SetPipelineLabelTestCase(unittest.TestCase):
    """`services.set_pipeline_label`: set a ready-for-* label, clear the sibling."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _FakeIssueRecorder.instances = []
        _FakeIssueRecorder.add_result = True
        _FakeIssueRecorder.remove_result = True

    def _make_checkout(self, owner: str, name: str) -> None:
        (self.base / owner / name / ".git").mkdir(parents=True, exist_ok=True)

    def test_sets_label_and_removes_sibling(self) -> None:
        self._make_checkout("acme", "widgets")
        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch("loony_dev.github.Issue", _FakeIssueRecorder):
            result = services.set_pipeline_label(
                self.base, "issue-7", "ready-for-development", "acme/widgets",
            )
        issue = _FakeIssueRecorder.instances[0]
        self.assertEqual(issue.number, 7)
        self.assertEqual(issue.added, ["ready-for-development"])
        self.assertEqual(issue.removed, ["ready-for-planning"])
        self.assertEqual(result["label"], "ready-for-development")
        self.assertEqual(result["labels"], ["ready-for-development"])

    def test_invalid_label_rejected(self) -> None:
        self._make_checkout("acme", "widgets")
        with self.assertRaises(services.LabelControlError):
            services.set_pipeline_label(self.base, "issue-7", "in-progress", "acme/widgets")

    def test_pr_pipeline_rejected(self) -> None:
        self._make_checkout("acme", "widgets")
        with self.assertRaises(services.LabelControlError):
            services.set_pipeline_label(
                self.base, "pr-9", "ready-for-planning", "acme/widgets",
            )

    def test_bad_repo_rejected(self) -> None:
        for repo in (None, "noslash", "owner/", "/repo", "a/b/c"):
            with self.subTest(repo=repo):
                with self.assertRaises(services.LabelControlError):
                    services.set_pipeline_label(
                        self.base, "issue-7", "ready-for-planning", repo,
                    )

    def test_unknown_checkout_raises_session_not_found(self) -> None:
        # 404 path: a valid request whose repo has no checkout under base_dir.
        with self.assertRaises(services.SessionNotFoundError):
            services.set_pipeline_label(
                self.base, "issue-7", "ready-for-planning", "acme/missing",
            )

    def test_add_label_failure_raises(self) -> None:
        self._make_checkout("acme", "widgets")
        _FakeIssueRecorder.add_result = False
        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch("loony_dev.github.Issue", _FakeIssueRecorder):
            with self.assertRaises(services.LabelControlError):
                services.set_pipeline_label(
                    self.base, "issue-7", "ready-for-planning", "acme/widgets",
                )
        # The sibling is never removed when the add fails.
        self.assertEqual(_FakeIssueRecorder.instances[0].removed, [])

    def test_sibling_removal_failure_raises(self) -> None:
        # A failed sibling removal must raise rather than report a successful
        # mutually-exclusive state the issue doesn't actually have.
        self._make_checkout("acme", "widgets")
        _FakeIssueRecorder.remove_result = False
        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch("loony_dev.github.Issue", _FakeIssueRecorder):
            with self.assertRaises(services.LabelControlError):
                services.set_pipeline_label(
                    self.base, "issue-7", "ready-for-development", "acme/widgets",
                )
        # The requested label was applied; only the sibling removal failed.
        issue = _FakeIssueRecorder.instances[0]
        self.assertEqual(issue.added, ["ready-for-development"])
        self.assertEqual(issue.removed, ["ready-for-planning"])


class SetPipelineLabelApiTestCase(unittest.TestCase):
    """`POST /api/pipelines/{key}/labels`: status mapping for the label control."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _make_worker(self.base, "acme", "widgets", os.getpid(), ["x"])
        (self.base / "acme" / "widgets" / ".git").mkdir(parents=True, exist_ok=True)
        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        _FakeIssueRecorder.instances = []
        _FakeIssueRecorder.add_result = True
        _FakeIssueRecorder.remove_result = True

    def test_success_returns_label_set(self) -> None:
        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch("loony_dev.github.Issue", _FakeIssueRecorder):
            resp = self.client.post(
                "/api/pipelines/issue-7/labels",
                json={"label": "ready-for-development", "repo": "acme/widgets"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["labels"], ["ready-for-development"])

    def test_invalid_label_is_422(self) -> None:
        resp = self.client.post(
            "/api/pipelines/issue-7/labels",
            json={"label": "bogus", "repo": "acme/widgets"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_structural_validation_is_400(self) -> None:
        # Wrong-type / missing fields are structural errors → 400 (matches the
        # inject_turn / interrogate_pipeline house style), distinct from the 422
        # semantic rejections above.
        cases = [
            {"label": 7, "repo": "acme/widgets"},          # non-string label
            {"label": "  ", "repo": "acme/widgets"},        # blank label
            {"label": "ready-for-planning"},                 # missing repo
            {"label": "ready-for-planning", "repo": 9},     # non-string repo
        ]
        for body in cases:
            with self.subTest(body=body):
                resp = self.client.post("/api/pipelines/issue-7/labels", json=body)
                self.assertEqual(resp.status_code, 400)

    def test_pr_pipeline_is_422(self) -> None:
        resp = self.client.post(
            "/api/pipelines/pr-9/labels",
            json={"label": "ready-for-planning", "repo": "acme/widgets"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_unknown_checkout_is_404(self) -> None:
        resp = self.client.post(
            "/api/pipelines/issue-7/labels",
            json={"label": "ready-for-planning", "repo": "acme/missing"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_add_failure_is_422(self) -> None:
        _FakeIssueRecorder.add_result = False
        with mock.patch.object(services, "_repo_for", lambda o, n, c: object()), \
                mock.patch("loony_dev.github.Issue", _FakeIssueRecorder):
            resp = self.client.post(
                "/api/pipelines/issue-7/labels",
                json={"label": "ready-for-planning", "repo": "acme/widgets"},
            )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
