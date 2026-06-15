"""Persistent PTY-backed Claude session.

Instead of spawning a fresh ``claude -p`` subprocess per turn (see
:mod:`loony_dev.agents.claude_quota`), this drives a *single* interactive
``claude`` process over a pseudo-terminal.  The bot owns the PTY master and
writes keystrokes (bracketed-paste prompts, ESC to interrupt); turn boundaries
and outputs are read from the session JSONL transcript at
``<claude-config>/projects/<cwd-slug>/<session-id>.jsonl`` — no screen scraping.

This module is the foundation for the persistent-PTY worker rework (issue
#161).  It deliberately does **not** touch ``CodingAgent`` — migrating the
multi-phase ``execute_issue`` flow onto :class:`ClaudeSession` is a separate
issue.
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
from dataclasses import dataclass
from pathlib import Path

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

# How long (s) to keep polling the JSONL after the terminal entry appears, so
# trailing tool/system/file-history-snapshot flushes are captured in full.
_DEFAULT_DEBOUNCE = 1.0
_POLL_INTERVAL = 0.1
# STOPGAP grace (s) before the first turn is sent. Interactive ``claude`` does
# not write the session JSONL transcript until the first turn is submitted, so
# we cannot use "transcript exists" as a startup readiness signal (it would
# never fire pre-input). Instead we give the child this long to reach its
# interactive prompt, then proceed; the first turn creates the transcript and
# the tailer picks it up. Callers MUST pre-trust the cwd (see
# ``trust_directory``) or interactive ``claude`` blocks on the folder-trust
# dialog. The durable fix is hook-driven readiness (#178). Override via the
# ``claude_session_startup_timeout_seconds`` key under ``[worker]``.
_DEFAULT_STARTUP_TIMEOUT = 10.0
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


class ClaudeSessionError(Exception):
    """Base class for errors raised by :class:`ClaudeSession`."""


class ReadinessTimeout(ClaudeSessionError):
    """Raised when the session JSONL never appears within the readiness window."""


class TurnTimeout(ClaudeSessionError):
    """Raised when a ``send_turn`` does not reach a terminal entry in time."""


class TurnInterrupted(ClaudeSessionError):
    """Raised (optionally) when an in-flight turn is interrupted via ESC."""


class QuotaExceededError(ClaudeSessionError):
    """Raised from ``send_turn`` when the JSONL surface shows a quota error.

    Carries the offending text in ``output`` so callers can reuse
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


class ClaudeSession:
    """A single, persistent interactive ``claude`` process over a PTY.

    The process is started once with :meth:`open` and reused across turns;
    :meth:`send_turn` injects a prompt and blocks until the transcript shows a
    terminal entry.  :meth:`interrupt` sends ESC to abort an in-flight turn
    without killing the process.
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
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT,
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
        self._startup_timeout = startup_timeout_seconds
        self._ring_bytes = ring_bytes

        self._jsonl_path = jsonl_path_for(self.cwd, self.session_id)
        self._log_path = log_file if log_file is not None else self._default_log_path()
        self._control_socket_path = Path(control_socket) if control_socket is not None else None

        self._pid: int | None = None
        self._master_fd: int | None = None
        self._tailer = _JsonlTailer(self._jsonl_path)

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
        """Fork ``claude`` onto a PTY and wait until the transcript exists."""
        if self._pid is not None:
            raise ClaudeSessionError("session already open")

        if self._log_path is not None:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self._log_path, "ab")
            except OSError as exc:
                logger.debug("Could not open session log %s: %s", self._log_path, exc)
                self._log_fh = None

        cmd = [
            self._binary,
            "--dangerously-skip-permissions",
            "--session-id",
            self.session_id,
            *self._extra_args,
        ]
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

        self._await_readiness()
        logger.info(
            "ClaudeSession ready (pid=%d, session=%s, jsonl=%s)",
            self._pid, self.session_id, self._jsonl_path,
        )

    def _await_readiness(self) -> None:
        """Give the forked ``claude`` a grace period to reach its interactive
        prompt, then return so the first turn can be sent.

        STOPGAP: interactive ``claude`` does not create the session JSONL until
        the first turn is submitted, so we cannot wait for the transcript here
        (it would never appear pre-input). We poll for it up to the grace window
        — a fast path for the rare case a transcript already exists (resumed
        sessions, tests) — but proceed anyway once the grace elapses; the first
        ``send_turn`` creates the transcript and the tailer picks it up. The
        cwd must be pre-trusted (see :func:`trust_directory`). Aborts early if
        the process dies. Durable fix: hook-driven readiness (#178).
        """
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._jsonl_path.exists():
                return
            if self._closed.is_set():
                raise ClaudeSessionError("session process exited during startup")
            time.sleep(_POLL_INTERVAL)
        logger.debug(
            "ClaudeSession %s: startup grace (%.0fs) elapsed without a transcript; "
            "proceeding to first turn (it will create the transcript)",
            self.session_id, self._startup_timeout,
        )

    def send_turn(self, prompt: str, *, timeout: float) -> TurnResult:
        """Inject *prompt* and block until the turn reaches a terminal entry.

        Returns a :class:`TurnResult`.  Raises :class:`QuotaExceededError` if a
        quota message appears, or :class:`TurnTimeout` if no terminal entry is
        seen within *timeout* seconds.
        """
        if self._pid is None:
            raise ClaudeSessionError("session not open")

        with self._turn_lock:
            # Mark the turn live so interrupt() may send ESC; the state lock
            # makes the flag flip atomic with interrupt()'s check + ESC write.
            with self._turn_state_lock:
                self._turn_in_progress = True
            try:
                return self._run_turn(prompt, timeout=timeout)
            finally:
                with self._turn_state_lock:
                    self._turn_in_progress = False

    def _run_turn(self, prompt: str, *, timeout: float) -> TurnResult:
        # Consume any entries already on disk so they are not mistaken for
        # this turn's output.
        self._tailer.read_new()

        self._inject_prompt(prompt)

        added: list[dict] = []
        terminal: dict | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for entry in self._tailer.read_new():
                added.append(entry)
                self._check_quota(entry)
                if terminal is None and (
                    _is_terminal_assistant(entry) or _is_interrupt(entry)
                ):
                    terminal = entry
            if terminal is not None:
                break
            if self._closed.is_set():
                raise ClaudeSessionError("session process exited mid-turn")
            time.sleep(_POLL_INTERVAL)
        else:
            raise TurnTimeout(
                f"No terminal entry within {timeout:.0f}s for session {self.session_id}",
            )

        # Debounce: let trailing tool/system/snapshot entries flush, then
        # fold them into the result.
        time.sleep(self._debounce)
        for entry in self._tailer.read_new():
            added.append(entry)
            self._check_quota(entry)

        was_interrupted = _is_interrupt(terminal)
        stop_reason = self._stop_reason(terminal, was_interrupted)
        text = self._assistant_text(added)
        return TurnResult(
            text=text,
            stop_reason=stop_reason,
            was_interrupted=was_interrupted,
            entries_added=len(added),
        )

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
        text = _entry_text(entry)
        if text and ClaudeQuotaMixin._is_quota_error(text):
            raise QuotaExceededError(text)

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
