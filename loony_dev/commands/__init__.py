"""Canonical slash-command markdown bundled with loony-dev (issue #165).

The agent's prompt vocabulary lives here as Claude Code slash commands rather
than as inline Python f-strings on the task classes. Each ``*.md`` file in this
package is the canonical body of one command (``/implement-issue``,
``/plan-issue``, ...). When a worker starts it installs/upgrades these into the
repo-scoped ``<repo-checkout>/.claude/commands/`` directory — the same per-repo
placement the dashboard's skills/commands editor (issue #133) manages — so
workers and operators see the same commands in the same scope.

Installation is idempotent: a file whose content already matches is left
untouched; one with different (or missing) content is overwritten. Installed
files carry a managed-marker header (see :data:`MANAGED_MARKER`) so operators
can tell loony-dev-managed commands apart from hand-authored ones.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Marker written at the top of every installed command body (after any YAML
# frontmatter) so operators can tell loony-dev-managed commands apart from ones
# they authored by hand. Files carrying this marker are (re)installed to track
# the running loony-dev checkout; hand-authored commands without it are never
# touched.
MANAGED_MARKER = "<!-- loony-dev:managed -->"

_MANAGED_NOTE = (
    f"{MANAGED_MARKER}\n"
    "<!-- Installed by loony-dev. Manual edits here may be overwritten when a "
    "worker restarts; edit the canonical copy under loony_dev/commands/ instead. -->\n"
)

_SOURCE_DIR = Path(__file__).parent
_COMMANDS_SUBPATH = Path(".claude") / "commands"
_FRONTMATTER_FENCE = "---\n"


def _command_sources() -> list[Path]:
    """Return the bundled canonical command markdown files, sorted by name."""
    return sorted(_SOURCE_DIR.glob("*.md"))


def _render(source_text: str) -> str:
    """Return *source_text* with the managed marker inserted.

    The marker is placed immediately after the YAML frontmatter block (which
    Claude Code requires at the very top of the file) so the frontmatter stays
    valid; if there is no frontmatter it goes at the very top.
    """
    if source_text.startswith(_FRONTMATTER_FENCE):
        end = source_text.find("\n" + _FRONTMATTER_FENCE, len(_FRONTMATTER_FENCE))
        if end != -1:
            split = end + len("\n" + _FRONTMATTER_FENCE)
            return source_text[:split] + _MANAGED_NOTE + source_text[split:]
    return _MANAGED_NOTE + source_text


def install_commands(repo_root: Path | str) -> list[Path]:
    """Install/upgrade the bundled slash commands into the repo checkout.

    Writes each canonical command into ``<repo_root>/.claude/commands/<name>.md``.
    Idempotent: a destination whose content already matches the rendered source
    is left untouched; one that is missing or differs is (over)written.

    Returns the list of destination paths that were created or updated (empty
    when everything was already up to date).
    """
    target_dir = Path(repo_root) / _COMMANDS_SUBPATH
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for source in _command_sources():
        desired = _render(source.read_text(encoding="utf-8"))
        dest = target_dir / source.name
        try:
            current: str | None = dest.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current == desired:
            continue
        dest.write_text(desired, encoding="utf-8")
        written.append(dest)

    if written:
        logger.info(
            "Installed/updated %d loony-dev slash command(s) in %s",
            len(written), target_dir,
        )
    else:
        logger.debug("loony-dev slash commands already up to date in %s", target_dir)
    return written
