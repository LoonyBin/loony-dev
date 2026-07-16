"""Tests for loony_dev.actors — per-comment actor attribution (one login, many roles).

Covers:
- decode_actor / comment_actor: first-line-only parsing, spoof-proofing,
  default-actor resolution for untagged bot comments
- stamp_actor: fold into marker-first bodies, prepend for plain bodies,
  idempotency
- validate_actor / configured_actor: grammar, reserved names, config resolution
- Comment.actor_for and the GitHubItem.add_comment/edit_comment choke point
- decode_last_seen coexistence with the actor attribute
"""
from __future__ import annotations

import contextlib
import unittest
from unittest.mock import MagicMock

from loony_dev.actors import (
    ACTOR_MARKER_PREFIX,
    DEFAULT_ACTOR,
    comment_actor,
    configured_actor,
    decode_actor,
    stamp_actor,
    validate_actor,
)
from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.github.issue import Issue
from loony_dev.tasks.base import (
    CI_FAILURE_MARKER,
    SUCCESS_MARKER,
    SUCCESS_MARKER_PREFIX,
    decode_last_seen,
    encode_marker,
)

BOT = "loony-bot"


@contextlib.contextmanager
def _settings(data: dict):
    from loony_dev import config

    old = config.settings
    config.settings = config.Settings(data)
    try:
        yield
    finally:
        config.settings = old


# ---------------------------------------------------------------------------
# decode_actor
# ---------------------------------------------------------------------------


class TestDecodeActor(unittest.TestCase):

    def test_folded_into_lifecycle_marker(self) -> None:
        self.assertEqual(decode_actor("<!-- loony-success actor=developer -->\n\nDone."), "developer")

    def test_sibling_of_last_seen(self) -> None:
        body = "<!-- loony-plan last-seen=2026-07-16T10:00:00Z actor=pm -->\n\nPlan."
        self.assertEqual(decode_actor(body), "pm")

    def test_standalone_marker(self) -> None:
        self.assertEqual(decode_actor(f"{ACTOR_MARKER_PREFIX} actor=pm -->\nHello"), "pm")

    def test_untagged_marker_returns_none(self) -> None:
        self.assertIsNone(decode_actor(f"{SUCCESS_MARKER}\n\nDone."))

    def test_plain_body_returns_none(self) -> None:
        self.assertIsNone(decode_actor("Just a comment with actor=pm in prose"))

    def test_quoted_marker_in_details_ignored(self) -> None:
        """A marker quoted later in the body (e.g. inside <details>) never counts."""
        body = (
            "<!-- loony-in-error -->\n\nStopping.\n\n"
            f"<details>\n\n{CI_FAILURE_MARKER[:-4]} actor=pm -->\n\nCI failed\n\n</details>"
        )
        self.assertIsNone(decode_actor(body))

    def test_malformed_tag_returns_none(self) -> None:
        self.assertIsNone(decode_actor("<!-- loony-success actor=PM! -->\n\nDone."))

    def test_first_line_must_be_loony_marker(self) -> None:
        self.assertIsNone(decode_actor("<!-- other actor=pm -->\nBody"))


# ---------------------------------------------------------------------------
# comment_actor
# ---------------------------------------------------------------------------


class TestCommentActor(unittest.TestCase):

    def test_bot_tagged_resolves_to_tag(self) -> None:
        body = "<!-- loony-success actor=pm -->\n\nDone."
        self.assertEqual(comment_actor(BOT, body, BOT), "pm")

    def test_bot_untagged_resolves_to_default(self) -> None:
        """Legacy comments and in-turn `gh` comments resolve to the default actor."""
        self.assertEqual(comment_actor(BOT, f"{SUCCESS_MARKER}\n\nDone.", BOT), DEFAULT_ACTOR)
        self.assertEqual(comment_actor(BOT, "plain text", BOT), DEFAULT_ACTOR)

    def test_foreign_login_spoof_returns_none(self) -> None:
        """A spoofed actor tag on someone else's comment is never honoured."""
        body = f"<!-- loony-success actor={DEFAULT_ACTOR} -->\n\nlooks legit"
        self.assertIsNone(comment_actor("mallory", body, BOT))

    def test_empty_author_or_bot_name_returns_none(self) -> None:
        self.assertIsNone(comment_actor("", "body", BOT))
        self.assertIsNone(comment_actor("", "body", ""))


# ---------------------------------------------------------------------------
# stamp_actor
# ---------------------------------------------------------------------------


class TestStampActor(unittest.TestCase):

    def test_folds_into_bare_marker(self) -> None:
        body = f"{SUCCESS_MARKER}\n\nDone."
        self.assertEqual(
            stamp_actor(body, "developer"),
            "<!-- loony-success actor=developer -->\n\nDone.",
        )

    def test_folds_after_last_seen(self) -> None:
        marker = encode_marker(SUCCESS_MARKER_PREFIX, "2026-07-16T10:00:00Z")
        stamped = stamp_actor(f"{marker}\n\nDone.", "developer")
        self.assertTrue(
            stamped.startswith(
                "<!-- loony-success last-seen=2026-07-16T10:00:00Z actor=developer -->"
            )
        )

    def test_prepends_standalone_for_plain_body(self) -> None:
        self.assertEqual(
            stamp_actor("Hello there.", "pm"),
            f"{ACTOR_MARKER_PREFIX} actor=pm -->\nHello there.",
        )

    def test_idempotent(self) -> None:
        once = stamp_actor(f"{SUCCESS_MARKER}\n\nDone.", "developer")
        self.assertEqual(stamp_actor(once, "developer"), once)
        self.assertEqual(stamp_actor(once, "pm"), once)  # never restamped

    def test_marker_only_body_no_trailing_newline(self) -> None:
        self.assertEqual(
            stamp_actor(SUCCESS_MARKER, "developer"),
            "<!-- loony-success actor=developer -->",
        )

    def test_round_trips_through_decode(self) -> None:
        for body in (f"{SUCCESS_MARKER}\n\nDone.", "plain text"):
            self.assertEqual(decode_actor(stamp_actor(body, "pm")), "pm")


