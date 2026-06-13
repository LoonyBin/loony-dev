"""Tests for the persistent PTY-backed ClaudeSession (issue #161).

Most tests drive a tiny stub binary (``_claude_stub.py``) that emulates the
real ``claude`` CLI's PTY + JSONL behaviour, so they run without the real
binary installed.  One integration test, gated on ``claude`` actually being on
PATH, exercises the real CLI end to end.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import unittest
from pathlib import Path
from unittest import mock

from loony_dev.agents.claude_session import (
    ClaudeSession,
    QuotaExceededError,
    ReadinessTimeout,
    TurnResult,
    _entry_text,
    _is_interrupt,
    _is_terminal_assistant,
    _JsonlTailer,
    _project_slug,
    jsonl_path_for,
)

_STUB = Path(__file__).parent / "_claude_stub.py"


# ---------------------------------------------------------------------------
# Pure-function unit tests (no subprocess)
# ---------------------------------------------------------------------------

class TestSlugAndPath(unittest.TestCase):
    def test_project_slug_replaces_non_alnum(self) -> None:
        self.assertEqual(
            _project_slug(Path("/home/u/loony-dev/.worktrees/x")),
            "-home-u-loony-dev--worktrees-x",
        )

    def test_jsonl_path_honours_config_dir(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/tmp/cfg"}):
            path = jsonl_path_for(Path("/home/u/repo"), "abc-123")
        self.assertEqual(
            path, Path("/tmp/cfg/projects/-home-u-repo/abc-123.jsonl"),
        )


class TestEntryHelpers(unittest.TestCase):
    def test_entry_text_from_block_list(self) -> None:
        entry = {"message": {"content": [
            {"type": "thinking", "text": "hmm"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Bash"},
        ]}}
        self.assertEqual(_entry_text(entry), "hmm\nhello")

    def test_entry_text_from_string(self) -> None:
        self.assertEqual(_entry_text({"message": {"content": "plain"}}), "plain")

    def test_terminal_assistant_detection(self) -> None:
        self.assertTrue(_is_terminal_assistant(
            {"type": "assistant", "message": {"stop_reason": "end_turn"}}))
        self.assertTrue(_is_terminal_assistant(
            {"type": "assistant", "message": {"stop_reason": "stop_sequence"}}))
        self.assertFalse(_is_terminal_assistant(
            {"type": "assistant", "message": {"stop_reason": "tool_use"}}))
        self.assertFalse(_is_terminal_assistant(
            {"type": "user", "message": {"stop_reason": "end_turn"}}))

    def test_interrupt_detection(self) -> None:
        self.assertTrue(_is_interrupt({"type": "user", "message": {
            "content": [{"type": "text", "text": "[Request interrupted by user]"}]}}))
        self.assertTrue(_is_interrupt({"type": "user", "message": {
            "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}}))
        self.assertFalse(_is_interrupt({"type": "user", "message": {
            "content": [{"type": "text", "text": "normal prompt"}]}}))


class TestJsonlTailer(unittest.TestCase):
    def test_incremental_reads_no_reparse(self) -> None:
        tmp = Path(self.enterContext(_tmpdir())) / "s.jsonl"
        tmp.write_text(json.dumps({"n": 1}) + "\n")
        tailer = _JsonlTailer(tmp)
        self.assertEqual([e["n"] for e in tailer.read_new()], [1])
        self.assertEqual(tailer.read_new(), [])  # nothing new
        with tmp.open("a") as fh:
            fh.write(json.dumps({"n": 2}) + "\n")
        self.assertEqual([e["n"] for e in tailer.read_new()], [2])

    def test_tolerates_partial_final_line(self) -> None:
        tmp = Path(self.enterContext(_tmpdir())) / "s.jsonl"
        tmp.write_text(json.dumps({"n": 1}) + "\n" + '{"n": 2')  # no newline yet
        tailer = _JsonlTailer(tmp)
        self.assertEqual([e["n"] for e in tailer.read_new()], [1])
        with tmp.open("a") as fh:
            fh.write("}\n")  # complete the dangling line
        self.assertEqual([e["n"] for e in tailer.read_new()], [2])

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(_JsonlTailer(Path("/nope/missing.jsonl")).read_new(), [])


# ---------------------------------------------------------------------------
# Stub-binary driven tests (real PTY, fake claude)
# ---------------------------------------------------------------------------

class _StubSessionTest(unittest.TestCase):
    """Base: a ClaudeSession backed by the stub binary, in an isolated config dir."""

    extra_env: dict[str, str] = {}

    def setUp(self) -> None:
        self.config_dir = Path(self.enterContext(_tmpdir()))
        self.cwd = Path(self.enterContext(_tmpdir()))
        os.chmod(_STUB, 0o755)
        env = {"CLAUDE_CONFIG_DIR": str(self.config_dir), **self.extra_env}
        self.enterContext(mock.patch.dict(os.environ, env))
        self.session = ClaudeSession(
            self.cwd,
            binary=str(_STUB),
            readiness_timeout=10.0,
            debounce=0.2,
        )

    def tearDown(self) -> None:
        self.session.close()


class TestOpenAndTurns(_StubSessionTest):
    def test_open_sets_handles_and_creates_jsonl(self) -> None:
        self.session.open()
        self.assertGreater(self.session.pid, 0)
        self.assertGreaterEqual(self.session.pty_master_fd, 0)
        self.assertTrue(self.session.jsonl_path.exists())

    def test_multi_turn_no_respawn(self) -> None:
        self.session.open()
        pid = self.session.pid

        r1 = self.session.send_turn("first prompt", timeout=10.0)
        self.assertIsInstance(r1, TurnResult)
        self.assertEqual(r1.stop_reason, "end_turn")
        self.assertFalse(r1.was_interrupted)
        self.assertIn("first prompt", r1.text)

        r2 = self.session.send_turn("second prompt", timeout=10.0)
        self.assertEqual(r2.stop_reason, "end_turn")
        self.assertIn("second prompt", r2.text)

        # Same process drove both turns.
        self.assertEqual(self.session.pid, pid)


class TestInterruptAndResume(_StubSessionTest):
    extra_env = {"STUB_LONGTURN_SECS": "20"}

    def test_interrupt_then_resume(self) -> None:
        self.session.open()
        pid = self.session.pid

        result: dict[str, TurnResult] = {}

        def run_long() -> None:
            result["turn"] = self.session.send_turn("LONGTURN please", timeout=20.0)

        t = threading.Thread(target=run_long)
        t.start()
        # Wait until the prompt has reached the (stub) process — its echo shows
        # up in the PTY relay — then interrupt the in-flight turn.
        _wait_until(lambda: b"LONGTURN" in self.session.recent_output(), timeout=5.0)
        self.session.interrupt()
        t.join(timeout=10.0)
        self.assertFalse(t.is_alive())

        long_turn = result["turn"]
        self.assertTrue(long_turn.was_interrupted)
        self.assertEqual(long_turn.stop_reason, "interrupted")

        # Session survives — a follow-up turn still completes on the same pid.
        r3 = self.session.send_turn("after interrupt", timeout=10.0)
        self.assertFalse(r3.was_interrupted)
        self.assertEqual(r3.stop_reason, "end_turn")
        self.assertEqual(self.session.pid, pid)


class TestReadinessTimeout(_StubSessionTest):
    extra_env = {"STUB_NO_JSONL": "1"}

    def setUp(self) -> None:
        super().setUp()
        # Rebuild the session with a short readiness window.
        self.session.close()
        self.session = ClaudeSession(
            self.cwd, binary=str(_STUB), readiness_timeout=1.0, debounce=0.2,
        )

    def test_readiness_timeout_raised(self) -> None:
        with self.assertRaises(ReadinessTimeout):
            self.session.open()


class TestQuota(_StubSessionTest):
    def test_quota_message_raises(self) -> None:
        self.session.open()
        with self.assertRaises(QuotaExceededError) as ctx:
            self.session.send_turn("trigger QUOTA path", timeout=10.0)
        self.assertIn("limit", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Integration test against the real claude CLI
# ---------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("claude"), "claude CLI not installed")
@unittest.skipUnless(
    os.environ.get("LOONY_CLAUDE_INTEGRATION") == "1",
    "set LOONY_CLAUDE_INTEGRATION=1 to run the real-claude integration test "
    "(spends Claude quota; needs a trusted cwd)",
)
class TestRealClaudeIntegration(unittest.TestCase):
    """End-to-end: two turns, interrupt a long turn, a third — one process.

    This is the persistent-PTY scaffolding the issue called for (replacing the
    throwaway ``claude_pty_probe2.py``).  It drives the *real* CLI, so it is
    opt-in: it spends Claude quota and needs the working directory to be
    trusted (we pre-trust the tmp dir in ``~/.claude.json`` to skip the
    interactive trust dialog).  It also requires a ``claude`` build that
    persists the interactive transcript under
    ``~/.claude/projects/<cwd-slug>/<session-id>.jsonl`` (as the prototype
    relied on); some builds only write the transcript in headless ``-p`` mode.
    """

    def test_persistent_session_four_events(self) -> None:
        cwd = Path(self.enterContext(_tmpdir()))
        self.enterContext(_trusted_dir(cwd))
        session = ClaudeSession(cwd, readiness_timeout=60.0)
        session.open()
        try:
            pid = session.pid
            session.send_turn("Reply with exactly: ONE", timeout=120.0)
            session.send_turn("Reply with exactly: TWO", timeout=120.0)

            result: dict[str, TurnResult] = {}

            def run_long() -> None:
                result["turn"] = session.send_turn(
                    "Count slowly from 1 to 100, one number per line.",
                    timeout=120.0,
                )

            t = threading.Thread(target=run_long)
            t.start()
            _wait_until(
                lambda: _count_entries(session.jsonl_path) >= 4, timeout=30.0,
            )
            session.interrupt()
            t.join(timeout=60.0)
            self.assertTrue(result["turn"].was_interrupted)

            session.send_turn("Reply with exactly: FOUR", timeout=120.0)

            # No respawn across all four turns.
            self.assertEqual(session.pid, pid)

            # Transcript holds the four user prompts in order with an interrupt.
            entries = _read_entries(session.jsonl_path)
            self.assertTrue(any(_is_interrupt(e) for e in entries))
            self.assertGreaterEqual(
                sum(1 for e in entries if _is_terminal_assistant(e)), 3,
            )
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmpdir():
    import tempfile

    class _Ctx:
        def __enter__(self):
            self.path = tempfile.mkdtemp()
            return self.path

        def __exit__(self, *exc):
            shutil.rmtree(self.path, ignore_errors=True)
            return False

    return _Ctx()


def _trusted_dir(cwd: Path):
    """Context manager: mark *cwd* trusted in ~/.claude.json, restore on exit.

    Without this the real CLI shows an interactive "trust this folder?" dialog
    and never writes the session transcript.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        config = Path.home() / ".claude.json"
        original = config.read_text() if config.exists() else None
        try:
            data = json.loads(original) if original else {}
        except json.JSONDecodeError:
            data = {}
        data.setdefault("projects", {})[str(cwd)] = {"hasTrustDialogAccepted": True}
        config.write_text(json.dumps(data))
        try:
            yield
        finally:
            if original is None:
                config.unlink(missing_ok=True)
            else:
                config.write_text(original)

    return _ctx()


def _read_entries(path: Path) -> list[dict]:
    entries: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _count_entries(path: Path) -> int:
    if not path.exists():
        return 0
    return len(_read_entries(path))


def _wait_until(predicate, *, timeout: float) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
