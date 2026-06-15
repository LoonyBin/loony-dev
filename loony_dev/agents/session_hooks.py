"""Claude Code hook integration for :class:`ClaudeSession` (issue #178).

Instead of polling/parsing the per-session JSONL transcript for lifecycle
transitions, we consume Claude Code's *authoritative* hook events. Claude Code
runs a configured shell command for each lifecycle event and feeds it a JSON
payload on stdin (see https://docs.claude.com/en/docs/claude-code/hooks). This
module:

* defines the small event contract loony-dev owns (the JSON lines a hook writes
  to a per-session control socket — :data:`EVENT_*` / :func:`encode_event`);
* computes the per-session control-socket path (:func:`channel_path`), keyed by
  ``session_id`` and honouring ``CLAUDE_CONFIG_DIR``;
* implements the hook executable itself (:func:`run_hook`), invoked as
  ``loony-dev hook <event>``: it reads the hook payload on stdin, looks up the
  session's socket by the payload's ``session_id``, and writes one event line;
* installs/verifies the hook block in ``~/.claude/settings.json`` idempotently
  (:func:`install_hooks` / :func:`verify_hooks`), mirroring the managed-marker
  read-modify-write pattern of :mod:`loony_dev.commands`.

The bet (vs. JSONL-shape coupling): hook payloads change less often and break
*louder* — if Claude Code stops firing the hook, the worker's backstop trips and
``verify_hooks`` refuses to start, rather than silently mis-parsing.

Schema confirmed against Claude Code ``2.1.177`` (binary Zod definitions). Base
payload (all events): ``session_id``, ``transcript_path``, ``cwd``,
``permission_mode?``. ``SessionStart`` adds ``source``. ``Stop`` adds
``stop_hook_active`` and ``last_assistant_message?`` (the assistant text — lets
us populate ``TurnResult.text`` without a transcript parse). ``PreToolUse`` /
``PostToolUse`` add ``tool_name`` / ``tool_input`` (+ ``tool_response`` on post).
The ``Stop`` payload carries no native interrupt flag, so the hook reads the
transcript tail once for the ``[Request interrupted by user]`` marker.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event contract (the JSON lines a hook writes to the session control socket).
# Versioned so a future schema change is detectable; we own this surface.
# ---------------------------------------------------------------------------

EVENT_VERSION = 1

EVENT_SESSION_START = "session_start"
EVENT_STOP = "stop"
EVENT_PRE_TOOL = "pre_tool"
EVENT_POST_TOOL = "post_tool"

# Claude Code hook event names → our event names. A single installed hookset
# serves every session; the hook routes to the per-session socket by the
# payload's ``session_id``.
HOOK_EVENT_NAMES: dict[str, str] = {
    "SessionStart": EVENT_SESSION_START,
    "Stop": EVENT_STOP,
    "PreToolUse": EVENT_PRE_TOOL,
    "PostToolUse": EVENT_POST_TOOL,
}

# Canonical text Claude records (as a ``user`` transcript entry) when a turn is
# interrupted with ESC. Matched as a prefix (Claude appends context, e.g.
# "[Request interrupted by user for tool use]"). The ``Stop`` payload carries no
# native interrupt flag, so the hook derives ``interrupted`` from the transcript
# tail. Kept here (not imported from claude_session) to keep the hook executable
# import-light.
INTERRUPT_PREFIX = "[Request interrupted by user"

# Managed-marker for the settings.json hook block, mirroring
# :data:`loony_dev.commands.MANAGED_MARKER`. We only ever (re)write hook entries
# whose command carries this token, so hand-authored hooks are never clobbered.
MANAGED_TOKEN = "loony-dev:hook"

# Bytes of a hook event line; events are tiny.
_RECV_BYTES = 64 * 1024
_CONNECT_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _claude_config_dir() -> Path:
    """Return the Claude config root (honours ``CLAUDE_CONFIG_DIR``)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def sessions_root(config_dir: Path | None = None) -> Path:
    """Return the directory under which per-session control sockets live."""
    base = config_dir if config_dir is not None else _claude_config_dir()
    return base / "_loony" / "sessions"


