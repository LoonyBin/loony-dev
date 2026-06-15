#!/usr/bin/env python3
"""A tiny stand-in for the ``claude`` binary, for ClaudeSession unit tests.

It emulates just enough of the real CLI's PTY + Claude-Code-hook behaviour to
exercise :class:`loony_dev.agents.claude_session.ClaudeSession` without the real
binary. It plays *both* the CLI and the hook scripts the CLI would invoke:

* On startup it (optionally) emits a ``session_start`` event to the per-session
  control socket and writes the session JSONL transcript at the path
  ClaudeSession computes (``$CLAUDE_CONFIG_DIR/projects/<cwd-slug>/<sid>.jsonl``).
  The transcript is still written because ClaudeSession does a bounded
  post-``Stop`` transcript read for quota detection / text backfill, and the
  legacy JSONL event-source path reads it directly.
* It reads the PTY, recognises bracketed-paste prompts (``\\e[200~ … \\e[201~``
  then ``\\r``) and a bare ESC interrupt. For each completed turn it:
    - appends the assistant/interrupt JSONL entry (as the real transcript would);
    - emits a ``pre_tool`` + ``post_tool`` event (so the observe path is covered);
    - emits a ``stop`` event carrying ``text`` and ``interrupted``.
  A prompt containing ``QUOTA`` produces an assistant entry whose text is a real
  usage-limit message; ``LONGTURN`` produces a delayed completion that ESC can
  pre-empt (recording an interrupt).

Behaviour is steered by environment variables:
    STUB_NO_JSONL=1        never create the JSONL transcript.
    STUB_NO_SESSION_START=1 never emit the session_start event (drives the
                            readiness backstop).
    STUB_STARTUP_DELAY     seconds to wait before signalling readiness.
    STUB_LONGTURN_SECS     how long a LONGTURN prompt runs before completing.
"""
from __future__ import annotations

import json
import os
import re
import select
import socket
import sys
import termios
import time
import tty
import uuid


def _slug(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(path))


def _config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _jsonl_path(session_id: str) -> str:
    return os.path.join(_config_dir(), "projects", _slug(os.getcwd()), f"{session_id}.jsonl")


def _socket_path(session_id: str) -> str:
    return os.path.join(_config_dir(), "_loony", "sessions", session_id, "control.sock")


def _emit(session_id: str, event: str, **fields: object) -> None:
    """Connect to the per-session control socket and write one event line.

    Mirrors :func:`loony_dev.agents.session_hooks.run_hook`: best-effort, never
    raise (a missing listener just means no one is waiting).
    """
    payload = {"event": event, "session_id": session_id, "v": 1}
    payload.update({k: v for k, v in fields.items() if v is not None})
    line = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(_socket_path(session_id))
            client.sendall(line)
    except OSError:
        pass


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
    no_jsonl = os.environ.get("STUB_NO_JSONL") == "1"

    startup_delay = float(os.environ.get("STUB_STARTUP_DELAY", "0") or "0")
    if startup_delay:
        time.sleep(startup_delay)
    if not no_jsonl:
        # Mimic the real CLI seeding a transcript before the first prompt.
        _append(path, {"type": "system", "subtype": "init"})

    # SessionStart hook → readiness signal.
    if os.environ.get("STUB_NO_SESSION_START") != "1":
        _emit(session_id, "session_start", source="startup")

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

    def complete_turn(text: str) -> None:
        """Emit the transcript entry + pre/post tool + stop events for a turn."""
        if not no_jsonl:
            _assistant(path, text)
        _emit(session_id, "pre_tool", tool="Bash")
        _emit(session_id, "post_tool", tool="Bash")
        _emit(session_id, "stop", text=text, interrupted=False)

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
                if not no_jsonl:
                    _interrupt(path)
                _emit(session_id, "stop", interrupted=True)
                pending_deadline = None

        # Complete a delayed LONGTURN once its deadline passes.
        if pending_deadline is not None and time.monotonic() >= pending_deadline:
            complete_turn(f"done turn {turn_index}")
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
                # Wording must stay matchable by ClaudeQuotaMixin._is_quota_error
                # (see loony_dev/agents/claude_quota.py); keep them in sync.
                quota_text = "You've hit your limit · resets 7:30pm (Asia/Calcutta)"
                complete_turn(quota_text)
            elif "LONGTURN" in prompt:
                pending_deadline = time.monotonic() + longturn_secs
                # Announce (via the PTY) that the long turn is now running so a
                # test can synchronise before interrupting.  The slave is in raw
                # mode (no input echo), so an explicit marker is needed.
                os.write(sys.stdout.fileno(), b"LONGTURN running\n")
            else:
                complete_turn(f"reply to: {prompt[:80]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
