"""Tests for the loony_dev config system."""
from __future__ import annotations

import os
import unittest

from loony_dev import config
from loony_dev.config import ConfigImmutabilityError


class TestConfigDefaults(unittest.TestCase):
    """Built-in defaults (from _defaults.toml) should be readable without calling initialize()."""

    def setUp(self) -> None:
        config._reset_for_testing()

    def test_top_level_defaults(self) -> None:
        self.assertEqual(config.settings.MIN_ROLE, "triage")
        self.assertEqual(config.settings.ALLOWED_USERS, [])
        self.assertEqual(config.settings.PERMISSION_CACHE_TTL, 600)
        self.assertEqual(config.settings.CLAUDE.QUOTA_FALLBACK_SECONDS, 300)
        self.assertEqual(config.settings.STUCK_THRESHOLD_HOURS, 12)
        self.assertFalse(config.settings.VERBOSE)

    def test_worker_defaults(self) -> None:
        self.assertEqual(config.settings.WORKER.INTERVAL, 60)
        self.assertEqual(config.settings.WORKER.WORK_DIR, ".")

    def test_supervisor_defaults(self) -> None:
        self.assertEqual(config.settings.SUPERVISOR.INTERVAL, 15)
        self.assertEqual(config.settings.SUPERVISOR.WORKER_INTERVAL, 60)
        self.assertEqual(config.settings.SUPERVISOR.REFRESH_INTERVAL, 1800)
        self.assertAlmostEqual(config.settings.SUPERVISOR.MIN_RESTART_DELAY, 5.0)
        self.assertAlmostEqual(config.settings.SUPERVISOR.MAX_RESTART_DELAY, 300.0)
        self.assertEqual(config.settings.SUPERVISOR.INCLUDE, [])
        self.assertEqual(config.settings.SUPERVISOR.EXCLUDE, [])

    def test_ui_defaults(self) -> None:
        self.assertEqual(config.settings.UI.MAX_BUFFER_LINES, 5000)
        self.assertEqual(config.settings.UI.TAIL_LINES, 100)
        self.assertEqual(config.settings.UI.SCAN_INTERVAL, 5)


class TestInitialize(unittest.TestCase):
    """initialize() should apply overrides and then freeze settings."""

    def setUp(self) -> None:
        config._reset_for_testing()

    def test_override_takes_effect(self) -> None:
        config.initialize({"worker.interval": 120, "min_role": "write"})
        self.assertEqual(config.settings.WORKER.INTERVAL, 120)
        self.assertEqual(config.settings.MIN_ROLE, "write")

    def test_none_values_are_ignored(self) -> None:
        """None values should not override lower-priority sources."""
        config.initialize({"worker.interval": None, "min_role": None})
        # Falls through to hardcoded default values
        self.assertEqual(config.settings.WORKER.INTERVAL, 60)
        self.assertEqual(config.settings.MIN_ROLE, "triage")

    def test_freeze_prevents_mutation(self) -> None:
        config.initialize({})
        with self.assertRaises(ConfigImmutabilityError):
            config.settings.set("MIN_ROLE", "admin")

    def test_double_initialize_raises(self) -> None:
        config.initialize({})
        with self.assertRaises(RuntimeError):
            config.initialize({})


