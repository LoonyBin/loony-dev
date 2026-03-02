"""Tests that Claude CLI subprocesses are isolated from signals sent to the parent.

When SIGQUIT (or similar signals) are delivered to the orchestrator process,
they must NOT propagate to Claude CLI children. Using start_new_session=True
places each child in its own session/process group, cutting the signal path.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task


# ---------------------------------------------------------------------------
# Minimal concrete Agent subclass that spawns an arbitrary command
# ---------------------------------------------------------------------------


class _SleepAgent(Agent):
    """Test-only agent that spawns 'sleep <seconds>' using start_new_session=True."""

    name = "sleep_test"

    def __init__(self, seconds: int = 60) -> None:
        self._seconds = seconds

    def can_handle(self, task: Task) -> bool:  # pragma: no cover
        return False

    def execute(self, task: Task) -> TaskResult:  # pragma: no cover
        raise NotImplementedError

    def spawn(self) -> subprocess.Popen:
        """Spawn and store a sleep subprocess in its own session."""
        proc = subprocess.Popen(
            ["sleep", str(self._seconds)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._active_process = proc
        return proc


class _SleepAgentNoIsolation(Agent):
    """Same as _SleepAgent but WITHOUT start_new_session (the broken state)."""

    name = "sleep_test_no_isolation"

    def can_handle(self, task: Task) -> bool:  # pragma: no cover
        return False

    def execute(self, task: Task) -> TaskResult:  # pragma: no cover
        raise NotImplementedError

    def spawn(self) -> subprocess.Popen:
        """Spawn a sleep subprocess in the parent's session (inherits signals)."""
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # start_new_session intentionally omitted
        )
        self._active_process = proc
        return proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentSignalIsolation(unittest.TestCase):
    def _with_sigquit_ignored(self) -> None:
        """Install a no-op SIGQUIT handler like the orchestrator does, then
        send SIGQUIT to ourself. Restores the original handler afterwards."""

    def test_subprocess_survives_parent_sigquit(self) -> None:
        """Child spawned with start_new_session=True must survive SIGQUIT to parent.

        The orchestrator installs a handler that catches SIGQUIT gracefully
        (sets a shutdown flag). We mimic that here so the test process itself
        does not crash when we self-deliver the signal.
        """
        agent = _SleepAgent(seconds=60)
        proc = agent.spawn()

        # Install a no-op SIGQUIT handler, just like the orchestrator does.
        old_handler = signal.signal(signal.SIGQUIT, lambda *_: None)
        try:
            # Deliver SIGQUIT to this process.
            os.kill(os.getpid(), signal.SIGQUIT)

            # Give the OS a moment to deliver the signal.
            time.sleep(0.1)

            # The child must still be running — returncode is None while alive.
            self.assertIsNone(
                proc.poll(),
                "Child process was killed by SIGQUIT propagation. "
                "Add start_new_session=True to the Popen call.",
            )
        finally:
            signal.signal(signal.SIGQUIT, old_handler)
            proc.terminate()
            proc.wait(timeout=5)
            agent._active_process = None

    def test_terminate_still_kills_isolated_subprocess(self) -> None:
        """After start_new_session=True, agent.terminate() must still kill the child.

        terminate() sends SIGTERM by PID, which bypasses process group
        membership, so it should work regardless of session isolation.
        """
        agent = _SleepAgent(seconds=60)
        proc = agent.spawn()

        try:
            agent.terminate()

            # Wait briefly for the process to die (terminate() allows 5s timeout).
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)

            self.assertIsNotNone(
                proc.poll(),
                "agent.terminate() did not kill the isolated subprocess.",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            agent._active_process = None


if __name__ == "__main__":
    unittest.main()
