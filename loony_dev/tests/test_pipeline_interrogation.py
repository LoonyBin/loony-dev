"""Web service + endpoint layer for on-demand interrogation (issue #199)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from loony_dev import pipeline_lease, session_registry as sr
from loony_dev.web import create_app, services

REPO = "acme/widgets"


def _record(base: Path, *, pipeline_key="issue-7", session_id="sid", worktree="/wt/issue-7"):
    sr.record_session_worktree(
        base, REPO, pipeline_key=pipeline_key, task_key=pipeline_key,
        session_id=session_id, worktree_path=worktree, branch=f"{pipeline_key}/x",
    )


class InterrogateServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.addCleanup(services._DRIVE_SESSIONS.clear)

    def test_observe_takes_no_lease(self) -> None:
        _record(self.base)
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.base / "cfg")}):
            result = services.interrogate_pipeline(self.base, "issue-7", "observe")
        self.assertEqual(result["mode"], "observe")
        self.assertFalse(result["lease"])
        self.assertIn("issue-7", result["transcript"])
        # No lease file was created.
        self.assertIsNone(pipeline_lease.read_pipeline_lease(self.base, REPO, "issue-7"))

    def test_drive_acquires_lease_and_returns_attach_url(self) -> None:
        _record(self.base)
        resumed = mock.MagicMock()
        resumed.coordinates.task_key = "issue-7"
        fake_resume = mock.MagicMock(return_value=resumed)
        result = services.interrogate_pipeline(
            self.base, "issue-7", "drive", resume_fn=fake_resume,
        )
        self.assertEqual(result["mode"], "drive")
        self.assertTrue(result["lease"])
        self.assertEqual(result["attach_url"], "/api/sessions/issue-7/attach")
        fake_resume.assert_called_once()
        # The drive now holds the lease.
        held = pipeline_lease.read_pipeline_lease(self.base, REPO, "issue-7")
        self.assertIsNotNone(held)
        self.assertEqual(held.holder, pipeline_lease.HOLDER_DRIVE)

    def test_drive_refused_when_bot_holds_lease(self) -> None:
        _record(self.base)
        pipeline_lease.acquire_pipeline_lease(
            self.base, REPO, "issue-7", holder=pipeline_lease.HOLDER_BOT,
        )
        with self.assertRaises(services.PipelineBusyError):
            services.interrogate_pipeline(
                self.base, "issue-7", "drive", resume_fn=mock.MagicMock(),
            )

    def test_drive_releases_lease_when_resume_fails(self) -> None:
        _record(self.base)

        def boom(*a, **k):
            raise RuntimeError("no claude")

        with self.assertRaises(RuntimeError):
            services.interrogate_pipeline(self.base, "issue-7", "drive", resume_fn=boom)
        # A failed resume must not leave the lease dangling.
        self.assertIsNone(pipeline_lease.read_pipeline_lease(self.base, REPO, "issue-7"))

    def test_stop_drive_releases_lease_and_closes_session(self) -> None:
        _record(self.base)
        resumed = mock.MagicMock()
        resumed.coordinates.task_key = "issue-7"
        services.interrogate_pipeline(
            self.base, "issue-7", "drive", resume_fn=mock.MagicMock(return_value=resumed),
        )
        out = services.stop_drive(self.base, "issue-7")
        self.assertTrue(out["stopped"])
        resumed.close.assert_called_once()
        self.assertIsNone(pipeline_lease.read_pipeline_lease(self.base, REPO, "issue-7"))

    def test_unknown_pipeline_raises_not_found(self) -> None:
        with self.assertRaises(services.SessionNotFoundError):
            services.interrogate_pipeline(self.base, "issue-99", "observe")


class InterrogateEndpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.addCleanup(services._DRIVE_SESSIONS.clear)
        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))

    def test_observe_endpoint_returns_no_lease(self) -> None:
        _record(self.base)
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.base / "cfg")}):
            resp = self.client.post("/api/pipelines/issue-7/interrogate", json={"mode": "observe"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["mode"], "observe")
        self.assertFalse(body["lease"])

    def test_drive_endpoint_409_when_bot_holds_lease(self) -> None:
        _record(self.base)
        pipeline_lease.acquire_pipeline_lease(
            self.base, REPO, "issue-7", holder=pipeline_lease.HOLDER_BOT,
        )
        resp = self.client.post("/api/pipelines/issue-7/interrogate", json={"mode": "drive"})
        self.assertEqual(resp.status_code, 409)

    def test_bad_mode_is_400(self) -> None:
        resp = self.client.post("/api/pipelines/issue-7/interrogate", json={"mode": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_pipeline_is_404(self) -> None:
        resp = self.client.post("/api/pipelines/issue-77/interrogate", json={"mode": "observe"})
        self.assertEqual(resp.status_code, 404)

    def test_non_string_repo_is_400(self) -> None:
        resp = self.client.post(
            "/api/pipelines/issue-7/interrogate", json={"mode": "observe", "repo": 123},
        )
        self.assertEqual(resp.status_code, 400)

    def test_release_non_string_repo_is_400(self) -> None:
        resp = self.client.post("/api/pipelines/issue-7/release", json={"repo": 123})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
