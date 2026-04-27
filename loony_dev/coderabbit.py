"""Coderabbit CLI integration for pre-commit code review."""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.config._settings import Settings

logger = logging.getLogger(__name__)

# Output patterns that indicate no issues were found.
_NO_ISSUE_PATTERNS = [
    "no issues found",
    "no actionable comments",
    "0 issues",
    "lgtm",
    "looks good",
]

# The coderabbit CLI wraps its AI-agent prompt between these delimiters.
_AGENT_PROMPT_START = "---AGENT PROMPT---"
_AGENT_PROMPT_END = "---END AGENT PROMPT---"


class CodeRabbitError(Exception):
    """Raised when the coderabbit CLI exits with an unexpected error code."""


@dataclass
class CodeRabbitResult:
    has_issues: bool
    raw_output: str
    # Prompt text to feed to an AI agent for fixing; may be the full output
    # when no structured agent-prompt block is present.
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
        ["coderabbit", "review"],
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

    lower = output.lower()
    has_issues = not any(p in lower for p in _NO_ISSUE_PATTERNS)

    # Extract the structured AI-agent prompt block if present.
    agent_prompt = ""
    if _AGENT_PROMPT_START in output:
        start = output.index(_AGENT_PROMPT_START) + len(_AGENT_PROMPT_START)
        end = output.find(_AGENT_PROMPT_END, start)
        agent_prompt = output[start:end].strip() if end >= 0 else output[start:].strip()

    if not agent_prompt and has_issues:
        # Fall back to the full output so the agent still gets actionable context.
        agent_prompt = output

    return CodeRabbitResult(
        has_issues=has_issues,
        raw_output=output,
        agent_prompt=agent_prompt,
    )
