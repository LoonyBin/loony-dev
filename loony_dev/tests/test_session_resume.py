"""On-demand session resume + registry plumbing (issue #199).

Covers the registry round-trip for the new ``worktree_path``/``pipeline_key``/
``branch`` fields, the resume helper's worktree recreation, and the #177
cross-worktree regression guard (resume must land in the recorded cwd).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loony_dev import session_registry as sr
from loony_dev.agents import session_resume
from loony_dev.agents.claude_session import jsonl_path_for
from loony_dev.git import GitRepo
from loony_dev.session import session_id_for

REPO = "acme/widgets"
_PATH = os.environ.get("PATH", "/usr/bin:/bin")


def _git_init(path: Path) -> GitRepo:
    """Create a real git repo at *path* with one commit on ``main``."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, env={**env, "PATH": _PATH})

    run("init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return GitRepo(work_dir=path, default_branch="main")


# ---------------------------------------------------------------------------
# Registry round-trip
# ---------------------------------------------------------------------------

class RegistryWorktreeFieldsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def test_record_and_find_pipeline_session(self) -> None:
        sr.record_session_worktree(
            self.base, REPO,
            pipeline_key="issue-7", task_key="issue-7",
            session_id="sess-abc", worktree_path="/wt/issue-7", branch="issue-7/slug",
        )
        found = sr.find_pipeline_session(self.base, REPO, "issue-7")
        self.assertIsNotNone(found)
        self.assertEqual(found.worktree_path, "/wt/issue-7")
        self.assertEqual(found.pipeline_key, "issue-7")
        self.assertEqual(found.branch, "issue-7/slug")
        self.assertEqual(found.session_id, "sess-abc")

    def test_find_pipeline_session_is_repo_scoped(self) -> None:
        sr.record_session_worktree(
            self.base, REPO, pipeline_key="issue-7", task_key="issue-7",
            session_id="s", worktree_path="/wt", branch="b",
        )
        # A different repo sharing the pipeline key must not cross over.
        self.assertIsNone(sr.find_pipeline_session(self.base, "other/repo", "issue-7"))

    def test_missing_fields_degrade_to_none(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-7")
        # An old-style entry without the #199 fields round-trips with None.
        sr.write_session_file(
            sess_dir, task_key="issue-7", repo=REPO,
            session_id="s", pid=1, started_at="t",
        )
        found = sr.find_session(self.base, "issue-7")
        self.assertIsNone(found.worktree_path)
        self.assertIsNone(found.pipeline_key)
        self.assertIsNone(found.branch)

    def test_record_preserves_existing_live_socket(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-7")
        sr.write_session_file(
            sess_dir, task_key="issue-7", repo=REPO, session_id="s", pid=42,
            started_at="t", socket="/live/attach.sock", status="running",
        )
        sr.record_session_worktree(
            self.base, REPO, pipeline_key="issue-7", task_key="issue-7",
            session_id="s", worktree_path="/wt/issue-7",
        )
        found = sr.find_session(self.base, "issue-7")
        # The live bridge's socket/pid/status are not clobbered by the recorder.
        self.assertEqual(found.socket, "/live/attach.sock")
        self.assertEqual(found.pid, 42)
        self.assertEqual(found.status, "running")
        self.assertEqual(found.worktree_path, "/wt/issue-7")


# ---------------------------------------------------------------------------
# Coordinate resolution + worktree recreation
# ---------------------------------------------------------------------------

class ResolveCoordinatesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.repo_dir = self.base / "acme" / "widgets"
        self.git = _git_init(self.repo_dir)

    def test_uses_recorded_entry_when_present(self) -> None:
        sr.record_session_worktree(
            self.base, REPO, pipeline_key="issue-7", task_key="issue-7",
            session_id="recorded-id", worktree_path="/recorded/cwd", branch="issue-7/x",
        )
        coords = session_resume.resolve_pipeline_coordinates(
            self.base, self.git, REPO, "issue-7",
        )
        self.assertEqual(coords.session_id, "recorded-id")
        self.assertEqual(coords.worktree_path, Path("/recorded/cwd"))
        self.assertEqual(coords.branch, "issue-7/x")

    def test_falls_back_to_deterministic_keys_with_no_record(self) -> None:
        # Create the feature branch so the fallback can discover it.
        subprocess.run(
            ["git", "branch", "issue-5/fix-thing"], cwd=self.repo_dir, check=True,
            capture_output=True, env={"PATH": _PATH},
        )
        coords = session_resume.resolve_pipeline_coordinates(
            self.base, self.git, REPO, "issue-5",
        )
        self.assertEqual(coords.session_id, session_id_for(REPO, "issue:5"))
        self.assertEqual(
            coords.worktree_path,
            self.git.work_dir / ".worktrees" / "acme" / "widgets" / "issue-5",
        )
        self.assertEqual(coords.branch, "issue-5/fix-thing")

    def test_ensure_worktree_recreates_torn_down_worktree(self) -> None:
        # The branch exists; the worktree does not (parked + torn down).
        subprocess.run(
            ["git", "branch", "issue-5/fix"], cwd=self.repo_dir, check=True,
            capture_output=True, env={"PATH": _PATH},
        )
        wt_path = self.git.work_dir / ".worktrees" / "acme" / "widgets" / "issue-5"
        coords = session_resume.PipelineCoordinates(
            session_id="s", worktree_path=wt_path, branch="issue-5/fix",
            task_key="issue-5", pipeline_key="issue-5",
        )
        self.assertFalse(wt_path.exists())
        with mock.patch("loony_dev.agents.session_resume.trust_directory") as trust:
            returned = session_resume.ensure_worktree(self.git, coords)
        self.assertTrue(wt_path.exists())
        self.assertEqual(returned, wt_path)
        trust.assert_called_once_with(wt_path)

    def test_ensure_worktree_is_noop_when_present(self) -> None:
        wt_path = self.git.work_dir / ".worktrees" / "acme" / "widgets" / "issue-5"
        self.git.create_worktree(branch="issue-5/x", path=wt_path, base="main")
        with mock.patch.object(self.git, "create_worktree") as create, \
                mock.patch("loony_dev.agents.session_resume.trust_directory"):
            coords = session_resume.PipelineCoordinates(
                session_id="s", worktree_path=wt_path, branch="issue-5/x",
                task_key="issue-5", pipeline_key="issue-5",
            )
            session_resume.ensure_worktree(self.git, coords)
        create.assert_not_called()  # already on disk → no recreation


# ---------------------------------------------------------------------------
# resume_session + the #177 cwd regression guard
# ---------------------------------------------------------------------------

class ResumeSessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.repo_dir = self.base / "acme" / "widgets"
        self.git = _git_init(self.repo_dir)

    def test_resume_opens_session_in_recorded_cwd(self) -> None:
        wt_path = self.git.work_dir / ".worktrees" / "acme" / "widgets" / "issue-7"
        self.git.create_worktree(branch="issue-7/x", path=wt_path, base="main")
        sr.record_session_worktree(
            self.base, REPO, pipeline_key="issue-7", task_key="issue-7",
            session_id="resume-me", worktree_path=str(wt_path), branch="issue-7/x",
        )

        fake_session = mock.MagicMock()
        fake_session.session_id = "resume-me"
        constructed: dict = {}

        def fake_ctor(cwd, **kwargs):
            constructed["cwd"] = cwd
            constructed["kwargs"] = kwargs
            return fake_session

        with mock.patch("loony_dev.agents.session_resume.ClaudeSession", side_effect=fake_ctor), \
                mock.patch("loony_dev.agents.session_resume.publish_session") as pub, \
                mock.patch("loony_dev.agents.session_resume.trust_directory"):
            pub.return_value = mock.MagicMock()
            resumed = session_resume.resume_session(self.base, self.git, REPO, "issue-7")

        # Resume must land in the exact recorded cwd (the #177 guard), and pass
        # --resume <id> for transcript continuity.
        self.assertEqual(constructed["cwd"], wt_path)
        self.assertEqual(constructed["kwargs"]["session_id"], "resume-me")
        self.assertIn("--resume", constructed["kwargs"]["extra_args"])
        fake_session.open.assert_called_once()
        self.assertEqual(resumed.coordinates.task_key, "issue-7")

    def test_transcript_path_resolves_under_recorded_cwd_slug(self) -> None:
        """The #177 class: the JSONL slug must derive from the recorded cwd."""
        recorded_cwd = self.git.work_dir / ".worktrees" / "acme" / "widgets" / "issue-7"
        sr.record_session_worktree(
            self.base, REPO, pipeline_key="issue-7", task_key="issue-7",
            session_id="sid-xyz", worktree_path=str(recorded_cwd), branch="issue-7/x",
        )
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.base / "cfg")}):
            path = session_resume.observe_transcript_path(self.base, self.git, REPO, "issue-7")
            expected = jsonl_path_for(recorded_cwd, "sid-xyz")
        self.assertEqual(path, expected)
        # The base checkout's cwd slug must NOT be what we resolve to — resuming
        # there was the #177 bug (transcript invisible, readiness times out).
        self.assertNotEqual(path, jsonl_path_for(self.git.work_dir, "sid-xyz"))


if __name__ == "__main__":
    unittest.main()
