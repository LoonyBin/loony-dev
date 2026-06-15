"""Tests for hook-driven ClaudeSession events (issue #178).

Covers:

* the hook install/verify contract (:mod:`loony_dev.agents.session_hooks`),
* the hook executable (:func:`run_hook`) routing a payload to the right socket,
* multi-session isolation (two sessions do not cross-signal),
* the legacy ``session_events="jsonl"`` fallback path,
* the worker bootstrap refusing to start when hooks are missing.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from loony_dev.agents import session_hooks
from loony_dev.agents.claude_session import ClaudeSession, HookEventSource, TurnResult

_STUB = Path(__file__).parent / "_claude_stub.py"


def _tmpdir() -> str:
    return tempfile.mkdtemp()


def _wait_until(predicate, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("predicate not satisfied in time")


# ---------------------------------------------------------------------------
# Install / verify
# ---------------------------------------------------------------------------

class TestInstallVerify(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)

    def _settings(self) -> dict:
        return json.loads((self.cfg / "settings.json").read_text())

    def test_install_creates_all_required_hooks(self) -> None:
        self.assertTrue(session_hooks.install_hooks(self.cfg))
        ok, reason = session_hooks.verify_hooks(self.cfg)
        self.assertTrue(ok, reason)
        hooks = self._settings()["hooks"]
        self.assertEqual(set(hooks), set(session_hooks.HOOK_EVENT_NAMES))

    def test_install_is_idempotent(self) -> None:
        self.assertTrue(session_hooks.install_hooks(self.cfg))
        # Second run changes nothing.
        self.assertFalse(session_hooks.install_hooks(self.cfg))

    def test_install_preserves_hand_authored_hooks(self) -> None:
        custom = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "my-own-stop-hook"}]}],
                "PreCompact": [{"hooks": [{"type": "command", "command": "my-compact"}]}],
            },
            "model": "opus",
        }
        (self.cfg / "settings.json").write_text(json.dumps(custom))

        self.assertTrue(session_hooks.install_hooks(self.cfg))
        data = self._settings()
        # Unrelated keys/hooks untouched.
        self.assertEqual(data["model"], "opus")
        self.assertIn("PreCompact", data["hooks"])
        # The user's own Stop hook is preserved alongside ours.
        stop_cmds = [
            s["command"]
            for entry in data["hooks"]["Stop"]
            for s in entry["hooks"]
        ]
        self.assertIn("my-own-stop-hook", stop_cmds)
        self.assertIn(session_hooks.hook_command("Stop"), stop_cmds)

    def test_verify_false_when_missing(self) -> None:
        ok, reason = session_hooks.verify_hooks(self.cfg)
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

    def test_verify_false_when_stale(self) -> None:
        session_hooks.install_hooks(self.cfg)
        data = self._settings()
        # Simulate an operator wiping our Stop hook.
        data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "something-else"}]}]
        (self.cfg / "settings.json").write_text(json.dumps(data))
        ok, reason = session_hooks.verify_hooks(self.cfg)
        self.assertFalse(ok)
        self.assertIn("Stop", reason)

    def test_install_idempotent_after_preserving_user_hooks(self) -> None:
        custom = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
        (self.cfg / "settings.json").write_text(json.dumps(custom))
        self.assertTrue(session_hooks.install_hooks(self.cfg))
        self.assertFalse(session_hooks.install_hooks(self.cfg))


# ---------------------------------------------------------------------------
# Hook executable (run_hook)
# ---------------------------------------------------------------------------

class TestRunHook(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}))

    def _listen(self, session_id: str) -> tuple[socket.socket, list[bytes]]:
        path = session_hooks.channel_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(4)
        srv.settimeout(3.0)
        received: list[bytes] = []

        def accept() -> None:
            try:
                conn, _ = srv.accept()
                with conn:
                    received.append(conn.recv(65536))
            except OSError:
                pass

        t = threading.Thread(target=accept, daemon=True)
        t.start()
        self.addCleanup(srv.close)
        self.addCleanup(t.join, 1.0)
        return srv, received

    def test_session_start_routes_to_socket(self) -> None:
        sid = "sess-aaa"
        _, received = self._listen(sid)
        payload = json.dumps({"session_id": sid, "source": "startup"})
        rc = session_hooks.run_hook(["SessionStart"], payload)
        self.assertEqual(rc, 0)
        _wait_until(lambda: received, timeout=3.0)
        event = json.loads(received[0])
        self.assertEqual(event["event"], session_hooks.EVENT_SESSION_START)
        self.assertEqual(event["session_id"], sid)
        self.assertEqual(event["source"], "startup")

    def test_stop_carries_text_and_interrupt_flag(self) -> None:
        sid = "sess-bbb"
        transcript = self.cfg / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "message": {"content": "do it"}}) + "\n"
            + json.dumps({
                "type": "user",
                "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
            }) + "\n"
        )
        _, received = self._listen(sid)
        payload = json.dumps({
            "session_id": sid,
            "stop_hook_active": True,
            "last_assistant_message": "partial work",
            "transcript_path": str(transcript),
        })
        session_hooks.run_hook(["Stop"], payload)
        _wait_until(lambda: received, timeout=3.0)
        event = json.loads(received[0])
        self.assertEqual(event["event"], session_hooks.EVENT_STOP)
        self.assertEqual(event["text"], "partial work")
        self.assertTrue(event["interrupted"])

    def test_stop_not_interrupted_when_last_entry_is_assistant(self) -> None:
        sid = "sess-ccc"
        transcript = self.cfg / "t2.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": "done"}}) + "\n"
        )
        _, received = self._listen(sid)
        payload = json.dumps({
            "session_id": sid, "last_assistant_message": "done",
            "transcript_path": str(transcript),
        })
        session_hooks.run_hook(["Stop"], payload)
        _wait_until(lambda: received, timeout=3.0)
        event = json.loads(received[0])
        self.assertFalse(event["interrupted"])

    def test_unknown_event_is_noop(self) -> None:
        # No socket bound; must not raise and must return 0.
        self.assertEqual(session_hooks.run_hook(["NotAHook"], "{}"), 0)

    def test_missing_session_id_is_noop(self) -> None:
        self.assertEqual(session_hooks.run_hook(["SessionStart"], json.dumps({})), 0)


# ---------------------------------------------------------------------------
# Multi-session isolation (acceptance criterion)
# ---------------------------------------------------------------------------

class TestMultiSessionIsolation(unittest.TestCase):
    """Two sessions in different cwds must not cross-signal each other."""

    def setUp(self) -> None:
        self.config_dir = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, self.config_dir, ignore_errors=True)
        os.chmod(_STUB, 0o755)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)}))

    def _open(self, session_id: str) -> ClaudeSession:
        cwd = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        sess = ClaudeSession(
            cwd, session_id=session_id, binary=str(_STUB),
            backstop_seconds=20.0, debounce=0.2,
        )
        sess.open()
        self.addCleanup(sess.close)
        return sess

    def test_stop_for_one_session_does_not_release_the_other(self) -> None:
        a = self._open("session-A")
        b = self._open("session-B")

        results: dict[str, TurnResult] = {}

        def run(sess: ClaudeSession, key: str, prompt: str) -> None:
            results[key] = sess.send_turn(prompt, timeout=20.0)

        # Drive a normal turn on A only; B never gets a prompt, so B must not
        # complete from A's Stop event.
        ta = threading.Thread(target=run, args=(a, "a", "hello from A"))
        ta.start()
        ta.join(timeout=15.0)
        self.assertFalse(ta.is_alive())
        self.assertIn("a", results)
        self.assertIn("hello from A", results["a"].text)
        # B never received a turn → no result recorded.
        self.assertNotIn("b", results)

    def test_each_sessions_socket_is_distinct(self) -> None:
        pa = session_hooks.channel_path("session-A")
        pb = session_hooks.channel_path("session-B")
        self.assertNotEqual(pa, pb)
        self.assertEqual(pa.parent.parent, pb.parent.parent)


# ---------------------------------------------------------------------------
# Legacy JSONL fallback path (kept selectable for one release)
# ---------------------------------------------------------------------------

class TestLegacyJsonlSource(unittest.TestCase):
    """``session_events="jsonl"`` drives the legacy poll/parse path verbatim."""

    def setUp(self) -> None:
        self.config_dir = Path(_tmpdir())
        self.cwd = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, self.config_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)
        os.chmod(_STUB, 0o755)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)}))

    def test_turn_completes_via_jsonl(self) -> None:
        sess = ClaudeSession(
            self.cwd, binary=str(_STUB), backstop_seconds=20.0, debounce=0.2,
            session_events="jsonl",
        )
        sess.open()
        self.addCleanup(sess.close)
        result = sess.send_turn("hello jsonl", timeout=20.0)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertIn("hello jsonl", result.text)


# ---------------------------------------------------------------------------
# CLI: setup installs hooks; worker refuses to start when hooks are missing
# ---------------------------------------------------------------------------

class TestSetupCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}))

    def test_setup_installs_and_verifies(self) -> None:
        from click.testing import CliRunner

        from loony_dev.cli import cli

        result = CliRunner().invoke(cli, ["setup"])
        self.assertEqual(result.exit_code, 0, result.output)
        ok, _ = session_hooks.verify_hooks(self.cfg)
        self.assertTrue(ok)
        # Re-running reports already-up-to-date.
        result2 = CliRunner().invoke(cli, ["setup"])
        self.assertEqual(result2.exit_code, 0, result2.output)
        self.assertIn("up to date", result2.output)


class TestWorkerRefusesWithoutHooks(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Path(_tmpdir())
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)

    def test_worker_refuses_when_verify_fails(self) -> None:
        from click.testing import CliRunner

        from loony_dev.cli import cli

        # install_hooks is a no-op (so the file is never written) and verify_hooks
        # reports failure → the worker must refuse to start with a clear message,
        # before any GitHub/orchestrator work happens.
        with mock.patch("loony_dev.agents.session_hooks.install_hooks"), \
             mock.patch(
                 "loony_dev.agents.session_hooks.verify_hooks",
                 return_value=(False, "missing hook for Stop"),
             ), \
             mock.patch("loony_dev.cli.install_commands", return_value=[]):
            result = CliRunner().invoke(cli, ["worker", "--repo", "acme/widgets"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("loony-dev setup", result.output)


if __name__ == "__main__":
    unittest.main()
