"""Tests for GitHubClient tick-scoped and cross-tick caching."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from loony_dev.github import GitHubClient, _CHECK_RUNS_CACHE_TTL


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


if __name__ == "__main__":
    unittest.main()
