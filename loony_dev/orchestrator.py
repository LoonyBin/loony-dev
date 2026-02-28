from __future__ import annotations

import logging
import signal
import time
from typing import TYPE_CHECKING

from loony_dev.tasks.conflict_task import ConflictResolutionTask
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
    [ConflictResolutionTask, PRReviewTask, PlanningTask, IssueTask],
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
        self._shutdown_requested: bool = False
        self._active_agent: Agent | None = None
        self._active_task: Task | None = None

    def run(self) -> None:
        """Main polling loop."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        logger.info("Orchestrator started. Polling every %ds.", self.interval)
        while not self._shutdown_requested:
            try:
                self._tick()
            except Exception:
                logger.exception("Error during tick")

            # Interruptible sleep: check shutdown flag each second.
            for _ in range(self.interval):
                if self._shutdown_requested:
                    break
                time.sleep(1)

        self._on_shutdown()

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info("Signal %s received, shutting down…", signum)
        self._shutdown_requested = True
        if self._active_agent is not None:
            self._active_agent.terminate()

    def _on_shutdown(self) -> None:
        """Clean up GitHub state for any task that was interrupted mid-flight."""
        logger.info("Shutting down.")
        task = self._active_task
        if task is not None:
            logger.info("Cleaning up GitHub state for interrupted task: %s", task.task_type)
            try:
                task.on_failure(self.github, RuntimeError("Interrupted by operator"))
            except Exception:
                logger.exception("Failed to clean up GitHub state on shutdown")

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
            logger.debug("Checking %s for work...", task_class.__name__)
            found_in_class = 0
            for task in task_class.discover(self.github):
                found_in_class += 1
                for agent in self.agents:
                    if agent.can_handle(task):
                        logger.debug(
                            "Selected agent '%s' for task '%s' (type=%s)",
                            agent.name, task, task.task_type,
                        )
                        return task, agent
                logger.debug(
                    "No agent can handle task type '%s' — skipping", task.task_type,
                )
            logger.debug("%s yielded %d candidate(s)", task_class.__name__, found_in_class)
        return None

    def dispatch(self, agent: Agent, task: Task) -> None:
        logger.debug("Task description:\n%s", task.describe())
        task.on_start(self.github)
        branch = self.git.current_branch()
        logger.debug("Current branch before sync: %s", branch)
        has_changes = self.git.has_uncommitted_changes()
        logger.debug("Uncommitted changes before sync: %s", has_changes)
        self.git.ensure_main_up_to_date()

        self._active_agent = agent
        self._active_task = task
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
        finally:
            self._active_agent = None
            self._active_task = None

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
