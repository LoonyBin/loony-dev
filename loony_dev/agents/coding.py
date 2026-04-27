from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.agents.claude_quota import ClaudeQuotaMixin
from loony_dev.models import GitError, HookFailureError, TaskResult, truncate_for_log

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task
    from loony_dev.tasks.issue_task import IssueTask

logger = logging.getLogger(__name__)


class CodingAgent(ClaudeQuotaMixin, Agent):
    """Invokes Claude Code CLI to implement code changes."""

    name = "coding"

    def __init__(self, work_dir: Path, repo: str = "") -> None:
        self.work_dir = work_dir
        self.repo = repo

    def _can_handle_task(self, task: Task) -> bool:
        return task.task_type in ("implement_issue", "address_review", "resolve_conflicts", "fix_ci")

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        session_id = self._session_id_for(task)
        logger.debug(
            "Running Claude CLI (cwd=%s, session=%s): claude -p --dangerously-skip-permissions <prompt>",
            self.work_dir, session_id,
        )
        logger.debug("Claude prompt: %s", truncate_for_log(prompt))

        baseline_commit = self._get_head_commit()

        stdout, stderr, returncode = self._run_claude_cli(
            prompt, cwd=self.work_dir, session_id=session_id,
        )

        logger.debug("Claude CLI exited with code %d", returncode)
        if stdout:
            logger.debug("Claude stdout: %s", truncate_for_log(stdout))
        if stderr:
            logger.debug("Claude stderr: %s", truncate_for_log(stderr))

        if returncode != 0:
            combined = f"{stdout}\n{stderr}"
            is_quota = self._is_quota_error(combined)
            if is_quota:
                self._handle_quota_error(combined)
            return TaskResult(
                success=False,
                output=combined,
                summary=f"Agent exited with code {returncode}",
                rate_limited=is_quota,
            )

        summary = self._generate_summary(stdout)
        has_changes = self._has_code_changes(baseline_commit)
        return TaskResult(success=True, output=stdout, summary=summary, post_summary=has_changes)

    def execute_issue(self, task: IssueTask) -> TaskResult:
        """Multi-phase execution for IssueTask with optional Coderabbit verification.

        Phases:
          1. Implement — Claude writes code, creates branch, does not commit.
          2. Verify    — Coderabbit reviews; Claude fixes (up to max_review_retries).
          3. Commit    — Orchestrator commits+pushes; Claude fixes hooks (up to max_commit_retries).
          4. PR        — gh pr create with [WIP] prefix if retries exhausted.
        """
        from loony_dev import coderabbit as cr
        from loony_dev import config
        from loony_dev.git import GitRepo

        coderabbit_cfg = config.settings.get("coderabbit") or {}
        if not isinstance(coderabbit_cfg, dict):
            coderabbit_cfg = {}
        max_review = int(coderabbit_cfg.get("max_review_retries", 3))
        max_commits = int(coderabbit_cfg.get("max_commit_retries", 3))
        cr_available = cr.is_available(config.settings)

        default_branch = GitRepo.detect_default_branch(self.work_dir)
        git = GitRepo(self.work_dir, default_branch=default_branch)
        session_id = self._session_id_for(task)

        # ── Phase 1: Implement ──────────────────────────────────────────────
        logger.info("Issue #%d: phase 1 — implementing", task.issue.number)
        logger.debug("Claude prompt: %s", truncate_for_log(task.implement_prompt()))

        stdout, stderr, returncode = self._run_claude_cli(
            task.implement_prompt(), cwd=self.work_dir, session_id=session_id,
        )
        logger.debug("Claude CLI exited with code %d", returncode)
        if stdout:
            logger.debug("Claude stdout: %s", truncate_for_log(stdout))
        if stderr:
            logger.debug("Claude stderr: %s", truncate_for_log(stderr))

        if returncode != 0:
            combined = f"{stdout}\n{stderr}"
            is_quota = self._is_quota_error(combined)
            if is_quota:
                self._handle_quota_error(combined)
            return TaskResult(
                success=False,
                output=combined,
                summary=f"Agent exited with code {returncode}",
                rate_limited=is_quota,
            )

        # Determine the branch Claude created (fall back to creating one if needed).
        branch = git.current_branch()
        if branch == git.default_branch:
            logger.warning(
                "Issue #%d: Claude did not create a feature branch; creating one now",
                task.issue.number,
            )
            branch = f"fix/issue-{task.issue.number}"
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=self.work_dir,
                check=True,
                capture_output=True,
            )

        # ── Phase 2: Coderabbit verify+fix loop ────────────────────────────
        if cr_available:
            logger.info("Issue #%d: phase 2 — Coderabbit review (max %d)", task.issue.number, max_review)
            for attempt in range(max_review):
                try:
                    cr_result = cr.run_review(self.work_dir)
                except cr.CodeRabbitError as exc:
                    logger.warning("Coderabbit review failed: %s", exc)
                    break

                if not cr_result.has_issues:
                    logger.info("Issue #%d: Coderabbit found no issues", task.issue.number)
                    break

                if attempt == max_review - 1:
                    logger.warning(
                        "Issue #%d: Coderabbit review retries exhausted",
                        task.issue.number,
                    )
                    task.mark_review_exhausted()
                    break

                logger.info(
                    "Issue #%d: Coderabbit found issues (attempt %d/%d), asking Claude to fix",
                    task.issue.number, attempt + 1, max_review,
                )
                fix_stdout, fix_stderr, fix_rc = self._run_claude_cli(
                    task.fix_review_prompt(cr_result.agent_prompt),
                    cwd=self.work_dir,
                    session_id=session_id,
                )
                if fix_rc != 0:
                    combined = f"{fix_stdout}\n{fix_stderr}"
                    is_quota = self._is_quota_error(combined)
                    if is_quota:
                        self._handle_quota_error(combined)
                    return TaskResult(
                        success=False,
                        output=combined,
                        summary=f"Agent exited with code {fix_rc} during review fix",
                        rate_limited=is_quota,
                    )
        else:
            logger.debug("Issue #%d: Coderabbit not available, skipping review phase", task.issue.number)

        # ── Phase 3: Commit message + commit+push loop ──────────────────────
        logger.info("Issue #%d: phase 3 — generating commit message", task.issue.number)
        commit_msg = self._generate_commit_message(task)
        self._save_commit_message(commit_msg, task)

        logger.info("Issue #%d: committing to branch '%s' (max %d attempts)", task.issue.number, branch, max_commits)
        hook_failed_output: str | None = None
        commit_succeeded = False

        for attempt in range(max_commits):
            try:
                git.commit_and_push(commit_msg, branch)
                commit_succeeded = True
                break
            except HookFailureError as exc:
                hook_failed_output = exc.output
                if attempt == max_commits - 1:
                    logger.warning(
                        "Issue #%d: hook failures exhausted all %d commit retries",
                        task.issue.number, max_commits,
                    )
                    break
                logger.info(
                    "Issue #%d: hook failure (attempt %d/%d), asking Claude to fix",
                    task.issue.number, attempt + 1, max_commits,
                )
                hook_fix_stdout, hook_fix_stderr, hook_fix_rc = self._run_claude_cli(
                    task.fix_hook_prompt(hook_failed_output),
                    cwd=self.work_dir,
                    session_id=session_id,
                )
                if hook_fix_rc != 0:
                    combined = f"{hook_fix_stdout}\n{hook_fix_stderr}"
                    is_quota = self._is_quota_error(combined)
                    if is_quota:
                        self._handle_quota_error(combined)
                    return TaskResult(
                        success=False,
                        output=combined,
                        summary=f"Agent exited with code {hook_fix_rc} during hook fix",
                        rate_limited=is_quota,
                    )
                if cr_available:
                    try:
                        cr_result = cr.run_review(self.work_dir)
                        if cr_result.has_issues:
                            cr_fix_stdout, cr_fix_stderr, cr_fix_rc = self._run_claude_cli(
                                task.fix_review_prompt(cr_result.agent_prompt),
                                cwd=self.work_dir,
                                session_id=session_id,
                            )
                            if cr_fix_rc != 0:
                                combined = f"{cr_fix_stdout}\n{cr_fix_stderr}"
                                is_quota = self._is_quota_error(combined)
                                if is_quota:
                                    self._handle_quota_error(combined)
                                return TaskResult(
                                    success=False,
                                    output=combined,
                                    summary=f"Agent exited with code {cr_fix_rc} during post-hook review fix",
                                    rate_limited=is_quota,
                                )
                    except cr.CodeRabbitError as exc:
                        logger.warning("Coderabbit review after hook fix failed: %s", exc)
            except GitError as exc:
                logger.warning("Issue #%d: git error during commit/push: %s", task.issue.number, exc)
                return TaskResult(
                    success=False,
                    output=str(exc),
                    summary=f"git error during commit/push: {exc}",
                )

        if not commit_succeeded or task.review_exhausted:
            if not commit_succeeded:
                task.mark_commit_exhausted(hook_failed_output)
                wip_msg = f"[WIP] {commit_msg}"
                logger.warning("Issue #%d: committing as WIP: %s", task.issue.number, wip_msg)
                try:
                    git.commit_and_push(wip_msg, branch)
                except Exception as exc:
                    logger.error("Issue #%d: failed to commit WIP: %s", task.issue.number, exc)
                    return TaskResult(
                        success=False,
                        output=str(exc),
                        summary=f"Failed to commit even as WIP: {exc}",
                    )
            else:
                logger.warning(
                    "Issue #%d: review retries exhausted, PR will be marked as WIP",
                    task.issue.number,
                )

        # ── Phase 4: Create PR ──────────────────────────────────────────────
        logger.info("Issue #%d: phase 4 — creating PR", task.issue.number)
        self._create_pr(task, branch)

        summary = self._generate_summary(stdout)
        return TaskResult(success=True, output=stdout, summary=summary, post_summary=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_pr(self, task: IssueTask, branch: str) -> None:
        """Run gh pr create for the given branch."""
        wip_prefix = "[WIP] " if (task.commit_exhausted or task.review_exhausted) else ""
        title = f"{wip_prefix}{task.issue.title} (#{task.issue.number})"
        body = f"Closes #{task.issue.number}"

        try:
            repo_name = subprocess.check_output(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                cwd=self.work_dir,
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception as exc:
            logger.warning("Could not determine repo name for PR creation: %s", exc)
            repo_name = None

        cmd = ["gh", "pr", "create", "--assignee", "@me", "--title", title, "--body", body, "--head", branch]
        if repo_name:
            cmd += ["-R", repo_name]

        try:
            result = subprocess.run(cmd, cwd=self.work_dir, capture_output=True, text=True, check=True)
            logger.info("Created PR: %s", result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%d: failed to create PR: %s",
                task.issue.number,
                (exc.stderr or "").strip(),
            )
            raise

    def _generate_commit_message(self, task: IssueTask) -> str:
        """Ask Claude (no session) to produce a conventional commit message."""
        stdout, _, returncode = self._invoke_claude(
            task.commit_message_prompt(), cwd=self.work_dir,
        )
        if returncode == 0 and stdout.strip():
            return _parse_commit_message(stdout)
        return f"feat: implement issue #{task.issue.number}"

    def _save_commit_message(self, msg: str, task: IssueTask) -> None:
        """Write commit message to the log directory for reference."""
        from loony_dev import config

        log_file = config.settings.get("log_file")
        if not log_file:
            return
        log_dir = Path(log_file).parent
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            out_path = log_dir / f"issue_{task.issue.number}_commit_msg.txt"
            out_path.write_text(msg)
            logger.debug("Commit message saved to %s", out_path)
        except Exception as exc:
            logger.debug("Could not save commit message: %s", exc)

    def _get_head_commit(self) -> str | None:
        """Return the current HEAD commit hash, or None if git is unavailable."""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.work_dir,
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            return None

    def _has_code_changes(self, baseline_commit: str | None) -> bool:
        """Return True if commits were added or files are staged/modified since baseline."""
        try:
            # Check for uncommitted changes (staged or unstaged)
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.work_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True

            # Check for new commits since baseline
            if baseline_commit:
                current = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.work_dir,
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                return current != baseline_commit

        except Exception:
            pass

        return True  # safe default: post summary if we can't determine

    def _generate_summary(self, output: str) -> str:
        """Use Claude to generate a brief summary of the work done."""
        summary_prompt = f"Summarize what was done in 2-3 sentences based on this output:\n\n{output[-3000:]}"
        logger.debug("Running summary Claude call")
        logger.debug("Summary prompt: %s", truncate_for_log(summary_prompt))
        # Bypass session: injecting a meta-summarisation turn into the issue
        # session would corrupt the conversation history that planning and
        # future review rounds rely on.
        stdout, _, returncode = self._invoke_claude(
            summary_prompt, cwd=self.work_dir,
        )
        logger.debug("Summary Claude call exited with code %d", returncode)
        if stdout:
            logger.debug("Summary output: %s", truncate_for_log(stdout))
        if returncode == 0 and stdout.strip():
            return stdout.strip()
        return "Changes were made successfully."


def _parse_commit_message(raw: str) -> str:
    """Extract just the commit message line from Claude's raw output."""
    for line in raw.strip().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("```") and not stripped.startswith("#"):
            return stripped
    return raw.strip().splitlines()[0] if raw.strip() else "feat: implement changes"
