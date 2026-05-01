"""Coderabbit CLI integration for pre-commit code review."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.config._settings import Settings

logger = logging.getLogger(__name__)


class CodeRabbitError(Exception):
    """Raised when the coderabbit CLI exits with an unexpected error code."""


@dataclass
class CodeRabbitResult:
    has_issues: bool
    raw_output: str
    agent_prompt: str


def is_available(settings: "Settings") -> bool:
    """Return True if coderabbit is installed and not disabled in config."""
    coderabbit_cfg = settings.get("coderabbit")
    if isinstance(coderabbit_cfg, dict) and not coderabbit_cfg.get("enabled", True):
        logger.debug("Coderabbit disabled in config")
        return False
    if shutil.which("coderabbit") is None:
        logger.debug("coderabbit binary not found on PATH")
        return False
    return True


def run_review(repo_dir: Path) -> CodeRabbitResult:
    """Run `coderabbit review` and return a structured result.

    Exit codes 0 and 1 are both treated as normal review outcomes (many CLIs
    exit 1 when issues are found).  Any other code raises CodeRabbitError.
    """
    logger.info("Running coderabbit review in %s", repo_dir)
    proc = subprocess.run(
        ["coderabbit", "review", "--agent"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout + "\n" + proc.stderr).strip()
    logger.debug("coderabbit exit code: %d", proc.returncode)
    logger.debug("coderabbit output (first 500 chars): %.500s", output)

    if proc.returncode not in (0, 1):
        raise CodeRabbitError(
            f"coderabbit exited with code {proc.returncode}: {output[:200]}"
        )

    complete_event = _find_complete_event(output)
    has_issues = complete_event.get("findings", 0) > 0 if complete_event else False
    agent_prompt = output if has_issues else ""

    return CodeRabbitResult(
        has_issues=has_issues,
        raw_output=output,
        agent_prompt=agent_prompt,
    )


def _find_complete_event(output: str) -> dict | None:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "complete":
            return event
    return None
