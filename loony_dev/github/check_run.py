"""CheckRun model for GitHub CI check results."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loony_dev.github.client import gh_setting
from loony_dev.github.repo import CheckRunsCacheEntry

if TYPE_CHECKING:
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


@dataclass
class CheckRun:
    """A GitHub check run result."""

    name: str
    status: str        # "completed" | "in_progress" | "queued"
    conclusion: str    # "failure" | "success" | "cancelled" | "timed_out" | ...
    details_url: str   # Link to the CI run log

    @classmethod
    def list_failing(cls, head_sha: str, *, repo: Repo) -> list[CheckRun]:
        """Return completed failing check runs for the given commit SHA.

        Filters for status == "completed" and conclusion in ("failure", "timed_out").
        Excludes checks whose name is in ``repo.skip_ci_checks``.

        Results for SHAs where all checks have completed are cached for
        ``check_runs_cache_ttl`` seconds across ticks.
        """
        now = time.monotonic()
        entry = repo._check_runs_cache.get(head_sha)
        if entry is not None and entry.all_completed and now - entry.cached_at < gh_setting("check_runs_cache_ttl"):
            logger.debug("CheckRun.list_failing(%r) cache hit (%d failing)", head_sha, len(entry.failing_runs))
            return entry.failing_runs

        try:
            data = repo.client.gh_api(f"commits/{head_sha}/check-runs")
            if not isinstance(data, dict):
                return []
            all_runs = data.get("check_runs", [])
            all_completed = all(r.get("status") == "completed" for r in all_runs)
            runs = []
            for run in all_runs:
                name = run.get("name", "")
                if name in repo.skip_ci_checks:
                    continue
                status = run.get("status", "")
                conclusion = run.get("conclusion") or ""
                if status == "completed" and conclusion in ("failure", "timed_out"):
                    runs.append(CheckRun(
                        name=name,
                        status=status,
                        conclusion=conclusion,
                        details_url=run.get("details_url", run.get("html_url", "")),
                    ))
            repo._check_runs_cache[head_sha] = CheckRunsCacheEntry(
                failing_runs=runs,
                all_completed=all_completed,
                cached_at=now,
            )
            logger.debug("CheckRun.list_failing(%r) returned %d failing run(s) (all_completed=%s)", head_sha, len(runs), all_completed)
            return runs
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch check runs for SHA %r", head_sha)
            return []
