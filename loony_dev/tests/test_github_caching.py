"""Tests for GitHubClient caching and retry logic."""
from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from loony_dev.github import GitHubClient, _CHECK_RUNS_CACHE_TTL, _GH_MAX_RETRIES, _is_retryable_gh_error


def _make_client() -> GitHubClient:
    return GitHubClient(repo="owner/repo", bot_name="loony-bot")


# ---------------------------------------------------------------------------
# Tick-scoped cache: list_open_prs
# ---------------------------------------------------------------------------


class TestTickCacheListOpenPrs(unittest.TestCase):
    """list_open_prs should return cached data within the same tick."""

    def test_second_call_uses_cache(self) -> None:
        client = _make_client()
        client._gh_json = MagicMock(return_value=[])

        client.list_open_prs()
        client.list_open_prs()

        client._gh_json.assert_called_once()

    def test_cache_cleared_between_ticks(self) -> None:
        client = _make_client()
        client._gh_json = MagicMock(return_value=[])

        client.list_open_prs()
        client.clear_tick_cache()
        client.list_open_prs()

        assert client._gh_json.call_count == 2


# ---------------------------------------------------------------------------
# Cross-tick cache: get_pr_check_runs
# ---------------------------------------------------------------------------


class TestCheckRunsCache(unittest.TestCase):
    """get_pr_check_runs should cache results when all checks have completed."""

    def _api_response(self, *, runs: list[dict]) -> dict:
        return {"check_runs": runs}

    def test_all_completed_sha_is_cached(self) -> None:
        client = _make_client()
        response = self._api_response(runs=[
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "failure", "details_url": "http://example.com"},
        ])
        client._gh_api = MagicMock(return_value=response)

        result1 = client.get_pr_check_runs("abc123")
        result2 = client.get_pr_check_runs("abc123")

        client._gh_api.assert_called_once()
        assert len(result1) == 1
        assert result1[0].name == "lint"
        assert result2 == result1

    def test_pending_checks_bypass_cache(self) -> None:
        client = _make_client()
        response = self._api_response(runs=[
            {"name": "build", "status": "in_progress", "conclusion": None},
            {"name": "lint", "status": "completed", "conclusion": "failure", "details_url": "http://example.com"},
        ])
        client._gh_api = MagicMock(return_value=response)

        client.get_pr_check_runs("abc123")
        client.get_pr_check_runs("abc123")

        assert client._gh_api.call_count == 2

    def test_ttl_expiry_forces_refetch(self) -> None:
        client = _make_client()
        response = self._api_response(runs=[
            {"name": "build", "status": "completed", "conclusion": "success"},
        ])
        client._gh_api = MagicMock(return_value=response)

        client.get_pr_check_runs("abc123")

        # Advance cached_at past TTL
        entry = client._check_runs_cache["abc123"]
        entry.cached_at = time.monotonic() - _CHECK_RUNS_CACHE_TTL - 1

        client.get_pr_check_runs("abc123")

        assert client._gh_api.call_count == 2

    def test_eviction_removes_stale_entries(self) -> None:
        client = _make_client()
        response = self._api_response(runs=[
            {"name": "build", "status": "completed", "conclusion": "success"},
        ])
        client._gh_api = MagicMock(return_value=response)

        client.get_pr_check_runs("abc123")
        assert "abc123" in client._check_runs_cache

        # Make entry stale
        entry = client._check_runs_cache["abc123"]
        entry.cached_at = time.monotonic() - _CHECK_RUNS_CACHE_TTL - 1

        client.evict_stale_check_runs_cache()
        assert "abc123" not in client._check_runs_cache


# ---------------------------------------------------------------------------
# Exponential backoff: _gh retries on rate-limit errors
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
    """_gh should retry with backoff on rate-limit errors."""

    @patch("loony_dev.github.time.sleep")
    @patch("loony_dev.github.subprocess.run")
    def test_retries_on_rate_limit_then_succeeds(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        """_gh delegates to _run_gh which retries on rate-limit errors."""
        ok = MagicMock(stdout="ok\n", stderr="")
        mock_run.side_effect = [_rate_limit_error(), _rate_limit_error(), ok]

        client = _make_client()
        result = client._gh("api", "repos/owner/repo/issues")

        assert result == "ok"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2
        # Verify exponential backoff: 2.0, 4.0
        assert mock_sleep.call_args_list[0][0][0] == 2.0
        assert mock_sleep.call_args_list[1][0][0] == 4.0

    @patch("loony_dev.github.time.sleep")
    @patch("loony_dev.github.subprocess.run")
    def test_raises_after_max_retries(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        mock_run.side_effect = [_rate_limit_error() for _ in range(_GH_MAX_RETRIES + 1)]

        client = _make_client()
        with self.assertRaises(subprocess.CalledProcessError):
            client._gh("api", "repos/owner/repo/issues")

        assert mock_run.call_count == _GH_MAX_RETRIES + 1
        assert mock_sleep.call_count == _GH_MAX_RETRIES

    @patch("loony_dev.github.time.sleep")
    @patch("loony_dev.github.subprocess.run")
    def test_no_retry_on_non_retryable_error(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        mock_run.side_effect = _non_retryable_error()

        client = _make_client()
        with self.assertRaises(subprocess.CalledProcessError):
            client._gh("api", "repos/owner/repo/issues")

        mock_run.assert_called_once()
        mock_sleep.assert_not_called()

    def test_is_retryable_detects_rate_limit_patterns(self) -> None:
        for msg in ["API rate limit exceeded", "abuse detection mechanism", "secondary rate limit", "HTTP 403", "HTTP 429"]:
            exc = _rate_limit_error(msg)
            assert _is_retryable_gh_error(exc), f"Should detect: {msg}"

    def test_is_retryable_rejects_normal_errors(self) -> None:
        exc = _non_retryable_error()
        assert not _is_retryable_gh_error(exc)


if __name__ == "__main__":
    unittest.main()
