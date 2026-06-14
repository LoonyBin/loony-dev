"""Branch model with Active Record pattern."""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github.check_run import CheckRun
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


class Branch:
    """A GitHub repository branch."""

    def __init__(self, name: str, *, repo: Repo) -> None:
        self.name = name
        self._repo = repo
        self._sha: str | None = None

    @property
    def sha(self) -> str:
        """Return the HEAD commit SHA, fetched once per Branch instance."""
        if self._sha is None:
            try:
                output = self._repo.client.gh(
                    "api", f"repos/{self._repo.name}/branches/{self.name}",
                    "-q", ".commit.sha",
                )
                self._sha = output.strip()
            except subprocess.CalledProcessError:
                logger.warning("Failed to get HEAD SHA for branch %r", self.name)
                self._sha = ""
        return self._sha

    @property
    def check_runs(self) -> list[CheckRun]:
        """Return all check runs for this branch's HEAD SHA (tick-cached)."""
        from loony_dev.github.check_run import CheckRun

        sha = self.sha
        if not sha:
            return []
        return CheckRun.list_all(sha, repo=self._repo)

    def __repr__(self) -> str:
        return f"Branch({self.name!r})"
