"""Tests for injection-warning deduplication via WarningComment (issue #68).

The bot polls GitHub on every cycle. Without deduplication, a prompt injection
detected on the read path would post a new warning comment on every poll —
spamming the issue indefinitely. The fix: WarningComment.exists() checks
whether a warning for the same field already exists before posting.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from loony_dev.actors import ACTOR_MARKER_PREFIX, DEFAULT_ACTOR, decode_actor
from loony_dev.github.comment import WarningComment


BOT_NAME = "loony-bot"


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.bot_name = BOT_NAME
    repo.actor = DEFAULT_ACTOR
    return repo


def _sentinel(field: str) -> str:
    return f'{WarningComment.SENTINEL_PREFIX}"{field}" -->'


def _warning_body(field: str) -> str:
    return f"{_sentinel(field)}\n> [!WARNING]\n> ..."


class TestWarningCommentDedup(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. No prior warning — save() posts the comment
    # ------------------------------------------------------------------
    def test_no_prior_warning_posts_comment(self) -> None:
        repo = _make_repo()
        repo.client.gh_json.return_value = {"comments": []}

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        wc.save()

        repo.client.gh.assert_called_once()

    # ------------------------------------------------------------------
    # 2. Prior warning present for same field — save() does NOT post
    # ------------------------------------------------------------------
    def test_prior_warning_suppresses_comment(self) -> None:
        repo = _make_repo()
        repo.client.gh_json.return_value = {
            "comments": [{"body": _warning_body("body")}],
        }

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        wc.save()

        repo.client.gh.assert_not_called()

    # ------------------------------------------------------------------
    # 3. Prior warning for a different field — save() DOES post
    # ------------------------------------------------------------------
    def test_different_field_posts_comment(self) -> None:
        repo = _make_repo()
        repo.client.gh_json.return_value = {
            "comments": [{"body": _warning_body("title")}],
        }

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        wc.save()

        repo.client.gh.assert_called_once()

    # ------------------------------------------------------------------
    # 4. Prior warning on a different issue — save() DOES post
    # ------------------------------------------------------------------
    def test_different_item_posts_comment(self) -> None:
        repo = _make_repo()

        def _comments(*args, **kwargs):
            # Parse the number from the gh_json call args
            # gh_json("issue", "view", str(number), "--json", "comments")
            num = int(args[2])
            if num == 1:
                return {"comments": [{"body": _warning_body("body")}]}
            return {"comments": []}

        repo.client.gh_json.side_effect = _comments

        wc = WarningComment(number=2, field_name="body", injections=[], _repo=repo)
        wc.save()

        repo.client.gh.assert_called_once()

    # ------------------------------------------------------------------
    # 5. Restart survival — fresh check still reads prior warning
    # ------------------------------------------------------------------
    def test_restart_survival_no_repost(self) -> None:
        """A freshly-constructed WarningComment with no in-memory state must not
        re-post if the warning comment is already present in GitHub."""
        repo = _make_repo()
        repo.client.gh_json.return_value = {
            "comments": [{"body": _warning_body("body")}],
        }

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        wc.save()

        repo.client.gh.assert_not_called()

    # ------------------------------------------------------------------
    # 6. Warning comment body contains the sentinel
    # ------------------------------------------------------------------
    def test_posted_comment_body_contains_sentinel(self) -> None:
        repo = _make_repo()
        repo.client.gh_json.return_value = {"comments": []}

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        self.assertIn(_sentinel("body"), str(wc.body))

    # ------------------------------------------------------------------
    # 7. Actor attribution — stamped without breaking the sentinel dedup
    # ------------------------------------------------------------------
    def test_save_stamps_worker_actor_and_keeps_sentinel_intact(self) -> None:
        """A non-default role's warning is attributed to that role; the
        loonybin sentinel stays byte-identical (prepended, never folded)."""
        repo = _make_repo()
        repo.actor = "pm"
        repo.client.gh_json.return_value = {"comments": []}

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        wc.save()

        posted = repo.client.gh.call_args[0][-1]
        self.assertTrue(posted.startswith(f"{ACTOR_MARKER_PREFIX} actor=pm -->\n"))
        self.assertIn(_sentinel("body"), posted)
        self.assertEqual(decode_actor(posted), "pm")

    def test_stamped_prior_warning_still_suppresses_repost(self) -> None:
        """Dedup matches the sentinel by substring, so a stamped prior warning
        (actor line above the sentinel) still suppresses a re-post."""
        repo = _make_repo()
        repo.client.gh_json.return_value = {
            "comments": [{"body": f"{ACTOR_MARKER_PREFIX} actor=pm -->\n{_warning_body('body')}"}],
        }

        wc = WarningComment(number=1, field_name="body", injections=[], _repo=repo)
        wc.save()

        repo.client.gh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
