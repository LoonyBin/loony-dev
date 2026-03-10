"""Prompt injection defense: sanitize user-controlled content before it enters prompts.

This module strips constructs that are invisible in rendered GitHub markdown but
readable verbatim by the AI agent — the primary vectors for prompt injection attacks.

Attack vectors mitigated
------------------------
1. HTML comments (``<!-- ... -->``) — invisible in rendered markdown, read verbatim by AI.
2. Zero-width and other invisible Unicode characters (Cf category + specific codepoints)
   that can encode hidden instructions character-by-character.

Intentionally NOT stripped
--------------------------
- Visible markdown syntax (bold, italic, headings, links, etc.)
- Code blocks and inline code (including those containing ``<!--``)
- URLs
- Normal HTML tags like ``<br>``, ``<details>``, ``<summary>`` that render visibly

Usage
-----
    from loony_dev.sanitize import sanitize_user_content

    result = sanitize_user_content(raw_text)
    clean_text = result.text
    if result.injections:
        # warn — result.injections is a list of InjectionType strings
        ...
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum


class InjectionType(str, Enum):
    HTML_COMMENT = "html_comment"
    ZERO_WIDTH_CHARS = "zero_width_chars"


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# HTML comments, including multiline.  DOTALL so `.` matches newlines.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Zero-width / invisible Unicode codepoints commonly abused for injection:
#   U+00AD  SOFT HYPHEN
#   U+200B  ZERO WIDTH SPACE
#   U+200C  ZERO WIDTH NON-JOINER
#   U+200D  ZERO WIDTH JOINER
#   U+2060  WORD JOINER
#   U+FEFF  ZERO WIDTH NO-BREAK SPACE / BOM
#
# In addition we strip any character whose Unicode general category is "Cf"
# (Format characters) because that category covers all the above plus
# additional invisible control characters used in bidirectional text attacks.
_INVISIBLE_CHARS_RE = re.compile(
    r"[\u00ad\u200b\u200c\u200d\u2060\ufeff]"
    r"|[\u202a-\u202e]"   # bidirectional override characters
    r"|[\u2066-\u2069]"   # isolate / pop directional formatting
)


def _has_invisible_chars(text: str) -> bool:
    """Return True if text contains invisible/zero-width Unicode characters."""
    if _INVISIBLE_CHARS_RE.search(text):
        return True
    # Also check for any Cf-category character not already covered by the regex.
    for ch in text:
        if unicodedata.category(ch) == "Cf":
            return True
    return False


def _strip_invisible_chars(text: str) -> str:
    """Remove invisible/zero-width Unicode characters from text."""
    # Remove regex-matched characters first (fast path).
    text = _INVISIBLE_CHARS_RE.sub("", text)
    # Then sweep for any remaining Cf-category characters.
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


# Collapse runs of 3+ blank lines down to 2, to tidy up gaps left by stripping.
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass
class SanitizeResult:
    """Result of sanitizing a piece of user-controlled content.

    Attributes
    ----------
    text:
        The sanitized text, safe to interpolate into a prompt.
    injections:
        A list of :class:`InjectionType` values identifying what was found and
        stripped.  Empty if the input was clean.
    """

    text: str
    injections: list[InjectionType] = field(default_factory=list)

    @property
    def has_injections(self) -> bool:
        """Return True if any injection content was detected."""
        return bool(self.injections)


def sanitize_user_content(text: str | None) -> SanitizeResult:
    """Strip hidden prompt-injection vectors from user-supplied text.

    Parameters
    ----------
    text:
        Raw string value from a GitHub issue title, body, or comment.
        May be ``None`` (treated as an empty string).

    Returns
    -------
    SanitizeResult
        ``.text`` is the cleaned string; ``.injections`` lists what was found.
    """
    if not text:
        return SanitizeResult(text="" if text is None else text)

    injections: list[InjectionType] = []
    result = text

    # --- Step 1: strip HTML comments ---
    stripped_html = _HTML_COMMENT_RE.sub("", result)
    if stripped_html != result:
        injections.append(InjectionType.HTML_COMMENT)
        result = stripped_html

    # --- Step 2: strip invisible Unicode characters ---
    if _has_invisible_chars(result):
        injections.append(InjectionType.ZERO_WIDTH_CHARS)
        result = _strip_invisible_chars(result)

    # --- Step 3: normalize whitespace (collapse excess blank lines) ---
    result = _EXCESS_BLANK_LINES_RE.sub("\n\n", result)

    # Strip leading/trailing whitespace that stripping may have introduced,
    # but only if the original text was not purely whitespace.
    if result.strip():
        result = result.strip()

    return SanitizeResult(text=result, injections=injections)
