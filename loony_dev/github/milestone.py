"""Milestone model with Active Record pattern."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


@dataclass
class Milestone:
    """Active Record model for a GitHub milestone."""

    number: int
    title: str
    due_on: datetime | None

    @classmethod
    def list_open(cls, *, repo: Repo) -> list[Milestone]:
        """Fetch all open milestones for the repository."""
        from loony_dev.github.repo import parse_datetime

        try:
            data = repo.client.gh_api("milestones?state=open&per_page=100")
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch milestones for %s", repo.name)
            return []

        if not isinstance(data, list):
            return []

        return [
            cls(
                number=ms.get("number", 0),
                title=ms.get("title", ""),
                due_on=parse_datetime(ms.get("due_on")),
            )
            for ms in data
        ]
