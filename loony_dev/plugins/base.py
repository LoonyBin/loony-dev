from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.agents.base import Agent
    from loony_dev.config import Settings
    from loony_dev.tasks.base import Task


class PluginConflictError(Exception):
    """Raised when two plugins register conflicting task types or agent names."""


class TaskPlugin(ABC):
    """Bundles one or more Task subclasses for registration with the orchestrator."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this plugin. Used for conflict detection."""
        ...

    @property
    @abstractmethod
    def task_classes(self) -> list[type[Task]]:
        """Return Task subclasses to register with the orchestrator.

        Each class must declare a ``task_type: str`` and ``priority: int``.
        The orchestrator sorts all registered classes by priority across plugins.
        """
        ...


class AgentPlugin(ABC):
    """Bundles one or more Agent instances for registration with the orchestrator."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this plugin. Used for conflict detection."""
        ...

    @abstractmethod
    def create_agents(self, work_dir: Path, settings: Settings) -> list[Agent]:
        """Instantiate and return Agent objects.

        ``settings`` provides access to the full resolved configuration so that
        plugins can read their own keys without coupling to environment variables
        directly.
        """
        ...
