from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from loony_dev.models import GitError, HookFailureError

logger = logging.getLogger(__name__)

_HOOK_KEYWORDS = ("pre-commit", "pre-push", "commit-msg", "hook failed", "hook exited", "hook script")


class GitRepo:
    def __init__(self, work_dir: Path, default_branch: str = "main") -> None:
        self.work_dir = work_dir
        self.default_branch = default_branch

    @staticmethod
    def detect_default_branch(work_dir: Path) -> str:
        """Query the actual default branch from the remote HEAD ref."""
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=work_dir, capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split("/")[-1]
        except Exception:
            pass
        return "main"

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
        """Checkout the default branch and pull latest."""
        if not self.has_commits():
            # Fetch so we can see whether the upstream already has commits.
            self._run("fetch", "origin")
            branch = self.get_default_branch()
            remote_ref = f"origin/{branch}"
            remote_has_commits = subprocess.run(
                ["git", "rev-parse", "--verify", remote_ref],
                cwd=self.work_dir, capture_output=True, text=True,
            ).returncode == 0
            if not remote_has_commits:
                logger.info(
                    "Repository at %s has no commits; skipping checkout. "
                    "Agent will handle the empty repo.",
                    self.work_dir,
                )
                return
            # Upstream has history – create a local branch tracking it.
            self._run("checkout", "-b", branch, "--track", remote_ref)
            return
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

    def reset_branch_to_upstream(self, branch: str) -> None:
        """Fetch and hard-reset a branch to match its upstream state, then clean untracked files."""
        if not branch.strip():
            raise ValueError("branch must be non-empty")
        self._run("fetch", "origin", branch)
        self._run("checkout", "-B", branch, f"origin/{branch}")
        self._run("clean", "-fd")

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

    def commit_and_push(self, message: str, branch: str) -> None:
        """Stage all changes, commit with message, and push to branch.

        Raises HookFailureError when a pre-commit or pre-push hook rejects the
        operation so callers can retry after fixing the offending code.
        Raises GitError for all other non-zero exits.
        """
        self._run("add", "-A")

        commit_proc = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
        )
        if commit_proc.returncode != 0:
            output = f"{commit_proc.stdout}\n{commit_proc.stderr}".strip()
            logger.debug("git commit failed: %s", output)
            if any(kw in output.lower() for kw in _HOOK_KEYWORDS):
                raise HookFailureError(output)
            raise GitError(output)

        push_proc = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
        )
        if push_proc.returncode != 0:
            output = f"{push_proc.stdout}\n{push_proc.stderr}".strip()
            logger.debug("git push failed: %s", output)
            if any(kw in output.lower() for kw in _HOOK_KEYWORDS):
                # Undo the local commit so retries don't accumulate failed commits.
                subprocess.run(
                    ["git", "reset", "--soft", "HEAD~1"],
                    cwd=self.work_dir, capture_output=True,
                )
                raise HookFailureError(output)
            raise GitError(output)

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
