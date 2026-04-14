"""Branch model with Active Record pattern."""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    def check_runs(self) -> list[dict]:
        """Return check runs for this branch's HEAD SHA (tick-cached).

        Each entry has ``name``, ``status``, and ``conclusion`` fields.
        Returns an empty list when the SHA cannot be determined.
        """
        sha = self.sha
        if not sha:
            return []

        cache_key = f"branch_checks:{sha}"
        cached = self._repo._tick_cache.get(cache_key)
        if cached is not None:
            logger.debug("Branch.check_runs(%r) tick-cache hit", sha)
            return cached

        try:
            data = self._repo.client.gh_api(f"commits/{sha}/check-runs")
            runs = []
            if isinstance(data, dict):
                for run in data.get("check_runs", []):
                    runs.append({
                        "name": run.get("name", ""),
                        "status": run.get("status", ""),
                        "conclusion": run.get("conclusion") or "",
                    })
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch check runs for SHA %r", sha)
            runs = []

        self._repo._tick_cache[cache_key] = runs
        logger.debug("Branch.check_runs(%r) returned %d run(s)", sha, len(runs))
        return runs

    def __repr__(self) -> str:
        return f"Branch({self.name!r})"