class TestEnvVarOverride(unittest.TestCase):
    """Environment variables should override config-file / default values."""

    def setUp(self) -> None:
        config._reset_for_testing()

    def tearDown(self) -> None:
        # Remove any env vars we set during the test
        os.environ.pop("LOONY_DEV_MIN_ROLE", None)
        os.environ.pop("LOONY_DEV_STUCK_THRESHOLD_HOURS", None)
        os.environ.pop("LOONY_DEV_WORKER__INTERVAL", None)

    def test_top_level_env_var(self) -> None:
        os.environ["LOONY_DEV_MIN_ROLE"] = "write"
        config._reset_for_testing()  # reload to pick up env var
        self.assertEqual(config.settings.MIN_ROLE, "write")

    def test_nested_env_var(self) -> None:
        os.environ["LOONY_DEV_WORKER__INTERVAL"] = "999"
        config._reset_for_testing()
        self.assertEqual(config.settings.WORKER.INTERVAL, 999)

    def test_env_var_overrides_default_before_initialize(self) -> None:
        os.environ["LOONY_DEV_STUCK_THRESHOLD_HOURS"] = "24"
        config._reset_for_testing()
        self.assertEqual(config.settings.STUCK_THRESHOLD_HOURS, 24)

    def test_cli_override_beats_env_var(self) -> None:
        os.environ["LOONY_DEV_MIN_ROLE"] = "write"
        config._reset_for_testing()
        config.initialize({"min_role": "admin"})
        self.assertEqual(config.settings.MIN_ROLE, "admin")


class TestResetForTesting(unittest.TestCase):
    """_reset_for_testing() must allow repeated initialize() calls."""

    def test_reset_allows_reinitialize(self) -> None:
        config._reset_for_testing()
        config.initialize({"worker.interval": 42})
        self.assertEqual(config.settings.WORKER.INTERVAL, 42)

        config._reset_for_testing()
        config.initialize({"worker.interval": 99})
        self.assertEqual(config.settings.WORKER.INTERVAL, 99)


class TestGetCliOverrides(unittest.TestCase):
    """get_cli_overrides() should return only the non-None CLI values passed to initialize()."""

    def setUp(self) -> None:
        config._reset_for_testing()

    def test_overrides_stored(self) -> None:
        config.initialize({"worker.interval": 120, "min_role": "write"})
        overrides = config.get_cli_overrides()
        self.assertEqual(overrides["worker.interval"], 120)
        self.assertEqual(overrides["min_role"], "write")

    def test_none_values_not_stored(self) -> None:
        config.initialize({"worker.interval": None, "min_role": "write"})
        overrides = config.get_cli_overrides()
        self.assertNotIn("worker.interval", overrides)
        self.assertIn("min_role", overrides)

    def test_returns_copy(self) -> None:
        config.initialize({"min_role": "admin"})
        overrides = config.get_cli_overrides()
        overrides["min_role"] = "mutated"
        self.assertEqual(config.get_cli_overrides()["min_role"], "admin")

    def test_reset_clears_overrides(self) -> None:
        config.initialize({"min_role": "admin"})
        config._reset_for_testing()
        self.assertEqual(config.get_cli_overrides(), {})


class TestNewSettings(unittest.TestCase):
    """new_settings() should return a fresh instance with defaults, unaffected by initialize()."""

    def setUp(self) -> None:
        config._reset_for_testing()

    def test_returns_default_values(self) -> None:
        fresh = config.new_settings()
        self.assertEqual(fresh.WORKER.INTERVAL, 60)
        self.assertEqual(fresh.MIN_ROLE, "triage")

    def test_independent_of_initialize(self) -> None:
        config.initialize({"worker.interval": 999})
        fresh = config.new_settings()
        self.assertEqual(fresh.WORKER.INTERVAL, 60)


class TestLegacyEnvVar(unittest.TestCase):
    """LOONY_STUCK_THRESHOLD_HOURS legacy env var should be applied inside config."""

    def setUp(self) -> None:
        config._reset_for_testing()

    def tearDown(self) -> None:
        os.environ.pop("LOONY_STUCK_THRESHOLD_HOURS", None)
        config._reset_for_testing()

    def test_legacy_env_var_applied(self) -> None:
        os.environ["LOONY_STUCK_THRESHOLD_HOURS"] = "48"
        config._reset_for_testing()  # reload to pick up env var
        self.assertEqual(config.settings.STUCK_THRESHOLD_HOURS, 48)


if __name__ == "__main__":
    unittest.main()
