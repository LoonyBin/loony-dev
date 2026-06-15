"""Tests for packaging agent prompts as slash commands (issue #165)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loony_dev import commands
from loony_dev.commands import MANAGED_MARKER, install_commands

# Every prompt that was previously inlined on a task class must have a command.
EXPECTED_COMMANDS = {
    "implement-issue",
    "fix-review",
    "fix-hook",
    "commit-message",
    "pr-body",
    "address-reviews",
    "plan-issue",
    "resolve-conflicts",
    "fix-ci",
    "cleanup-stuck",
}


class CommandInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    @property
    def commands_dir(self) -> Path:
        return self.repo_root / ".claude" / "commands"

    def test_bundled_sources_cover_every_prompt(self) -> None:
        names = {p.stem for p in commands._command_sources()}
        self.assertEqual(names, EXPECTED_COMMANDS)

    def test_install_creates_every_command(self) -> None:
        written = install_commands(self.repo_root)
        self.assertEqual({p.stem for p in written}, EXPECTED_COMMANDS)
        installed = {p.stem for p in self.commands_dir.glob("*.md")}
        self.assertEqual(installed, EXPECTED_COMMANDS)

    def test_installed_files_carry_marker_below_frontmatter(self) -> None:
        install_commands(self.repo_root)
        for path in self.commands_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn(MANAGED_MARKER, text, path.name)
            # Frontmatter must remain at the very top for Claude Code to parse it,
            # so the marker sits *after* the closing fence, not within frontmatter.
            self.assertTrue(text.startswith("---\n"), path.name)
            closing_fence = text.index("---\n", len("---\n"))
            self.assertLess(closing_fence, text.index(MANAGED_MARKER), path.name)

    def test_idempotent_second_run_is_noop(self) -> None:
        install_commands(self.repo_root)
        before = {
            p: p.read_text(encoding="utf-8") for p in self.commands_dir.glob("*.md")
        }
        written = install_commands(self.repo_root)
        self.assertEqual(written, [])
        after = {
            p: p.read_text(encoding="utf-8") for p in self.commands_dir.glob("*.md")
        }
        self.assertEqual(before, after)

    def test_divergent_content_is_overwritten(self) -> None:
        install_commands(self.repo_root)
        target = self.commands_dir / "plan-issue.md"
        target.write_text("hand-edited\n", encoding="utf-8")

        written = install_commands(self.repo_root)
        self.assertEqual([p.name for p in written], ["plan-issue.md"])
        self.assertIn(MANAGED_MARKER, target.read_text(encoding="utf-8"))

    def test_hand_authored_command_is_left_untouched(self) -> None:
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        mine = self.commands_dir / "my-own-command.md"
        mine.write_text("my custom prompt\n", encoding="utf-8")

        install_commands(self.repo_root)

        self.assertTrue(mine.exists())
        self.assertEqual(mine.read_text(encoding="utf-8"), "my custom prompt\n")


# Each path-based command (#166) and the JSON keys its payload provides. The
# command body must reference $ARGUMENTS as a path and name every key, so a
# future edit to a task payload can't silently drop a key the body relies on.
# `cleanup-stuck` is intentionally absent — it still interpolates $ARGUMENTS
# inline and is not migrated.
_COMMAND_PAYLOAD_KEYS = {
    "implement-issue": ["issue_number", "title", "body", "plan"],
    "fix-review": ["issue_number", "review_output"],
    "fix-hook": ["issue_number", "hook_output"],
    "commit-message": ["issue_number", "title"],
    "pr-body": ["issue_number", "title", "body", "diff"],
    "address-reviews": [
        "pr_number", "title", "branch", "owner", "repo", "pr",
        "allow_create_issues", "comments",
    ],
    "plan-issue": [
        "issue_number", "title", "body", "current_plan", "feedback",
        "revision_note_delimiter",
    ],
    "resolve-conflicts": ["pr_number", "title", "branch", "default_branch"],
    "fix-ci": ["pr_number", "title", "branch", "failed_checks"],
}


class CommandTemplateContractTest(unittest.TestCase):
    """Migrated bodies take $ARGUMENTS as a path and document their JSON keys."""

    def _source(self, name: str) -> str:
        for path in commands._command_sources():
            if path.stem == name:
                return path.read_text(encoding="utf-8")
        self.fail(f"no bundled command source for {name!r}")

    def test_every_payload_key_is_named_in_body(self) -> None:
        for name, keys in _COMMAND_PAYLOAD_KEYS.items():
            body = self._source(name)
            with self.subTest(command=name):
                self.assertIn("$ARGUMENTS", body)
                # $ARGUMENTS is documented as a path to a JSON context file.
                self.assertIn("JSON", body)
                self.assertRegex(body, r"argument-hint:.*path")
                for key in keys:
                    self.assertIn(key, body, f"{name} body never names key {key!r}")

    def test_plan_issue_emits_revision_delimiter(self) -> None:
        # The delimiter contract with planning_task._split_revision_note must hold.
        from loony_dev.tasks.planning_task import REVISION_NOTE_DELIMITER

        self.assertIn(REVISION_NOTE_DELIMITER, self._source("plan-issue"))

    def test_cleanup_stuck_is_not_path_based(self) -> None:
        # cleanup-stuck still interpolates content inline; it is not migrated.
        self.assertNotIn("JSON context file", self._source("cleanup-stuck"))


if __name__ == "__main__":
    unittest.main()
