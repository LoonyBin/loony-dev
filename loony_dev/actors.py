"""GitHub comment actor attribution — one login, many agent roles.

An *actor* is the agent role that authored a bot comment (e.g. ``developer``,
``pm``). All roles share one GitHub login (``Repo.bot_name``); the role is
carried inside the comment body as an ``actor=<name>`` attribute folded into
the hidden first-line ``<!-- loony-* ... -->`` marker (a sibling of
``last-seen=``), or a standalone ``<!-- loony-actor actor=<name> -->`` first
line for comments that carry no lifecycle marker. "My comment" therefore
means ``author == bot_name AND actor == my actor`` — never the login alone.

Trust model (deliberate — do not weaken):

* The actor tag is honoured **only on bot-authored comments**:
  :func:`comment_actor` returns ``None`` for any other login before the tag
  is even read, so a spoofed ``actor=`` in a foreign comment can never make
  it "mine". The tag likewise never grants authorization
  (``Repo.is_authorized``) and never affects the ``Content`` ``safe`` flag —
  sanitization stays purely login-based. All roles share one login, one
  process, and one credential, so sanitizing one role's output against
  another would add no real boundary; cross-role content is trusted.
* Untagged bot comments — posted before stamping existed, or by Claude
  itself running ``gh`` inside a turn — resolve to :data:`DEFAULT_ACTOR`,
  which keeps the single-role deployment's behavior identical.
* The tag is read from the **first line only**: failure comments quote other
  comment bodies inside ``<details>`` blocks, and a quoted marker must never
  be read as the quoting comment's actor.
* Only comments can carry the label. Labels, assignees, requested reviewers,
  and issue/PR authorship are login-scoped GitHub primitives; role-level
  interaction must be expressed through stamped comments.

Distinct from :func:`loony_dev.execution_state.resolve_actor`, which resolves
*display names* for the JSONL execution-event feed (bot/capo/human/system);
this module is the GitHub comment attribution vocabulary.
"""
from __future__ import annotations

import re

DEFAULT_ACTOR = "developer"

# Standalone marker prepended to comments whose body has no lifecycle marker.
ACTOR_MARKER_PREFIX = "<!-- loony-actor"

# The lifecycle-marker family an actor attribute may live in (loony-failure,
# loony-success, loony-ci-failure, loony-in-error, loony-plan, loony-actor).
# The trailing hyphen deliberately excludes the `loonybin-` namespace (the
# injection-warning sentinel), whose exact-string dedup must never be folded
# into and whose first line must never be read as an actor marker.
_LOONY_MARKER_FAMILY = "<!-- loony-"

# Actor names are a tight lowercase slug so a malformed or hostile tag simply
# fails to parse (resolving to DEFAULT_ACTOR) instead of smuggling arbitrary
# text into decision logic. Length cap 32 — not configurable: slug sanity
# bound, not a tuning knob.
_ACTOR_NAME_PATTERN = r"[a-z0-9][a-z0-9_-]{0,31}"
_ACTOR_NAME_RE = re.compile(rf"^{_ACTOR_NAME_PATTERN}$")
_ACTOR_ATTR_RE = re.compile(rf"\bactor=({_ACTOR_NAME_PATTERN})(?=[\s>])")

# Reserved for the execution-event vocabulary (execution_state.ACTOR_HUMAN /
# ACTOR_SYSTEM) — a comment actor may never claim them.
RESERVED_ACTORS = frozenset({"human", "system"})


def decode_actor(body: str) -> str | None:
    """Extract the actor tag from *body*'s first-line marker, or ``None``.

    Only the first line is consulted, and only when it is a ``<!-- loony-``
    family marker — markers quoted later in the body (e.g. inside a
    ``<details>`` block) and ``loonybin-`` sentinels never match.
    """
    first_line = str(body).lstrip().split("\n", 1)[0].strip()
    if not first_line.startswith(_LOONY_MARKER_FAMILY) or not first_line.endswith("-->"):
        return None
    m = _ACTOR_ATTR_RE.search(first_line)
    return m.group(1) if m else None


def comment_actor(author: str, body: str, bot_name: str) -> str | None:
    """Resolve a comment's actor: a role name for bot-authored comments, else ``None``.

    ``None`` for any non-bot login — the tag is never honoured on a foreign
    comment (spoof-proofing). Bot-authored comments resolve to their tag, or
    :data:`DEFAULT_ACTOR` when untagged/malformed.
    """
    if not author or not bot_name or author != bot_name:
        return None
    return decode_actor(body) or DEFAULT_ACTOR


def validate_actor(name: str) -> str:
    """Return *name* if it is a valid, non-reserved actor name; raise ``ValueError``."""
    if not _ACTOR_NAME_RE.match(name):
        raise ValueError(
            f"invalid actor name {name!r}: must match {_ACTOR_NAME_PATTERN}"
        )
    if name in RESERVED_ACTORS:
        raise ValueError(
            f"actor name {name!r} is reserved for the execution-event vocabulary"
        )
    return name


def stamp_actor(body: str, actor: str) -> str:
    """Return *body* with ``actor=<name>`` stamped into its first line.

    Marker-first bodies get the attribute folded in before the closing
    ``-->`` (a sibling of ``last-seen=``); other bodies — including ones led
    by a ``loonybin-`` sentinel, which must stay byte-identical for its
    dedup — get a standalone ``<!-- loony-actor actor=<name> -->`` line
    prepended. Idempotent: a body whose first-line marker already carries an
    actor is returned unchanged. Raises :class:`ValueError` on an invalid
    *actor* so a bad explicit override fails loudly instead of corrupting
    the marker.
    """
    validate_actor(actor)
    first, sep, rest = body.partition("\n")
    line = first.strip()
    if line.startswith(_LOONY_MARKER_FAMILY) and line.endswith("-->"):
        if _ACTOR_ATTR_RE.search(line):
            return body
        stamped = f"{line[:-3].rstrip()} actor={actor} -->"
        return f"{stamped}{sep}{rest}" if sep else stamped
    return f"{ACTOR_MARKER_PREFIX} actor={actor} -->\n{body}"


def configured_actor() -> str:
    """Resolve this worker's actor role from config.

    Reads ``[worker] actor`` falling back to a flat top-level ``actor`` key
    (the same nested+flat lookup as ``execution_state._worker_setting``),
    defaulting to :data:`DEFAULT_ACTOR`. Raises :class:`ValueError` on an
    invalid or reserved name — a misconfigured identity must surface loudly
    at startup, not silently mis-attribute every comment.
    """
    from loony_dev import config

    worker_cfg = config.settings.get("worker")
    if isinstance(worker_cfg, dict) and "actor" in worker_cfg:
        name = worker_cfg["actor"]
    else:
        name = config.settings.get("actor", DEFAULT_ACTOR)
    return validate_actor(str(name))
