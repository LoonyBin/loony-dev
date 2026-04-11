from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class GitRepo:
    def __init__(self, work_dir: Path, default_branch: str = "main") -> None:
        self.work_dir = work_dir
        self.default_branch = default_branch

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        logger.debug("Running: %s", " ".join(cmd))
        try:
            return subprocess.run(cmd, cwd=self.work_dir, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            logger.debug(
                "git command failed (exit %d): %s\nstdout: %s\nstderr: %s",
                exc.returncode,
                " ".join(cmd),
                (exc.stdout or "").strip(),
                (exc.stderr or "").strip(),
            )
            raise

    def ensure_main_up_to_date(self) -> None:
        """Checkout the default branch and pull latest."""
        self._run("checkout", self.default_branch)
        self._run("fetch", "origin", self.default_branch)
        try:
            self._run("pull", "--ff-only")
        except subprocess.CalledProcessError:
            logger.warning(
                "Fast-forward pull failed; resetting local %s to origin/%s",
                self.default_branch,
                self.default_branch,
            )
            self._run("reset", "--hard", f"origin/{self.default_branch}")

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
        self._run("checkout", self.default_branch)

    def current_branch(self) -> str:
        result = self._run("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()
