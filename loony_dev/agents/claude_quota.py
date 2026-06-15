"""Claude CLI quota / rate-limit mixin."""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loony_dev import config
from loony_dev.agents.context_file import (
    CommandNotInstalledError,
    write_context_file,
)
from loony_dev.session import session_id_for

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

# Deprecated IANA timezone names that may not exist in minimal tzdata packages.
# Claude's API sometimes returns these; map them to their canonical successors.
_TZ_ALIASES: dict[str, str] = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}

# Phrases that genuinely indicate a Claude *usage-limit* error, not merely
# content that *discusses* quotas. The old broad substrings ("quota", "rate
# limit", "429", "resource_exhausted", "too many requests") matched normal prose
# and code — so any task whose text simply talks about rate limits (e.g. issue
# #178 itself) was misread as a rate-limit hit and the agent self-disabled for
# 30 minutes. These phrases are specific to Claude's actual usage-limit message.
_QUOTA_PATTERNS = [
    "usage limit reached",
    "hit your limit",
    "reached your usage limit",
    "approaching your usage limit",
    "claude usage limit",
    "usage limit will reset",
]

_SESSION_NOT_FOUND_PATTERNS = (
    "no session",
    "session not found",
    "could not find session",
    "invalid session",
    "does not exist",
    "no conversation found",
)

