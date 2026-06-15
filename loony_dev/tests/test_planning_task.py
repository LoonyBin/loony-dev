"""Tests for PlanningTask._analyze_planning_comments() — issue #82.

Covers both the timestamp-based filtering (new markers with last-seen) and the
position-based fallback (old markers without last-seen).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.tasks.base import encode_marker
from loony_dev.tasks.planning_task import (
    PLAN_MARKER,
    PLAN_MARKER_PREFIX,
    REVISION_NOTE_DELIMITER,
    PlanningTask,
    _split_revision_note,
)

BOT_NAME = "loony-bot"
USER = "alice"


def _comment(author: str, body: str, ts: str) -> Comment:
    return Comment(author=author, body=body, created_at=ts)


def _plan(ts: str, last_seen: str | None = None, plan_text: str = "The plan.") -> Comment:
    if last_seen is not None:
        marker = encode_marker(PLAN_MARKER_PREFIX, last_seen)
    else:
        marker = PLAN_MARKER
    return _comment(BOT_NAME, f"{marker}\n\n{plan_text}", ts)


def _user(ts: str, body: str = "Some feedback.") -> Comment:
    return _comment(USER, body, ts)


class TestAnalyzePlanningComments(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. No plan yet — all non-bot comments returned
    # ------------------------------------------------------------------
    def test_no_plan_returns_all_user_comments(self) -> None:
        c1 = _user("2024-01-01T09:00:00Z")
        c2 = _user("2024-01-01T10:00:00Z")
        plan, _, new = PlanningTask._analyze_planning_comments([c1, c2], BOT_NAME)
        self.assertIsNone(plan)
        self.assertEqual(new, [c1, c2])

    def test_no_plan_excludes_bot_comments(self) -> None:
        c1 = _user("2024-01-01T09:00:00Z")
        bot = _comment(BOT_NAME, "failure notice", "2024-01-01T10:00:00Z")
        plan, _, new = PlanningTask._analyze_planning_comments([c1, bot], BOT_NAME)
        self.assertIsNone(plan)
        self.assertEqual(new, [c1])

    # ------------------------------------------------------------------
    # 2. Old marker (no last-seen) → position-based backward compat
    # ------------------------------------------------------------------
    def test_old_marker_position_based_filter(self) -> None:
        pre = _user("2024-01-01T09:00:00Z", "Old feedback")
        plan_comment = _plan("2024-01-01T10:00:00Z")  # old format, no last_seen
        post = _user("2024-01-01T11:00:00Z", "New feedback")

        plan, _, new = PlanningTask._analyze_planning_comments([pre, plan_comment, post], BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [post])

    def test_old_marker_extracts_plan_text(self) -> None:
        plan_comment = _plan("2024-01-01T10:00:00Z", plan_text="Step 1\nStep 2")
        plan, _id, _ = PlanningTask._analyze_planning_comments([plan_comment], BOT_NAME)
        self.assertEqual(plan, "Step 1\nStep 2")

    # ------------------------------------------------------------------
    # 3. New marker (with last-seen) → timestamp-based filter
    # ------------------------------------------------------------------
    def test_timestamp_filter_picks_up_midrun_comment(self) -> None:
        t1 = _user("2024-01-01T09:00:00Z", "First feedback")
        t2 = _user("2024-01-01T10:30:00Z", "Mid-run feedback")
        t3_plan = _plan("2024-01-01T11:00:00Z", last_seen="2024-01-01T09:00:00Z")

        comments = sorted([t1, t2, t3_plan], key=lambda c: c.created_at)
        plan, _, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [t2])

    def test_timestamp_filter_excludes_already_seen_comments(self) -> None:
        t1 = _user("2024-01-01T09:00:00Z", "Old feedback")
        t2 = _user("2024-01-01T10:00:00Z", "Also old")
        plan_comment = _plan("2024-01-01T11:00:00Z", last_seen="2024-01-01T10:00:00Z")
        t3 = _user("2024-01-01T12:00:00Z", "New feedback")

        comments = sorted([t1, t2, plan_comment, t3], key=lambda c: c.created_at)
        plan, _, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [t3])

    def test_new_marker_extracts_plan_text(self) -> None:
        plan_comment = _plan("2024-01-01T10:00:00Z", last_seen="2024-01-01T09:00:00Z", plan_text="My plan text")
        plan, _id, _ = PlanningTask._analyze_planning_comments([plan_comment], BOT_NAME)
        self.assertEqual(plan, "My plan text")

    def test_no_new_comments_after_last_seen(self) -> None:
        t1 = _user("2024-01-01T09:00:00Z")
        plan_comment = _plan("2024-01-01T10:00:00Z", last_seen="2024-01-01T09:00:00Z")

        comments = [t1, plan_comment]
        plan, _, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [])

    # ------------------------------------------------------------------
    # 4. Uses the last plan marker when multiple exist
    # ------------------------------------------------------------------
    def test_uses_last_plan_marker(self) -> None:
        first_plan = _plan("2024-01-01T10:00:00Z", plan_text="First plan")
        feedback = _user("2024-01-01T11:00:00Z", "Update please")
        second_plan = _plan("2024-01-01T12:00:00Z", last_seen="2024-01-01T11:00:00Z", plan_text="Revised plan")
        new_feedback = _user("2024-01-01T13:00:00Z", "Looks good")

        comments = [first_plan, feedback, second_plan, new_feedback]
        plan, _, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "Revised plan")
        self.assertEqual(new, [new_feedback])


class TestSplitRevisionNote(unittest.TestCase):

    def test_explicit_delimiter(self) -> None:
        summary = f"# Plan\n\nStep 1\n\n{REVISION_NOTE_DELIMITER}\n\nClarified step 1."
        plan, note = _split_revision_note(summary)
        self.assertEqual(plan, "# Plan\n\nStep 1")
        self.assertEqual(note, "Clarified step 1.")

    def test_strips_trailing_horizontal_rule_before_delimiter(self) -> None:
        summary = f"Plan body\n\n---\n\n{REVISION_NOTE_DELIMITER}\n\nNote."
        plan, note = _split_revision_note(summary)
        self.assertEqual(plan, "Plan body")
        self.assertEqual(note, "Note.")

    def test_fallback_to_revision_note_heading(self) -> None:
        summary = "Plan body\n\n---\n\n**Revision note:** Adjusted scope."
        plan, note = _split_revision_note(summary)
        self.assertEqual(plan, "Plan body")
        self.assertEqual(note, "Adjusted scope.")

    def test_no_delimiter_returns_whole_summary_as_plan(self) -> None:
        plan, note = _split_revision_note("Just a plan with no note.")
        self.assertEqual(plan, "Just a plan with no note.")
        self.assertEqual(note, "")

    def test_fallback_picks_last_trailing_heading(self) -> None:
        summary = (
            "Plan body\n\n**Revision note:** earlier mention in plan body\n\n"
            "More plan content\n\n**Revision note:** Real trailing note."
        )
        plan, note = _split_revision_note(summary)
        self.assertEqual(
            plan,
            "Plan body\n\n**Revision note:** earlier mention in plan body\n\nMore plan content",
        )
        self.assertEqual(note, "Real trailing note.")

    def test_fallback_ignores_inline_revision_note_phrase(self) -> None:
        summary = "Plan body mentions **Revision note:** inline but no trailing heading."
        plan, note = _split_revision_note(summary)
        self.assertEqual(plan, summary)
        self.assertEqual(note, "")


class TestContextPayload(unittest.TestCase):
    """PlanningTask.context_payload() — the /plan-issue slash-command context (#166)."""

    def _issue(self, number: int = 5, title: str = "Add X", body: str = "details") -> MagicMock:
        issue = MagicMock()
        issue.number = number
        issue.title = title
        issue.body = body
        return issue

    def test_command_name_is_plan_issue(self) -> None:
        self.assertEqual(PlanningTask.command_name, "plan-issue")

    def test_fresh_plan_payload_has_no_revision_keys(self) -> None:
        task = PlanningTask(self._issue(), existing_plan=None, new_comments=[])
        payload = task.context_payload()
        self.assertEqual(payload["issue_number"], 5)
        self.assertEqual(payload["title"], "Add X")
        self.assertEqual(payload["body"], "details")
        self.assertNotIn("current_plan", payload)
        self.assertNotIn("feedback", payload)

    def test_revision_payload_carries_plan_feedback_and_delimiter(self) -> None:
        comments = [
            _comment(USER, "Please tweak step 2.", "2024-01-01T10:00:00Z"),
            _comment("bob", "And step 3.", "2024-01-01T11:00:00Z"),
        ]
        task = PlanningTask(
            self._issue(), existing_plan="The current plan.", new_comments=comments,
        )
        payload = task.context_payload()
        self.assertEqual(payload["current_plan"], "The current plan.")
        self.assertIn("Please tweak step 2.", payload["feedback"])
        self.assertIn("And step 3.", payload["feedback"])
        self.assertEqual(payload["revision_note_delimiter"], REVISION_NOTE_DELIMITER)

    def test_describe_is_short_label(self) -> None:
        fresh = PlanningTask(self._issue(number=9, title="T"), existing_plan=None, new_comments=[])
        self.assertEqual(fresh.describe(), "Create implementation plan for issue #9: T")
        revision = PlanningTask(self._issue(number=9, title="T"), existing_plan="p", new_comments=[])
        self.assertEqual(revision.describe(), "Revise implementation plan for issue #9: T")


if __name__ == "__main__":
    unittest.main()
