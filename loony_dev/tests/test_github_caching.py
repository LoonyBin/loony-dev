"""Tests for caching and retry logic in the GitHub package."""
from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from loony_dev.github.client import GitHubClient, _DEFAULTS, gh_setting, is_retryable_gh_error
from loony_dev.github.pull_request import PullRequest
from loony_dev.github.check_run import CheckRun


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.bot_name = "loony-bot"
    repo.client = GitHubClient("owner/repo")
    repo._tick_cache = {}
    repo._check_runs_cache = {}
    repo.skip_ci_checks = set()
    return repo


# ---------------------------------------------------------------------------
# Tick-scoped cache: PullRequest.list_open
# ---------------------------------------------------------------------------


class TestTickCacheListOpen(unittest.TestCase):
    """PullRequest.list_open should return cached data within the same tick."""

    def test_second_call_uses_cache(self) -> None:
        repo = _make_repo()
        repo.client.gh_json = MagicMock(return_value=[])

        PullRequest.list_open(repo=repo)
        PullRequest.list_open(repo=repo)

        repo.client.gh_json.assert_called_once()

    def test_cache_cleared_between_ticks(self) -> None:
        repo = _make_repo()
        repo.client.gh_json = MagicMock(return_value=[])

        PullRequest.list_open(repo=repo)
        repo._tick_cache.clear()
        PullRequest.list_open(repo=repo)

        assert repo.client.gh_json.call_count == 2


# ---------------------------------------------------------------------------
# Cross-tick cache: CheckRun.list_failing
# ---------------------------------------------------------------------------


class TestCheckRunsCache(unittest.TestCase):
    """CheckRun.list_failing should cache results when all checks have completed."""

    def _api_response(self, *, runs: list[dict]) -> dict:
        return {"check_runs": runs}

    def test_all_completed_sha_is_cached(self) -> None:
        repo = _make_repo()
        response = self._api_response(runs=[
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "failure", "details_url": "http://example.com"},
        ])
        repo.client.gh_api = MagicMock(return_value=response)

        result1 = CheckRun.list_failing("abc123", repo=repo)
        result2 = CheckRun.list_failing("abc123", repo=repo)

        repo.client.gh_api.assert_called_once()
        assert len(result1) == 1
        assert result1[0].name == "lint"
        assert result2 == result1

    def test_pending_checks_bypass_cache(self) -> None:
        repo = _make_repo()
        response = self._api_response(runs=[
            {"name": "build", "status": "in_progress", "conclusion": None},
            {"name": "lint", "status": "completed", "conclusion": "failure", "details_url": "http://example.com"},
        ])
        repo.client.gh_api = MagicMock(return_value=response)

        CheckRun.list_failing("abc123", repo=repo)
        CheckRun.list_failing("abc123", repo=repo)

        assert repo.client.gh_api.call_count == 2

    def test_ttl_expiry_forces_refetch(self) -> None:
        repo = _make_repo()
        response = self._api_response(runs=[
            {"name": "build", "status": "completed", "conclusion": "success"},
        ])
        repo.client.gh_api = MagicMock(return_value=response)

        CheckRun.list_failing("abc123", repo=repo)

        # Advance cached_at past TTL
        entry = repo._check_runs_cache["abc123"]
        entry.cached_at = time.monotonic() - gh_setting("check_runs_cache_ttl") - 1

        CheckRun.list_failing("abc123", repo=repo)

        assert repo.client.gh_api.call_count == 2

    def test_eviction_removes_stale_entries(self) -> None:
        repo = _make_repo()
        response = self._api_response(runs=[
            {"name": "build", "status": "completed", "conclusion": "success"},
        ])
        repo.client.gh_api = MagicMock(return_value=response)

        CheckRun.list_failing("abc123", repo=repo)
        assert "abc123" in repo._check_runs_cache

        # Make entry stale
        entry = repo._check_runs_cache["abc123"]
        entry.cached_at = time.monotonic() - gh_setting("check_runs_cache_ttl") - 1

        # Manually evict (Repo method)
        stale = [k for k, v in repo._check_runs_cache.items()
                 if time.monotonic() - v.cached_at >= gh_setting("check_runs_cache_ttl")]
        for k in stale:
            del repo._check_runs_cache[k]
        assert "abc123" not in repo._check_runs_cache


