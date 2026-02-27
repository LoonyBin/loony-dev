from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.agents.base import Agent
    from loony_dev.git import GitRepo
    from loony_dev.github import GitHubClient
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)


class NoAgentError(Exception):
    def __init__(self, task: Task) -> None:
        super().__init__(f"No agent can handle task type: {task.task_type}")
        self.task = task


class Orchestrator:
    def __init__(
        self,
        github: GitHubClient,
        git: GitRepo,
        agents: list[Agent],
        interval: int = 60,
    ) -> None:
        self.github = github
        self.git = git
        self.agents = agents
        self.interval = interval

    def run(self) -> None:
        """Main polling loop."""
        logger.info("Orchestrator started. Polling every %ds.", self.interval)
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Shutting down.")
                break
            except Exception:
                logger.exception("Error during tick")
            time.sleep(self.interval)

    def _tick(self) -> None:
        tasks = self.gather_tasks()
        if not tasks:
            logger.debug("No tasks found.")
            return

        task = tasks[0]
        logger.info("Dispatching task: %s", task.task_type)
        agent = self.find_agent(task)
        self.dispatch(agent, task)

    def gather_tasks(self) -> list[Task]:
        """Collect tasks from all registered agents in registration order."""
        tasks: list[Task] = []
        for agent in self.agents:
            tasks.extend(agent.discover_tasks(self.github))
        return tasks

    def find_agent(self, task: Task) -> Agent:
        for agent in self.agents:
            if agent.can_handle(task):
                return agent
        raise NoAgentError(task)

    def dispatch(self, agent: Agent, task: Task) -> None:
        task.on_start(self.github)
        self.git.ensure_main_up_to_date()
        try:
            result = agent.execute(task)
            self._cleanup()
            if result.success:
                task.on_complete(self.github, result)
            else:
                task.on_failure(self.github, RuntimeError(result.summary))
        except Exception as e:
            self._cleanup()
            task.on_failure(self.github, e)

    def _cleanup(self) -> None:
        """Ensure working directory is clean and on main."""
        try:
            if self.git.has_uncommitted_changes():
                logger.warning("Uncommitted changes detected after task. Force committing.")
                self.git.force_commit_and_push("chore: auto-commit uncommitted changes")
        except Exception:
            logger.exception("Failed to clean up uncommitted changes")
        try:
            self.git.checkout_main()
        except Exception:
            logger.exception("Failed to checkout main")
