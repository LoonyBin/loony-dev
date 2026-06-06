"""Read-only FastAPI web dashboard for monitoring loony-dev (issue #130)."""

from __future__ import annotations

from loony_dev.web.app import create_app

__all__ = ["create_app"]
