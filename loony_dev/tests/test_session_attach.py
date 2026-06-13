"""Tests for the dashboard observe + steer bridge to worker PTY sessions (#164).

Four layers, bottom-up:

* the on-disk registry contract (:mod:`loony_dev.session_registry`),
* the :class:`ClaudeSession` operator-write / live-attach primitives,
* the worker-side :class:`SessionBridge` Unix-socket server,
* the web ``inject`` endpoint and the ``attach`` websocket proxy.

The session layers reuse the ``_claude_stub.py`` fake ``claude`` (real PTY, no
real binary), mirroring ``test_claude_session.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from loony_dev import session_registry as sr
from loony_dev.agents.claude_session import (
    OPERATOR_INTERRUPTED,
    OPERATOR_REFUSED,
    OPERATOR_WRITTEN,
    ClaudeSession,
)
from loony_dev.agents.session_bridge import (
    FRAME_CONTROL,
    FRAME_DATA,
    SessionBridge,
    encode_frame,
    publish_session,
    unpublish_session,
)
from loony_dev.web import create_app, services

_STUB = Path(__file__).parent / "_claude_stub.py"
_HEADER = struct.Struct(">BI")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmpdir() -> str:
    return tempfile.mkdtemp()


def _wait_until(predicate, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError(f"predicate not satisfied within {timeout:.1f}s")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


class _FrameClient:
    """Buffered frame decoder over a client socket (no cross-call frame loss)."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def _fill(self, n: int, timeout: float) -> None:
        self._sock.settimeout(timeout)
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf += chunk

    def recv_frame(self, *, timeout: float = 5.0) -> tuple[int, bytes]:
        self._fill(_HEADER.size, timeout)
        ftype, length = _HEADER.unpack(self._buf[: _HEADER.size])
        self._fill(_HEADER.size + length, timeout)
        payload = bytes(self._buf[_HEADER.size : _HEADER.size + length])
        del self._buf[: _HEADER.size + length]
        return ftype, payload

    def recv_control_until(self, predicate, *, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ftype, payload = self.recv_frame(timeout=remaining)
            if ftype == FRAME_CONTROL:
                msg = json.loads(payload)
                if predicate(msg):
                    return msg
        raise TimeoutError("control frame predicate not met")


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------

class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def test_slug_is_safe_and_distinct(self) -> None:
        a = sr.task_slug("acme/widgets#42")
        b = sr.task_slug("acme-widgets-42")
        self.assertNotIn("/", a)
        self.assertNotIn("#", a)
        self.assertNotEqual(a, b)  # collision-resistant despite similar sanitisation

    def test_write_read_and_find_session(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-7")
        sr.write_session_file(
            sess_dir, task_key="issue-7", repo="acme/widgets",
            session_id="sess-abc", pid=1234, started_at="2026-06-13T00:00:00Z",
        )
        found = sr.find_session(self.base, "issue-7")
        self.assertIsNotNone(found)
        self.assertEqual(found.task_key, "issue-7")
        self.assertEqual(found.repo, "acme/widgets")
        self.assertEqual(found.session_id, "sess-abc")
        self.assertEqual(found.socket, str(sr.socket_path(sess_dir)))
        self.assertEqual([s.task_key for s in sr.iter_sessions(self.base)], ["issue-7"])

    def test_find_missing_returns_none(self) -> None:
        self.assertIsNone(sr.find_session(self.base, "nope"))

    def test_malformed_session_file_skipped(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "bad")
        sess_dir.mkdir(parents=True)
        (sess_dir / sr.SESSION_FILE_NAME).write_text("{not json")
        self.assertEqual(list(sr.iter_sessions(self.base)), [])

    def test_enqueue_and_drain_injections_in_order(self) -> None:
        sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-7")
        sr.write_session_file(
            sess_dir, task_key="issue-7", repo="acme/widgets",
            session_id="s", pid=1, started_at="t",
        )
        sr.enqueue_injection(sess_dir, "first guidance")
        sr.enqueue_injection(sess_dir, "second guidance")
        drained = sr.drain_injections(sess_dir)
        self.assertEqual([d["prompt"] for d in drained], ["first guidance", "second guidance"])
        self.assertTrue(all(d["source"] == sr.SOURCE_OPERATOR for d in drained))
        # Draining is destructive: a second drain yields nothing.
        self.assertEqual(sr.drain_injections(sess_dir), [])


# ---------------------------------------------------------------------------
# ClaudeSession operator primitives (stub-backed PTY)
# ---------------------------------------------------------------------------

class _StubSession(unittest.TestCase):
    extra_env: dict[str, str] = {}

    def setUp(self) -> None:
        self.config_dir = Path(_tmpdir())
        self.cwd = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.config_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.cwd, ignore_errors=True))
        os.chmod(_STUB, 0o755)
        env = {"CLAUDE_CONFIG_DIR": str(self.config_dir), **self.extra_env}
        self.enterContext(mock.patch.dict(os.environ, env))
        self.session = ClaudeSession(
            self.cwd, binary=str(_STUB), readiness_timeout=10.0, debounce=0.2,
        )
        self.session.open()
        self.addCleanup(self.session.close)

    def _run_long_turn(self, prompt: str) -> threading.Thread:
        """Start a LONGTURN in a background thread, joined cleanly at teardown."""
        t = threading.Thread(
            target=lambda: self.session.send_turn(prompt, timeout=20.0), daemon=True,
        )
        t.start()
        self.addCleanup(self._end_long_turn, t)
        return t

    def _end_long_turn(self, t: threading.Thread) -> None:
        if t.is_alive() and self.session.is_open:
            self.session.interrupt()
        t.join(timeout=10.0)


