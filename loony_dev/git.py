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

    def has_commits(self) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.work_dir, capture_output=True, text=True,
        )
        return result.returncode == 0

    def get_default_branch(self, remote: str = "origin") -> str:
        result = subprocess.run(
            ["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
            cwd=self.work_dir, capture_output=True, text=True,
        )
        if result.returncode == 0:
            # refs/remotes/origin/main -> main
            return result.stdout.strip().split("/")[-1]
        logger.warning(
            "Could not resolve default branch for remote '%s'; falling back to 'main'.", remote
        )
        return "main"

    def ensure_main_up_to_date(self) -> None:
        """Checkout default branch and pull latest."""
        if not self.has_commits():
            logger.info(
                "Repository at %s has no commits; skipping checkout. "
                "Agent will handle the empty repo.",
                self.work_dir,
            )
            return
        branch = self.get_default_branch()
        self._run("checkout", branch)
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
