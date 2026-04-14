"""Content class — a string that tracks prompt-injection safety.

Inspired by Rails' ``ActiveSupport::SafeBuffer``: strings from external
sources (GitHub API) are unsafe by default. Call ``.sanitize()`` to get a
safe copy, or ``.validate()`` to inspect without modifying.

The ``Content`` class subclasses ``str`` so it is a drop-in replacement
anywhere a plain string is expected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.sanitize import InjectionType


class Content(str):
    """A string that tracks whether it has been sanitized for prompt injection."""

    def __new__(cls, value: str = "", *, safe: bool = False) -> Content:
        instance = super().__new__(cls, value)
        instance._safe = safe
        return instance

    @property
    def is_safe(self) -> bool:
        """Return True if this content has been sanitized."""
        return self._safe

    def sanitize(self) -> Content:
        """Return a sanitized copy with ``is_safe=True``."""
        if self._safe:
            return self
        from loony_dev.sanitize import sanitize_user_content

        result = sanitize_user_content(str(self))
        return Content(result.text, safe=True)

    def validate(self) -> ValidationResult:
        """Check for injection vectors without modifying content."""
        from loony_dev.sanitize import sanitize_user_content

        result = sanitize_user_content(str(self))
        return ValidationResult(
            errors=result.injections,
            sanitized_text=result.text,
        )

    def __repr__(self) -> str:
        flag = "safe" if self._safe else "unsafe"
        return f"Content({super().__repr__()}, {flag})"


@dataclass
class ValidationResult:
    """Result of validating content for injection vectors."""

    errors: list[InjectionType] = field(default_factory=list)
    sanitized_text: str = ""

    @property
    def is_valid(self) -> bool:
        """Return True if no injection vectors were detected."""
        return not self.errors
