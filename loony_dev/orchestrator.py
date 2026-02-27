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
    from loony_dev.models import Comment
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
        """Gather and prioritize tasks.

        Priority order:
          1. PR reviews
          2. Planning (ready-for-planning, no plan yet or new user feedback)
          3. Development (ready-for-development)
        """
        tasks: list[Task] = []

        # Priority 1: PR reviews
        for pr in self.github.get_prs_needing_review():
            tasks.append(PRReviewTask(pr))

        # Priority 2: Planning tasks
        for issue in self.github.get_planning_issues():
            comments = self.github.get_issue_comments(issue.number)
            existing_plan, new_comments = self._analyze_planning_comments(comments)
            if existing_plan is None or new_comments:
                tasks.append(PlanningTask(issue, existing_plan, new_comments))

        # Priority 3: Development tasks
        for issue in self.github.get_ready_issues():
            comments = self.github.get_issue_comments(issue.number)
            approved_plan, _ = self._analyze_planning_comments(comments)
            tasks.append(IssueTask(issue, plan=approved_plan))

        return tasks

    def _analyze_planning_comments(
        self, comments: list[Comment]
    ) -> tuple[str | None, list[Comment]]:
        """Return (existing_bot_plan, new_user_comments_since_last_bot_comment)."""
        bot_name = self.github.bot_name
        bot_last_idx = -1
        bot_last_plan: str | None = None

        for i, c in enumerate(comments):
            if c.author == bot_name:
                bot_last_idx = i
                bot_last_plan = c.body

        if bot_last_idx == -1:
            new_comments = [c for c in comments if c.author != bot_name]
        else:
            new_comments = [
                c for c in comments[bot_last_idx + 1:] if c.author != bot_name
            ]

        return bot_last_plan, new_comments

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
