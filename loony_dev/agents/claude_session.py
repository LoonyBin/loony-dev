"""Persistent PTY-backed Claude session.

Instead of spawning a fresh ``claude -p`` subprocess per turn (see
:mod:`loony_dev.agents.claude_quota`), this drives a *single* interactive
``claude`` process over a pseudo-terminal.  The bot owns the PTY master and
writes keystrokes (bracketed-paste prompts, ESC to interrupt).

Lifecycle transitions (startup readiness, turn completion, interrupt, tool
calls) are driven by **Claude Code hook events** delivered over a per-session
Unix-domain control socket (issue #178), not by polling/parsing the JSONL
transcript:

* ``SessionStart`` → readiness signal (replaces the startup-grace poll).
* ``Stop`` → turn completion, carrying the assistant text and an interrupt flag.
* ``PreToolUse`` / ``PostToolUse`` → tool-activity events for the dashboard
  observe path (consumed by :class:`~loony_dev.agents.session_bridge.SessionBridge`).

The event listener is bound **before** ``pty.fork()`` so a ``SessionStart``
firing immediately after launch always has a socket to connect to. A long
*backstop* timeout on ``open`` / ``send_turn`` is a liveness net only (for a CLI
that crashed before firing a hook) — not the primary signal.

A :class:`SessionEventSource` seam keeps the legacy JSONL path selectable for
one release via the ``[worker] session_events`` config (default ``"hooks"``);
see :mod:`loony_dev.agents.session_hooks` for the hook install/verify/executable.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import queue
import re
import signal
import socket
import struct
import termios
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loony_dev.agents import session_hooks
from loony_dev.agents.claude_quota import ClaudeQuotaMixin

logger = logging.getLogger(__name__)

# Assistant ``stop_reason`` values that mark a normally-completed turn.  Other
# values (notably ``tool_use``) are mid-turn and must not be treated as done.
TERMINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

# Canonical text Claude records (as a ``user`` JSONL entry) when a turn is
# interrupted with ESC.  Matched as a prefix because Claude appends context
# (e.g. "[Request interrupted by user for tool use]").
INTERRUPT_PREFIX = "[Request interrupted by user"

# Bracketed-paste control sequences — let us inject a multi-line prompt as a
# single paste event rather than line-by-line keystrokes.
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_ESC = b"\x1b"

_DEFAULT_COLS = 120
_DEFAULT_ROWS = 40

# How long (s) to keep reading the JSONL after the terminal entry appears, so
# trailing tool/system/file-history-snapshot flushes are captured in full. Used
# by the legacy :class:`JsonlEventSource` path only.
_DEFAULT_DEBOUNCE = 1.0
_POLL_INTERVAL = 0.1
# Backstop (s) for ``open`` (await ``SessionStart``) and ``send_turn`` (await
# ``Stop``). This is a *liveness net only* — the authoritative signal is the
# hook event; the backstop merely stops a crashed CLI (one that exits before
# firing the hook) from hanging the worker forever. It must therefore be large.
# Override via the ``claude_session_backstop_seconds`` key under ``[worker]``.
# Callers MUST pre-trust the cwd (see ``trust_directory``) or interactive
# ``claude`` blocks on the folder-trust dialog and never fires ``SessionStart``.
_DEFAULT_BACKSTOP = 600.0
# Gap between writing the bracketed-paste block and the Enter that submits it.
# Sending them in one write lets the trailing CR be absorbed as a literal
# newline inside the (multi-line) input rather than submitting the turn.
_SUBMIT_DELAY = 0.1
# Bytes of recent PTY output retained for a dashboard relay to read back.
_DEFAULT_RING_BYTES = 256 * 1024
# Max chunks buffered per live attach subscriber before the oldest is dropped.
# A slow/parked dashboard client must never back-pressure the drain thread (that
# would wedge the bot's turn), so the queue is bounded and lossy by design — a
# terminal repaints itself, so a dropped chunk at worst causes a transient gap.
_SUBSCRIBER_QUEUE_MAX = 1024

# Result of :meth:`ClaudeSession.operator_write` — distinguishes a write that
# went to the PTY, an ESC routed to ``interrupt`` while the bot held the mic, and
# input refused because the bot owns the channel mid-turn.
OPERATOR_WRITTEN = "written"
OPERATOR_INTERRUPTED = "interrupted"
OPERATOR_REFUSED = "refused"

# Control-channel (Unix-socket) tunables. The optional control socket lets a
# *separate* process (the web dashboard, which does not own this session's PTY)
# ask the session to interrupt its in-flight turn — see ``control_socket``.
_CONTROL_BACKLOG = 8
_CONTROL_ACCEPT_TIMEOUT = 0.5  # accept() wake-up period so the loop sees _closed
_CONTROL_RECV_BYTES = 256
# One-line command the control socket understands.
_CONTROL_INTERRUPT = "interrupt"

# Event-listener (inbound hook channel) tunables. Hooks connect to the
# per-session socket, write one event line, and disconnect, so connections are
# short-lived; the accept timeout lets the loop notice ``_closed`` on shutdown.
_EVENT_BACKLOG = 16
_EVENT_ACCEPT_TIMEOUT = 0.5
_EVENT_RECV_TIMEOUT = 2.0
_EVENT_RECV_BYTES = 64 * 1024

# Selectable inbound event source (migration seam, #178). "hooks" is the new
# hook-driven control-channel path; "jsonl" is the legacy poll/parse fallback.
SESSION_EVENTS_HOOKS = "hooks"
SESSION_EVENTS_JSONL = "jsonl"


class ClaudeSessionError(Exception):
    """Base class for errors raised by :class:`ClaudeSession`."""


class ReadinessTimeout(ClaudeSessionError):
    """Raised when no ``SessionStart`` event arrives within the backstop window.

    The backstop is a liveness net only — this signals the CLI likely died
    before firing the hook, not a slow-but-healthy startup.
    """


class TurnTimeout(ClaudeSessionError):
    """Raised when a turn produces no output for the whole backstop window.

    The backstop is an *idle/liveness* net, not a total-time cap: it resets on
    any turn activity (the transcript file growing) and only trips after a full
    ``backstop`` window with no activity *and* no terminal entry in the
    transcript — i.e. a genuinely stalled CLI, not a long-but-productive turn.

    The message is intentionally stable (no volatile ``session_id``) so that the
    repeated-failure → ``in-error`` dedup, which compares normalised failure
    comment bodies, can match identical stalls and escalate instead of looping.
    The session id is logged separately via :data:`logger`.
    """


class TurnInterrupted(ClaudeSessionError):
    """Raised (optionally) when an in-flight turn is interrupted via ESC."""


class QuotaExceededError(ClaudeSessionError):
    """Raised from ``send_turn`` when a genuine Claude usage-limit error appears.

    Detected by a bounded post-``Stop`` transcript read (the ``Stop`` event is
    the authoritative *signal*; the read pulls the assistant content the signal
    refers to). Carries the offending text in ``output`` so callers can reuse
    :meth:`ClaudeQuotaMixin._handle_quota_error` to parse the reset time.
    """

    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


@dataclass
class TurnResult:
    """Outcome of a single :meth:`ClaudeSession.send_turn`."""

    text: str
    stop_reason: str | None
    was_interrupted: bool
    entries_added: int


def _project_slug(cwd: Path) -> str:
    """Return Claude's transcript-directory slug for *cwd*.

    Claude replaces every non-alphanumeric character of the absolute working
    directory with ``-`` (e.g. ``/home/u/loony-dev`` →
    ``-home-u-loony-dev``).
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(str(cwd)))


