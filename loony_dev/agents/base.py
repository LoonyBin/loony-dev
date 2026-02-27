from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.models import TaskResult
    from loony_dev.tasks.base import Task


class Agent(ABC):
    """Base class for all agents. Different agents use different tools."""

    name: str

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
