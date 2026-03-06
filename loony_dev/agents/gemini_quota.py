"""Gemini CLI quota / rate-limit mixin."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_QUOTA_PATTERNS = [
    "rate limit",
    "quota",
    "too many requests",
    "429",
    "resource_exhausted",
    "quota exceeded",
    "retry after",
]

# Matches: "Retry after 300s" or "retry after 60 seconds"
_RETRY_AFTER_RE = re.compile(
    r"retry\s+after\s+(\d+)\s*s(?:ec(?:ond)?s?)?",
    re.IGNORECASE,
)


class GeminiQuotaMixin:
    """Mixin for agents that call the Gemini CLI and may encounter quota errors.

    Provides detection, parsing, and self-disabling on rate-limit errors.
    Must be used alongside Agent (accesses ``name`` and overrides
    ``is_disabled``).
    """

    QUOTA_FALLBACK_SECONDS = 5 * 60
    _disabled_until: datetime | None = None

    def can_handle(self, task: object) -> bool:
        """Check availability then delegate to subclass task-type check.

        Returns False while the agent is disabled, giving the
        orchestrator a chance to try the next agent in the queue.
        """
        if self.is_disabled():
            logger.debug("Agent '%s' is disabled — skipping.", self.name)  # type: ignore[attr-defined]
            return False
        return self._can_handle_task(task)  # type: ignore[attr-defined]

    def is_disabled(self) -> bool:
        """True while the Gemini quota cooldown is active."""
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
        """Detect Gemini/Google API rate-limit signals."""
        lower = output.lower()
        return any(p in lower for p in _QUOTA_PATTERNS)

    @staticmethod
    def _parse_reset_time(output: str) -> datetime | None:
        """Parse reset time from Gemini error output.

        Gemini errors may include retry-after seconds: "Retry after Xs".
        Falls back to None if unparseable.
        """
        match = _RETRY_AFTER_RE.search(output)
        if not match:
            return None
        try:
            seconds = int(match.group(1))
            return datetime.now(timezone.utc) + timedelta(seconds=seconds)
        except (ValueError, OverflowError):
            return None

    def _handle_quota_error(self, output: str) -> None:
        """Disable this agent until the quota resets.

        Parses the reset time from *output* and sets ``_disabled_until``.
        Falls back to a fixed cooldown when the time cannot be parsed.
        """
        reset_at = self._parse_reset_time(output)
        if reset_at:
            # Add a 30-second buffer so we don't race the provider clock.
            self._disabled_until = reset_at + timedelta(seconds=30)
            logger.warning(
                "Agent '%s' rate-limited. Disabled until %s.",
                self.name,  # type: ignore[attr-defined]
                self._disabled_until,
            )
        else:
            self._disabled_until = (
                datetime.now(timezone.utc) + timedelta(seconds=self.QUOTA_FALLBACK_SECONDS)
            )
            logger.warning(
                "Agent '%s' rate-limited (couldn't parse reset time). Disabled for %ds.",
                self.name,  # type: ignore[attr-defined]
                self.QUOTA_FALLBACK_SECONDS,
            )