def _claude_config_dir() -> Path:
    """Return the Claude config root (honours ``CLAUDE_CONFIG_DIR``)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def jsonl_path_for(cwd: Path, session_id: str) -> Path:
    """Compute the JSONL transcript path for *session_id* run in *cwd*."""
    return _claude_config_dir() / "projects" / _project_slug(cwd) / f"{session_id}.jsonl"


def _claude_json_path() -> Path:
    """Return the path to Claude's ``.claude.json`` config file.

    When ``CLAUDE_CONFIG_DIR`` is set the file lives inside it; otherwise it is
    ``~/.claude.json`` (note: a sibling of the ``~/.claude`` config *dir*, not
    inside it).
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(override) / ".claude.json") if override else Path.home() / ".claude.json"


def trust_directory(cwd: Path) -> bool:
    """Mark *cwd* as a trusted workspace so interactive ``claude`` skips its
    folder-trust dialog ("Is this a project you trust?").

    Interactive ``claude`` blocks on that dialog for any directory it has not
    recorded as trusted, and ``--dangerously-skip-permissions`` does NOT bypass
    it (only non-interactive ``-p`` mode does). Freshly-created worktrees are
    always new, untrusted paths, so a ``ClaudeSession`` launched in one would
    hang at the prompt and never start. This sets
    ``projects.<cwd>.hasTrustDialogAccepted = true`` in ``~/.claude.json``.

    Idempotent and lock-guarded (``flock``) so concurrent workers do not clobber
    each other. Returns ``True`` if the directory is trusted afterwards, ``False``
    if the config could not be updated (logged, non-fatal).
    """
    key = os.path.abspath(str(cwd))
    path = _claude_json_path()
    try:
        with open(path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                try:
                    data = json.load(fh)
                except json.JSONDecodeError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                projects = data.setdefault("projects", {})
                entry = projects.setdefault(key, {})
                if entry.get("hasTrustDialogAccepted") is True:
                    return True
                entry["hasTrustDialogAccepted"] = True
                fh.seek(0)
                json.dump(data, fh, indent=2)
                fh.truncate()
                return True
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except FileNotFoundError:
        logger.warning(
            "Cannot pre-trust %s: %s does not exist (run claude once first)", cwd, path,
        )
    except OSError as exc:
        logger.warning("Failed to pre-trust %s in %s: %s", cwd, path, exc)
    return False


def _entry_text(entry: dict) -> str:
    """Extract all human-readable text from a JSONL *entry*.

    Handles the two content shapes seen in transcripts: a plain string, or a
    list of typed blocks (``text`` / ``thinking`` carry text; ``tool_use`` etc.
    are skipped).
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


def _is_terminal_assistant(entry: dict) -> bool:
    if entry.get("type") != "assistant":
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    return message.get("stop_reason") in TERMINAL_STOP_REASONS


def _is_interrupt(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    return _entry_text(entry).lstrip().startswith(INTERRUPT_PREFIX)


class _JsonlTailer:
    """Stateful incremental reader of a JSONL file.

    Remembers a byte offset so each poll only parses freshly appended data, and
    tolerates a partial final line (a write caught mid-flush) by buffering it
    until the rest arrives.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._buf = b""

    def read_new(self) -> list[dict]:
        """Return entries appended since the last call (oldest first)."""
        try:
            with open(self._path, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        if not data:
            return []
        self._offset += len(data)
        self._buf += data

        *complete, self._buf = self._buf.split(b"\n")
        entries: list[dict] = []
        for raw in complete:
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # A complete (newline-terminated) line should parse; if it does
                # not, skip it rather than wedging the tailer.
                logger.debug("Skipping unparseable JSONL line in %s", self._path)
        return entries


class SessionEventSource:
    """Strategy for how a :class:`ClaudeSession` learns of lifecycle events.

    Two implementations exist for the #178 migration: :class:`HookEventSource`
    (the new hook-driven control channel) and :class:`JsonlEventSource` (the
    legacy poll/parse fallback, kept selectable for one release). Each owns the
    "wait for readiness" and "wait for turn completion" semantics; the session
    delegates ``open`` / ``send_turn`` to it.
    """

    def bind(self, session: "ClaudeSession") -> None:
        """Set up any resources needed *before* the child is forked."""

    def started(self, session: "ClaudeSession") -> None:
        """Notify that the child has been forked (parent side)."""

    def await_ready(self, session: "ClaudeSession", *, backstop: float) -> None:
        """Block until the session is ready, or raise :class:`ReadinessTimeout`."""
        raise NotImplementedError

    def run_turn(
        self, session: "ClaudeSession", prompt: str, *, backstop: float,
    ) -> "TurnResult":
        """Inject *prompt* and block until the turn completes."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources (idempotent)."""


class HookEventSource(SessionEventSource):
    """Hook-driven event source (#178).

    Binds a per-session Unix-domain stream socket *before* the child is forked,
    so a ``SessionStart`` hook firing immediately after launch always has a
    listener. A background thread accepts short-lived hook connections, decodes
    one event line per connection, and routes it:

    * ``session_start`` → sets the readiness :class:`threading.Event`;
    * ``stop`` → enqueued on the turn-completion queue;
    * ``pre_tool`` / ``post_tool`` → fanned out to tool observers.

    The ``Stop`` event carries the assistant text (``last_assistant_message``)
    and an interrupt flag derived (inside the hook) from the transcript tail, so
    ``run_turn`` needs no JSONL parse for the *signal*. It still does a bounded
    post-``Stop`` transcript read to detect a genuine quota/usage-limit error
    (the addendum's chosen approach) and to backfill ``text`` if the payload
    omitted it.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._path: Path | None = None
        self._ready = threading.Event()
        self._stops: "queue.Queue[dict]" = queue.Queue()
        self._closed = threading.Event()
        self._session: ClaudeSession | None = None

    # -- lifecycle ------------------------------------------------------

    def bind(self, session: "ClaudeSession") -> None:
        self._session = session
        path = session_hooks.channel_path(session.session_id)
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_socket():
            path.unlink()
        elif path.exists():
            raise ClaudeSessionError(
                f"event-channel path {path} exists and is not a socket",
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.listen(_EVENT_BACKLOG)
        sock.settimeout(_EVENT_ACCEPT_TIMEOUT)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._accept_loop,
            name=f"claude-events-{session.session_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _accept_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._closed.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(_EVENT_RECV_TIMEOUT)
                    data = conn.recv(_EVENT_RECV_BYTES)
                except OSError:
                    continue
            self._dispatch(data)

    def _dispatch(self, data: bytes) -> None:
        # One connection may carry one line; split defensively in case a hook
        # batches more than one.
        for raw in data.split(b"\n"):
            event = session_hooks.decode_event(raw)
            if event is None:
                continue
            kind = event.get("event")
            if kind == session_hooks.EVENT_SESSION_START:
                self._ready.set()
            elif kind == session_hooks.EVENT_STOP:
                self._stops.put(event)
            elif kind in (session_hooks.EVENT_PRE_TOOL, session_hooks.EVENT_POST_TOOL):
                if self._session is not None:
                    self._session._notify_tool_observers(event)

    def await_ready(self, session: "ClaudeSession", *, backstop: float) -> None:
        deadline = time.monotonic() + backstop
        while True:
            if self._ready.wait(timeout=min(_POLL_INTERVAL, backstop)):
                return
            if session._closed.is_set():
                raise ClaudeSessionError("session process exited during startup")
            if time.monotonic() >= deadline:
                raise ReadinessTimeout(
                    f"no SessionStart hook within {backstop:.0f}s for session "
                    f"{session.session_id} — CLI process likely dead",
                )

    def run_turn(
        self, session: "ClaudeSession", prompt: str, *, backstop: float,
    ) -> "TurnResult":
        # Drain stale Stop events so a previous turn's completion is not
        # mistaken for this one (mirrors the JSONL tailer's discard at turn
        # start; correlation is by sequencing under the per-turn lock).
        self._drain_stops()
        # Remember where the transcript ends *before* this turn so the missed-Stop
        # fallback only inspects entries this turn appended — otherwise a later
        # stalled turn could reuse a previous turn's terminal entry (the session
        # is persistent and the transcript accumulates across turns).
        try:
            turn_start_offset = session._jsonl_path.stat().st_size
        except FileNotFoundError:
            turn_start_offset = 0
        session._inject_prompt(prompt)

        # The backstop is an *idle* window, not a total cap: an actively-working
        # turn (transcript still growing) keeps resetting the deadline, so it
        # never times out. The deadline only advances toward expiry while the CLI
        # is silent. ``_activity_marker`` summarises the transcript's size+mtime;
        # any change means the turn is alive.
        deadline = time.monotonic() + backstop
        last_marker = self._activity_marker(session)
        while True:
            try:
                event = self._stops.get(timeout=min(_POLL_INTERVAL, backstop))
                break
            except queue.Empty:
                if session._closed.is_set():
                    # The PTY closed. A Stop may have raced in just before EOF
                    # (turn completed, then the process exited) — honour it
                    # rather than reporting a spurious mid-turn exit.
                    try:
                        event = self._stops.get_nowait()
                        break
                    except queue.Empty:
                        raise ClaudeSessionError("session process exited mid-turn")
                # Liveness check: if the transcript grew since the last poll the
                # CLI is productive, so push the idle deadline out. Only a fully
                # silent ``backstop`` window (no growth) lets the deadline lapse.
                marker = self._activity_marker(session)
                if marker != last_marker:
                    last_marker = marker
                    deadline = time.monotonic() + backstop
                    continue
                if time.monotonic() >= deadline:
                    # No Stop event and no transcript growth for the whole idle
                    # window. Before declaring failure, fall back to the
                    # transcript: a *completed* turn whose ``Stop`` hook was
                    # missed leaves a terminal assistant entry. If we find one,
                    # synthesise a normal completion; only a truly silent CLI
                    # (no terminal entry) is a real stall.
                    fallback = self._fallback_from_transcript(
                        session, start_offset=turn_start_offset,
                    )
                    if fallback is not None:
                        logger.warning(
                            "No Stop hook within %.0fs for session %s; recovered a "
                            "completed turn from the transcript (missed Stop).",
                            backstop, session.session_id,
                        )
                        return fallback
                    logger.warning(
                        "Turn stalled: no output for %.0fs and no terminal "
                        "transcript entry for session %s.",
                        backstop, session.session_id,
                    )
                    raise TurnTimeout(
                        f"no turn output for {backstop:.0f}s — Claude CLI appears stalled",
                    )

        was_interrupted = bool(event.get("interrupted"))
        text = event.get("text") or ""
        transcript_path = event.get("transcript_path")

        # Bounded post-Stop transcript read: detect a genuine usage-limit error
        # (the addendum's chosen quota path) and backfill assistant text when the
        # Stop payload omitted ``last_assistant_message`` (e.g. a tool-only turn).
        quota_text = session._scan_transcript_after_stop(transcript_path, want_text=not text)
        if quota_text is not None and quota_text.quota_output is not None:
            raise QuotaExceededError(quota_text.quota_output)
        if not text and quota_text is not None and quota_text.assistant_text:
            text = quota_text.assistant_text

        # The real ``Stop`` hook carries no ``stop_reason`` (only
        # ``stop_hook_active``); a Stop firing without an interrupt *means* the
        # turn ended normally, so default to "end_turn". An interrupt is derived
        # from the transcript tail (inside the hook) → "interrupted".
        if was_interrupted:
            stop_reason: str | None = "interrupted"
        else:
            stop_reason = event.get("stop_reason") or "end_turn"
        return TurnResult(
            text=text,
            stop_reason=stop_reason,
            was_interrupted=was_interrupted,
            entries_added=0,
        )

    @staticmethod
    def _activity_marker(session: "ClaudeSession") -> tuple[int, int]:
        """Return a cheap (size, mtime_ns) marker for the session transcript.

        Any change between two polls means the turn produced output (Claude
        appends entries continuously while working), so it is alive. Missing
        file → ``(0, 0)`` so a transcript appearing for the first time counts as
        activity. Other OS errors propagate rather than being masked as a stall.
        """
        try:
            st = session._jsonl_path.stat()
        except FileNotFoundError:
            return (0, 0)
        return (st.st_size, st.st_mtime_ns)

    def _fallback_from_transcript(
        self, session: "ClaudeSession", *, start_offset: int,
    ) -> "TurnResult | None":
        """Synthesise a completed :class:`TurnResult` from the transcript, or None.

        Used when the idle window lapses without a ``Stop`` event: if the turn
        actually finished (a terminal assistant entry, or a trailing interrupt
        marker) but the ``Stop`` hook was missed, we recover a normal completion
        instead of falsely reporting a stall. Returns ``None`` when no terminal
        entry exists (a genuine stall mid-turn).

        Only entries appended at/after ``start_offset`` (the transcript size at
        this turn's start) are inspected, so a stalled turn cannot reuse an
        earlier turn's terminal entry from the accumulated transcript.

        Mirrors the ``Stop`` path's shape: quota errors still raise
        :class:`QuotaExceededError`; ``was_interrupted`` is derived from the
        transcript tail exactly as the hook would; text comes from the assistant
        entries via :meth:`_assistant_text`. A missing transcript → ``None``
        (a genuine stall); other OS errors propagate.
        """
        path = session._jsonl_path
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError:
            return None
        raw = raw_bytes[max(0, start_offset):].decode("utf-8", "replace")
        entries: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Find the last terminal marker: a terminal assistant entry (turn ended
        # normally) or an interrupt user entry (turn was aborted).
        terminal: dict | None = None
        for entry in reversed(entries):
            if _is_terminal_assistant(entry) or _is_interrupt(entry):
                terminal = entry
                break
        if terminal is None:
            return None

        # Same quota guard as the Stop path: a usage-limit message in the
        # transcript must surface as QuotaExceededError, not a plain result.
        scan = session._scan_transcript_after_stop(str(path), want_text=True)
        if scan is not None and scan.quota_output is not None:
            raise QuotaExceededError(scan.quota_output)

        was_interrupted = _is_interrupt(terminal)
        stop_reason = "interrupted" if was_interrupted else "end_turn"
        # Text comes from *this turn's* entries only (scoped above), not the
        # whole-transcript ``scan`` which the quota guard uses.
        text = session._assistant_text(entries)
        return TurnResult(
            text=text,
            stop_reason=stop_reason,
            was_interrupted=was_interrupted,
            entries_added=0,
        )

    def _drain_stops(self) -> None:
        try:
            while True:
                self._stops.get_nowait()
        except queue.Empty:
            return

    def close(self) -> None:
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass


class JsonlEventSource(SessionEventSource):
    """Legacy poll/parse event source (pre-#178), kept selectable for one release.

    Wraps the original ``_await_readiness`` grace poll and JSONL tailing
    verbatim. Selected via ``[worker] session_events = "jsonl"``; scheduled for
    deletion once the hook path has soaked.
    """

    def __init__(self, debounce: float) -> None:
        self._debounce = debounce
        self._tailer: _JsonlTailer | None = None

    def bind(self, session: "ClaudeSession") -> None:
        self._tailer = _JsonlTailer(session.jsonl_path)

    def await_ready(self, session: "ClaudeSession", *, backstop: float) -> None:
        # Legacy grace: the JSONL is not written until the first turn, so we wait
        # only up to a short grace (capped by the backstop) for a pre-existing
        # transcript, then proceed — the first turn creates it. Use a modest
        # cap so a healthy fresh session is not delayed by the large backstop.
        grace = min(backstop, 10.0)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if session.jsonl_path.exists():
                return
            if session._closed.is_set():
                raise ClaudeSessionError("session process exited during startup")
            time.sleep(_POLL_INTERVAL)

    def run_turn(
        self, session: "ClaudeSession", prompt: str, *, backstop: float,
    ) -> "TurnResult":
        tailer = self._tailer
        if tailer is None:  # pragma: no cover - bind always runs first
            raise ClaudeSessionError("jsonl source not bound")
        tailer.read_new()  # discard pre-turn entries
        session._inject_prompt(prompt)

        added: list[dict] = []
        terminal: dict | None = None
        deadline = time.monotonic() + backstop
        while time.monotonic() < deadline:
            for entry in tailer.read_new():
                added.append(entry)
                session._check_quota(entry)
                if terminal is None and (
                    _is_terminal_assistant(entry) or _is_interrupt(entry)
                ):
                    terminal = entry
            if terminal is not None:
                break
            if session._closed.is_set():
                raise ClaudeSessionError("session process exited mid-turn")
            time.sleep(_POLL_INTERVAL)
        else:
            raise TurnTimeout(
                f"No terminal entry within {backstop:.0f}s for session "
                f"{session.session_id}",
            )

        time.sleep(self._debounce)
        for entry in tailer.read_new():
            added.append(entry)
            session._check_quota(entry)

        was_interrupted = _is_interrupt(terminal)
        stop_reason = session._stop_reason(terminal, was_interrupted)
        text = session._assistant_text(added)
        return TurnResult(
            text=text,
            stop_reason=stop_reason,
            was_interrupted=was_interrupted,
            entries_added=len(added),
        )


@dataclass
class _TranscriptScan:
    """Result of a bounded post-``Stop`` transcript read."""

    quota_output: str | None
    assistant_text: str


class ClaudeSession:
    """A single, persistent interactive ``claude`` process over a PTY.

    The process is started once with :meth:`open` and reused across turns;
    :meth:`send_turn` injects a prompt and blocks until a ``Stop`` hook event
    arrives.  :meth:`interrupt` sends ESC to abort an in-flight turn without
    killing the process.
    """

    def __init__(
        self,
        cwd: Path,
        session_id: str | None = None,
        *,
        binary: str = "claude",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        log_file: Path | None = None,
        control_socket: Path | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
        debounce: float = _DEFAULT_DEBOUNCE,
        backstop_seconds: float = _DEFAULT_BACKSTOP,
        session_events: str = SESSION_EVENTS_HOOKS,
        ring_bytes: int = _DEFAULT_RING_BYTES,
    ) -> None:
        self.cwd = Path(cwd)
        self.session_id = session_id or str(uuid.uuid4())
        self._binary = binary
        self._extra_args = list(extra_args or [])
        self._env_overrides = dict(env or {})
        self._cols = cols
        self._rows = rows
        self._debounce = debounce
        self._backstop = backstop_seconds
        self._session_events = session_events
        self._ring_bytes = ring_bytes

        self._jsonl_path = jsonl_path_for(self.cwd, self.session_id)
        self._log_path = log_file if log_file is not None else self._default_log_path()
        self._control_socket_path = Path(control_socket) if control_socket is not None else None

        # The inbound event source (migration seam, #178). Defaults to the
        # hook-driven control channel; the legacy JSONL path is selectable.
        if session_events == SESSION_EVENTS_JSONL:
            self._event_source: SessionEventSource = JsonlEventSource(debounce)
        else:
            self._event_source = HookEventSource()

        self._pid: int | None = None
        self._master_fd: int | None = None

        # Tool-activity observers (dashboard observe path). Each callback receives
        # the decoded ``pre_tool`` / ``post_tool`` event dict.
        self._tool_observers: list[Callable[[dict], None]] = []
        self._tool_observers_lock = threading.Lock()

        # One bot turn at a time per session; a future human-attach channel will
        # respect the same lock.
        self._turn_lock = threading.Lock()
        # Turn-in-progress handshake for interrupt(). A separate small lock (not
        # ``_turn_lock``, which is held for the whole turn) makes the "is a turn
        # running?" check atomic with the ESC write, so an ESC cannot be queued
        # between turns.
        self._turn_state_lock = threading.Lock()
        self._turn_in_progress = False

        self._drain_thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._ring = bytearray()
        self._ring_lock = threading.Lock()
        self._log_fh = None
        self._control_sock: socket.socket | None = None
        self._control_thread: threading.Thread | None = None

        # Live attach fan-out: each subscriber gets a bounded queue of raw PTY
        # chunks. Registration is atomic with the ring snapshot (see
        # :meth:`attach_stream`) so an attaching client never loses or duplicates
        # a chunk across the backlog/live boundary.
        self._subscribers: set[queue.Queue[bytes]] = set()
        self._subscribers_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pid(self) -> int:
        if self._pid is None:
            raise ClaudeSessionError("session not open")
        return self._pid

    @property
    def pty_master_fd(self) -> int:
        if self._master_fd is None:
            raise ClaudeSessionError("session not open")
        return self._master_fd

    @property
    def jsonl_path(self) -> Path:
        return self._jsonl_path

    @property
    def is_open(self) -> bool:
        """True while the PTY process is live (open, not yet closed/exited)."""
        return self._pid is not None and not self._closed.is_set()

    @property
    def turn_in_progress(self) -> bool:
        """True while a bot ``send_turn`` holds the mic (the turn lock).

        The dashboard attach channel is read-only whenever this is true: between
        turns the human owns the input, mid-turn only ESC (interrupt) is honoured.
        """
        return self._turn_lock.locked()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Fork ``claude`` onto a PTY and wait for the ``SessionStart`` event.

        The event listener is bound **before** ``pty.fork()`` so a hook firing
        immediately after launch always has a socket to connect to. Readiness is
        the ``SessionStart`` hook event; the backstop is a liveness net only.
        """
        if self._pid is not None:
            raise ClaudeSessionError("session already open")

        if self._log_path is not None:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self._log_path, "ab")
            except OSError as exc:
                logger.debug("Could not open session log %s: %s", self._log_path, exc)
                self._log_fh = None

        # Bind the inbound event source BEFORE forking the child (mirrors the
        # control-listener pattern) so an immediate SessionStart is never missed.
        self._event_source.bind(self)

        cmd = [
            self._binary,
            "--dangerously-skip-permissions",
            "--session-id",
            self.session_id,
        ]
        # Hook-driven sessions carry loony-dev's lifecycle hooks via a
        # per-session ``--settings`` payload, scoping them to this session only
        # (never the operator's own ``claude`` runs); the legacy JSONL source
        # needs no hooks. See :mod:`loony_dev.agents.session_hooks`.
        if self._session_events != SESSION_EVENTS_JSONL:
            cmd += ["--settings", session_hooks.session_settings_json()]
        cmd += self._extra_args
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.update(self._env_overrides)

        pid, master_fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.cwd)
                os.execvpe(self._binary, cmd, env)
            except Exception:  # pragma: no cover - child cannot log normally
                os._exit(127)

        # parent
        self._pid = pid
        self._master_fd = master_fd
        self._set_winsize(master_fd, self._rows, self._cols)

        self._drain_thread = threading.Thread(
            target=self._drain_loop, name=f"claude-drain-{self.session_id[:8]}", daemon=True,
        )
        self._drain_thread.start()

        if self._control_socket_path is not None:
            self._start_control_listener()

        self._event_source.started(self)
        self._event_source.await_ready(self, backstop=self._backstop)
        logger.info(
            "ClaudeSession ready (pid=%d, session=%s, events=%s)",
            self._pid, self.session_id, self._session_events,
        )

    def send_turn(self, prompt: str, *, timeout: float) -> TurnResult:
        """Inject *prompt* and block until the turn's ``Stop`` event arrives.

        Returns a :class:`TurnResult`.  Raises :class:`QuotaExceededError` if a
        genuine usage-limit error is detected, or :class:`TurnTimeout` if no
        ``Stop`` event arrives within *timeout* seconds (a liveness backstop).

        *timeout* is the backstop window for this turn; the authoritative signal
        is the ``Stop`` hook event, not the timeout.
        """
        if self._pid is None:
            raise ClaudeSessionError("session not open")

        with self._turn_lock:
            # Mark the turn live so interrupt() may send ESC; the state lock
            # makes the flag flip atomic with interrupt()'s check + ESC write.
            with self._turn_state_lock:
                self._turn_in_progress = True
            try:
                return self._event_source.run_turn(self, prompt, backstop=timeout)
            finally:
                with self._turn_state_lock:
                    self._turn_in_progress = False

    def interrupt(self) -> bool:
        """Send ESC to abort an in-flight turn (process survives).

        Returns ``True`` if ESC was written (a turn was running), ``False``
        otherwise. No-op when no turn is running: a stray ESC written between
        turns would sit in the PTY buffer and corrupt the next bracketed-paste
        prompt. The check and the ESC write happen under ``_turn_state_lock``,
        which ``send_turn`` also holds when it flips ``_turn_in_progress``, so
        a turn cannot end between the check and the write.
        """
        if self._master_fd is None:
            raise ClaudeSessionError("session not open")
        with self._turn_state_lock:
            if not self._turn_in_progress:
                return False
            self._write_all(_ESC)
            return True

    def close(self) -> None:
        """Terminate the process and release the PTY / log handles."""
        # Signal the process first and let it flush shutdown output — the drain
        # thread is blocked in ``os.read`` and keeps reading until the slave
        # closes — then stop the drain loop only once the process has exited.
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._reap(grace=5.0)
        self._closed.set()
        self._stop_control_listener()
        self._event_source.close()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2.0)
            self._drain_thread = None
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            finally:
                self._log_fh = None
        self._pid = None

    # ------------------------------------------------------------------
    # Dashboard relay
    # ------------------------------------------------------------------

    def recent_output(self) -> bytes:
        """Return the tail of raw PTY output retained for a dashboard relay."""
        with self._ring_lock:
            return bytes(self._ring)

    def attach_stream(self) -> tuple[bytes, "queue.Queue[bytes]"]:
        """Register a live subscriber, returning ``(backlog, queue)``.

        *backlog* is the retained recent PTY output (so an attaching dashboard
        opens to context); *queue* yields every raw chunk drained thereafter.
        The snapshot and registration happen under both internal locks so the
        boundary is exact — no chunk is lost or duplicated. The caller MUST call
        :meth:`detach_stream` when done to release the subscriber.
        """
        sub: queue.Queue[bytes] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        with self._ring_lock, self._subscribers_lock:
            backlog = bytes(self._ring)
            self._subscribers.add(sub)
        return backlog, sub

    def detach_stream(self, sub: "queue.Queue[bytes]") -> None:
        """Release a subscriber registered via :meth:`attach_stream`."""
        with self._subscribers_lock:
            self._subscribers.discard(sub)

    # ------------------------------------------------------------------
    # Tool-activity observers (dashboard observe path, #178)
    # ------------------------------------------------------------------

    def add_tool_observer(self, callback: Callable[[dict], None]) -> None:
        """Subscribe *callback* to ``pre_tool`` / ``post_tool`` hook events.

        Each call receives the decoded event dict (keys: ``event``, ``tool``,
        ``session_id``). Used by :class:`SessionBridge` to surface authoritative
        tool activity to the dashboard. No-op for the JSONL source (no tool
        events). Pair with :meth:`remove_tool_observer`.
        """
        with self._tool_observers_lock:
            self._tool_observers.append(callback)

    def remove_tool_observer(self, callback: Callable[[dict], None]) -> None:
        """Unsubscribe a callback registered via :meth:`add_tool_observer`."""
        with self._tool_observers_lock:
            try:
                self._tool_observers.remove(callback)
            except ValueError:
                pass

    def _notify_tool_observers(self, event: dict) -> None:
        with self._tool_observers_lock:
            observers = list(self._tool_observers)
        for cb in observers:
            try:
                cb(event)
            except Exception:  # pragma: no cover - an observer must not break the listener
                logger.debug("tool observer raised", exc_info=True)

    def operator_write(self, data: bytes) -> str:
        """Route operator keystrokes from an attached dashboard to the PTY.

        Write coordination mirrors the mic model: between turns the human owns
        the input and *data* is written straight through (``OPERATOR_WRITTEN``).
        While a bot turn is in flight the channel is read-only — a lone ESC is
        routed to :meth:`interrupt` (``OPERATOR_INTERRUPTED``) so the operator can
        still abort a wedged turn, and anything else is rejected
        (``OPERATOR_REFUSED``) for the caller to surface as "bot has the mic".
        """
        if self._master_fd is None:
            raise ClaudeSessionError("session not open")
        if self._turn_lock.locked():
            if data == _ESC:
                self.interrupt()
                return OPERATOR_INTERRUPTED
            return OPERATOR_REFUSED
        self._write_all(data)
        return OPERATOR_WRITTEN

    def resize(self, rows: int, cols: int) -> None:
        """Apply a terminal resize (``TIOCSWINSZ``) from an attached client."""
        if self._master_fd is None:
            raise ClaudeSessionError("session not open")
        rows = max(1, int(rows))
        cols = max(1, int(cols))
        self._rows = rows
        self._cols = cols
        self._set_winsize(self._master_fd, rows, cols)

    # ------------------------------------------------------------------
    # Control channel (out-of-process interrupt)
    # ------------------------------------------------------------------
    #
    # The dashboard runs in a different process and cannot write to this
    # session's PTY master directly. When ``control_socket`` is set, ``open``
    # binds a Unix-domain socket the dashboard connects to; a one-line command
    # (``interrupt``) is dispatched to :meth:`interrupt`, so ESC reaches the PTY
    # from the process that actually owns it.

    def _start_control_listener(self) -> None:
        path = self._control_socket_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # A stale socket file from a crashed predecessor blocks bind(); only
            # remove an actual socket node, never an unrelated regular file (a
            # typo/bad config must not destroy data — leave interrupt disabled).
            if path.is_socket():
                path.unlink()
            elif path.exists():
                logger.warning(
                    "Control socket path %s exists and is not a socket; "
                    "interrupt disabled", path,
                )
                self._control_sock = None
                return
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
            sock.listen(_CONTROL_BACKLOG)
            sock.settimeout(_CONTROL_ACCEPT_TIMEOUT)
        except OSError as exc:
            # The control channel is best-effort: a bind failure must not stop
            # the session from running — interrupt simply stays unavailable.
            logger.warning("Could not start control socket %s: %s", path, exc)
            self._control_sock = None
            return
        self._control_sock = sock
        self._control_thread = threading.Thread(
            target=self._control_loop, name=f"claude-ctl-{self.session_id[:8]}", daemon=True,
        )
        self._control_thread.start()

    def _control_loop(self) -> None:
        sock = self._control_sock
        if sock is None:
            return
        while not self._closed.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener closed during shutdown
            with conn:
                try:
                    conn.settimeout(_CONTROL_ACCEPT_TIMEOUT)
                    data = conn.recv(_CONTROL_RECV_BYTES)
                except OSError:
                    continue
                try:
                    conn.sendall(self._handle_control(data))
                except OSError:
                    pass

    def _handle_control(self, data: bytes) -> bytes:
        command = data.decode("utf-8", "replace").strip()
        if command == _CONTROL_INTERRUPT:
            return b"interrupted\n" if self.interrupt() else b"idle\n"
        return b"error: unknown command\n"

    def _stop_control_listener(self) -> None:
        if self._control_sock is not None:
            try:
                self._control_sock.close()
            except OSError:
                pass
            self._control_sock = None
        if self._control_thread is not None:
            self._control_thread.join(timeout=2.0)
            self._control_thread = None
        if self._control_socket_path is not None:
            try:
                self._control_socket_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _default_log_path(self) -> Path | None:
        try:
            from loony_dev import config

            log_file = config.settings.get("log_file")
        except Exception as exc:
            # The session log is optional, so config trouble must never block
            # startup — but record why we fell back so it is not fully silent.
            logger.debug("Could not determine default session log path: %s", exc)
            return None
        if not log_file:
            return None
        return Path(log_file).parent / f"claude_session_{self.session_id}.log"

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.debug("TIOCSWINSZ failed: %s", exc)

    def _drain_loop(self) -> None:
        """Continuously read the PTY so ``claude`` never blocks on a full buffer."""
        fd = self._master_fd
        if fd is None:  # guard, not assert: must hold under ``python -O`` too
            raise ClaudeSessionError("session not open")
        while not self._closed.is_set():
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break  # EIO on Linux when the slave side closes
            if not chunk:
                break
            # Append to the ring and fan out to live subscribers under both locks
            # together so :meth:`attach_stream` sees a consistent backlog/live
            # boundary (a chunk is either in the snapshot or delivered to the
            # queue, never both and never neither).
            with self._ring_lock, self._subscribers_lock:
                self._ring += chunk
                if len(self._ring) > self._ring_bytes:
                    del self._ring[: len(self._ring) - self._ring_bytes]
                for sub in self._subscribers:
                    try:
                        sub.put_nowait(chunk)
                    except queue.Full:
                        # Lossy by design: a backed-up client must not stall the
                        # drain (and thereby the bot). See _SUBSCRIBER_QUEUE_MAX.
                        pass
            if self._log_fh is not None:
                try:
                    self._log_fh.write(chunk)
                    self._log_fh.flush()
                except OSError:
                    pass
        self._closed.set()

    def _inject_prompt(self, prompt: str) -> None:
        self._write_all(_PASTE_START + prompt.encode("utf-8") + _PASTE_END)
        # Let the UI register paste-end before submitting, so the CR is treated
        # as Enter rather than a newline inside the pasted text.
        time.sleep(_SUBMIT_DELAY)
        self._write_all(b"\r")

    def _write_all(self, data: bytes) -> None:
        fd = self._master_fd
        if fd is None:  # guard, not assert: must hold under ``python -O`` too
            raise ClaudeSessionError("session not open")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]

    @staticmethod
    def _check_quota(entry: dict) -> None:
        """Raise :class:`QuotaExceededError` if *entry* is a genuine quota error.

        Scoped to ``assistant`` / ``system`` entries: a genuine usage-limit
        message is surfaced by Claude itself, never inside the *user* prompt
        (which may merely discuss quotas — issue #178). :meth:`_is_quota_error`
        is also strict (real usage-limit signal only), so a turn whose content
        only *talks about* rate limits is not misread as a rate-limit hit.
        """
        if entry.get("type") not in ("assistant", "system"):
            return
        text = _entry_text(entry)
        if text and ClaudeQuotaMixin._is_quota_error(text):
            raise QuotaExceededError(text)

    def _scan_transcript_after_stop(
        self, transcript_path: str | None, *, want_text: bool,
    ) -> _TranscriptScan | None:
        """One-shot transcript read triggered by a ``Stop`` event.

        The ``Stop`` event is the authoritative *signal*; this pulls the content
        it refers to. Returns a :class:`_TranscriptScan` with:

        * ``quota_output`` — the offending text if a genuine usage-limit error is
          present (the addendum's chosen quota path: a bounded post-``Stop``
          read, not polling), else ``None``;
        * ``assistant_text`` — the concatenated assistant text, used to backfill
          ``TurnResult.text`` when the Stop payload omitted it.

        Returns ``None`` if the transcript cannot be read. This is *not* polling:
        it reads the file once, after the Stop signal has already fired.
        """
        path = Path(transcript_path) if transcript_path else self._jsonl_path
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        assistant_parts: list[str] = []
        quota_output: str | None = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            if etype in ("assistant", "system"):
                text = _entry_text(entry)
                if text and quota_output is None and ClaudeQuotaMixin._is_quota_error(text):
                    quota_output = text
            if want_text and etype == "assistant":
                text = _entry_text(entry)
                if text:
                    assistant_parts.append(text)
        return _TranscriptScan(
            quota_output=quota_output,
            assistant_text="\n".join(assistant_parts),
        )

    @staticmethod
    def _stop_reason(terminal: dict, was_interrupted: bool) -> str | None:
        if was_interrupted:
            return "interrupted"
        message = terminal.get("message")
        if isinstance(message, dict):
            return message.get("stop_reason")
        return None

    @staticmethod
    def _assistant_text(entries: list[dict]) -> str:
        parts: list[str] = []
        for entry in entries:
            if entry.get("type") == "assistant":
                text = _entry_text(entry)
                if text:
                    parts.append(text)
        return "\n".join(parts)

    def _reap(self, *, grace: float) -> None:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                return
            if pid != 0:
                return
            time.sleep(0.05)
        try:
            os.kill(self._pid, signal.SIGKILL)
            os.waitpid(self._pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
