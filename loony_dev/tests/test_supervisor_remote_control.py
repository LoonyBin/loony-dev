"""Tests for the supervisor's remote-control session management (issue #129).

``multiprocessing`` and ``subprocess`` are mocked throughout so no real
``claude`` process is ever launched.
"""
from __future__ import annotations

import json
import struct
import tempfile
import termios
import unittest
from pathlib import Path
from unittest import mock

from loony_dev import config, supervisor
from loony_dev.config._settings import Settings


class _FakeProcess:
    """Stand-in for ``multiprocessing.Process`` that never actually runs."""

    def __init__(self, target=None, args=(), name=None) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.pid = 4321
        self._exitcode = None
        self.started = False
        self.terminated = False
        self.killed = False

    def start(self) -> None:
        self.started = True

    @property
    def exitcode(self):
        return self._exitcode

    def terminate(self) -> None:
        self.terminated = True
        self._exitcode = -15

    def kill(self) -> None:
        self.killed = True
        self._exitcode = -9

    def join(self, timeout=None) -> None:
        pass


class _FakeContext:
    def __init__(self, factory) -> None:
        self._factory = factory
        self.created: list[_FakeProcess] = []

    def Process(self, *, target=None, args=(), name=None):  # noqa: N802
        proc = self._factory(target=target, args=args, name=name)
        self.created.append(proc)
        return proc


class LaunchRemoteControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = "acme/widgets"
        self.session_id = supervisor._remote_control_session_id(self.repo)
        self.checkout = self.base / "acme" / "widgets"
        self.checkout.mkdir(parents=True)
        self.log_dir = self.base / ".logs" / "acme" / "widgets"
        self.log_file = self.log_dir / "remote-control.log"
        self.pid_file = self.log_dir / "remote-control.pid"
        self.conn_file = self.log_dir / "remote-control.json"

    def _launch(self) -> tuple[supervisor.RemoteControlProcess, _FakeContext]:
        ctx = _FakeContext(_FakeProcess)
        with mock.patch.object(supervisor.multiprocessing, "get_context", return_value=ctx):
            rcp = supervisor.launch_remote_control(
                repo=self.repo,
                base_dir=self.checkout,
                log_file=self.log_file,
                pid_file=self.pid_file,
                conn_file=self.conn_file,
            )
        return rcp, ctx

    def test_builds_expected_command_and_cwd(self) -> None:
        rcp, ctx = self._launch()
        # The spawned process targets the child entrypoint with the right args.
        proc = ctx.created[0]
        self.assertIs(proc.target, supervisor._run_remote_control_process)
        _log_file, _conn_file, repo, base_dir, session_id, key, _started = proc.args
        self.assertEqual(repo, self.repo)
        self.assertEqual(base_dir, self.checkout)
        self.assertEqual(session_id, self.session_id)
        self.assertEqual(key, "base")
        self.assertEqual(proc.name, "remote-control-acme-widgets")
        self.assertEqual(rcp.session_id, self.session_id)
        self.assertEqual(rcp.key, "base")

    def test_writes_pid_and_connection_files(self) -> None:
        _rcp, _ctx = self._launch()
        self.assertEqual(self.pid_file.read_text(), "4321")
        data = json.loads(self.conn_file.read_text())
        self.assertEqual(data["repo"], self.repo)
        self.assertEqual(data["mode"], "remote-control")
        self.assertEqual(data["session_id"], self.session_id)
        self.assertEqual(data["key"], "base")
        self.assertEqual(data["cwd"], str(self.checkout))
        self.assertEqual(data["pid"], 4321)
        self.assertEqual(data["status"], "running")
        self.assertIsNone(data["join_url"])
        self.assertEqual(
            data["command"],
            ["claude", "--remote-control", self.session_id, "--dangerously-skip-permissions"],
        )
        self.assertIsNotNone(data["started_at"])

    def test_connection_file_rewritten_on_relaunch(self) -> None:
        self._launch()
        first = json.loads(self.conn_file.read_text())
        # Simulate a restart: the connection file is rewritten each (re)launch but
        # the session id is stable.
        with mock.patch.object(supervisor, "datetime") as fake_dt:
            fake_dt.now.return_value.isoformat.return_value = "2026-06-06T00:00:00+00:00"
            self._launch()
        second = json.loads(self.conn_file.read_text())
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(second["started_at"], "2026-06-06T00:00:00+00:00")


