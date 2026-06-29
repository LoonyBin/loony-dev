"""Cross-process per-pipeline lease (issue #199); reliability tiers (issue #268)."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from loony_dev import execution_state as es
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


class HeartbeatStaleTierTestCase(unittest.TestCase):
    """The wedged-but-alive middle reclaim tier (issue #268)."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.now = 10_000_000.0

    def _write_bot_lease(
        self, key: str, *, holder: str = pl.HOLDER_BOT, started_age: float = 0.0,
    ) -> None:
        # A live holder (this process). ``started_age`` puts the acquisition that
        # far before ``now`` — small enough that the 12h tier stays silent, so
        # only the heartbeat tier can fire.
        path = pl.lease_path(self.base, REPO, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'{{"holder": "{holder}", "pid": {os.getpid()}, '
            f'"pipeline_key": "{key}", "started_at": {self.now - started_age}}}'
        )

    def _write_snapshot(self, key: str, *, state: str, heartbeat_age: float) -> None:
        hb = datetime.fromtimestamp(self.now - heartbeat_age, tz=timezone.utc).isoformat()
        es.write_snapshot(
            self.base, REPO, key,
            es.LiveState(
                pipeline_key=key, repo=REPO, stage="Implementing",
                current_skill="implement-issue", state=state, live=(state == "running"),
                last_heartbeat=hb,
            ),
        )

    def test_wedged_bot_with_stale_heartbeat_is_reclaimed(self) -> None:
        # Headline acceptance case: alive pid, a lease acquired a while ago, but a
        # ``running`` snapshot whose heartbeat froze long ago ⇒ reclaimed after
        # the window. ``started_age`` puts the acquisition before the frozen
        # heartbeat so the ``started_at`` clamp does not mask the staleness.
        self._write_bot_lease("issue-7", started_age=10_000.0)
        self._write_snapshot("issue-7", state="running", heartbeat_age=5_000.0)
        self.assertTrue(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=self.now, heartbeat_stale_after_seconds=3600.0,
            )
        )
        # The new lease carries this acquirer's pid + a fresh started_at.
        new = pl.read_pipeline_lease(self.base, REPO, "issue-7")
        self.assertEqual(new.pid, os.getpid())
        self.assertEqual(new.started_at, self.now)

    def test_fresh_heartbeat_is_not_reclaimed(self) -> None:
        self._write_bot_lease("issue-7")
        self._write_snapshot("issue-7", state="running", heartbeat_age=10.0)
        self.assertFalse(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=self.now, heartbeat_stale_after_seconds=3600.0,
            )
        )

    def test_idle_snapshot_does_not_trigger_heartbeat_tier(self) -> None:
        # A stale heartbeat on a non-``running`` snapshot is a prior phase's
        # leftover, not a wedged turn — the tier must not fire.
        self._write_bot_lease("issue-7")
        self._write_snapshot("issue-7", state="idle", heartbeat_age=5_000.0)
        self.assertFalse(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=self.now, heartbeat_stale_after_seconds=3600.0,
            )
        )

    def test_missing_snapshot_does_not_trigger_heartbeat_tier(self) -> None:
        self._write_bot_lease("issue-7")  # no snapshot written
        self.assertFalse(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=self.now, heartbeat_stale_after_seconds=3600.0,
            )
        )

    def test_drive_lease_is_exempt_from_heartbeat_tier(self) -> None:
        # A drive lease carries no progress heartbeat; the tier is bot-only, so a
        # stale running snapshot must not let the bot reclaim a human's drive.
        self._write_bot_lease("issue-7", holder=pl.HOLDER_DRIVE)
        self._write_snapshot("issue-7", state="running", heartbeat_age=5_000.0)
        self.assertFalse(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=self.now, heartbeat_stale_after_seconds=3600.0,
            )
        )

    def test_heartbeat_tier_off_when_not_configured(self) -> None:
        # Without heartbeat_stale_after_seconds the middle tier is dormant — a
        # wedged-but-alive worker is left for the 12h backstop (the #199 default).
        self._write_bot_lease("issue-7", started_age=10_000.0)
        self._write_snapshot("issue-7", state="running", heartbeat_age=5_000.0)
        self.assertFalse(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT, now=self.now,
            )
        )

    def test_fresh_lease_clamps_a_stale_prior_holder_heartbeat(self) -> None:
        # A ``running`` snapshot left by a previous holder carries a heartbeat
        # older than *this* lease's acquisition. The staleness baseline clamps to
        # the lease's started_at, so a brand-new lease is NOT instantly reclaimed.
        self._write_bot_lease("issue-7", started_age=10.0)  # just acquired
        self._write_snapshot("issue-7", state="running", heartbeat_age=5_000.0)
        self.assertFalse(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT,
                now=self.now, heartbeat_stale_after_seconds=3600.0,
            )
        )


