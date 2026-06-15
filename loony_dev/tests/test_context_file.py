"""Tests for the slash-command context-file helper (issue #166)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loony_dev.agents import context_file
from loony_dev.agents.context_file import cleanup_context_dir, write_context_file


class TestWriteContextFile(unittest.TestCase):
    def setUp(self) -> None:
        # Redirect the scratch root into an isolated temp dir.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.enterContext(mock.patch.object(context_file.tempfile, "gettempdir", return_value=self._tmp.name))

    def test_round_trips_payload_to_predictable_path(self) -> None:
        payload = {"issue_number": 166, "title": "do the thing", "nested": [1, 2]}
        path = write_context_file("implement-issue", payload, task_key="issue-166")

        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "implement-issue.json")
        self.assertIn("issue-166", path.parts)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_unicode_is_preserved(self) -> None:
        path = write_context_file("plan-issue", {"body": "café — déjà"}, task_key="issue-1")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["body"], "café — déjà")

    def test_cleanup_removes_the_dir(self) -> None:
        path = write_context_file("fix-ci", {"a": 1}, task_key="pr-9-ci")
        self.assertTrue(path.exists())

        cleanup_context_dir("pr-9-ci")
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_cleanup_none_is_noop(self) -> None:
        cleanup_context_dir(None)  # must not raise

    def test_unsafe_task_key_stays_within_root(self) -> None:
        path = write_context_file("fix-ci", {"a": 1}, task_key="../escape/me")
        root = Path(self._tmp.name).resolve()
        self.assertTrue(str(path.resolve()).startswith(str(root)))

    def test_pure_traversal_task_key_falls_back(self) -> None:
        path = write_context_file("fix-ci", {"a": 1}, task_key="..")
        self.assertEqual(path.parent.name, "task")


if __name__ == "__main__":
    unittest.main()
