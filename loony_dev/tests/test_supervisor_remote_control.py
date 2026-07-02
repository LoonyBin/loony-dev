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
        self.name = supervisor._remote_control_name(self.repo)
        self.command = supervisor._remote_control_command(self.repo)
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
        _log_file, _conn_file, repo, base_dir, started_at = proc.args
        self.assertEqual(repo, self.repo)
        self.assertEqual(base_dir, self.checkout)
        self.assertIsNotNone(started_at)
        self.assertEqual(proc.name, "remote-control-acme-widgets")
        self.assertEqual(rcp.repo, self.repo)
        self.assertEqual(rcp.started_at_iso, started_at)

    def test_writes_pid_and_connection_files(self) -> None:
        _rcp, _ctx = self._launch()
        self.assertEqual(self.pid_file.read_text(), "4321")
        data = json.loads(self.conn_file.read_text())
        self.assertEqual(data["repo"], self.repo)
        self.assertEqual(data["mode"], "remote-control")
        self.assertEqual(data["pid"], 4321)
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["command"], self.command)
        self.assertIsNotNone(data["started_at"])
        # The shrunk health schema carries no attach-handle / join-URL fields (#304).
        for gone in ("session_id", "key", "join_url", "cwd"):
            self.assertNotIn(gone, data)

    def test_connection_file_rewritten_on_relaunch(self) -> None:
        self._launch()
        first = json.loads(self.conn_file.read_text())
        # Simulate a restart: the connection file is rewritten each (re)launch;
        # the command is stable while started_at advances.
        with mock.patch.object(supervisor, "datetime") as fake_dt:
            fake_dt.now.return_value.isoformat.return_value = "2026-06-06T00:00:00+00:00"
            self._launch()
        second = json.loads(self.conn_file.read_text())
        self.assertEqual(first["command"], second["command"])
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


class RemoteControlNameTestCase(unittest.TestCase):
    def test_sanitizes_special_characters(self) -> None:
        # The name keeps a readable ``loony-<sanitized>`` prefix and appends a
        # 10-char hex digest of the original repo string.
        cases = {
            "acme/widgets": "loony-acme-widgets-",
            "My.Org/Repo_X": "loony-My-Org-Repo-X-",
            "a--b/c.d": "loony-a-b-c-d-",
            "owner/repo.git": "loony-owner-repo-git-",
        }
        for repo, prefix in cases.items():
            with self.subTest(repo=repo):
                name = supervisor._remote_control_name(repo)
                self.assertTrue(name.startswith(prefix), name)
                digest = name[len(prefix):]
                self.assertEqual(len(digest), 10)
                self.assertTrue(all(c in "0123456789abcdef" for c in digest), digest)

    def test_is_deterministic(self) -> None:
        self.assertEqual(
            supervisor._remote_control_name("acme/widgets"),
            supervisor._remote_control_name("acme/widgets"),
        )

    def test_collision_resistant(self) -> None:
        # These all sanitize to ``loony-acme-foo-bar`` but must stay distinct.
        repos = ["acme/foo-bar", "acme/foo_bar", "acme-foo/bar"]
        names = {supervisor._remote_control_name(r) for r in repos}
        self.assertEqual(len(names), len(repos))


class RemoteControlCommandTestCase(unittest.TestCase):
    def test_builds_claude_rc_server_argv(self) -> None:
        # #304: a persistent ``claude rc`` server, not a single followed session.
        cmd = supervisor._remote_control_command("acme/widgets")
        self.assertEqual(cmd[:2], ["claude", "rc"])
        self.assertIn("--allow-dangerously-skip-permissions", cmd)
        self.assertEqual(cmd[cmd.index("--spawn") + 1], "worktree")
        self.assertIn("--no-create-session-in-dir", cmd)
        self.assertEqual(
            cmd[cmd.index("--name") + 1], supervisor._remote_control_name("acme/widgets")
        )
        # The retired single-session flags are gone.
        self.assertNotIn("--remote-control", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)


