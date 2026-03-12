"""Tests for quota / rate-limit detection, parsing, and disabling logic."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from loony_dev import config
from loony_dev.agents.base import Agent
from loony_dev.agents.claude_quota import ClaudeQuotaMixin


# -- Minimal concrete agent using the mixin for testing --------------------

class _DummyClaudeAgent(ClaudeQuotaMixin, Agent):
    name = "dummy_claude"

    def _can_handle_task(self, task):  # noqa: ANN001
        return True

    def execute(self, task):  # noqa: ANN001
        raise NotImplementedError


class _DummyPlainAgent(Agent):
    """Agent without the mixin — never disabled."""
    name = "dummy_plain"

    def can_handle(self, task):  # noqa: ANN001
        return True

    def execute(self, task):  # noqa: ANN001
        raise NotImplementedError


# -- Tests ----------------------------------------------------------------

class TestIsQuotaError(unittest.TestCase):
    """ClaudeQuotaMixin._is_quota_error should recognise various rate-limit messages."""

    def test_hit_your_limit(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error(
            "You've hit your limit · resets 7:30pm (Asia/Calcutta)"))

    def test_rate_limit(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error("Error: rate limit exceeded"))

    def test_429(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error("HTTP 429 Too Many Requests"))

    def test_usage_limit_reached(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error("usage limit reached, try again later"))

    def test_resource_exhausted(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error("resource_exhausted"))

    def test_normal_output(self) -> None:
        self.assertFalse(ClaudeQuotaMixin._is_quota_error("Here is the code you requested"))


class TestParseResetTime(unittest.TestCase):
    """ClaudeQuotaMixin._parse_reset_time should handle multiple message formats."""

    def test_resets_without_at(self) -> None:
        """The format from the bug report: 'resets 7:30pm (Asia/Calcutta)'."""
        msg = "You've hit your limit · resets 7:30pm (Asia/Calcutta)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 19)
        self.assertEqual(result.minute, 30)
        self.assertEqual(str(result.tzinfo), "Asia/Calcutta")

    def test_resets_with_at(self) -> None:
        """The original expected format: 'reset at 2pm (America/New_York)'."""
        msg = "Your limit will reset at 2pm (America/New_York)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 0)

    def test_resets_at_with_minutes(self) -> None:
        msg = "resets at 10:45pm (Europe/London)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 22)
        self.assertEqual(result.minute, 45)

    def test_no_match(self) -> None:
        self.assertIsNone(ClaudeQuotaMixin._parse_reset_time("some random error"))

    def test_invalid_timezone(self) -> None:
        msg = "resets 2pm (Fake/Timezone)"
        self.assertIsNone(ClaudeQuotaMixin._parse_reset_time(msg))

    def test_result_is_in_the_future(self) -> None:
        """Parsed reset time should always be in the future."""
        msg = "resets 12:00am (UTC)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        now = datetime.now(timezone.utc)
        self.assertGreater(result.astimezone(timezone.utc), now)


class TestDisabledUntil(unittest.TestCase):
    """An agent with ClaudeQuotaMixin should be disabled after a quota error."""

    def test_agent_disabled_after_quota_error(self) -> None:
        agent = _DummyClaudeAgent()
        self.assertFalse(agent.is_disabled())

        agent._handle_quota_error("You've hit your limit · resets 7:30pm (Asia/Calcutta)")

        self.assertTrue(agent.is_disabled())

    def test_can_handle_returns_false_while_disabled(self) -> None:
        agent = _DummyClaudeAgent()
        agent._disabled_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertFalse(agent.can_handle(None))  # type: ignore[arg-type]

    def test_can_handle_returns_true_after_cooldown_expires(self) -> None:
        agent = _DummyClaudeAgent()
        agent._disabled_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertTrue(agent.can_handle(None))  # type: ignore[arg-type]

    def test_handle_quota_error_fallback(self) -> None:
        """When the reset time can't be parsed, a fallback cooldown is used."""
        agent = _DummyClaudeAgent()
        agent._handle_quota_error("some unknown error")
        self.assertTrue(agent.is_disabled())
        # Fallback should be roughly QUOTA_FALLBACK_SECONDS from now
        delta = (agent._disabled_until - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(delta, 200)
        self.assertLess(delta, config.settings.CLAUDE.QUOTA_FALLBACK_SECONDS + 10)

    def test_plain_agent_never_disabled(self) -> None:
        """Agent without the mixin is never disabled."""
        agent = _DummyPlainAgent()
        self.assertTrue(agent.can_handle(None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
