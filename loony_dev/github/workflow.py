"""Workflow and WorkflowRun models with Active Record pattern."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


@dataclass
class WorkflowRun:
    """A single GitHub Actions workflow run."""

    id: int
    name: str
    status: str
    conclusion: str | None
    completed_at: datetime | None


class WorkflowRunCollection(list):
    """A list of WorkflowRun instances supporting chainable filters."""

    def where(
        self,
        *,
        conclusion: str | None = None,
        timestamp_is_gt: datetime | None = None,
    ) -> WorkflowRunCollection:
        """Return a new collection containing only runs that match all criteria."""
        result = self
        if conclusion is not None:
            result = [r for r in result if r.conclusion == conclusion]
        if timestamp_is_gt is not None:
            result = [r for r in result if r.completed_at and r.completed_at > timestamp_is_gt]
        return WorkflowRunCollection(result)


class Workflow:
    """A GitHub Actions workflow."""

    def __init__(self, name: str, *, repo: Repo) -> None:
        self.name = name
        self._repo = repo

    @property
    def runs(self) -> WorkflowRunCollection:
        """Fetch recent runs for this workflow (up to 10 most recent successes)."""
        from loony_dev.github.repo import parse_datetime

        try:
            data = self._repo.client.gh_api(
                f"actions/workflows/{self.name}.yml/runs?status=success&per_page=10"
            )
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch workflow runs for %r", self.name)
            return WorkflowRunCollection()

        if not isinstance(data, dict):
            return WorkflowRunCollection()

        runs = [
            WorkflowRun(
                id=run.get("id", 0),
                name=run.get("name", ""),
                status=run.get("status", ""),
                conclusion=run.get("conclusion"),
                completed_at=parse_datetime(run.get("completed_at")),
            )
            for run in data.get("workflow_runs", [])
        ]
        logger.debug("Workflow(%r).runs returned %d run(s)", self.name, len(runs))
        return WorkflowRunCollection(runs)

    def __repr__(self) -> str:
        return f"Workflow({self.name!r})"