class OperatorWriteTestCase(_StubSession):
    extra_env = {"STUB_LONGTURN_SECS": "20"}

    def test_write_between_turns_drives_the_pty(self) -> None:
        # A full bracketed-paste prompt written by the operator (between bot
        # turns) reaches the stub, which records an assistant reply.
        status = self.session.operator_write(b"\x1b[200~hello there\x1b[201~\r")
        self.assertEqual(status, OPERATOR_WRITTEN)
        _wait_until(
            lambda: any("reply to: hello there" in _txt(e)
                        for e in _read_jsonl(self.session.jsonl_path)),
            timeout=10.0,
        )

    def test_refused_while_bot_holds_the_mic(self) -> None:
        out: dict[str, object] = {}

        def run_long() -> None:
            out["turn"] = self.session.send_turn("LONGTURN please", timeout=20.0)

        t = threading.Thread(target=run_long)
        t.start()
        try:
            _wait_until(lambda: b"LONGTURN" in self.session.recent_output(), timeout=5.0)
            self.assertTrue(self.session.turn_in_progress)
            # A plain keystroke is refused mid-turn …
            self.assertEqual(self.session.operator_write(b"x"), OPERATOR_REFUSED)
            # … but ESC is routed to interrupt so a wedged turn can still be aborted.
            self.assertEqual(self.session.operator_write(b"\x1b"), OPERATOR_INTERRUPTED)
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive())
            self.assertTrue(out["turn"].was_interrupted)
        finally:
            if t.is_alive():
                self.session.interrupt()
                t.join(timeout=10.0)

    def test_attach_stream_backlog_and_live(self) -> None:
        backlog, q = self.session.attach_stream()
        t = self._run_long_turn("LONGTURN go")
        try:
            self.assertIsInstance(backlog, bytes)
            collected = b""
            deadline = time.monotonic() + 5.0
            while b"LONGTURN" not in collected and time.monotonic() < deadline:
                try:
                    collected += q.get(timeout=0.5)
                except Exception:
                    pass
            self.assertIn(b"LONGTURN", collected)
        finally:
            self.session.detach_stream(q)
            self._end_long_turn(t)

    def test_resize_updates_winsize(self) -> None:
        self.session.resize(50, 132)
        self.assertEqual(self.session._rows, 50)
        self.assertEqual(self.session._cols, 132)

    def test_publish_then_unpublish_round_trip(self) -> None:
        base = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        bridge = publish_session(self.session, base, "acme/widgets", "issue-7")
        try:
            # The registry now advertises an attachable session.
            views = services.list_task_sessions(base)
            self.assertEqual(len(views), 1)
            self.assertEqual(views[0].task_key, "issue-7")
            self.assertTrue(views[0].attachable)
            self.assertTrue(os.path.exists(bridge.socket_path))
        finally:
            unpublish_session(bridge, base, "acme/widgets", "issue-7")
        # Teardown removes the registry entry and the socket.
        self.assertEqual(services.list_task_sessions(base), [])
        self.assertFalse(os.path.exists(bridge.socket_path))


