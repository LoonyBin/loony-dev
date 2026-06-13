#!/usr/bin/env python3
"""A tiny stand-in for the ``claude`` binary, for ClaudeSession unit tests.

It emulates just enough of the real CLI's PTY + JSONL behaviour to exercise
:class:`loony_dev.agents.claude_session.ClaudeSession` without the real binary:

* On startup it writes its session JSONL at the same path ClaudeSession
  computes (``$CLAUDE_CONFIG_DIR/projects/<cwd-slug>/<session-id>.jsonl``),
  honouring ``CLAUDE_CONFIG_DIR`` so tests can redirect it to a tmp dir.
* It reads the PTY, recognises bracketed-paste prompts (``\\e[200~ … \\e[201~``
  then ``\\r``) and a bare ESC interrupt, and appends JSONL entries:
    - a normal prompt → an ``assistant`` entry with ``stop_reason=end_turn``;
    - a prompt containing ``QUOTA`` → an ``assistant`` entry whose text is a
      rate-limit message;
    - a prompt containing ``LONGTURN`` → a delayed ``end_turn`` that can be
      pre-empted by ESC, which instead records a ``user`` interrupt entry.

Behaviour is steered by environment variables:
    STUB_NO_JSONL=1     never create the JSONL (drives the readiness timeout).
    STUB_STARTUP_DELAY  seconds to wait before creating the JSONL.
    STUB_LONGTURN_SECS  how long a LONGTURN prompt runs before completing.
"""
from __future__ import annotations

import json
import os
import re
import select
import sys
import termios
import time
import tty
import uuid


def _slug(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(path))


def _jsonl_path(session_id: str) -> str:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(config_dir, "projects", _slug(os.getcwd()), f"{session_id}.jsonl")


def _append(path: str, entry: dict) -> None:
    entry.setdefault("uuid", str(uuid.uuid4()))
    entry.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
        fh.flush()


def _assistant(path: str, text: str, stop_reason: str = "end_turn") -> None:
    _append(path, {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
        },
    })


def _interrupt(path: str) -> None:
    _append(path, {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "[Request interrupted by user]"}],
        },
    })


def main() -> int:
    argv = sys.argv[1:]
    session_id = None
    for i, arg in enumerate(argv):
        if arg == "--session-id" and i + 1 < len(argv):
            session_id = argv[i + 1]
    if session_id is None:
        session_id = str(uuid.uuid4())

    path = _jsonl_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    startup_delay = float(os.environ.get("STUB_STARTUP_DELAY", "0") or "0")
    if startup_delay:
        time.sleep(startup_delay)
    if os.environ.get("STUB_NO_JSONL") != "1":
        # Mimic the real CLI seeding a transcript before the first prompt.
        _append(path, {"type": "system", "subtype": "init"})

    longturn_secs = float(os.environ.get("STUB_LONGTURN_SECS", "3") or "3")

    # The real CLI puts the tty in raw mode so a bare ESC (interrupt) is
    # delivered immediately rather than buffered until the next newline.
    try:
        tty.setraw(sys.stdin.fileno())
    except (termios.error, ValueError):
        pass

    buf = bytearray()
    pending_deadline: float | None = None  # set while a LONGTURN is "running"
    turn_index = 0

    while True:
        timeout = 0.05 if pending_deadline is not None else 1.0
        rlist, _, _ = select.select([sys.stdin.buffer], [], [], timeout)
        if rlist:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            buf += chunk

        # Bare ESC (not part of a paste marker) interrupts a running turn.
        if b"\x1b" in buf and b"\x1b[200~" not in buf and b"\x1b[201~" not in buf:
            buf = bytearray(buf.replace(b"\x1b", b""))
            if pending_deadline is not None:
                _interrupt(path)
                pending_deadline = None

        # Complete a delayed LONGTURN once its deadline passes.
        if pending_deadline is not None and time.monotonic() >= pending_deadline:
            _assistant(path, f"done turn {turn_index}")
            pending_deadline = None

        # Process any complete bracketed-paste prompt(s).
        while b"\x1b[200~" in buf and b"\x1b[201~" in buf:
            start = buf.index(b"\x1b[200~") + len(b"\x1b[200~")
            end = buf.index(b"\x1b[201~")
            if end < start:
                break
            prompt = bytes(buf[start:end]).decode("utf-8", "replace")
            # Drop everything up to and including the trailing CR after the paste.
            tail = buf.index(b"\x1b[201~") + len(b"\x1b[201~")
            cr = buf.find(b"\r", tail)
            buf = bytearray(buf[cr + 1:] if cr != -1 else buf[tail:])

            turn_index += 1
            if "QUOTA" in prompt:
                _assistant(path, "You've hit your limit · resets 7:30pm (Asia/Calcutta)")
            elif "LONGTURN" in prompt:
                pending_deadline = time.monotonic() + longturn_secs
            else:
                _assistant(path, f"reply to: {prompt[:80]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
