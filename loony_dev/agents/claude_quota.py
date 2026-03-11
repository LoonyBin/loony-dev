"""Claude CLI quota / rate-limit mixin."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loony_dev import config

logger = logging.getLogger(__name__)

_QUOTA_PATTERNS = [
    "rate limit",
    "quota",
    "too many requests",
    "429",
    "resource_exhausted",
    "usage limit reached",
    "hit your limit",
]

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
        lower = output.lower()
        return any(p in lower for p in _QUOTA_PATTERNS)

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

    def _handle_quota_error(self, output: str) -> None:
        """Disable this agent until the quota resets.

        Parses the reset time from *output* and sets ``_disabled_until``.
        Falls back to a fixed cooldown when the time cannot be parsed.
        """
        reset_at = self._parse_reset_time(output)
        if reset_at:
            # Add a 30-second buffer so we don't race the provider clock.
            self._disabled_until = reset_at.astimezone(timezone.utc) + timedelta(seconds=30)
            logger.warning(
                "Agent '%s' rate-limited. Disabled until %s.",
                self.name, self._disabled_until,
            )
        else:
            fallback = config.settings.QUOTA_FALLBACK_SECONDS
            self._disabled_until = (
                datetime.now(timezone.utc) + timedelta(seconds=fallback)
            )
            logger.warning(
                "Agent '%s' rate-limited (couldn't parse reset time). Disabled for %ds.",
                self.name, fallback,
            )