class HeartbeatConfigTestCase(unittest.TestCase):
    """``heartbeat_stale_after_seconds`` fails loudly on bad config (issue #268)."""

    def setUp(self) -> None:
        from loony_dev import config
        self._config = config
        self._original = config.settings
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self._config.settings = self._original

    def _set(self, value: object) -> None:
        self._config.settings = self._config.Settings({"worker": {"heartbeat_stale_after": value}})

    def test_default_when_unset(self) -> None:
        self._config.settings = self._config.Settings({})
        self.assertEqual(
            pl.heartbeat_stale_after_seconds(), float(pl.DEFAULT_HEARTBEAT_STALE_AFTER_SECONDS)
        )

    def test_valid_value_is_used(self) -> None:
        self._set(900)
        self.assertEqual(pl.heartbeat_stale_after_seconds(), 900.0)

    def test_non_numeric_raises(self) -> None:
        self._set("soon")
        with self.assertRaises(ValueError):
            pl.heartbeat_stale_after_seconds()

    def test_zero_raises(self) -> None:
        self._set(0)
        with self.assertRaises(ValueError):
            pl.heartbeat_stale_after_seconds()

    def test_negative_raises(self) -> None:
        self._set(-60)
        with self.assertRaises(ValueError):
            pl.heartbeat_stale_after_seconds()


class FenceTestCase(unittest.TestCase):
    """The fencing token / stand-down detection (issue #268)."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        # Reset the contextvar between tests so one never leaks into another.
        token = pl.current_lease_token.set(None)
        self.addCleanup(lambda: pl.current_lease_token.reset(token))

    def test_no_token_is_a_noop(self) -> None:
        pl.current_lease_token.set(None)
        pl.check_fence(self.base, REPO, "issue-7")  # must not raise

    def test_matching_lease_passes(self) -> None:
        now = time.time()
        self.assertTrue(
            pl.acquire_pipeline_lease(
                self.base, REPO, "issue-7", holder=pl.HOLDER_BOT, now=now,
            )
        )
        lease = pl.read_pipeline_lease(self.base, REPO, "issue-7")
        pl.current_lease_token.set(lease.started_at)
        pl.check_fence(self.base, REPO, "issue-7")  # ours — must not raise

    def test_reclaimed_lease_raises(self) -> None:
        # We held started_at=1.0; the lease is now a different acquisition.
        pl.current_lease_token.set(1.0)
        path = pl.lease_path(self.base, REPO, "issue-7")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'{{"holder": "bot", "pid": {os.getpid()}, '
            f'"pipeline_key": "issue-7", "started_at": 2.0}}'
        )
        with self.assertRaises(pl.LeaseFencedError):
            pl.check_fence(self.base, REPO, "issue-7")

    def test_missing_lease_raises(self) -> None:
        pl.current_lease_token.set(1.0)  # we think we hold it, but it's gone
        with self.assertRaises(pl.LeaseFencedError):
            pl.check_fence(self.base, REPO, "issue-7")


if __name__ == "__main__":
    unittest.main()
