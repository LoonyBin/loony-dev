from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from loony_dev.tasks.issue_task import IssueTask
from loony_dev.tasks.planning_task import PlanningTask
from loony_dev.tasks.pr_review_task import PRReviewTask

if TYPE_CHECKING:
    from loony_dev.agents.base import Agent
    from loony_dev.git import GitRepo
    from loony_dev.github import GitHubClient
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

# Task classes ordered by priority (lowest number = highest priority).
# The orchestrator iterates these in order, stopping as soon as it finds
# a task that some configured agent can handle.
TASK_CLASSES = sorted(
    [PRReviewTask, PlanningTask, IssueTask],
    key=lambda tc: tc.priority,
)


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
        result = self._find_work()
        if result is None:
            logger.debug("No tasks found.")
            return

        task, agent = result
        logger.info("Dispatching task: %s", task.task_type)
        self.dispatch(agent, task)

    def _find_work(self) -> tuple[Task, Agent] | None:
        """Iterate task classes by priority; return first (task, agent) pair found.

        Each task class's discover() is an iterator so discovery stops as soon
        as a handleable task is found — avoiding unnecessary GitHub API calls.
        """
        for task_class in TASK_CLASSES:
            for task in task_class.discover(self.github):
                for agent in self.agents:
                    if agent.can_handle(task):
                        return task, agent
        return None

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