def channel_path(session_id: str, config_dir: Path | None = None) -> Path:
    """Return the per-session control-socket path for *session_id*.

    Keyed by ``session_id`` (not cwd) so a single installed hookset routes every
    session's events to the right socket; honours ``CLAUDE_CONFIG_DIR`` so tests
    and isolated workers do not collide.
    """
    return sessions_root(config_dir) / session_id / "control.sock"


# ---------------------------------------------------------------------------
# Encode / decode the event line
# ---------------------------------------------------------------------------

def encode_event(event: str, session_id: str, **fields: object) -> bytes:
    """Encode one event as a single newline-terminated JSON line."""
    payload: dict[str, object] = {"event": event, "session_id": session_id, "v": EVENT_VERSION}
    payload.update({k: v for k, v in fields.items() if v is not None})
    return (json.dumps(payload) + "\n").encode("utf-8")


def decode_event(line: bytes) -> dict | None:
    """Decode one event line, or ``None`` if it is not a valid event object."""
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "event" in obj:
        return obj
    return None


# ---------------------------------------------------------------------------
# settings.json hook block (install / verify)
# ---------------------------------------------------------------------------

def hook_command(event_name: str) -> str:
    """Return the shell command settings.json runs for *event_name*.

    Carries :data:`MANAGED_TOKEN` (as a trailing comment-style arg the CLI
    ignores) so :func:`install_hooks` can recognise and upgrade its own entries
    without touching hand-authored ones.
    """
    # ``loony-dev hook <event>`` invokes :func:`run_hook`. The token is appended
    # as an extra positional that ``run_hook`` ignores; it makes the managed
    # entry self-identifying in settings.json.
    return f"loony-dev hook {event_name} #{MANAGED_TOKEN}"


def desired_settings_hooks() -> dict:
    """Return the ``hooks`` block loony-dev wants merged into settings.json."""
    hooks: dict[str, list] = {}
    for hook_event in HOOK_EVENT_NAMES:
        entry: dict = {"hooks": [{"type": "command", "command": hook_command(hook_event)}]}
        # PreToolUse / PostToolUse take a matcher; "*" matches every tool.
        if hook_event in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = "*"
        hooks[hook_event] = [entry]
    return hooks


def _is_managed_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    for spec in entry.get("hooks", []):
        if isinstance(spec, dict) and MANAGED_TOKEN in str(spec.get("command", "")):
            return True
    return False


def install_hooks(config_dir: Path | None = None) -> bool:
    """Idempotently merge loony-dev's hooks into ``<config>/settings.json``.

    Read-modify-write under ``flock`` (mirrors :func:`trust_directory` and
    :func:`loony_dev.commands.install_commands`): for each required hook event we
    drop any prior *managed* entry of ours and install the current one, while
    preserving every hand-authored hook entry. Returns ``True`` if the file was
    changed (or created), ``False`` if it was already up to date.
    """
    base = config_dir if config_dir is not None else _claude_config_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / "settings.json"
    desired = desired_settings_hooks()

    # ``open(..., "a+")`` creates the file if missing without truncating, and we
    # flock it so concurrent workers/`setup` runs serialise their read-modify-write.
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                logger.warning("settings.json at %s is not valid JSON; leaving it alone", path)
                return False
            if not isinstance(data, dict):
                data = {}

            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                hooks = {}

            changed = False
            for hook_event, desired_entries in desired.items():
                existing = hooks.get(hook_event)
                existing = existing if isinstance(existing, list) else []
                # Keep hand-authored entries; drop our own prior managed entries.
                preserved = [e for e in existing if not _is_managed_entry(e)]
                new_list = preserved + desired_entries
                if hooks.get(hook_event) != new_list:
                    hooks[hook_event] = new_list
                    changed = True

            if not changed:
                return False

            data["hooks"] = hooks
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=2)
            fh.write("\n")
            logger.info("Installed/updated loony-dev Claude Code hooks in %s", path)
            return True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def verify_hooks(config_dir: Path | None = None) -> tuple[bool, str]:
    """Return ``(ok, reason)`` confirming every required hook is installed.

    ``ok`` is ``True`` only if ``settings.json`` exists, parses, and maps each
    required Claude Code event to our current managed hook command. Otherwise
    ``reason`` names what is missing/stale so the worker can refuse to start with
    a clear message.
    """
    base = config_dir if config_dir is not None else _claude_config_dir()
    path = base / "settings.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, f"{path} does not exist"
    except OSError as exc:
        return False, f"could not read {path}: {exc}"

    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return False, f"{path} is not valid JSON"
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False, f"{path} has no hooks block"

    for hook_event in HOOK_EVENT_NAMES:
        entries = hooks.get(hook_event)
        if not isinstance(entries, list):
            return False, f"missing hook for {hook_event}"
        expected = hook_command(hook_event)
        found = any(
            isinstance(e, dict)
            and any(
                isinstance(s, dict) and s.get("command") == expected
                for s in e.get("hooks", [])
            )
            for e in entries
        )
        if not found:
            return False, f"hook for {hook_event} is missing or stale"
    return True, "ok"


