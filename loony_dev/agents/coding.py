from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.agents.claude_quota import ClaudeQuotaMixin
from loony_dev.agents.claude_session import (
    ClaudeSession,
    ClaudeSessionError,
    QuotaExceededError,
    TurnResult,
)
from loony_dev.models import GitError, HookFailureError, TaskResult, truncate_for_log

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task
    from loony_dev.tasks.issue_task import IssueTask

logger = logging.getLogger(__name__)

# Per-turn timeout for ``ClaudeSession.send_turn``. A single phase (implement a
# whole issue, fix a review) can run for many minutes, so this is generous;
# override via the ``claude_turn_timeout_seconds`` key under ``[worker]``.
_DEFAULT_TURN_TIMEOUT = 30 * 60

# Startup grace for ``ClaudeSession.open`` — how long to let ``claude`` reach
# its interactive prompt before the first turn is sent. Interactive ``claude``
# does not write the session transcript until the first turn, so this is a
# fixed grace, not a wait-for-transcript timeout (see ``ClaudeSession`` /
# #178). Override via the ``claude_session_startup_timeout_seconds`` key under
# ``[worker]``.
_DEFAULT_STARTUP_TIMEOUT = 10


def _turn_timeout() -> float:
    """Return the per-turn timeout (seconds) for the persistent session."""
    from loony_dev import config

    # The key is documented under [worker], which lands in config.settings as
    # a nested dict — not a flat top-level key. Fall back to a top-level key
    # if present for forward compatibility.
    worker_cfg = config.settings.get("worker")
    if isinstance(worker_cfg, dict) and "claude_turn_timeout_seconds" in worker_cfg:
        raw = worker_cfg["claude_turn_timeout_seconds"]
    else:
        raw = config.settings.get("claude_turn_timeout_seconds", _DEFAULT_TURN_TIMEOUT)

    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_DEFAULT_TURN_TIMEOUT)


def _startup_timeout() -> float:
    """Return the session-startup readiness timeout (seconds)."""
    from loony_dev import config

    # Same lookup shape as ``_turn_timeout``: the key lives under [worker]
    # (nested dict), with a flat top-level fallback for forward compatibility.
    worker_cfg = config.settings.get("worker")
    if isinstance(worker_cfg, dict) and "claude_session_startup_timeout_seconds" in worker_cfg:
        raw = worker_cfg["claude_session_startup_timeout_seconds"]
    else:
        raw = config.settings.get(
            "claude_session_startup_timeout_seconds", _DEFAULT_STARTUP_TIMEOUT,
        )

    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_DEFAULT_STARTUP_TIMEOUT)