# ---------------------------------------------------------------------------
# decode_last_seen coexistence
# ---------------------------------------------------------------------------


class TestLastSeenCoexistence(unittest.TestCase):

    def test_last_seen_survives_actor_attribute(self) -> None:
        ts = "2026-07-16T10:00:00Z"
        marker = encode_marker(SUCCESS_MARKER_PREFIX, ts)
        stamped = stamp_actor(f"{marker}\n\nDone.", "developer")
        self.assertEqual(decode_last_seen(stamped), ts)

    def test_last_seen_survives_either_attribute_order(self) -> None:
        ts = "2026-07-16T10:00:00Z"
        body = f"<!-- loony-success actor=developer last-seen={ts} -->\n\nDone."
        self.assertEqual(decode_last_seen(body), ts)
        self.assertEqual(decode_actor(body), "developer")


# ---------------------------------------------------------------------------
# validate_actor / configured_actor
# ---------------------------------------------------------------------------


class TestValidateActor(unittest.TestCase):

    def test_valid_names(self) -> None:
        for name in ("developer", "pm", "qa-2", "code_reviewer"):
            self.assertEqual(validate_actor(name), name)

    def test_invalid_grammar_raises(self) -> None:
        for name in ("", "PM", "has space", "-leading", "x" * 33, "a>b"):
            with self.assertRaises(ValueError):
                validate_actor(name)

    def test_reserved_names_raise(self) -> None:
        for name in ("human", "system"):
            with self.assertRaises(ValueError):
                validate_actor(name)


class TestConfiguredActor(unittest.TestCase):

    def test_defaults_to_developer(self) -> None:
        with _settings({}):
            self.assertEqual(configured_actor(), DEFAULT_ACTOR)

    def test_reads_worker_section(self) -> None:
        with _settings({"worker": {"actor": "pm"}}):
            self.assertEqual(configured_actor(), "pm")

    def test_falls_back_to_flat_key(self) -> None:
        with _settings({"actor": "qa"}):
            self.assertEqual(configured_actor(), "qa")

    def test_worker_section_wins_over_flat(self) -> None:
        with _settings({"worker": {"actor": "pm"}, "actor": "qa"}):
            self.assertEqual(configured_actor(), "pm")

    def test_invalid_configured_name_raises(self) -> None:
        with _settings({"worker": {"actor": "Not Valid"}}):
            with self.assertRaises(ValueError):
                configured_actor()

    def test_reserved_configured_name_raises(self) -> None:
        with _settings({"worker": {"actor": "human"}}):
            with self.assertRaises(ValueError):
                configured_actor()


# ---------------------------------------------------------------------------
# Comment.actor_for
# ---------------------------------------------------------------------------


class TestCommentActorFor(unittest.TestCase):

    def test_bot_comment_untagged(self) -> None:
        c = Comment(author=BOT, body=Content("hi", safe=True), created_at="")
        self.assertEqual(c.actor_for(BOT), DEFAULT_ACTOR)

    def test_bot_comment_tagged(self) -> None:
        c = Comment(
            author=BOT,
            body=Content("<!-- loony-actor actor=pm -->\nhi", safe=True),
            created_at="",
        )
        self.assertEqual(c.actor_for(BOT), "pm")

    def test_foreign_comment_is_none_and_stays_unsafe(self) -> None:
        c = Comment(
            author="mallory",
            body=Content("<!-- loony-actor actor=developer -->\nhi"),
            created_at="",
        )
        self.assertIsNone(c.actor_for(BOT))
        self.assertFalse(c.body.is_safe)


# ---------------------------------------------------------------------------
# Stamping choke point — GitHubItem.add_comment / edit_comment
# ---------------------------------------------------------------------------


class TestStampingChokePoint(unittest.TestCase):

    def _item(self) -> Issue:
        repo = MagicMock()
        repo.bot_name = BOT
        repo.actor = DEFAULT_ACTOR
        return Issue(number=7, _repo=repo)

    def test_add_comment_folds_marker_first_body(self) -> None:
        item = self._item()
        item.add_comment(f"{SUCCESS_MARKER}\n\nDone.")
        body = item._repo.client.gh.call_args[0][-1]
        self.assertEqual(body, "<!-- loony-success actor=developer -->\n\nDone.")

    def test_add_comment_prepends_standalone_for_plain_body(self) -> None:
        item = self._item()
        item.add_comment("Plain notice.")
        body = item._repo.client.gh.call_args[0][-1]
        self.assertEqual(body, f"{ACTOR_MARKER_PREFIX} actor=developer -->\nPlain notice.")

    def test_add_comment_explicit_actor_overrides_repo_default(self) -> None:
        item = self._item()
        item.add_comment("Plain notice.", actor="pm")
        body = item._repo.client.gh.call_args[0][-1]
        self.assertEqual(decode_actor(body), "pm")

    def test_edit_comment_stamps_too(self) -> None:
        item = self._item()
        item.edit_comment(42, f"{SUCCESS_MARKER}\n\nUpdated.")
        kwargs = item._repo.client.gh_api_patch.call_args.kwargs
        self.assertEqual(decode_actor(kwargs["body"]), DEFAULT_ACTOR)


if __name__ == "__main__":
    unittest.main()