class RemoteControlGaveUpTestCase(unittest.TestCase):
    """The #304 crash-budget predicate: error out instead of restarting forever."""

    def test_restarts_below_threshold(self) -> None:
        self.assertFalse(supervisor._remote_control_gave_up(0, 5))
        self.assertFalse(supervisor._remote_control_gave_up(4, 5))

    def test_gives_up_at_threshold(self) -> None:
        self.assertTrue(supervisor._remote_control_gave_up(5, 5))
        self.assertTrue(supervisor._remote_control_gave_up(6, 5))

    def test_errored_status_schema(self) -> None:
        # Once given up, the supervisor rewrites the health file as errored.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = Path(tmp.name) / "remote-control.json"
        supervisor._write_connection_file(
            conn,
            repo="acme/widgets",
            pid=None,
            started_at="2026-06-06T00:00:00+00:00",
            command=supervisor._remote_control_command("acme/widgets"),
            status=supervisor.STATUS_ERRORED,
        )
        data = json.loads(conn.read_text())
        self.assertEqual(data["status"], "errored")
        self.assertIsNone(data["pid"])
        self.assertEqual(data["repo"], "acme/widgets")


class RestartBackoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = config.settings
        config.settings = Settings({"min_restart_delay": 5.0, "max_restart_delay": 300.0})
        self.addCleanup(lambda: setattr(config, "settings", self._saved))

    def _record(self, restart_count: int) -> supervisor.RemoteControlProcess:
        return supervisor.RemoteControlProcess(
            repo="acme/widgets",
            base_dir=Path("/x"),
            log_file=Path("/x.log"),
            pid_file=Path("/x.pid"),
            conn_file=Path("/x.json"),
            process=_FakeProcess(),
            started_at=0.0,
            started_at_iso="2026-06-06T00:00:00+00:00",
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
                    "2026-06-06T00:00:00+00:00",
                )

        self.assertEqual(cm.exception.code, 0)
        # The slave PTY is sized to a sane, non-zero geometry before claude launches.
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
        # The persistent ``claude rc`` server launched with the PTY slave as its
        # stdio in a new session.
        args, kwargs = popen.call_args
        self.assertEqual(args[0], supervisor._remote_control_command("acme/widgets"))
        self.assertEqual(args[0][:2], ["claude", "rc"])
        self.assertEqual(kwargs["cwd"], str(self.base))
        self.assertEqual(kwargs["stdin"], 11)
        self.assertEqual(kwargs["stdout"], 11)
        self.assertEqual(kwargs["stderr"], 11)
        self.assertTrue(kwargs["start_new_session"])
        # The connection file is refreshed with the live (child) PID + running status.
        data = json.loads(self.conn_file.read_text())
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["command"], supervisor._remote_control_command("acme/widgets"))

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
                    "2026-06-06T00:00:00+00:00",
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
        # A PTY that can't be sized exits the child (the backoff loop restarts it)
        # rather than launching claude on a broken terminal.
        closed: list[int] = []
        with mock.patch("pty.openpty", return_value=(10, 11)), \
             mock.patch("fcntl.ioctl", side_effect=OSError("not a tty")), \
             mock.patch.object(supervisor.os, "close", side_effect=closed.append), \
             mock.patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaises(SystemExit) as cm:
                supervisor._run_remote_control_process(
                    self.log_file, self.conn_file, "acme/widgets", self.base,
                    "2026-06-06T00:00:00+00:00",
                )
        self.assertEqual(cm.exception.code, 1)
        popen.assert_not_called()  # claude is never launched
        self.assertCountEqual(closed, [10, 11])  # both PTY fds released
        # The child writes its "running" connection file only after the PTY is
        # sized and claude launches, so a winsize failure leaves no fake live
        # server behind.
        self.assertFalse(self.conn_file.exists())


if __name__ == "__main__":
    unittest.main()
