"""Unix-domain-socket bridge exposing a :class:`ClaudeSession` PTY (issue #164).

The web dashboard runs as a *separate process* from the worker that owns the
task's ``ClaudeSession``, so it cannot touch the PTY master fd directly. This
module is the worker-side half of the bridge: it serves a Unix-domain socket
(path published via :mod:`loony_dev.session_registry`) that the dashboard's
``WS /api/sessions/{task_key}/attach`` proxy connects to.

Wire protocol (both directions), framed so PTY bytes and JSON control messages
share one stream::

    1 byte  type   (0 = raw PTY data, 1 = JSON control)
    4 bytes length (big-endian, unsigned)
    length bytes    payload

On connect the bridge sends the current mic status (a control frame) and the
retained backlog (a data frame), then streams live PTY output. Client data
frames are routed through :meth:`ClaudeSession.operator_write` (so the bot's
in-flight mutex makes the channel read-only mid-turn — a refused write echoes a
``mic`` control frame back); ``resize`` control frames drive ``TIOCSWINSZ``.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path

from loony_dev import session_registry
from loony_dev.agents.claude_session import (
    OPERATOR_INTERRUPTED,
    OPERATOR_REFUSED,
    ClaudeSession,
)
from loony_dev.agents.session_hooks import EVENT_POST_TOOL, EVENT_PRE_TOOL

logger = logging.getLogger(__name__)

FRAME_DATA = 0
FRAME_CONTROL = 1
_HEADER = struct.Struct(">BI")
# Refuse absurd client frames outright (a keystroke or resize is tiny); this is
# a sanity bound on a trusted-but-defensive local socket, not a security gate.
_MAX_FRAME = 1 << 20

# How often a connection's output pump re-checks the mic holder so a turn
# boundary is reflected to the client even while the PTY is silent.
_MIC_POLL_INTERVAL = 0.2


def encode_frame(ftype: int, payload: bytes) -> bytes:
    """Encode one ``(type, payload)`` wire frame."""
    return _HEADER.pack(ftype, len(payload)) + payload


def _control(obj: dict) -> bytes:
    return encode_frame(FRAME_CONTROL, json.dumps(obj).encode("utf-8"))


def _mic_message(session: ClaudeSession, *, refused: bool = False) -> dict:
    holder = "bot" if session.turn_in_progress else "human"
    msg = {"type": "mic", "holder": holder}
    if refused:
        msg["refused"] = True
    return msg


def _tool_message(event: dict) -> dict:
    """Build a ``tool`` control frame from a ``pre_tool`` / ``post_tool`` event.

    ``phase`` is ``"start"`` for ``pre_tool`` and ``"end"`` for ``post_tool``;
    ``tool`` is the tool name (e.g. ``"Bash"``). This is the authoritative
    tool-activity signal for the dashboard observe path (#178), independent of
    the coarse mic/turn state.
    """
    phase = "start" if event.get("event") == EVENT_PRE_TOOL else "end"
    return {"type": "tool", "phase": phase, "tool": event.get("tool")}


class _FrameReader:
    """Incremental frame decoder over a blocking socket."""

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._buf = bytearray()

    def _recv_at_least(self, n: int) -> bool:
        while len(self._buf) < n:
            try:
                chunk = self._conn.recv(65536)
            except OSError:
                return False
            if not chunk:
                return False
            self._buf += chunk
        return True

    def read_frame(self) -> tuple[int, bytes] | None:
        """Return the next ``(type, payload)`` frame, or ``None`` at EOF/error."""
        if not self._recv_at_least(_HEADER.size):
            return None
        ftype, length = _HEADER.unpack(self._buf[: _HEADER.size])
        if length > _MAX_FRAME:
            logger.warning("attach client frame too large (%d bytes); dropping", length)
            return None
        if not self._recv_at_least(_HEADER.size + length):
            return None
        start = _HEADER.size
        payload = bytes(self._buf[start : start + length])
        del self._buf[: start + length]
        return ftype, payload


class SessionBridge:
    """Serve a :class:`ClaudeSession`'s PTY over a Unix-domain socket.

    Construct with the session and a socket path, then :meth:`serve`. Each
    accepted connection gets a reader thread (client → PTY) and a writer thread
    (PTY → client). :meth:`close` stops accepting, drops all connections, and
    unlinks the socket; it is idempotent and releases every subscriber handle so
    repeated attach/detach cycles do not leak.
    """

    def __init__(self, session: ClaudeSession, socket_path: str | os.PathLike) -> None:
        self._session = session
        self._socket_path = os.fspath(socket_path)
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._conns: set[socket.socket] = set()
        self._conns_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def serve(self) -> None:
        """Bind the listening socket and start accepting attach connections."""
        if self._server is not None:
            raise RuntimeError("bridge already serving")
        try:
            os.unlink(self._socket_path)  # clear a stale socket from a crash
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not unlink stale socket %s: %s", self._socket_path, exc)
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        server.listen(8)
        self._server = server
        # Authoritative tool-activity observe path (#178): the session fans out
        # pre/post tool hook events; we rebroadcast each as a ``tool`` control
        # frame to every attached dashboard client.
        self._session.add_tool_observer(self._on_tool_event)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="session-bridge-accept", daemon=True,
        )
        self._accept_thread.start()

    def _on_tool_event(self, event: dict) -> None:
        """Tool observer: broadcast a ``tool`` control frame to all clients."""
        if event.get("event") not in (EVENT_PRE_TOOL, EVENT_POST_TOOL):
            return
        frame = _control(_tool_message(event))
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                _send(conn, frame)
            except OSError:
                pass  # a vanished client is cleaned up by its own pump

    def close(self) -> None:
        """Stop serving, drop connections, and unlink the socket (idempotent)."""
        self._stop.set()
        self._session.remove_tool_observer(self._on_tool_event)
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            _shutdown(conn)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        # Snapshot under the lock (the accept loop mutates _threads), then join
        # outside it so a slow join can't block _accept_loop on the lock.
        with self._conns_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=2.0)
        with self._conns_lock:
            self._threads = [t for t in self._threads if t.is_alive()]
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                break  # server closed by close()
            with self._conns_lock:
                self._conns.add(conn)
            # Per-connection close flag: whichever pump notices the client is
            # gone sets it so the *other* pump wakes promptly (the writer would
            # otherwise sit idle in ``sub.get`` and leak its subscriber).
            closed = threading.Event()
            reader = threading.Thread(
                target=self._handle_reader, args=(conn, closed),
                name="session-bridge-reader", daemon=True,
            )
            writer = threading.Thread(
                target=self._handle_writer, args=(conn, closed),
                name="session-bridge-writer", daemon=True,
            )
            # Drop references to finished handler threads so a long-lived bridge
            # with many reconnects doesn't accumulate dead Thread objects.
            # Serialised with close()'s join via _conns_lock.
            with self._conns_lock:
                self._threads = [t for t in self._threads if t.is_alive()]
                self._threads += [reader, writer]
            reader.start()
            writer.start()

    def _drop_conn(self, conn: socket.socket, closed: threading.Event) -> None:
        closed.set()
        with self._conns_lock:
            self._conns.discard(conn)
        _shutdown(conn)

    def _handle_writer(self, conn: socket.socket, closed: threading.Event) -> None:
        """PTY → client: backlog, mic status, then live output + mic changes."""
        backlog, sub = self._session.attach_stream()
        last_mic: str | None = None
        try:
            mic = _mic_message(self._session)
            last_mic = mic["holder"]
            _send(conn, _control(mic))
            if backlog:
                _send(conn, encode_frame(FRAME_DATA, backlog))
            while not self._stop.is_set() and not closed.is_set():
                holder = "bot" if self._session.turn_in_progress else "human"
                if holder != last_mic:
                    last_mic = holder
                    _send(conn, _control({"type": "mic", "holder": holder}))
                try:
                    chunk = sub.get(timeout=_MIC_POLL_INTERVAL)
                except queue.Empty:  # poll timeout — re-check mic/liveness
                    if not self._session.is_open:
                        break
                    continue
                _send(conn, encode_frame(FRAME_DATA, chunk))
        except OSError:
            pass  # client vanished mid-write
        finally:
            self._session.detach_stream(sub)
            self._drop_conn(conn, closed)

    def _handle_reader(self, conn: socket.socket, closed: threading.Event) -> None:
        """client → PTY: route keystrokes (mic-gated) and control messages."""
        reader = _FrameReader(conn)
        try:
            while not self._stop.is_set() and not closed.is_set():
                frame = reader.read_frame()
                if frame is None:
                    break
                ftype, payload = frame
                if ftype == FRAME_DATA:
                    self._route_input(conn, payload)
                elif ftype == FRAME_CONTROL:
                    self._route_control(payload)
        finally:
            self._drop_conn(conn, closed)

    def _route_input(self, conn: socket.socket, payload: bytes) -> None:
        if not payload:
            return
        try:
            status = self._session.operator_write(payload)
        except Exception as exc:
            logger.debug("operator_write failed: %s", exc)
            return
        # A refused write means the bot holds the mic — tell the client so it can
        # show the "bot has the mic" indicator and re-queue/abandon the keystroke.
        if status == OPERATOR_REFUSED:
            try:
                _send(conn, _control(_mic_message(self._session, refused=True)))
            except OSError:
                pass
        elif status == OPERATOR_INTERRUPTED:
            logger.info("operator interrupted in-flight turn via attach ESC")

    def _route_control(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "resize":
            try:
                self._session.resize(int(msg["rows"]), int(msg["cols"]))
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("ignoring malformed resize control: %s", exc)


def publish_session(
    session: ClaudeSession,
    base_dir: str | os.PathLike,
    repo: str,
    task_key: str,
    *,
    pid: int | None = None,
    started_at: str | None = None,
) -> SessionBridge:
    """Serve *session* over a bridge and publish its registry entry.

    The one call a worker makes once it owns a task's ``ClaudeSession`` (issue
    #161 integration): it allocates the per-task socket, starts the bridge, and
    writes ``session.json`` so the dashboard can discover and attach. Pair with
    :func:`unpublish_session` on teardown.
    """
    owner, name = repo.split("/", 1)
    sess_dir = session_registry.session_dir(base_dir, owner, name, task_key)
    sock = session_registry.socket_path(sess_dir)
    bridge = SessionBridge(session, sock)
    bridge.serve()
    session_registry.write_session_file(
        sess_dir,
        task_key=task_key,
        repo=repo,
        session_id=session.session_id,
        pid=pid if pid is not None else os.getpid(),
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        socket=str(sock),
    )
    return bridge


def unpublish_session(
    bridge: SessionBridge,
    base_dir: str | os.PathLike,
    repo: str,
    task_key: str,
) -> None:
    """Stop *bridge* and remove the task session's registry directory."""
    bridge.close()
    owner, name = repo.split("/", 1)
    sess_dir = session_registry.session_dir(base_dir, owner, name, task_key)
    session_registry.remove_session_dir(Path(sess_dir))


def _send(conn: socket.socket, data: bytes) -> None:
    conn.sendall(data)


def _shutdown(conn: socket.socket) -> None:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass
