from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult, truncate_for_log

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

QUOTA_PATTERNS = [
    "rate limit",
    "quota",
    "too many requests",
    "429",
    "resource_exhausted",
    "usage limit reached",
]

# Matches: "Your limit will reset at 2pm (America/New_York)"
#      or: "resets 10pm (America/New_York)"
_RESET_RE = re.compile(
    r"reset[s ].*?at\s+(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


class CodingAgent(Agent):
    """Invokes Claude Code CLI to implement code changes."""

    name = "coding"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def can_handle(self, task: Task) -> bool:
        return task.task_type in ("implement_issue", "address_review", "resolve_conflicts")

    QUOTA_FALLBACK_SECONDS = 5 * 60

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        cmd = ["claude", "-p", "--dangerously-skip-permissions", prompt]
        logger.debug("Running Claude CLI (cwd=%s): claude -p --dangerously-skip-permissions <prompt>", self.work_dir)
        logger.debug("Claude prompt: %s", truncate_for_log(prompt))

        while True:
            with subprocess.Popen(
                cmd,
                cwd=self.work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as proc:
                self._active_process = proc
                try:
                    stdout, stderr = proc.communicate()
                finally:
                    self._active_process = None

            returncode = proc.returncode
            logger.debug("Claude CLI exited with code %d", returncode)
            if stdout:
                logger.debug("Claude stdout: %s", truncate_for_log(stdout))
            if stderr:
                logger.debug("Claude stderr: %s", truncate_for_log(stderr))

            if returncode != 0:
                combined = f"{stdout}\n{stderr}"
                if self._is_quota_error(combined):
                    self._wait_for_quota_reset(combined)
                    continue
                return TaskResult(
                    success=False,
                    output=combined,
                    summary=f"Agent exited with code {returncode}",
                )

            summary = self._generate_summary(stdout)
            return TaskResult(success=True, output=stdout, summary=summary)

    def _wait_for_quota_reset(self, output: str) -> None:
        """Parse the reset time from Claude's output and sleep until then."""
        reset_at = self._parse_reset_time(output)
        if reset_at:
            now = datetime.now(timezone.utc)
            wait = (reset_at.astimezone(timezone.utc) - now).total_seconds() + 30
            wait = max(wait, 0)
            logger.warning("Quota exhausted. Sleeping %ds until %s.", wait, reset_at)
        else:
            wait = self.QUOTA_FALLBACK_SECONDS
            logger.warning("Quota exhausted, couldn't parse reset time. Sleeping %ds.", wait)
        time.sleep(wait)

    @staticmethod
    def _is_quota_error(output: str) -> bool:
        lower = output.lower()
        return any(p in lower for p in QUOTA_PATTERNS)

    @staticmethod
    def _parse_reset_time(output: str) -> datetime | None:
        """Parse reset time from Claude's quota message.

        Expected format: "Your limit will reset at 2pm (America/New_York)"
        """
        match = _RESET_RE.search(output)
        if not match:
            return None

        time_str = match.group(1).strip()
        tz_str = match.group(2).strip()

        try:
            tz = ZoneInfo(tz_str)
        except (KeyError, ValueError):
            return None

        now = datetime.now(tz)

        # Parse time — handles "2pm", "2:30pm", "10 am", etc.
        try:
            for fmt in ("%I%p", "%I:%M%p", "%I %p", "%I:%M %p"):
                try:
                    parsed = datetime.strptime(time_str.upper(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        except Exception:
            return None

        reset = now.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0,
        )

        # If the reset time is in the past, it means tomorrow
        if reset <= now:
            reset += timedelta(days=1)

        return reset

    def _generate_summary(self, output: str) -> str:
        """Use Claude to generate a brief summary of the work done."""
        summary_prompt = f"Summarize what was done in 2-3 sentences based on this output:\n\n{output[-3000:]}"
        logger.debug("Running summary Claude call")
        logger.debug("Summary prompt: %s", truncate_for_log(summary_prompt))
        with subprocess.Popen(
            ["claude", "-p", "--dangerously-skip-permissions", summary_prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as proc:
            self._active_process = proc
            try:
                stdout, _ = proc.communicate()
            finally:
                self._active_process = None
        logger.debug("Summary Claude call exited with code %d", proc.returncode)
        if stdout:
            logger.debug("Summary output: %s", truncate_for_log(stdout))
        if proc.returncode == 0 and stdout.strip():
            return stdout.strip()
        return "Changes were made successfully."