class LaunchWorkerTestCase(unittest.TestCase):
    """The supervisor threads its --base-dir down to each worker (#285)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = self.base / "LoonyBin" / "loony-dev"
        self.log_file = self.base / ".logs" / "worker.log"
        self.pid_file = self.base / ".logs" / "worker.pid"

    def _launch(self) -> _FakeContext:
        ctx = _FakeContext(_FakeProcess)
        with mock.patch.object(supervisor.multiprocessing, "get_context", return_value=ctx), \
                mock.patch.object(supervisor.config, "_load_config", return_value={}):
            supervisor.launch_worker(
                repo="LoonyBin/loony-dev",
                work_dir=self.work_dir,
                log_file=self.log_file,
                pid_file=self.pid_file,
                base_dir=self.base,
            )
        return ctx

    def test_threads_base_dir_into_worker_command(self) -> None:
        ctx = self._launch()
        proc = ctx.created[0]
        self.assertIs(proc.target, supervisor._run_worker_process)
        _log_file, cmd_args = proc.args
        # The worker must resolve the same base-dir as the supervisor/web so its
        # session registry + pipeline logs land where the dashboard reads them.
        self.assertIn("--base-dir", cmd_args)
        idx = cmd_args.index("--base-dir")
        self.assertEqual(cmd_args[idx + 1], str(self.base))
        self.assertEqual(
            cmd_args[:6],
            ["worker", "--repo", "LoonyBin/loony-dev", "--work-dir", str(self.work_dir), "--base-dir"],
        )
        self.assertTrue(proc.started)

    def test_writes_pid_file(self) -> None:
        self._launch()
        self.assertEqual(self.pid_file.read_text(), "4321")


class LaunchWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.supervisor_log = self.base / ".logs" / "supervisor.log"
        self.log_file = self.base / ".logs" / "web.log"
        self.pid_file = self.base / ".logs" / "web.pid"

    def _launch(self) -> tuple[supervisor.WebProcess, _FakeContext]:
        ctx = _FakeContext(_FakeProcess)
        with mock.patch.object(supervisor.multiprocessing, "get_context", return_value=ctx), \
                mock.patch.object(supervisor.config, "_load_config", return_value={}):
            wp = supervisor.launch_web(
                base_dir=self.base,
                supervisor_log=self.supervisor_log,
                log_file=self.log_file,
                pid_file=self.pid_file,
            )
        return wp, ctx

    def test_forwards_base_dir_and_supervisor_log(self) -> None:
        wp, ctx = self._launch()
        proc = ctx.created[0]
        # Reuses the generic CLI child entrypoint with the `web` sub-command.
        self.assertIs(proc.target, supervisor._run_worker_process)
        _log_file, cmd_args = proc.args
        self.assertEqual(
            cmd_args,
            [
                "web",
                "--base-dir", str(self.base),
                "--supervisor-log", str(self.supervisor_log),
            ],
        )
        # No host/port flags — the dashboard keeps its own [web] config defaults.
        self.assertNotIn("--host", cmd_args)
        self.assertNotIn("--port", cmd_args)
        self.assertEqual(proc.name, "web-dashboard")
        self.assertTrue(proc.started)
        self.assertEqual(wp.base_dir, self.base)
        self.assertEqual(wp.restart_count, 0)

    def test_writes_pid_file(self) -> None:
        self._launch()
        self.assertEqual(self.pid_file.read_text(), "4321")


class SupervisorWebFlagTestCase(unittest.TestCase):
    """The --web flag defaults off, so the supervisor is unchanged without it."""

    def test_web_flag_defaults_off(self) -> None:
        from click.testing import CliRunner

        from loony_dev.cli import cli

        captured: dict[str, object] = {}

        def fake_run_supervisor() -> None:
            captured["web"] = config.settings.get("web")

        # Ignore any config file installed on the host so the assertion reflects
        # the flag's own default, not a local `[supervisor] web = true`.
        with mock.patch("loony_dev.config._loader._load_config", return_value={}), \
                mock.patch.object(supervisor, "run_supervisor", fake_run_supervisor), \
                tempfile.TemporaryDirectory() as tmp:
            result = CliRunner().invoke(
                cli, ["supervisor", "--base-dir", tmp], catch_exceptions=False
            )
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(captured["web"])


class SessionIdSanitizationTestCase(unittest.TestCase):
    def test_sanitizes_special_characters(self) -> None:
        # The id keeps a readable ``loony-<sanitized>`` prefix and appends a
        # 10-char hex digest of the original repo string.
        cases = {
            "acme/widgets": "loony-acme-widgets-",
            "My.Org/Repo_X": "loony-My-Org-Repo-X-",
            "a--b/c.d": "loony-a-b-c-d-",
            "owner/repo.git": "loony-owner-repo-git-",
        }
        for repo, prefix in cases.items():
            with self.subTest(repo=repo):
                session_id = supervisor._remote_control_session_id(repo)
                self.assertTrue(session_id.startswith(prefix), session_id)
                digest = session_id[len(prefix):]
                self.assertEqual(len(digest), 10)
                self.assertTrue(all(c in "0123456789abcdef" for c in digest), digest)

    def test_is_deterministic(self) -> None:
        self.assertEqual(
            supervisor._remote_control_session_id("acme/widgets"),
            supervisor._remote_control_session_id("acme/widgets"),
        )

    def test_collision_resistant(self) -> None:
        # These all sanitize to ``loony-acme-foo-bar`` but must stay distinct.
        repos = ["acme/foo-bar", "acme/foo_bar", "acme-foo/bar"]
        ids = {supervisor._remote_control_session_id(r) for r in repos}
        self.assertEqual(len(ids), len(repos))


class RestartBackoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = config.settings
        config.settings = Settings({"min_restart_delay": 5.0, "max_restart_delay": 300.0})
        self.addCleanup(lambda: setattr(config, "settings", self._saved))

    def _record(self, restart_count: int) -> supervisor.RemoteControlProcess:
        return supervisor.RemoteControlProcess(
            repo="acme/widgets",
            base_dir=Path("/x"),
            session_id="loony-acme-widgets",
            key="base",
            log_file=Path("/x.log"),
            pid_file=Path("/x.pid"),
            conn_file=Path("/x.json"),
            process=_FakeProcess(),
            started_at=0.0,
            restart_count=restart_count,
        )

    def test_backoff_delay_sequence_capped(self) -> None:
        delays: list[float] = []
        new_record = self._record(0)

        def fake_sleep(seconds, should_stop):
            delays.append(seconds)

        with mock.patch.object(supervisor, "_interruptible_sleep", side_effect=fake_sleep):
            for n in range(8):
                record = self._record(n)
                supervisor._restart_after_backoff(
                    record, "remote-control", lambda: new_record, lambda: False
                )
        # 5, 10, 20, 40, 80, 160, 300 (capped), 300 (capped)
        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0])

    def test_increments_restart_count_on_relaunch(self) -> None:
        record = self._record(3)
        new_record = self._record(0)
        with mock.patch.object(supervisor, "_interruptible_sleep"):
            result = supervisor._restart_after_backoff(
                record, "remote-control", lambda: new_record, lambda: False
            )
        self.assertIs(result, new_record)
        self.assertEqual(result.restart_count, 4)

    def test_shutdown_during_delay_skips_relaunch(self) -> None:
        record = self._record(0)
        relaunch = mock.Mock()
        with mock.patch.object(supervisor, "_interruptible_sleep"):
            result = supervisor._restart_after_backoff(
                record, "remote-control", relaunch, lambda: True
            )
        self.assertIsNone(result)
        relaunch.assert_not_called()

    def test_relaunch_failure_returns_none(self) -> None:
        record = self._record(0)

        def boom():
            raise RuntimeError("launch failed")

        with mock.patch.object(supervisor, "_interruptible_sleep"):
            result = supervisor._restart_after_backoff(
                record, "remote-control", boom, lambda: False
            )
        self.assertIsNone(result)


class TeardownTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_terminate_process_removes_pid_file(self) -> None:
        pid_file = self.base / "remote-control.pid"
        pid_file.write_text("4321")
        proc = _FakeProcess()
        proc._exitcode = None  # alive until terminate()
        supervisor._terminate_process(proc, pid_file, "Remote-control for acme/widgets")
        self.assertTrue(proc.terminated)
        self.assertFalse(pid_file.exists())

    def test_remove_connection_file_preserves_log(self) -> None:
        conn_file = self.base / "remote-control.json"
        log_file = self.base / "remote-control.log"
        conn_file.write_text("{}")
        log_file.write_text("log lines\n")
        supervisor._remove_connection_file(conn_file)
        self.assertFalse(conn_file.exists())
        self.assertTrue(log_file.exists())  # logs preserved on teardown

    def test_remove_connection_file_missing_is_noop(self) -> None:
        # Must not raise when the file does not exist.
        supervisor._remove_connection_file(self.base / "absent.json")


class ChildEntrypointTestCase(unittest.TestCase):
    """Exercise ``_run_remote_control_process`` without launching ``claude``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.log_file = self.base / "remote-control.log"
        self.conn_file = self.base / "remote-control.json"

    def test_allocates_pty_and_launches_claude(self) -> None:
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = 0  # already exited -> loop breaks immediately
        fake_proc.wait.return_value = 0

        with mock.patch("pty.openpty", return_value=(10, 11)), \
             mock.patch("fcntl.ioctl") as ioctl, \
             mock.patch("select.select", return_value=([], [], [])), \
             mock.patch.object(supervisor.os, "close"), \
             mock.patch.object(supervisor.subprocess, "Popen", return_value=fake_proc) as popen:
            with self.assertRaises(SystemExit) as cm:
                supervisor._run_remote_control_process(
                    self.log_file,
                    self.conn_file,
                    "acme/widgets",
                    self.base,
                    "loony-acme-widgets",
                    "base",
                    "2026-06-06T00:00:00+00:00",
                )

        self.assertEqual(cm.exception.code, 0)
        # The slave PTY is sized to the pinned geometry before claude launches,
        # so the join-URL footer never wraps (issue #293).
        ioctl.assert_called_once_with(
            11,
            termios.TIOCSWINSZ,
            struct.pack(
                "HHHH",
                supervisor._REMOTE_CONTROL_PTY_ROWS,
                supervisor._REMOTE_CONTROL_PTY_COLS,
                0,
                0,
            ),
        )
        # claude launched with the PTY slave as its stdio in a new session.
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0],
            ["claude", "--remote-control", "loony-acme-widgets", "--dangerously-skip-permissions"],
        )
        self.assertEqual(kwargs["cwd"], str(self.base))
        self.assertEqual(kwargs["stdin"], 11)
        self.assertEqual(kwargs["stdout"], 11)
        self.assertEqual(kwargs["stderr"], 11)
        self.assertTrue(kwargs["start_new_session"])
        # The connection file is refreshed with the live (child) PID.
        data = json.loads(self.conn_file.read_text())
        self.assertEqual(data["session_id"], "loony-acme-widgets")
        self.assertEqual(data["key"], "base")

    def test_exits_with_claude_return_code(self) -> None:
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = 3
        fake_proc.wait.return_value = 3
        with mock.patch("pty.openpty", return_value=(10, 11)), \
             mock.patch("fcntl.ioctl") as ioctl, \
             mock.patch("select.select", return_value=([], [], [])), \
             mock.patch.object(supervisor.os, "close"), \
             mock.patch.object(supervisor.subprocess, "Popen", return_value=fake_proc):
            with self.assertRaises(SystemExit) as cm:
                supervisor._run_remote_control_process(
                    self.log_file, self.conn_file, "acme/widgets", self.base,
                    "loony-acme-widgets", "base", "2026-06-06T00:00:00+00:00",
                )
        self.assertEqual(cm.exception.code, 3)
        ioctl.assert_called_once_with(
            11,
            termios.TIOCSWINSZ,
            struct.pack(
                "HHHH",
                supervisor._REMOTE_CONTROL_PTY_ROWS,
                supervisor._REMOTE_CONTROL_PTY_COLS,
                0,
                0,
            ),
        )

    def test_winsize_failure_fails_fast_before_launch(self) -> None:
        # A PTY that can't be sized to the scanner's geometry can only yield a
        # wrapped/unusable join URL, so the child exits (backoff restarts it)
        # rather than launching claude on a mismatched terminal (#293 review).
        closed: list[int] = []
        with mock.patch("pty.openpty", return_value=(10, 11)), \
             mock.patch("fcntl.ioctl", side_effect=OSError("not a tty")), \
             mock.patch.object(supervisor.os, "close", side_effect=closed.append), \
             mock.patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaises(SystemExit) as cm:
                supervisor._run_remote_control_process(
                    self.log_file, self.conn_file, "acme/widgets", self.base,
                    "loony-acme-widgets", "base", "2026-06-06T00:00:00+00:00",
                )
        self.assertEqual(cm.exception.code, 1)
        popen.assert_not_called()  # claude is never launched
        self.assertCountEqual(closed, [10, 11])  # both PTY fds released
        # The child writes its "running" connection file only after the PTY is
        # sized and claude launches, so a winsize failure leaves no fake live
        # session behind (#293 review).
        self.assertFalse(self.conn_file.exists())


