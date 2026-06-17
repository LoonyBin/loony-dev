"""Tests for per-pipeline worker logging (issue #220)."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from loony_dev import pipeline_log, session_registry


class PipelineLogPathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_log_path_is_forward_deterministic(self) -> None:
        p1 = pipeline_log.pipeline_log_path(self.base, "acme", "widgets", "issue-5")
        p2 = pipeline_log.pipeline_log_path(self.base, "acme", "widgets", "issue-5")
        self.assertEqual(p1, p2)
        # It is the slug under <repo>/pipelines/, matching the session registry.
        expected = (
            session_registry.repo_log_dir(self.base, "acme", "widgets")
            / "pipelines"
            / f"{session_registry.task_slug('issue-5')}.log"
        )
        self.assertEqual(p1, expected)

    def test_logs_dir_is_sibling_of_sessions(self) -> None:
        logs_dir = pipeline_log.pipeline_logs_dir(self.base, "acme", "widgets")
        repo_dir = session_registry.repo_log_dir(self.base, "acme", "widgets")
        self.assertEqual(logs_dir.parent, repo_dir)
        self.assertEqual(logs_dir.name, "pipelines")


class PipelineLogHandlerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.handler = pipeline_log.PipelineLogHandler(self.base, "acme", "widgets")
        self.logger = logging.getLogger(f"test.pipeline_log.{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.logger.propagate = False
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.addCleanup(self.handler.close)

    def _path(self, key: str) -> Path:
        return pipeline_log.pipeline_log_path(self.base, "acme", "widgets", key)

    def test_record_routes_to_active_pipeline_only(self) -> None:
        with pipeline_log.pipeline_log_context("issue-5"):
            self.logger.info("hello from issue 5")
        target = self._path("issue-5")
        self.assertTrue(target.exists())
        self.assertIn("hello from issue 5", target.read_text())
        # No sibling pipeline file was created.
        sibling = self._path("issue-6")
        self.assertFalse(sibling.exists())

    def test_no_context_writes_no_pipeline_file(self) -> None:
        self.logger.info("worker-scope only")
        pipelines_dir = pipeline_log.pipeline_logs_dir(self.base, "acme", "widgets")
        self.assertFalse(pipelines_dir.exists())

    def test_sidecar_records_raw_key(self) -> None:
        with pipeline_log.pipeline_log_context("issue-5"):
            self.logger.info("first record")
        sidecar = pipeline_log.pipeline_key_sidecar_path(self.base, "acme", "widgets", "issue-5")
        self.assertTrue(sidecar.exists())
        self.assertEqual(sidecar.read_text().strip(), "issue-5")

    def test_debug_dropped_info_kept(self) -> None:
        with pipeline_log.pipeline_log_context("issue-5"):
            self.logger.debug("a debug line")
            self.logger.info("an info line")
        contents = self._path("issue-5").read_text()
        self.assertNotIn("a debug line", contents)
        self.assertIn("an info line", contents)

    def test_concurrent_keys_do_not_cross_contaminate(self) -> None:
        with pipeline_log.pipeline_log_context("issue-5"):
            self.logger.info("for five")
        with pipeline_log.pipeline_log_context("pr-9"):
            self.logger.info("for nine")
        self.assertIn("for five", self._path("issue-5").read_text())
        self.assertNotIn("for nine", self._path("issue-5").read_text())
        self.assertIn("for nine", self._path("pr-9").read_text())
        self.assertNotIn("for five", self._path("pr-9").read_text())

    def test_close_releases_handles(self) -> None:
        with pipeline_log.pipeline_log_context("issue-5"):
            self.logger.info("line")
        self.assertTrue(self.handler._handles)
        self.handler.close()
        self.assertFalse(self.handler._handles)

    def test_context_resets_to_previous(self) -> None:
        self.assertIsNone(pipeline_log.current_pipeline.get())
        with pipeline_log.pipeline_log_context("issue-5"):
            self.assertEqual(pipeline_log.current_pipeline.get(), "issue-5")
        self.assertIsNone(pipeline_log.current_pipeline.get())


if __name__ == "__main__":
    unittest.main()
