"""Tests for quota / rate-limit detection, parsing, and disabling logic."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    """``_is_quota_error`` must fire only on a *genuine* usage-limit error.

    A naive substring match flagged any task whose text merely *discussed*
    quotas — e.g. issue #178, which is about replacing rate-limit handling — as a
    rate-limit hit, self-disabling the agent for 30 minutes (#178). Detection is
    now scoped to specific usage-limit phrasing.
    """

    # -- genuine usage-limit messages (must fire) --------------------------

    def test_hit_your_limit(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error(
            "You've hit your limit · resets 7:30pm (Asia/Calcutta)"))

    def test_usage_limit_reached(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error("usage limit reached, try again later"))

    def test_claude_usage_limit_with_reset_time(self) -> None:
        self.assertTrue(ClaudeQuotaMixin._is_quota_error(
            "Claude usage limit reached. Your limit will reset at 2pm (America/New_York)"))

    def test_limit_phrase_with_reset_time(self) -> None:
        # "limit" + a parseable reset time is a genuine signal even without an
        # exact canned phrase.
        self.assertTrue(ClaudeQuotaMixin._is_quota_error(
            "Your limit will reset at 9am (Europe/London)"))

    # -- topical content that merely DISCUSSES limits (must NOT fire, #178) -

    def test_178_style_prose_is_not_quota(self) -> None:
        """The exact false-positive class from #178: prose about rate limits."""
        text = (
            "This issue replaces the JSONL polling that detects a rate limit / "
            "quota error. We tail the transcript for a 429 or resource_exhausted "
            "status and parse 'too many requests'. The quota patterns must be "
            "tightened so topical content is not misread as a usage-limit hit."
        )
        self.assertFalse(ClaudeQuotaMixin._is_quota_error(text))

    def test_bare_rate_limit_phrase_is_not_quota(self) -> None:
        self.assertFalse(ClaudeQuotaMixin._is_quota_error("Error: rate limit exceeded"))

    def test_bare_429_is_not_quota(self) -> None:
        self.assertFalse(ClaudeQuotaMixin._is_quota_error("HTTP 429 Too Many Requests"))

    def test_bare_resource_exhausted_is_not_quota(self) -> None:
        self.assertFalse(ClaudeQuotaMixin._is_quota_error("resource_exhausted"))

    def test_word_quota_alone_is_not_quota(self) -> None:
        self.assertFalse(ClaudeQuotaMixin._is_quota_error(
            "Let's add a quota config so the limit is configurable."))

    def test_normal_output(self) -> None:
        self.assertFalse(ClaudeQuotaMixin._is_quota_error("Here is the code you requested"))


class TestParseResetTime(unittest.TestCase):
    """ClaudeQuotaMixin._parse_reset_time should handle multiple message formats."""

    def test_resets_without_at(self) -> None:
        """The format from the bug report: 'resets 7:30pm (Asia/Calcutta)'.

        Asia/Calcutta is a deprecated IANA name; the alias map resolves it
        to Asia/Kolkata so it works even on hosts without the legacy entry.
        """
        msg = "You've hit your limit · resets 7:30pm (Asia/Calcutta)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 19)
        self.assertEqual(result.minute, 30)

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

    def test_deprecated_tz_asia_calcutta(self) -> None:
        """Deprecated 'Asia/Calcutta' alias must be resolved to Asia/Kolkata."""
        msg = "You've hit your limit · resets 1:30pm (Asia/Calcutta)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 13)
        self.assertEqual(result.minute, 30)

    def test_deprecated_tz_us_eastern(self) -> None:
        """Deprecated 'US/Eastern' alias must be resolved to America/New_York."""
        msg = "resets 3pm (US/Eastern)"
        result = ClaudeQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 15)
        self.assertEqual(result.minute, 0)

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
        # Fallback should be roughly quota_fallback_seconds (default 1800s) from now
        delta = (agent._disabled_until - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(delta, 200)
        self.assertLess(delta, 1800 + 10)

    def test_fallback_duration_default_is_at_least_30_minutes(self) -> None:
        """Default quota_fallback_seconds must be >= 1800 (30 minutes)."""
        from loony_dev import config
        fallback = int(config.settings.get("quota_fallback_seconds", 30 * 60))
        self.assertGreaterEqual(fallback, 1800)

    def test_handle_quota_error_fallback_logs_raw_output(self) -> None:
        """When parse fails the warning log must include the raw output."""
        agent = _DummyClaudeAgent()
        unparseable = "some completely unrecognised quota message xyz"
        with patch("loony_dev.agents.claude_quota.logger") as mock_logger:
            agent._handle_quota_error(unparseable)
        mock_logger.warning.assert_called_once()
        call_args = str(mock_logger.warning.call_args)
        self.assertIn(unparseable, call_args)

    def test_plain_agent_never_disabled(self) -> None:
        """Agent without the mixin is never disabled."""
        agent = _DummyPlainAgent()
        self.assertTrue(agent.can_handle(None))  # type: ignore[arg-type]


class TestQuotaWorkerGracefulHandling(unittest.TestCase):
    """Quota errors must not raise or exit the worker; they return TaskResult(success=False).

    ``execute`` now drives a persistent :class:`ClaudeSession`, which surfaces a
    quota condition as :class:`QuotaExceededError` from ``send_turn`` (rather
    than a non-zero subprocess exit), so these tests inject the error there.
    """

    def _quota_session(self, message: str) -> MagicMock:
        from loony_dev.agents.claude_session import QuotaExceededError

        session = MagicMock()
        session.send_turn.side_effect = QuotaExceededError(message)
        return session

    def test_quota_error_returns_task_result_not_raises(self) -> None:
        """execute() with a quota-error turn must return TaskResult without raising."""
        from loony_dev.agents.coding import CodingAgent
        from loony_dev.models import TaskResult

        agent = CodingAgent()
        session = self._quota_session("You've hit your limit · resets 7:30pm (Asia/Calcutta)")

        mock_task = MagicMock()
        mock_task.describe.return_value = "implement test feature"
        mock_task.task_type = "implement_issue"

        with patch.object(agent, "_open_session", return_value=session), \
             patch.object(agent, "_close_session"), \
             patch.object(agent, "_get_head_commit", return_value="abc123"):
            result = agent.execute(mock_task, Path("/tmp"))

        self.assertIsInstance(result, TaskResult)
        self.assertFalse(result.success)
        self.assertTrue(result.rate_limited)
        self.assertTrue(agent.is_disabled())

    def test_quota_error_is_disabled_not_crashed(self) -> None:
        """After a quota error the agent must be disabled, not in an error state."""
        from loony_dev.agents.coding import CodingAgent

        agent = CodingAgent()
        session = self._quota_session("rate limit exceeded")

        mock_task = MagicMock()
        mock_task.describe.return_value = "implement test feature"

        with patch.object(agent, "_open_session", return_value=session), \
             patch.object(agent, "_close_session"), \
             patch.object(agent, "_get_head_commit", return_value="deadbeef"):
            agent.execute(mock_task, Path("/tmp"))

        self.assertTrue(agent.is_disabled())
        self.assertFalse(agent.can_handle(mock_task))


if __name__ == "__main__":
    unittest.main()
