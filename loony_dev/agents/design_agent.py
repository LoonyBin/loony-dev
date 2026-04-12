from __future__ import annotations

import logging
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.agents.gemini_quota import GeminiQuotaMixin
from loony_dev.models import TaskResult, truncate_for_log

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)


class DesignAgent(GeminiQuotaMixin, Agent):
    """Uses Gemini to generate or update a UI/UX design specification for an issue."""

    name = "design"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def _can_handle_task(self, task: Task) -> bool:
        return task.task_type == "design_issue"

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        image_urls: list[str] = getattr(task, "image_urls", [])

        logger.debug("Running design Gemini CLI (cwd=%s)", self.work_dir)
        logger.debug("Design prompt: %s", truncate_for_log(prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            image_paths = self._download_images(image_urls, Path(tmpdir))
            cmd = self._build_cmd(prompt, image_paths)

            with subprocess.Popen(
                cmd,
                cwd=self.work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as proc:
                self._active_process = proc
                try:
                    stdout, stderr = proc.communicate()
                finally:
                    self._active_process = None

        logger.debug("Design Gemini CLI exited with code %d", proc.returncode)
        if stdout:
            logger.debug("Design output (%d chars): %s", len(stdout), truncate_for_log(stdout))
        if stderr:
            logger.debug("Design stderr: %s", truncate_for_log(stderr))

        if proc.returncode != 0:
            combined = f"{stdout}\n{stderr}"
            if self._is_quota_error(combined):
                self._handle_quota_error(combined)
            return TaskResult(
                success=False,
                output=combined,
                summary=f"Agent exited with code {proc.returncode}",
            )

        if not stdout.strip():
            return TaskResult(
                success=False,
                output=stdout,
                summary="Gemini returned empty output",
            )

        # The raw output IS the design spec; use it directly as the summary so
        # DesignTask.on_complete can post it as a GitHub comment.
        return TaskResult(
            success=True,
            output=stdout,
            summary=stdout.strip(),
        )

    def _build_cmd(self, prompt: str, image_paths: list[Path]) -> list[str]:
        """Build the gemini-cli command.

        The Gemini CLI uses @path syntax to include files inline in the prompt.
        Images are appended as @<path> references so Gemini can process them.
        """
        if image_paths:
            # Append @path references for each image so Gemini can read them.
            file_refs = "\n".join(f"@{p}" for p in image_paths)
            full_prompt = f"{prompt}\n\n{file_refs}"
        else:
            full_prompt = prompt

        return ["gemini", "--yolo", "-p", full_prompt]

    def _download_images(self, urls: list[str], tmpdir: Path) -> list[Path]:
        """Download images from URLs into tmpdir. Skips failures with a warning."""
        paths: list[Path] = []
        for i, url in enumerate(urls):
            # Derive a filename from the URL, falling back to index-based name.
            url_path = url.split("?")[0]  # strip query params
            suffix = Path(url_path).suffix or ".png"
            dest = tmpdir / f"image_{i}{suffix}"
            try:
                urllib.request.urlretrieve(url, dest)  # noqa: S310
                paths.append(dest)
                logger.debug("Downloaded image %d: %s -> %s", i, url, dest)
            except Exception as exc:
                logger.warning("Failed to download image %s: %s — skipping", url, exc)
        return paths
