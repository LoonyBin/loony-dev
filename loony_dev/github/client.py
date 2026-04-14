"""Low-level ``gh`` CLI wrapper with retry and rate-limit handling.

This module is the only place that shells out to the ``gh`` binary.
It has zero business logic — just transport, retry, and JSON parsing.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, int | float] = {
    "permission_cache_ttl": 600,
    "check_runs_cache_ttl": 3600,
    "max_retries": 5,
    "initial_backoff": 2.0,
}

_GH_RATE_LIMIT_PATTERNS = ("rate limit", "abuse detection", "secondary rate", "403", "429")


def gh_setting(key: str) -> int | float:
    """Read a ``[github]`` config value, falling back to ``_DEFAULTS``."""
    from loony_dev import config

    section = config.settings.get("github")
    if isinstance(section, dict) and key in section:
        return type(_DEFAULTS[key])(section[key])
    return _DEFAULTS[key]


def is_retryable_gh_error(exc: subprocess.CalledProcessError) -> bool:
    """Return True if the gh CLI error looks like a rate-limit or transient server error."""
    combined = ((exc.stdout or "") + (exc.stderr or "")).lower()
    return any(p in combined for p in _GH_RATE_LIMIT_PATTERNS)


def run_gh(*cmd: str) -> str:
    """Run a gh CLI command with retry and exponential backoff on rate-limit errors."""
    max_retries = int(gh_setting("max_retries"))
    logger.debug("Running: %s", " ".join(cmd))
    backoff = float(gh_setting("initial_backoff"))
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            if attempt < max_retries and is_retryable_gh_error(exc):
                logger.warning(
                    "gh rate-limited (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    max_retries + 1,
                    backoff,
                    (exc.stderr or exc.stdout or "").strip()[:200],
                )
                time.sleep(backoff)
                backoff *= 2
            else:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# GitHubClient — thin transport
# ---------------------------------------------------------------------------


class GitHubClient:
    """Low-level gh CLI wrapper.  All higher-level logic lives elsewhere."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def gh(self, *args: str) -> str:
        """Run a gh CLI command and return stdout (with retry on rate-limit)."""
        cmd = ["gh", *args]
        if args and args[0] != "api":
            cmd += ["-R", self.repo]
        return run_gh(*cmd)

    def gh_api(self, endpoint: str) -> list | dict:
        """Call ``gh api`` for this repo and parse JSON output."""
        output = self.gh("api", f"repos/{self.repo}/{endpoint}")
        if not output:
            return []
        return json.loads(output)

    def gh_json(self, *args: str) -> list | dict:
        """Run a gh CLI command and parse JSON output."""
        output = self.gh(*args)
        if not output:
            return []
        return json.loads(output)