class CodingAgent(ClaudeQuotaMixin, Agent):
    """Invokes Claude Code CLI to implement code changes."""

    name = "coding"

    def __init__(self, repo: str = "") -> None:
        self.repo = repo

    def _can_handle_task(self, task: Task) -> bool:
        return task.task_type in ("implement_issue", "address_review", "resolve_conflicts", "fix_ci")

    def execute(self, task: Task, work_dir: Path) -> TaskResult:
        prompt = task.describe()
        session_id = self._session_id_for(task)
        logger.debug(
            "Opening ClaudeSession (cwd=%s, session=%s)", work_dir, session_id,
        )
        logger.debug("Claude prompt: %s", truncate_for_log(prompt))

        baseline_commit = self._get_head_commit(work_dir)

        try:
            session = self._open_session(work_dir, session_id)
        except ClaudeSessionError as exc:
            logger.warning("Failed to open ClaudeSession: %s", exc)
            return TaskResult(
                success=False,
                output=str(exc),
                summary=f"Failed to start Claude session: {exc}",
            )

        try:
            turn, failure = self._run_turn(
                session, prompt, timeout=_turn_timeout(), phase="execution",
            )
            if failure is not None:
                return failure
            output = turn.text
        finally:
            self._close_session(session)

        if output:
            logger.debug("Claude output: %s", truncate_for_log(output))

        summary = self._generate_summary(output, work_dir)
        has_changes = self._has_code_changes(baseline_commit, work_dir)
        return TaskResult(success=True, output=output, summary=summary, post_summary=has_changes)

    def execute_issue(self, task: IssueTask, work_dir: Path) -> TaskResult:
        """Multi-phase execution for IssueTask with optional Coderabbit verification.

        Phases:
          1. Implement — branch prepared; Claude writes code, does not commit.
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

        default_branch = GitRepo.detect_default_branch(work_dir)
        git = GitRepo(work_dir, default_branch=default_branch)
        session_id = self._session_id_for(task)
        branch = task.branch_name
        timeout = _turn_timeout()

        # The worktree handed to us by the orchestrator is already checked out
        # on `branch` (git worktree add -B), so no branch preparation is needed.
        logger.info("Issue #%d: working on branch '%s'", task.issue.number, branch)

        # If the branch has no prior commits, any deterministic session would
        # carry stale context from a different branch — open a fresh one
        # (session_id=None) so Claude implements from scratch.
        ahead = git.count_commits_ahead(default_branch, branch)
        if ahead == 0:
            logger.info(
                "Issue #%d: branch '%s' is empty — using fresh session",
                task.issue.number, branch,
            )
            session_id = None

        try:
            session = self._open_session(work_dir, session_id)
        except ClaudeSessionError as exc:
            logger.warning("Issue #%d: failed to open ClaudeSession: %s", task.issue.number, exc)
            return TaskResult(
                success=False,
                output=str(exc),
                summary=f"Failed to start Claude session: {exc}",
            )

        try:
            # ── Phase 1: Implement ──────────────────────────────────────────
            logger.info("Issue #%d: phase 1 — implementing", task.issue.number)
            implement_prompt = task.implement_prompt()
            logger.debug("Claude prompt: %s", truncate_for_log(implement_prompt))

            turn, failure = self._run_turn(
                session, implement_prompt, timeout=timeout, phase="implementation",
            )
            if failure is not None:
                return failure
            implement_output = turn.text
            if implement_output:
                logger.debug("Claude output: %s", truncate_for_log(implement_output))

            # ── Phase 2: Coderabbit verify+fix loop ─────────────────────────
            if cr_available:
                logger.info("Issue #%d: phase 2 — Coderabbit review (max %d)", task.issue.number, max_review)
                for attempt in range(max_review):
                    try:
                        cr_result = cr.run_review(work_dir)
                    except cr.CodeRabbitError as exc:
                        logger.warning("Coderabbit review failed: %s", exc)
                        break

                    if not cr_result.has_issues:
                        logger.info("Issue #%d: Coderabbit found no issues", task.issue.number)
                        break

                    if attempt == max_review - 1:
                        logger.warning(
                            "Issue #%d: Coderabbit review retries exhausted — continuing anyway",
                            task.issue.number,
                        )
                        break

                    logger.info(
                        "Issue #%d: Coderabbit found issues (attempt %d/%d), asking Claude to fix",
                        task.issue.number, attempt + 1, max_review,
                    )
                    _, failure = self._run_turn(
                        session,
                        task.fix_review_prompt(cr_result.agent_prompt),
                        timeout=timeout,
                        phase="review fix",
                    )
                    if failure is not None:
                        return failure
            else:
                logger.debug("Issue #%d: Coderabbit not available, skipping review phase", task.issue.number)

            # ── Phase 3: Commit message + commit+push loop ──────────────────
            logger.info("Issue #%d: phase 3 — generating commit message", task.issue.number)
            commit_msg = self._generate_commit_message(task, work_dir)
            self._save_commit_message(commit_msg, task)

            logger.info("Issue #%d: committing to branch '%s' (max %d attempts)", task.issue.number, branch, max_commits)
            hook_failed_output: str | None = None
            commit_succeeded = False

            _COMMIT_MSG_REJECTION = ("commit message", "conventional commit", "commit-msg")

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
                    if any(kw in hook_failed_output.lower() for kw in _COMMIT_MSG_REJECTION):
                        logger.info(
                            "Issue #%d: commit message rejected (attempt %d/%d), regenerating",
                            task.issue.number, attempt + 1, max_commits,
                        )
                        commit_msg = self._generate_commit_message(task, work_dir)
                        continue
                    logger.info(
                        "Issue #%d: hook failure (attempt %d/%d), asking Claude to fix",
                        task.issue.number, attempt + 1, max_commits,
                    )
                    _, failure = self._run_turn(
                        session,
                        task.fix_hook_prompt(hook_failed_output),
                        timeout=timeout,
                        phase="hook fix",
                    )
                    if failure is not None:
                        return failure
                    if cr_available:
                        try:
                            cr_result = cr.run_review(work_dir)
                            if cr_result.has_issues:
                                _, failure = self._run_turn(
                                    session,
                                    task.fix_review_prompt(cr_result.agent_prompt),
                                    timeout=timeout,
                                    phase="post-hook review fix",
                                )
                                if failure is not None:
                                    return failure
                        except cr.CodeRabbitError as exc:
                            logger.warning("Coderabbit review after hook fix failed: %s", exc)
                except GitError as exc:
                    logger.warning("Issue #%d: git error during commit/push: %s", task.issue.number, exc)
                    return TaskResult(
                        success=False,
                        output=str(exc),
                        summary=f"git error during commit/push: {exc}",
                    )

            if not commit_succeeded:
                task.mark_commit_exhausted(hook_failed_output)
                wip_msg = f"[WIP] {commit_msg}"
                logger.warning("Issue #%d: committing as WIP: %s", task.issue.number, wip_msg)
                try:
                    git.commit_and_push(wip_msg, branch, no_verify=True)
                except (GitError, HookFailureError) as exc:
                    logger.error("Issue #%d: failed to commit WIP: %s", task.issue.number, exc)
                    return TaskResult(
                        success=False,
                        output=str(exc),
                        summary=f"Failed to commit even as WIP: {exc}",
                    )

            # ── Phase 4: Create PR ──────────────────────────────────────────
            logger.info("Issue #%d: phase 4 — creating PR", task.issue.number)
            self._create_pr(task, branch, default_branch, work_dir)

            summary = self._generate_summary(implement_output, work_dir)
            return TaskResult(success=True, output=implement_output, summary=summary, post_summary=True)
        finally:
            self._close_session(session)

    # ------------------------------------------------------------------
    # Persistent session helpers
    # ------------------------------------------------------------------

    def _open_session(self, work_dir: Path, session_id: str | None) -> ClaudeSession:
        """Open and register a persistent ClaudeSession for the task.

        Registration lets :meth:`ClaudeQuotaMixin.terminate` close the session
        on an orchestrator shutdown signal. The caller owns the session and
        must release it via :meth:`_close_session`.
        """
        session = ClaudeSession(
            cwd=work_dir,
            session_id=session_id,
            startup_timeout_seconds=_startup_timeout(),
        )
        self._register_session(session)
        try:
            session.open()
        except Exception:
            self._unregister_session(session)
            try:
                session.close()
            except Exception:  # pragma: no cover - best-effort teardown
                logger.debug("Error closing ClaudeSession after open failure", exc_info=True)
            raise
        return session

    def _close_session(self, session: ClaudeSession) -> None:
        """Close *session* and drop it from the registry (best-effort)."""
        self._unregister_session(session)
        try:
            session.close()
        except Exception:  # pragma: no cover - best-effort teardown
            logger.debug("Error closing ClaudeSession", exc_info=True)

    def _run_turn(
        self,
        session: ClaudeSession,
        prompt: str,
        *,
        timeout: float,
        phase: str,
    ) -> tuple[TurnResult, None] | tuple[None, TaskResult]:
        """Send one turn; translate quota/session errors into a TaskResult.

        Returns ``(turn_result, None)`` on success, or ``(None, failure)``
        where *failure* is a ready-to-return :class:`TaskResult`. A quota error
        triggers :meth:`_handle_quota_error` (self-disabling the agent) exactly
        as the per-respawn path did, and is reported with ``rate_limited=True``.
        """
        try:
            turn = session.send_turn(prompt, timeout=timeout)
        except QuotaExceededError as exc:
            self._handle_quota_error(exc.output)
            return None, TaskResult(
                success=False,
                output=exc.output,
                summary=f"Rate limited during {phase}",
                rate_limited=True,
            )
        except ClaudeSessionError as exc:
            logger.warning("ClaudeSession error during %s: %s", phase, exc)
            return None, TaskResult(
                success=False,
                output=str(exc),
                summary=f"Agent error during {phase}: {exc}",
            )
        return turn, None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_pr(self, task: IssueTask, branch: str, default_branch: str, work_dir: Path) -> None:
        """Run gh pr create for the given branch."""
        wip_prefix = "[WIP] " if task.commit_exhausted else ""
        title = f"{wip_prefix}{task.issue.title} (#{task.issue.number})"
        body = self._generate_pr_body(task, branch, default_branch, work_dir)

        try:
            repo_name = subprocess.check_output(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                cwd=work_dir,
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception as exc:
            logger.warning("Could not determine repo name for PR creation: %s", exc)
            repo_name = None

        cmd = ["gh", "pr", "create", "--assignee", "@me", "--title", title, "--body", body, "--head", branch]
        if repo_name:
            cmd += ["-R", repo_name]

        try:
            result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, check=True)
            logger.info("Created PR: %s", result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            err_text = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
            if "a pull request already exists" in err_text:
                view_cmd = ["gh", "pr", "view", "--head", branch, "--json", "url", "-q", ".url"]
                if repo_name:
                    view_cmd += ["-R", repo_name]
                try:
                    existing_url = subprocess.check_output(
                        view_cmd, cwd=work_dir, stderr=subprocess.DEVNULL,
                    ).decode().strip()
                    logger.info("Issue #%d: PR already exists: %s", task.issue.number, existing_url)
                    return
                except Exception:
                    logger.info("Issue #%d: PR already exists for branch '%s'", task.issue.number, branch)
                    return
            logger.error(
                "Issue #%d: failed to create PR: %s",
                task.issue.number,
                (exc.stderr or "").strip(),
            )
            raise

    def _generate_pr_body(self, task: IssueTask, branch: str, default_branch: str, work_dir: Path) -> str:
        """Ask Claude to write a PR body using the issue description and diff."""
        try:
            diff = subprocess.check_output(
                ["git", "diff", f"{default_branch}...{branch}"],
                cwd=work_dir,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            diff = diff[:8000]
        except Exception as exc:
            logger.warning("Issue #%d: could not get diff for PR body: %s", task.issue.number, exc)
            return f"Closes #{task.issue.number}"

        stdout, _, returncode = self._invoke_claude(
            task.pr_body_prompt(diff), cwd=work_dir,
        )
        if returncode == 0 and stdout.strip():
            return stdout.strip()
        return f"Closes #{task.issue.number}"

    def _generate_commit_message(self, task: IssueTask, work_dir: Path) -> str:
        """Ask Claude (no session) to produce a conventional commit message."""
        stdout, _, returncode = self._invoke_claude(
            task.commit_message_prompt(), cwd=work_dir,
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

    def _get_head_commit(self, work_dir: Path) -> str | None:
        """Return the current HEAD commit hash, or None if git is unavailable."""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=work_dir,
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            return None

    def _has_code_changes(self, baseline_commit: str | None, work_dir: Path) -> bool:
        """Return True if commits were added or files are staged/modified since baseline."""
        try:
            # Check for uncommitted changes (staged or unstaged)
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=work_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True

            # Check for new commits since baseline
            if baseline_commit:
                current = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=work_dir,
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                return current != baseline_commit

        except Exception:
            pass

        return True  # safe default: post summary if we can't determine

    def _generate_summary(self, output: str, work_dir: Path) -> str:
        """Use Claude to generate a brief summary of the work done."""
        summary_prompt = f"Summarize what was done in 2-3 sentences based on this output:\n\n{output[-3000:]}"
        logger.debug("Running summary Claude call")
        logger.debug("Summary prompt: %s", truncate_for_log(summary_prompt))
        # Bypass session: injecting a meta-summarisation turn into the issue
        # session would corrupt the conversation history that planning and
        # future review rounds rely on.
        stdout, _, returncode = self._invoke_claude(
            summary_prompt, cwd=work_dir,
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
