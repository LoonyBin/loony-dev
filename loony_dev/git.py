from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class GitRepo:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        logger.debug("Running: %s", " ".join(cmd))
        return subprocess.run(cmd, cwd=self.work_dir, capture_output=True, text=True, check=True)

    def ensure_main_up_to_date(self) -> None:
        """Checkout main and pull latest."""
        self._run("checkout", "main")
        self._run("pull", "--ff-only")

    def has_uncommitted_changes(self) -> bool:
        result = self._run("status", "--porcelain")
        return bool(result.stdout.strip())

    def force_commit_and_push(self, message: str) -> None:
        """Stage all changes, commit, and push current branch."""
        self._run("add", "-A")
        self._run("commit", "-m", message)
        # Push current branch
        result = self._run("rev-parse", "--abbrev-ref", "HEAD")
        branch = result.stdout.strip()
        self._run("push", "-u", "origin", branch)

    def checkout_branch(self, branch: str) -> None:
        """Checkout an existing remote-tracking branch."""
        self._run("checkout", branch)

    def push_branch(self, branch: str) -> None:
        """Push the current branch (force-with-lease to protect against races)."""
        self._run("push", "--force-with-lease", "-u", "origin", branch)

    def checkout_main(self) -> None:
        self._run("checkout", "main")

    def current_branch(self) -> str:
        result = self._run("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()
