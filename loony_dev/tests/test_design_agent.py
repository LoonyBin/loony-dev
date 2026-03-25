"""Tests for the design agent: quota handling, image URL extraction,
label transition logic, and signal isolation."""
from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone

from loony_dev.agents.base import Agent
from loony_dev.agents.design_agent import DesignAgent
from loony_dev.agents.gemini_quota import GeminiQuotaMixin
from loony_dev.models import Comment, Issue, TaskResult
from loony_dev.tasks.design_task import DESIGN_MARKER, DesignTask


# ---------------------------------------------------------------------------
# Minimal concrete agent using the mixin for testing
# ---------------------------------------------------------------------------


class _DummyGeminiAgent(GeminiQuotaMixin, Agent):
    name = "dummy_gemini"

    def _can_handle_task(self, task: object) -> bool:
        return True

    def execute(self, task: object) -> TaskResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Quota error detection
# ---------------------------------------------------------------------------


class TestGeminiIsQuotaError(unittest.TestCase):
    """GeminiQuotaMixin._is_quota_error should recognise various rate-limit messages."""

    def test_quota(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("quota exceeded for project"))

    def test_rate_limit(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("Error: rate limit exceeded"))

    def test_429(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("HTTP 429 Too Many Requests"))

    def test_resource_exhausted(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("RESOURCE_EXHAUSTED"))

    def test_resource_exhausted_lower(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("resource_exhausted"))

    def test_too_many_requests(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("too many requests, slow down"))

    def test_retry_after(self) -> None:
        self.assertTrue(GeminiQuotaMixin._is_quota_error("Retry after 300s"))

    def test_normal_output(self) -> None:
        self.assertFalse(GeminiQuotaMixin._is_quota_error("Here is the design specification"))


# ---------------------------------------------------------------------------
# Reset time parsing
# ---------------------------------------------------------------------------