# ---------------------------------------------------------------------------
# SessionBridge (real Unix socket over a stub session)
# ---------------------------------------------------------------------------

class SessionBridgeTestCase(_StubSession):
    extra_env = {"STUB_LONGTURN_SECS": "20"}

    def setUp(self) -> None:
        super().setUp()
        self.sock_dir = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.sock_dir, ignore_errors=True))
        self.bridge = SessionBridge(self.session, self.sock_dir / "attach.sock")
        self.bridge.serve()
        self.addCleanup(self.bridge.close)

    def _connect(self) -> tuple[socket.socket, _FrameClient]:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _wait_until(lambda: os.path.exists(self.bridge.socket_path), timeout=5.0)
        client.connect(self.bridge.socket_path)
        self.addCleanup(lambda: _close(client))
        return client, _FrameClient(client)

    def test_initial_mic_frame_is_human(self) -> None:
        _client, frames = self._connect()
        msg = frames.recv_control_until(lambda m: m.get("type") == "mic")
        self.assertEqual(msg["holder"], "human")

    def test_mic_flips_to_bot_and_refuses_writes(self) -> None:
        client, frames = self._connect()
        frames.recv_control_until(lambda m: m.get("holder") == "human")

        self._run_long_turn("LONGTURN")
        # The writer pump reflects the turn boundary as a mic=bot control frame.
        frames.recv_control_until(lambda m: m.get("holder") == "bot", timeout=5.0)

        # A keystroke sent while the bot holds the mic is bounced with a refusal.
        client.sendall(encode_frame(FRAME_DATA, b"x"))
        refused = frames.recv_control_until(lambda m: m.get("refused") is True)
        self.assertEqual(refused["holder"], "bot")

        # ESC interrupts the turn (same path as the Interrupt button).
        client.sendall(encode_frame(FRAME_DATA, b"\x1b"))
        _wait_until(lambda: not self.session.turn_in_progress, timeout=10.0)

    def test_resize_control_reaches_session(self) -> None:
        client, frames = self._connect()
        frames.recv_control_until(lambda m: m.get("type") == "mic")
        client.sendall(encode_frame(FRAME_CONTROL, json.dumps(
            {"type": "resize", "rows": 24, "cols": 99}).encode()))
        _wait_until(lambda: self.session._cols == 99 and self.session._rows == 24, timeout=5.0)

    def test_reconnects_do_not_leak_subscribers(self) -> None:
        for _ in range(8):
            client, frames = self._connect()
            frames.recv_control_until(lambda m: m.get("type") == "mic")
            _close(client)
            time.sleep(0.05)
        # Every disconnect must release its subscriber handle.
        _wait_until(lambda: len(self.session._subscribers) == 0, timeout=5.0)


# ---------------------------------------------------------------------------
# Web endpoints: inject + task-sessions + attach websocket proxy
# ---------------------------------------------------------------------------

class InjectEndpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-9")
        sr.write_session_file(
            self.sess_dir, task_key="issue-9", repo="acme/widgets",
            session_id="s", pid=1, started_at="t",
        )
        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        self.addCleanup(self.client.close)

    def test_inject_enqueues_with_operator_provenance(self) -> None:
        resp = self.client.post("/api/sessions/issue-9/inject", json={"prompt": "try X instead"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source"], "operator")
        self.assertEqual(body["task_key"], "issue-9")
        drained = sr.drain_injections(self.sess_dir)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["prompt"], "try X instead")
        self.assertEqual(drained[0]["source"], "operator")

    def test_inject_unknown_task_404(self) -> None:
        resp = self.client.post("/api/sessions/ghost/inject", json={"prompt": "hi"})
        self.assertEqual(resp.status_code, 404)

    def test_inject_rejects_empty_prompt(self) -> None:
        for bad in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 5}):
            with self.subTest(body=bad):
                resp = self.client.post("/api/sessions/issue-9/inject", json=bad)
                self.assertEqual(resp.status_code, 400)

    def test_task_sessions_listing(self) -> None:
        rows = self.client.get("/api/task-sessions").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_key"], "issue-9")
        self.assertEqual(rows[0]["repo"], "acme/widgets")
        # No socket file present -> not attachable.
        self.assertFalse(rows[0]["attachable"])


class AttachWebSocketTestCase(unittest.TestCase):
    """End-to-end proxy: TestClient websocket -> route -> bridge -> stub PTY."""

    def setUp(self) -> None:
        self.base = Path(_tmpdir())
        self.config_dir = Path(_tmpdir())
        self.cwd = Path(_tmpdir())
        for d in (self.base, self.config_dir, self.cwd):
            self.addCleanup(lambda d=d: shutil.rmtree(d, ignore_errors=True))
        os.chmod(_STUB, 0o755)
        self.enterContext(mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir), "STUB_LONGTURN_SECS": "20"},
        ))
        self.session = ClaudeSession(
            self.cwd, binary=str(_STUB), readiness_timeout=10.0, debounce=0.2,
        )
        self.session.open()
        self.addCleanup(self.session.close)

        self.sess_dir = sr.session_dir(self.base, "acme", "widgets", "issue-3")
        sock = self.sess_dir / "attach.sock"
        sr.write_session_file(
            self.sess_dir, task_key="issue-3", repo="acme/widgets",
            session_id=self.session.session_id, pid=os.getpid(),
            started_at="t", socket=str(sock),
        )
        self.bridge = SessionBridge(self.session, sock)
        self.bridge.serve()
        self.addCleanup(self.bridge.close)

        self.client = TestClient(create_app(base_dir=self.base, supervisor_log=None))
        self.addCleanup(self.client.close)

    def test_attach_proxies_mic_status_and_live_output(self) -> None:
        with self.client.websocket_connect("/api/sessions/issue-3/attach") as ws:
            # First inbound text frame is the mic status control message.
            mic = json.loads(ws.receive_text())
            self.assertEqual(mic, {"type": "mic", "holder": "human"})

            # Drive PTY output; the bridge streams it as a binary frame.
            turn = threading.Thread(
                target=lambda: self.session.send_turn("LONGTURN", timeout=20.0),
                daemon=True,
            )
            turn.start()
            try:
                collected = b""
                for _ in range(50):
                    msg = ws.receive()
                    if msg.get("bytes"):
                        collected += msg["bytes"]
                        if b"LONGTURN" in collected:
                            break
                self.assertIn(b"LONGTURN", collected)
            finally:
                self.session.interrupt()
                turn.join(timeout=10.0)

    def test_attach_unknown_session_closes(self) -> None:
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/api/sessions/ghost/attach") as ws:
                ws.receive()
        self.assertEqual(ctx.exception.code, 4404)


def _txt(entry: dict) -> str:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
