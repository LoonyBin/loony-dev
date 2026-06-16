"""Cross-process per-pipeline lease (issue #199)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from loony_dev import pipeline_lease as pl

REPO = "acme/widgets"


class PipelineLeaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def test_acquire_then_second_acquire_fails(self) -> None:
        self.assertTrue(
            pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_DRIVE)
        )
        # A live holder blocks a second acquisition (whichever holder).
        self.assertFalse(
            pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_BOT)
        )

    def test_release_frees_the_lease(self) -> None:
        pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_BOT)
        self.assertTrue(pl.release_pipeline_lease(self.base, REPO, "issue-7"))
        # Now re-acquirable.
        self.assertTrue(
            pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_DRIVE)
        )

    def test_release_with_wrong_holder_is_a_noop(self) -> None:
        pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_DRIVE)
        # A bot release must not stomp a drive's lease.
        self.assertFalse(
            pl.release_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_BOT)
        )
        self.assertFalse(
            pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_BOT)
        )

    def test_dead_pid_lease_is_reclaimed(self) -> None:
        # Hand-write a lease owned by a pid that cannot exist.
        path = pl.lease_path(self.base, REPO, "issue-7")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"holder": "drive", "pid": 2147483646, "pipeline_key": "issue-7", "started_at": 1.0}'
        )
        # The dead holder is reclaimed, so a fresh acquire succeeds.
        self.assertTrue(
            pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_BOT)
        )

    def test_aged_lease_is_reclaimed(self) -> None:
        # A live holder (this process) but a very old lease → reclaimable via TTL.
        path = pl.lease_path(self.base, REPO, "issue-7")
        path.parent.mkdir(parents=True, exist_ok=True)
        import os
        path.write_text(
            f'{{"holder": "drive", "pid": {os.getpid()}, "pipeline_key": "issue-7", "started_at": 1.0}}'
        )
        self.assertTrue(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=10_000_000.0, stale_after_seconds=10.0,
            )
        )

    def test_active_drive_keys_lists_only_live_drive_leases(self) -> None:
        pl.acquire_pipeline_lease(self.base, REPO, "issue-7", holder=pl.HOLDER_DRIVE)
        pl.acquire_pipeline_lease(self.base, REPO, "issue-9", holder=pl.HOLDER_BOT)
        keys = pl.active_drive_pipeline_keys(self.base, REPO)
        self.assertEqual(keys, {"issue-7"})  # only the drive lease, not the bot's

    def test_active_drive_keys_skips_stale(self) -> None:
        import os
        path = pl.lease_path(self.base, REPO, "issue-7")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'{{"holder": "drive", "pid": {os.getpid()}, "pipeline_key": "issue-7", "started_at": 1.0}}'
        )
        keys = pl.active_drive_pipeline_keys(
            self.base, REPO, now=10_000_000.0, stale_after_seconds=10.0,
        )
        self.assertEqual(keys, set())

    def test_lease_filename_is_filesystem_safe(self) -> None:
        # A pipeline key with a slash must never escape the leases directory.
        path = pl.lease_path(self.base, REPO, "issue-7/weird")
        self.assertIn("leases", path.parts)
        self.assertEqual(path.suffix, ".json")
        self.assertNotIn("/weird", str(path.name))


if __name__ == "__main__":
    unittest.main()