# Matches: "Your limit will reset at 2pm (America/New_York)"
#      or: "resets 7:30pm (Asia/Calcutta)"
_RESET_RE = re.compile(
    r"resets?\s+(?:.*?at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


class ClaudeQuotaMixin:
    """Mixin for agents that call the Claude CLI and may encounter quota errors.

    Provides detection, parsing, and self-disabling on rate-limit errors.
    Must be used alongside Agent (accesses ``name`` and overrides
    ``is_disabled``).
    """

    _disabled_until: datetime | None = None
    # Class-level lock guarding _disabled_until. A single agent instance is
    # shared across all concurrent tasks, so a quota disable in one worker is
    # visible to the others; the lock makes the check-then-clear / set sequences
    # race-free across threads.
    _quota_lock = threading.Lock()
    repo: str = ""  # Set by subclass __init__; used for session ID generation.

    # ------------------------------------------------------------------
    # Persistent session registry
    # ------------------------------------------------------------------
    # Agents that drive a long-lived ``ClaudeSession`` (see
    # :mod:`loony_dev.agents.claude_session`) register it here so that
    # ``terminate()`` — invoked from the orchestrator's signal handler — can
    # close the underlying ``claude`` process on shutdown, just as it does for
    # the throwaway ``-p`` subprocesses tracked by :class:`Agent`.

    def _ensure_session_registry(self) -> None:
        """Lazily create the per-instance session registry (thread-safe)."""
        self.__dict__.setdefault("_session_lock", threading.Lock())
        self.__dict__.setdefault("_active_sessions", set())

    def _register_session(self, session: object) -> None:
        """Record a freshly opened session so terminate() can reach it."""
        self._ensure_session_registry()
        with self._session_lock:
            self._active_sessions.add(session)

    def _unregister_session(self, session: object) -> None:
        """Drop a closed session from the registry."""
        self._ensure_session_registry()
        with self._session_lock:
            self._active_sessions.discard(session)

    def terminate(self) -> None:
        """Terminate active subprocesses *and* close any open sessions.

        Extends :meth:`Agent.terminate` so a shutdown signal also tears down the
        persistent ``ClaudeSession`` processes, not just the one-shot ``-p``
        subprocesses.
        """
        try:
            super().terminate()
        finally:
            self._ensure_session_registry()
            with self._session_lock:
                sessions = list(self._active_sessions)
            for session in sessions:
                try:
                    session.close()
                except Exception:  # pragma: no cover - best-effort shutdown
                    logger.debug("Error closing session on terminate", exc_info=True)

    def can_handle(self, task: Task) -> bool:
        """Check availability then delegate to subclass task-type check.

        Returns False while the agent is disabled, giving the
        orchestrator a chance to try the next agent in the queue.
        """
        if self.is_disabled():
            logger.debug("Agent '%s' is disabled — skipping.", self.name)
            return False
        return self._can_handle_task(task)

    def is_disabled(self) -> bool:
        """True while the Claude quota cooldown is active."""
        with self._quota_lock:
            if self._disabled_until is None:
                return False
            now = datetime.now(timezone.utc)
            if now < self._disabled_until:
                return True
            # Cooldown expired — re-enable.
            self._disabled_until = None
            return False

    @staticmethod
    def _is_quota_error(output: str) -> bool:
        """Return True only for a *genuine* Claude usage-limit error.

        A naive substring match against broad terms like "quota" or "rate limit"
        produced false positives on any task whose text merely *discusses*
        quotas (issue #178). We instead require a specific usage-limit phrase, or
        a usage-limit phrase paired with a parseable reset time. Topical prose
        and code that talk about rate limits no longer trip self-disable.
        """
        lower = output.lower()
        if any(p in lower for p in _QUOTA_PATTERNS):
            return True
        # A parseable reset time is only a quota signal when paired with the word
        # "limit" — Claude's real message always says e.g. "your limit will reset
        # at …". The reset time alone is too generic (could be ambient text).
        if "limit" in lower and _RESET_RE.search(output) is not None:
            return True
        return False

    @staticmethod
    def _parse_reset_time(output: str) -> datetime | None:
        """Parse reset time from Claude's quota message.

        Expected formats:
            "Your limit will reset at 2pm (America/New_York)"
            "resets 7:30pm (Asia/Calcutta)"
        """
        match = _RESET_RE.search(output)
        if not match:
            return None

        time_str = match.group(1).strip()
        tz_str = match.group(2).strip()

        canonical = _TZ_ALIASES.get(tz_str, tz_str)
        try:
            tz = ZoneInfo(canonical)
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

    def _handle_quota_error(self, output: str) -> None:
        """Disable this agent until the quota resets.

        Parses the reset time from *output* and sets ``_disabled_until``.
        Falls back to a fixed cooldown when the time cannot be parsed,
        and logs the raw output to aid diagnosis.
        """
        reset_at = self._parse_reset_time(output)
        if reset_at:
            # Add a 30-second buffer so we don't race the provider clock.
            with self._quota_lock:
                self._disabled_until = reset_at.astimezone(timezone.utc) + timedelta(seconds=30)
            logger.warning(
                "Agent '%s' rate-limited. Disabled until %s.",
                self.name, self._disabled_until,
            )
        else:
            fallback = int(config.settings.get("quota_fallback_seconds", 30 * 60))
            with self._quota_lock:
                self._disabled_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=fallback)
                )
            logger.warning(
                "Agent '%s' rate-limited (couldn't parse reset time). "
                "Disabled for %ds. Raw output (truncated): %.500s",
                self.name,
                fallback,
                output,
            )

    # ------------------------------------------------------------------
    # Shared Claude CLI runner with session continuity
    # ------------------------------------------------------------------

    def _session_id_for(self, task: Task) -> str | None:
        """Compute a deterministic session ID for *task*, or None."""
        key = task.session_key
        if not key or not self.repo:
            return None
        return session_id_for(self.repo, key)

    def _command_turn(
        self,
        work_dir: Path,
        command: str,
        payload: dict,
        *,
        task_key: str | None,
    ) -> str:
        """Write *payload* to a context file and return ``/<command> <path>``.

        The turn injected into the session is a short slash-command invocation;
        Claude Code expands ``$ARGUMENTS`` to the context-file path and the
        command body (under ``<work_dir>/.claude/commands/``) reads the JSON.

        Raises :class:`CommandNotInstalledError` if the command is not installed
        in *work_dir* — #165 installs the bundled commands into every worker
        checkout, so its absence is config drift, surfaced loudly rather than
        falling back to an inline prompt.
        """
        command_file = Path(work_dir) / ".claude" / "commands" / f"{command}.md"
        if not command_file.is_file():
            raise CommandNotInstalledError(
                f"slash command '/{command}' is not installed at {command_file} — "
                f"run `loony-dev setup` (commands are installed at worker startup, #165)",
            )
        path = write_context_file(command, payload, task_key=task_key or command)
        return f"/{command} {path}"

    def _run_claude_cli(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
    ) -> tuple[str, str, int]:
        """Run the Claude CLI with optional session continuity.

        When *session_id* is provided, attempts ``--resume`` first to
        continue an existing session.  If that fails because no matching
        session is found, retries with ``--session-id`` to create a new
        session with the given UUID.
        """
        if session_id:
            stdout, stderr, rc = self._invoke_claude(
                prompt, cwd=cwd, extra_flags=["--resume", session_id],
            )
            if rc == 0 or not self._is_session_not_found(f"{stdout}\n{stderr}"):
                return stdout, stderr, rc
            logger.debug("Session %s not found — creating new session", session_id)
            return self._invoke_claude(
                prompt, cwd=cwd, extra_flags=["--session-id", session_id],
            )
        return self._invoke_claude(prompt, cwd=cwd)

    def _invoke_claude(
        self,
        prompt: str,
        *,
        cwd: Path,
        extra_flags: list[str] | None = None,
    ) -> tuple[str, str, int]:
        """Spawn ``claude -p`` and return (stdout, stderr, returncode).

        Prompt is passed via stdin to avoid OS ARG_MAX limits on large inputs.
        """
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        if extra_flags:
            cmd.extend(extra_flags)

        with subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as proc:
            self._register_process(proc)
            try:
                stdout, stderr = proc.communicate(input=prompt)
            finally:
                self._unregister_process(proc)
        return stdout, stderr, proc.returncode

    @staticmethod
    def _is_session_not_found(output: str) -> bool:
        """Return True if *output* indicates a missing/invalid session."""
        lower = output.lower()
        return any(p in lower for p in _SESSION_NOT_FOUND_PATTERNS)