# ---------------------------------------------------------------------------
# The hook executable (invoked as ``loony-dev hook <event>``)
# ---------------------------------------------------------------------------

def _transcript_was_interrupted(transcript_path: str | None) -> bool:
    """Return True if the transcript's last user entry is an interrupt marker.

    The ``Stop`` payload carries no native interrupt flag, so we read the
    transcript *once* (a contained, single read inside the hook — not polling)
    and look for the ``[Request interrupted by user]`` marker on the final
    user entry. Best-effort: any error means "not interrupted".
    """
    if not transcript_path:
        return False
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            # Only consider the most recent *user* entry; a trailing assistant
            # or system entry means the turn completed normally.
            return False
        return _entry_starts_with_interrupt(entry)
    return False


def _entry_starts_with_interrupt(entry: dict) -> bool:
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text = block["text"]
                break
    return text.lstrip().startswith(INTERRUPT_PREFIX)


def run_hook(argv: list[str], stdin_text: str) -> int:
    """Entry point for ``loony-dev hook <event>``.

    Reads the Claude Code hook payload (*stdin_text*), maps the Claude event name
    to our event, looks up the session's control socket by the payload's
    ``session_id``, and writes one event line. Always exits 0 — a hook must never
    block or fail Claude; if the socket is gone the worker's backstop covers it.
    """
    hook_event = argv[0] if argv else ""
    event = HOOK_EVENT_NAMES.get(hook_event)
    if event is None:
        return 0

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    session_id = payload.get("session_id")
    if not session_id:
        return 0

    fields: dict[str, object] = {}
    if event == EVENT_SESSION_START:
        fields["source"] = payload.get("source")
    elif event == EVENT_STOP:
        fields["text"] = payload.get("last_assistant_message")
        fields["interrupted"] = _transcript_was_interrupted(payload.get("transcript_path"))
        fields["transcript_path"] = payload.get("transcript_path")
    elif event in (EVENT_PRE_TOOL, EVENT_POST_TOOL):
        fields["tool"] = payload.get("tool_name")

    line = encode_event(event, session_id, **fields)

    # Hooks inherit ``CLAUDE_CONFIG_DIR`` from the claude process, so the socket
    # path the hook computes matches the one the session bound.
    sock_path = channel_path(str(session_id))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_CONNECT_TIMEOUT)
            client.connect(str(sock_path))
            client.sendall(line)
    except OSError:
        # No listener (session not loony-managed, or already closed) — drop it.
        pass
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin shim
    args = list(sys.argv[1:] if argv is None else argv)
    return run_hook(args, sys.stdin.read())