# ---------------------------------------------------------------------------
# Exponential backoff: run_gh retries on rate-limit errors
# ---------------------------------------------------------------------------


def _rate_limit_error(stderr: str = "API rate limit exceeded") -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(1, "gh")
    exc.stdout = ""
    exc.stderr = stderr
    return exc


def _non_retryable_error() -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(1, "gh")
    exc.stdout = ""
    exc.stderr = "not found"
    return exc


class TestGhRetry(unittest.TestCase):
    """GitHubClient.gh should retry with backoff on rate-limit errors."""

    def setUp(self) -> None:
        """Pin config to defaults so tests are independent of config file state."""
        import loony_dev.config as config_mod
        from loony_dev.config import Settings
        self._original_settings = config_mod.settings
        config_mod.settings = Settings({})

    def tearDown(self) -> None:
        import loony_dev.config as config_mod
        config_mod.settings = self._original_settings

    @patch("loony_dev.github.client.time.sleep")
    @patch("loony_dev.github.client.subprocess.run")
    def test_retries_on_rate_limit_then_succeeds(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        ok = MagicMock(stdout="ok\n", stderr="")
        mock_run.side_effect = [_rate_limit_error(), _rate_limit_error(), ok]

        client = GitHubClient("owner/repo")
        result = client.gh("api", "repos/owner/repo/issues")

        assert result == "ok"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2
        # Verify exponential backoff: 2.0, 4.0 (defaults)
        assert mock_sleep.call_args_list[0][0][0] == 2.0
        assert mock_sleep.call_args_list[1][0][0] == 4.0

    @patch("loony_dev.github.client.time.sleep")
    @patch("loony_dev.github.client.subprocess.run")
    def test_raises_after_max_retries(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        max_retries = int(_DEFAULTS["max_retries"])
        mock_run.side_effect = [_rate_limit_error() for _ in range(max_retries + 1)]

        client = GitHubClient("owner/repo")
        with self.assertRaises(subprocess.CalledProcessError):
            client.gh("api", "repos/owner/repo/issues")

        assert mock_run.call_count == max_retries + 1
        assert mock_sleep.call_count == max_retries

    @patch("loony_dev.github.client.time.sleep")
    @patch("loony_dev.github.client.subprocess.run")
    def test_no_retry_on_non_retryable_error(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        mock_run.side_effect = _non_retryable_error()

        client = GitHubClient("owner/repo")
        with self.assertRaises(subprocess.CalledProcessError):
            client.gh("api", "repos/owner/repo/issues")

        mock_run.assert_called_once()
        mock_sleep.assert_not_called()

    def test_is_retryable_detects_rate_limit_patterns(self) -> None:
        for msg in ["API rate limit exceeded", "abuse detection mechanism", "secondary rate limit", "HTTP 403", "HTTP 429"]:
            exc = _rate_limit_error(msg)
            assert is_retryable_gh_error(exc), f"Should detect: {msg}"

    def test_is_retryable_rejects_normal_errors(self) -> None:
        exc = _non_retryable_error()
        assert not is_retryable_gh_error(exc)


# ---------------------------------------------------------------------------
# [github] config section
# ---------------------------------------------------------------------------


class TestGithubConfig(unittest.TestCase):
    """gh_setting reads from config.settings['github'], falling back to _DEFAULTS."""

    def test_defaults_when_no_github_section(self) -> None:
        import loony_dev.config as config_mod
        from loony_dev.config import Settings
        original = config_mod.settings
        try:
            config_mod.settings = Settings({})
            for key, default in _DEFAULTS.items():
                assert gh_setting(key) == default, f"{key} should be {default}"
        finally:
            config_mod.settings = original

    def test_overrides_from_settings(self) -> None:
        import loony_dev.config as config_mod
        from loony_dev.config import Settings
        original = config_mod.settings
        try:
            config_mod.settings = Settings({"github": {"max_retries": 5, "initial_backoff": 10.0}})
            assert gh_setting("max_retries") == 5
            assert gh_setting("initial_backoff") == 10.0
            assert gh_setting("permission_cache_ttl") == _DEFAULTS["permission_cache_ttl"]
        finally:
            config_mod.settings = original


if __name__ == "__main__":
    unittest.main()