class JoinUrlScanTestCase(unittest.TestCase):
    """Recovering the join URL from a remote-control PTY stream (issues #284/#293).

    The scanner renders the byte stream through a terminal emulator and reads the
    URL from the *rendered* screen — naive escape-stripping cannot reconstruct a
    line the TUI composed by repositioning the cursor (#293).
    """

    # The true URL embedded in the real ``ESC[28G`` capture below: ``session_``,
    # not the malformed ``sssion_`` that byte-scraping produced on the live box.
    TRUE_URL = "https://claude.ai/code/session_019DJ7qa3YebXAiD7UugmMzm"

    @staticmethod
    def _cha_capture() -> bytes:
        """A real CHA-repositioned frame captured from a live remote-control log.

        Contains the ``…/code/s`` ``ESC[28G`` ``ssion_…`` frame plus the preceding
        footer paint that wrote the column the cursor jumps over — both are needed
        because the correct ``e`` lives in cumulative screen state, not one chunk.
        """
        fixture = Path(__file__).parent / "fixtures" / "remote_control_cha.bin"
        return fixture.read_bytes()

    def test_finds_claude_ai_url(self) -> None:
        data = b"Join here: https://claude.ai/remote-control/abc123 now\r\n"
        self.assertEqual(
            supervisor._scan_for_join_url(data),
            "https://claude.ai/remote-control/abc123",
        )

    def test_returns_none_without_url(self) -> None:
        self.assertIsNone(supervisor._scan_for_join_url(b"booting interactive session"))

    def test_ignores_unrelated_url(self) -> None:
        self.assertIsNone(supervisor._scan_for_join_url(b"see https://example.com/docs"))

    def test_clean_url_preserved(self) -> None:
        # A clean URL with no cursor games renders unchanged (don't regress #284).
        clean = b"Join here: https://claude.ai/remote-control/abc123 now\r\n"
        self.assertEqual(
            supervisor._scan_for_join_url(clean),
            "https://claude.ai/remote-control/abc123",
        )

    def test_reconstructs_cha_repositioned_url(self) -> None:
        # The core regression: the raw capture greps as ``…/code/sssion_…`` (the
        # ``e`` dropped, an extra ``s``); rendering the screen recovers the true
        # ``session_<id>`` URL exactly.
        self.assertEqual(
            supervisor._scan_for_join_url(self._cha_capture()),
            self.TRUE_URL,
        )

    def test_reconstructs_across_chunk_boundaries(self) -> None:
        # One long-lived scanner must stitch the URL together no matter where the
        # read boundaries fall — including a split inside ``ESC[28G`` and inside
        # the URL — guarding the cross-chunk statefulness per-chunk scraping lacked.
        capture = self._cha_capture()
        for size in (1, 7, 64, 250):
            with self.subTest(chunk_size=size):
                scanner = supervisor._JoinUrlScanner()
                result = None
                for i in range(0, len(capture), size):
                    found = scanner.feed(capture[i : i + size])
                    if found:
                        result = found
                self.assertEqual(result, self.TRUE_URL)

    def test_malformed_render_degrades_to_none(self) -> None:
        # A screen that renders the broken ``…/code/sssion_…`` must not persist —
        # validation drops it to ``None`` (the issue's "degrade, never persist").
        data = b"  https://claude.ai/code/sssion_019DJ7qa3YebXAiD7UugmMzm\r\n"
        self.assertIsNone(supervisor._scan_for_join_url(data))

    def test_partial_cha_frame_degrades_to_none(self) -> None:
        # A mid-redraw CHA frame with no preceding paint renders a gap at the
        # skipped column (``…/code/s   ssion_…``); the candidate is just
        # ``…/code/s`` and fails validation, so nothing is persisted yet.
        data = b"\x1b[2C\x1b[1Bhttps://claude.ai/code/s\x1b[28Gssion_01ABC\r\n"
        self.assertIsNone(supervisor._scan_for_join_url(data))


if __name__ == "__main__":
    unittest.main()
