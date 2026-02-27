from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import TaskResult
    from loony_dev.tasks.base import Task


class Agent(ABC):
    """Base class for all agents. Different agents use different tools."""

    name: str

    @abstractmethod
    def discover_tasks(self, github: GitHubClient) -> list[Task]:
        """Return tasks this agent wants to work on, in priority order."""
        ...

    @abstractmethod
    def execute(self, task: Task) -> TaskResult:
        """Execute a task. Blocking. Returns result."""
        ...

    @abstractmethod
    def can_handle(self, task: Task) -> bool:
        """Whether this agent can handle the given task type."""
        ...
