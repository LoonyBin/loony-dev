from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.models import TaskResult
    from loony_dev.tasks.base import Task


class Agent(ABC):
    """Base class for all agents. Different agents use different tools."""

    name: str
    _active_process: subprocess.Popen | None = None

    @abstractmethod
    def can_handle(self, task: Task) -> bool:
        """Whether this agent can handle the given task type.

        Multiple agents may be able to handle the same task type (e.g. a
        Claude coding agent and a Gemini coding agent). Only one will be
        configured with valid credentials in a given deployment, so the
        orchestrator checks can_handle on each agent in turn and uses the
        first affirmative response.
        """
        ...

    @abstractmethod
    def execute(self, task: Task) -> TaskResult:
        """Execute a task. Blocking. Returns result."""
        ...

    def terminate(self) -> None:
        """Terminate the currently active subprocess, if any."""
        proc = self._active_process
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass
