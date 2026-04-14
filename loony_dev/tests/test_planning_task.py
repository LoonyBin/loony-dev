"""Tests for PlanningTask._analyze_planning_comments() — issue #82.

Covers both the timestamp-based filtering (new markers with last-seen) and the
position-based fallback (old markers without last-seen).
"""
from __future__ import annotations

import unittest

from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.tasks.base import encode_marker
from loony_dev.tasks.planning_task import PLAN_MARKER, PLAN_MARKER_PREFIX, PlanningTask

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
        plan, new = PlanningTask._analyze_planning_comments([c1, c2], BOT_NAME)
        self.assertIsNone(plan)
        self.assertEqual(new, [c1, c2])

    def test_no_plan_excludes_bot_comments(self) -> None:
        c1 = _user("2024-01-01T09:00:00Z")
        bot = _comment(BOT_NAME, "failure notice", "2024-01-01T10:00:00Z")
        plan, new = PlanningTask._analyze_planning_comments([c1, bot], BOT_NAME)
        self.assertIsNone(plan)
        self.assertEqual(new, [c1])

    # ------------------------------------------------------------------
    # 2. Old marker (no last-seen) → position-based backward compat
    # ------------------------------------------------------------------
    def test_old_marker_position_based_filter(self) -> None:
        pre = _user("2024-01-01T09:00:00Z", "Old feedback")
        plan_comment = _plan("2024-01-01T10:00:00Z")  # old format, no last_seen
        post = _user("2024-01-01T11:00:00Z", "New feedback")

        plan, new = PlanningTask._analyze_planning_comments([pre, plan_comment, post], BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [post])

    def test_old_marker_extracts_plan_text(self) -> None:
        plan_comment = _plan("2024-01-01T10:00:00Z", plan_text="Step 1\nStep 2")
        plan, _ = PlanningTask._analyze_planning_comments([plan_comment], BOT_NAME)
        self.assertEqual(plan, "Step 1\nStep 2")

    # ------------------------------------------------------------------
    # 3. New marker (with last-seen) → timestamp-based filter
    # ------------------------------------------------------------------
    def test_timestamp_filter_picks_up_midrun_comment(self) -> None:
        t1 = _user("2024-01-01T09:00:00Z", "First feedback")
        t2 = _user("2024-01-01T10:30:00Z", "Mid-run feedback")
        t3_plan = _plan("2024-01-01T11:00:00Z", last_seen="2024-01-01T09:00:00Z")

        comments = sorted([t1, t2, t3_plan], key=lambda c: c.created_at)
        plan, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [t2])

    def test_timestamp_filter_excludes_already_seen_comments(self) -> None:
        t1 = _user("2024-01-01T09:00:00Z", "Old feedback")
        t2 = _user("2024-01-01T10:00:00Z", "Also old")
        plan_comment = _plan("2024-01-01T11:00:00Z", last_seen="2024-01-01T10:00:00Z")
        t3 = _user("2024-01-01T12:00:00Z", "New feedback")

        comments = sorted([t1, t2, plan_comment, t3], key=lambda c: c.created_at)
        plan, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "The plan.")
        self.assertEqual(new, [t3])

    def test_new_marker_extracts_plan_text(self) -> None:
        plan_comment = _plan("2024-01-01T10:00:00Z", last_seen="2024-01-01T09:00:00Z", plan_text="My plan text")
        plan, _ = PlanningTask._analyze_planning_comments([plan_comment], BOT_NAME)
        self.assertEqual(plan, "My plan text")

    def test_no_new_comments_after_last_seen(self) -> None:
        t1 = _user("2024-01-01T09:00:00Z")
        plan_comment = _plan("2024-01-01T10:00:00Z", last_seen="2024-01-01T09:00:00Z")

        comments = [t1, plan_comment]
        plan, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
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
        plan, new = PlanningTask._analyze_planning_comments(comments, BOT_NAME)
        self.assertEqual(plan, "Revised plan")
        self.assertEqual(new, [new_feedback])


if __name__ == "__main__":
    unittest.main()
