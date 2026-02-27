from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import TaskResult


class Task(ABC):
    """A unit of work dispatched to an agent."""

    task_type: str
    priority: int  # Lower number = higher priority; used to order discovery across tick

    @staticmethod
    @abstractmethod
    def discover(github: GitHubClient) -> Iterator[Task]:
        """Yield tasks of this type discovered from GitHub. Called each tick.

        Implementations should yield lazily so the orchestrator can stop
        iterating as soon as a can-perform task is found.
        """
        ...

    @abstractmethod
    def describe(self) -> str:
        """Human/agent-readable description of work to do."""
        ...

    @abstractmethod
    def on_start(self, github: GitHubClient) -> None:
        """Called before agent execution. Update GitHub state (labels etc)."""
        ...

    @abstractmethod
    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        """Called after successful completion. Update GitHub state."""
        ...

    @abstractmethod
    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        """Called on failure. Update GitHub state."""
        ...
