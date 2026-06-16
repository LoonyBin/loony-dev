"""Parse Claude Code JSONL transcript entries into structured UI events.

This module is **pure** (no I/O, no asyncio): it maps one parsed JSONL entry to
zero-or-more structured events that the dashboard's JSONL-driven *observe*
surface (issue #202) renders as a conversation — user prompts, assistant
text/thinking, tool calls and their results, turn-terminal stop reasons, and
interrupts.

It is the single source of truth for the JSONL entry shape: the constants and
shape predicates here (``TERMINAL_STOP_REASONS``, ``INTERRUPT_PREFIX``,
:func:`entry_text`, :func:`is_terminal_assistant`, :func:`is_interrupt`) are
re-exported by :mod:`loony_dev.agents.claude_session` under their historical
names, so there is exactly one place that knows how a transcript line is shaped.

Each emitted event is a JSON-serialisable ``dict`` carrying a stable ``id`` — the
entry ``uuid`` plus a per-block suffix — so a client that replays the whole
transcript on reconnect renders an identical conversation no matter how many
times it reconnects (acceptance: reconnect idempotency).
"""
from __future__ import annotations

# Assistant ``stop_reason`` values that mark a normally-completed turn.  Other
# values (notably ``tool_use``) are mid-turn and must not be treated as done.
TERMINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

# Canonical text Claude records (as a ``user`` JSONL entry) when a turn is
# interrupted with ESC.  Matched as a prefix because Claude appends context
# (e.g. "[Request interrupted by user for tool use]").
INTERRUPT_PREFIX = "[Request interrupted by user"


def entry_text(entry: dict) -> str:
    """Extract the human-readable assistant/user *text* from a JSONL *entry*.

    Handles the two content shapes seen in transcripts: a plain string, or a
    list of typed blocks (only ``text`` blocks carry text here; ``thinking`` /
    ``tool_use`` / ``tool_result`` are skipped). ``thinking`` is deliberately
    excluded so this stays a faithful drop-in for the historical
    ``claude_session._entry_text`` (whose text/quota logic must not start
    counting thinking content); :func:`parse_entry` surfaces thinking separately
    by iterating the blocks directly.
    """
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def is_terminal_assistant(entry: dict) -> bool:
    """True if *entry* is an assistant turn that ended normally."""
    if entry.get("type") != "assistant":
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    return message.get("stop_reason") in TERMINAL_STOP_REASONS


def is_interrupt(entry: dict) -> bool:
    """True if *entry* is the user marker recorded when a turn is interrupted."""
    if entry.get("type") != "user":
        return False
    return entry_text(entry).lstrip().startswith(INTERRUPT_PREFIX)


def _ts(entry: dict) -> str | None:
    ts = entry.get("timestamp")
    return ts if isinstance(ts, str) else None


def _uuid(entry: dict) -> str:
    uid = entry.get("uuid")
    return str(uid) if uid else ""


def _tool_result_text(content: object) -> str:
    """Flatten a ``tool_result`` block's content to text.

    The content is either a plain string or a list of typed blocks (most
    commonly ``{"type": "text", "text": ...}``); non-text blocks (e.g. images)
    contribute nothing.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def parse_entry(entry: dict) -> list[dict]:
    """Map one JSONL *entry* to zero-or-more structured observe events.

    Returns a list because a single entry may carry several content blocks
    (e.g. an assistant turn with a thinking block, a text block, and a
    ``tool_use`` block produces three events, plus a terminal ``stop`` event).
    Unknown / non-conversational entry types (``system``, ``queue-operation``,
    ``ai-title``, …) yield ``[]`` so the tailer tolerates transcript noise.

    Event kinds (each a JSON dict with at least ``kind``, ``id``, ``ts``):

    * ``user``        — a real user prompt (``text``).
    * ``assistant``   — assistant ``text`` block (``text``).
    * ``thinking``    — assistant ``thinking`` block (``text``); rendered collapsed.
    * ``tool_use``    — a tool call (``tool``, ``args``, ``tool_use_id``).
    * ``tool_result`` — a tool's output (``tool_use_id``, ``text``, ``is_error``).
    * ``stop``        — a terminal assistant turn (``stop_reason``).
    * ``interrupt``   — a turn aborted by the user.
    """
    if not isinstance(entry, dict):
        return []
    etype = entry.get("type")
    uid = _uuid(entry)
    ts = _ts(entry)

    if etype == "user":
        return _parse_user(entry, uid, ts)
    if etype == "assistant":
        return _parse_assistant(entry, uid, ts)
    return []


def _parse_user(entry: dict, uid: str, ts: str | None) -> list[dict]:
    # An interrupted turn is recorded as a user entry whose text starts with the
    # interrupt marker — surface it as its own event, never as a prompt.
    if is_interrupt(entry):
        return [{"kind": "interrupt", "id": f"{uid}#interrupt", "ts": ts}]

    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None

    # A plain-string user message is a genuine prompt/paste.
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return []
        return [{"kind": "user", "id": f"{uid}#0", "ts": ts, "text": content}]

    if not isinstance(content, list):
        return []

    # A list-content user entry is most often *tool output* (blocks of
    # ``tool_result``), not a paste. Emit a tool_result per such block; any
    # genuine text blocks (rare for user entries) surface as a user prompt.
    events: list[dict] = []
    text_parts: list[str] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            events.append(
                {
                    "kind": "tool_result",
                    "id": f"{uid}#{index}",
                    "ts": ts,
                    "tool_use_id": block.get("tool_use_id"),
                    "text": _tool_result_text(block.get("content")),
                    "is_error": bool(block.get("is_error")),
                }
            )
        elif isinstance(block.get("text"), str):
            text_parts.append(block["text"])
    if text_parts:
        events.append(
            {"kind": "user", "id": f"{uid}#text", "ts": ts, "text": "\n".join(text_parts)}
        )
    return events


def _parse_assistant(entry: dict, uid: str, ts: str | None) -> list[dict]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    events: list[dict] = []

    if isinstance(content, str):
        if content.strip():
            events.append({"kind": "assistant", "id": f"{uid}#0", "ts": ts, "text": content})
    elif isinstance(content, list):
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    events.append(
                        {"kind": "assistant", "id": f"{uid}#{index}", "ts": ts, "text": block["text"]}
                    )
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                if block["thinking"].strip():
                    events.append(
                        {"kind": "thinking", "id": f"{uid}#{index}", "ts": ts, "text": block["thinking"]}
                    )
            elif btype == "tool_use":
                events.append(
                    {
                        "kind": "tool_use",
                        "id": f"{uid}#{index}",
                        "ts": ts,
                        "tool": block.get("name"),
                        "args": block.get("input"),
                        "tool_use_id": block.get("id"),
                    }
                )

    # A terminal assistant turn also emits a stop marker so the renderer can draw
    # the turn boundary and stop reason.
    if is_terminal_assistant(entry):
        stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
        events.append(
            {"kind": "stop", "id": f"{uid}#stop", "ts": ts, "stop_reason": stop_reason}
        )
    return events