class TestGeminiParseResetTime(unittest.TestCase):
    """GeminiQuotaMixin._parse_reset_time should handle Gemini error formats."""

    def test_retry_after_seconds(self) -> None:
        msg = "Retry after 300s"
        result = GeminiQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        # Should be roughly 300s from now
        now = datetime.now(timezone.utc)
        delta = (result - now).total_seconds()
        self.assertGreater(delta, 290)
        self.assertLess(delta, 310)

    def test_retry_after_seconds_full_word(self) -> None:
        msg = "retry after 60 seconds"
        result = GeminiQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        now = datetime.now(timezone.utc)
        delta = (result - now).total_seconds()
        self.assertGreater(delta, 50)
        self.assertLess(delta, 70)

    def test_no_match(self) -> None:
        self.assertIsNone(GeminiQuotaMixin._parse_reset_time("some random error"))

    def test_result_is_in_the_future(self) -> None:
        msg = "Retry after 1s"
        result = GeminiQuotaMixin._parse_reset_time(msg)
        self.assertIsNotNone(result)
        self.assertGreater(result, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Disabled/cooldown behaviour
# ---------------------------------------------------------------------------


class TestGeminiDisabledUntil(unittest.TestCase):
    def test_agent_disabled_after_quota_error(self) -> None:
        agent = _DummyGeminiAgent()
        self.assertFalse(agent.is_disabled())
        agent._handle_quota_error("Retry after 300s")
        self.assertTrue(agent.is_disabled())

    def test_can_handle_returns_false_while_disabled(self) -> None:
        agent = _DummyGeminiAgent()
        agent._disabled_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertFalse(agent.can_handle(None))

    def test_can_handle_returns_true_after_cooldown_expires(self) -> None:
        agent = _DummyGeminiAgent()
        agent._disabled_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertTrue(agent.can_handle(None))

    def test_handle_quota_error_fallback(self) -> None:
        """When the reset time can't be parsed, a fallback cooldown is used."""
        agent = _DummyGeminiAgent()
        agent._handle_quota_error("some unknown quota error")
        self.assertTrue(agent.is_disabled())
        delta = (agent._disabled_until - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(delta, 200)
        self.assertLess(delta, agent.QUOTA_FALLBACK_SECONDS + 10)


# ---------------------------------------------------------------------------
# Image URL extraction
# ---------------------------------------------------------------------------


class TestExtractImageUrls(unittest.TestCase):
    def test_markdown_image(self) -> None:
        body = "![mockup](https://example.com/mockup.png)"
        self.assertEqual(
            DesignTask._extract_image_urls(body),
            ["https://example.com/mockup.png"],
        )

    def test_html_img_double_quotes(self) -> None:
        body = '<img src="https://example.com/screen.jpg">'
        self.assertEqual(
            DesignTask._extract_image_urls(body),
            ["https://example.com/screen.jpg"],
        )

    def test_html_img_single_quotes(self) -> None:
        body = "<img src='https://example.com/screen.jpg'>"
        self.assertEqual(
            DesignTask._extract_image_urls(body),
            ["https://example.com/screen.jpg"],
        )

    def test_mixed_markdown_and_html(self) -> None:
        body = (
            "![a](https://example.com/a.png)\n"
            '<img src="https://example.com/b.png">'
        )
        urls = DesignTask._extract_image_urls(body)
        self.assertIn("https://example.com/a.png", urls)
        self.assertIn("https://example.com/b.png", urls)

    def test_deduplication(self) -> None:
        body = (
            "![a](https://example.com/same.png)\n"
            "![b](https://example.com/same.png)"
        )
        self.assertEqual(
            DesignTask._extract_image_urls(body),
            ["https://example.com/same.png"],
        )

    def test_no_images(self) -> None:
        self.assertEqual(DesignTask._extract_image_urls("No images here."), [])

    def test_non_http_ignored(self) -> None:
        body = "![local](file:///local/path.png)"
        self.assertEqual(DesignTask._extract_image_urls(body), [])


# ---------------------------------------------------------------------------
# Label transition logic
# ---------------------------------------------------------------------------


def _make_issue(number: int = 1) -> Issue:
    return Issue(number=number, title="Test issue", body="Body")


def _make_comment(author: str, body: str) -> Comment:
    return Comment(author=author, body=body, created_at="2024-01-01T00:00:00Z")


class FakeGitHub:
    """Minimal GitHub client stub for testing discover()."""

    def __init__(self, issues: list[tuple[Issue, list[str]]], comments: list[Comment]) -> None:
        self._issues = issues
        self._comments = comments
        self.bot_name = "bot"
        self.removed_labels: list[tuple[int, str]] = []

    def list_issues(self, label: str) -> list[tuple[Issue, list[str]]]:
        return self._issues

    def get_issue_comments(self, number: int) -> list[Comment]:
        return self._comments

    def remove_label(self, number: int, label: str) -> None:
        self.removed_labels.append((number, label))


class TestDesignTaskDiscover(unittest.TestCase):
    def test_yields_task_when_no_design_exists(self) -> None:
        issue = _make_issue()
        github = FakeGitHub([(issue, ["ready-for-design"])], [])
        tasks = list(DesignTask.discover(github))
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0].existing_design)
        self.assertEqual(tasks[0].new_comments, [])

    def test_skips_when_design_exists_no_feedback(self) -> None:
        issue = _make_issue()
        bot_comment = _make_comment("bot", f"{DESIGN_MARKER}\n\nDesign content")
        github = FakeGitHub([(issue, ["ready-for-design"])], [bot_comment])
        tasks = list(DesignTask.discover(github))
        self.assertEqual(len(tasks), 0)

    def test_yields_task_when_design_exists_with_feedback(self) -> None:
        issue = _make_issue()
        bot_comment = _make_comment("bot", f"{DESIGN_MARKER}\n\nDesign content")
        user_comment = _make_comment("user", "Please add dark mode support")
        github = FakeGitHub([(issue, ["ready-for-design"])], [bot_comment, user_comment])
        tasks = list(DesignTask.discover(github))
        self.assertEqual(len(tasks), 1)
        self.assertIsNotNone(tasks[0].existing_design)
        self.assertEqual(len(tasks[0].new_comments), 1)

    def test_removes_design_label_when_ready_for_planning(self) -> None:
        """When ready-for-planning is also present, ready-for-design should be removed."""
        issue = _make_issue(number=42)
        github = FakeGitHub(
            [(issue, ["ready-for-design", "ready-for-planning"])],
            [],
        )
        tasks = list(DesignTask.discover(github))
        self.assertEqual(len(tasks), 0)
        self.assertIn((42, "ready-for-design"), github.removed_labels)

    def test_extracts_image_urls_from_issue_body(self) -> None:
        issue = Issue(
            number=1,
            title="With image",
            body="Look at this: ![mockup](https://example.com/mock.png)",
        )
        github = FakeGitHub([(issue, ["ready-for-design"])], [])
        tasks = list(DesignTask.discover(github))
        self.assertEqual(tasks[0].image_urls, ["https://example.com/mock.png"])


# ---------------------------------------------------------------------------
# Signal isolation
# ---------------------------------------------------------------------------


class _GeminiSleepAgent(Agent):
    """Test-only agent that spawns 'sleep' with start_new_session=True."""

    name = "gemini_sleep_test"

    def can_handle(self, task: object) -> bool:  # pragma: no cover
        return False

    def execute(self, task: object) -> TaskResult:  # pragma: no cover
        raise NotImplementedError

    def spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._active_process = proc
        return proc


class TestDesignAgentSignalIsolation(unittest.TestCase):
    def test_subprocess_survives_parent_sigquit(self) -> None:
        agent = _GeminiSleepAgent()
        proc = agent.spawn()
        old_handler = signal.signal(signal.SIGQUIT, lambda *_: None)
        try:
            os.kill(os.getpid(), signal.SIGQUIT)
            time.sleep(0.1)
            self.assertIsNone(
                proc.poll(),
                "Child process was killed by SIGQUIT propagation. "
                "Add start_new_session=True to the Popen call.",
            )
        finally:
            signal.signal(signal.SIGQUIT, old_handler)
            proc.terminate()
            proc.wait(timeout=5)
            agent._active_process = None

    def test_terminate_kills_isolated_subprocess(self) -> None:
        agent = _GeminiSleepAgent()
        proc = agent.spawn()
        try:
            agent.terminate()
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(
                proc.poll(),
                "agent.terminate() did not kill the isolated subprocess.",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            agent._active_process = None


if __name__ == "__main__":
    unittest.main()
