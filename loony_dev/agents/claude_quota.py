"""Claude CLI quota / rate-limit mixin."""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loony_dev import config, pipeline_log
from loony_dev.agents.context_file import (
    CommandNotInstalledError,
    write_context_file,
)
from loony_dev.session import session_id_for

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

# Deprecated IANA timezone names that may not exist in minimal tzdata packages.
# Claude's API sometimes returns these; map them to their canonical successors.
_TZ_ALIASES: dict[str, str] = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}

# Phrases that genuinely indicate a Claude *usage-limit* error, not merely
# content that *discusses* quotas. The old broad substrings ("quota", "rate
# limit", "429", "resource_exhausted", "too many requests") matched normal prose
# and code — so any task whose text simply talks about rate limits (e.g. issue
# #178 itself) was misread as a rate-limit hit and the agent self-disabled for
# 30 minutes. These phrases are specific to Claude's actual usage-limit message.
_QUOTA_PATTERNS = [
    "usage limit reached",
    "hit your limit",
    "reached your usage limit",
    "approaching your usage limit",
    "claude usage limit",
    "usage limit will reset",
]

_SESSION_NOT_FOUND_PATTERNS = (
    "no session",
    "session not found",
    "could not find session",
    "invalid session",
    "does not exist",
    "no conversation found",
)

# Matches: "Your limit will reset at 2pm (America/New_York)"
#      or: "resets 7:30pm (Asia/Calcutta)"
_RESET_RE = re.compile(
    r"resets?\s+(?:.*?at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


class ClaudeQuotaMixin:
    """Mixin for agents that call the Claude CLI and may encounter quota errors.

    Provides detection, parsing, and self-disabling on rate-limit errors.
    Must be used alongside Agent (accesses ``name`` and overrides
    ``is_disabled``).
    """

    _disabled_until: datetime | None = None
    # Class-level lock guarding _disabled_until. A single agent instance is
    # shared across all concurrent tasks, so a quota disable in one worker is
    # visible to the others; the lock makes the check-then-clear / set sequences
    # race-free across threads.
    _quota_lock = threading.Lock()
    repo: str = ""  # Set by subclass __init__; used for session ID generation.
    # The resolved workspace base dir, threaded down from the orchestrator
    # (``Orchestrator.__init__``) so the turn-boundary heartbeat writes the
    # execution-state substrate (#267) under the *same* tree the orchestrator
    # does. ``None`` for bare/test agents — the heartbeat is then a no-op (the
    # substrate is strictly best-effort). Never derived here from
    # ``config.settings.base_dir`` (that property raises ``KeyError`` when unset,
    # which would diverge the agent's heartbeat from the orchestrator's events).
    base_dir: Path | None = None

    # ------------------------------------------------------------------
    # Persistent session registry
    # ------------------------------------------------------------------
    # Agents that drive a long-lived ``ClaudeSession`` (see
    # :mod:`loony_dev.agents.claude_session`) register it here so that
    # ``terminate()`` — invoked from the orchestrator's signal handler — can
    # close the underlying ``claude`` process on shutdown, just as it does for
    # the throwaway ``-p`` subprocesses tracked by :class:`Agent`.

    def _ensure_session_registry(self) -> None:
        """Lazily create the per-instance session registry (thread-safe)."""
        self.__dict__.setdefault("_session_lock", threading.Lock())
        self.__dict__.setdefault("_active_sessions", set())

    def _register_session(self, session: object) -> None:
        """Record a freshly opened session so terminate() can reach it."""
        self._ensure_session_registry()
        with self._session_lock:
            self._active_sessions.add(session)

    def _unregister_session(self, session: object) -> None:
        """Drop a closed session from the registry."""
        self._ensure_session_registry()
        with self._session_lock:
            self._active_sessions.discard(session)

    def terminate(self) -> None:
        """Terminate active subprocesses *and* close any open sessions.

        Extends :meth:`Agent.terminate` so a shutdown signal also tears down the
        persistent ``ClaudeSession`` processes, not just the one-shot ``-p``
        subprocesses.
        """
        try:
            super().terminate()
        finally:
            self._ensure_session_registry()
            with self._session_lock:
                sessions = list(self._active_sessions)
            for session in sessions:
                try:
                    session.close()
                except Exception:  # pragma: no cover - best-effort shutdown
                    logger.debug("Error closing session on terminate", exc_info=True)

    def can_handle(self, task: Task) -> bool:
        """Check availability then delegate to subclass task-type check.

        Returns False while the agent is disabled, giving the
        orchestrator a chance to try the next agent in the queue.
        """
        if self.is_disabled():
            logger.debug("Agent '%s' is disabled — skipping.", self.name)
            return False
        return self._can_handle_task(task)

    def is_disabled(self) -> bool:
        """True while the Claude quota cooldown is active."""
        with self._quota_lock:
            if self._disabled_until is None:
                return False
            now = datetime.now(timezone.utc)
            if now < self._disabled_until:
                return True
            # Cooldown expired — re-enable.
            self._disabled_until = None
            return False

    @staticmethod
    def _is_quota_error(output: str) -> bool:
        """Return True only for a *genuine* Claude usage-limit error.

        A naive substring match against broad terms like "quota" or "rate limit"
        produced false positives on any task whose text merely *discusses*
        quotas (issue #178). We instead require a specific usage-limit phrase, or
        a usage-limit phrase paired with a parseable reset time. Topical prose
        and code that talk about rate limits no longer trip self-disable.
        """
        lower = output.lower()
        if any(p in lower for p in _QUOTA_PATTERNS):
            return True
        # A parseable reset time is only a quota signal when paired with the word
        # "limit" — Claude's real message always says e.g. "your limit will reset
        # at …". The reset time alone is too generic (could be ambient text).
        if "limit" in lower and _RESET_RE.search(output) is not None:
            return True
        return False

    @staticmethod
    def _parse_reset_time(output: str) -> datetime | None:
        """Parse reset time from Claude's quota message.

        Expected formats:
            "Your limit will reset at 2pm (America/New_York)"
            "resets 7:30pm (Asia/Calcutta)"
        """
        match = _RESET_RE.search(output)
        if not match:
            return None

        time_str = match.group(1).strip()
        tz_str = match.group(2).strip()

        canonical = _TZ_ALIASES.get(tz_str, tz_str)
        try:
            tz = ZoneInfo(canonical)
        except (KeyError, ValueError):
            return None

        now = datetime.now(tz)

        # Parse time — handles "2pm", "2:30pm", "10 am", etc.
        try:
            for fmt in ("%I%p", "%I:%M%p", "%I %p", "%I:%M %p"):
                try:
                    parsed = datetime.strptime(time_str.upper(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        except Exception:
            return None

        reset = now.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0,
        )

        # If the reset time is in the past, it means tomorrow
        if reset <= now:
            reset += timedelta(days=1)

        return reset

    def _handle_quota_error(self, output: str) -> None:
        """Disable this agent until the quota resets.

        Parses the reset time from *output* and sets ``_disabled_until``.
        Falls back to a fixed cooldown when the time cannot be parsed,
        and logs the raw output to aid diagnosis.
        """
        reset_at = self._parse_reset_time(output)
        if reset_at:
            # Add a 30-second buffer so we don't race the provider clock.
            with self._quota_lock:
                self._disabled_until = reset_at.astimezone(timezone.utc) + timedelta(seconds=30)
            logger.warning(
                "Agent '%s' rate-limited. Disabled until %s.",
                self.name, self._disabled_until,
            )
        else:
            fallback = int(config.settings.get("quota_fallback_seconds", 30 * 60))
            with self._quota_lock:
                self._disabled_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=fallback)
                )
            logger.warning(
                "Agent '%s' rate-limited (couldn't parse reset time). "
                "Disabled for %ds. Raw output (truncated): %.500s",
                self.name,
                fallback,
                output,
            )

    # ------------------------------------------------------------------
    # Execution-state substrate — turn-boundary heartbeat (#267)
    # ------------------------------------------------------------------
    # The progress-driven write path: a real turn flowing through
    # ``_run_claude_cli`` stamps a ``turn_*`` event and bumps ``last_heartbeat``
    # in the pipeline's live-state snapshot. The pipeline key is read from the
    # ``pipeline_log.current_pipeline`` contextvar (already bound for the whole
    # ``_run_task`` body), ``repo`` from ``self.repo`` (a string), and ``base_dir``
    # from the value the orchestrator threaded down — so the agent never touches
    # the ``config.settings.base_dir`` property (which raises when unset) and its
    # heartbeat always lands beside the orchestrator's events. Strictly
    # best-effort: a substrate failure must never break a turn.

    def _execution_coords(self) -> tuple[Path, str, str] | None:
        """Return ``(base_dir, repo, pipeline_key)`` for substrate writes, or ``None``.

        ``None`` (no-op) whenever any coordinate is missing: no threaded
        ``base_dir`` (a bare/test agent), no repo, or no active pipeline.
        """
        base = self.base_dir
        repo = self.repo or ""
        pkey = pipeline_log.current_pipeline.get()
        if base is None or not repo or pkey is None:
            return None
        return base, repo, pkey

    def _emit_turn_event(self, event_type: str, state_tone: str) -> None:
        """Append a turn-boundary event to the active pipeline's log (best-effort)."""
        coords = self._execution_coords()
        if coords is None:
            return
        base, repo, pkey = coords
        try:
            from loony_dev import execution_state

            what = {
                "turn_start": "Claude turn started",
                "turn_complete": "Claude turn complete",
                "error": "Claude turn failed",
            }.get(event_type, event_type)
            execution_state.append_event(
                base, repo, pkey,
                execution_state.ExecutionEvent(
                    type=event_type,
                    what=what,
                    actor=execution_state.resolve_actor(execution_state.ACTOR_BOT),
                    target=execution_state.target_for(repo, pkey),
                    state_tone=state_tone,
                ),
            )
        except Exception:  # pragma: no cover - substrate is best-effort
            logger.debug("execution_state turn event failed", exc_info=True)

    def _bump_heartbeat(self) -> None:
        """Bump the active pipeline's snapshot ``last_heartbeat`` (best-effort)."""
        coords = self._execution_coords()
        if coords is None:
            return
        base, repo, pkey = coords
        try:
            from loony_dev import execution_state

            execution_state._bump_snapshot(base, repo, pkey)
        except Exception:  # pragma: no cover - substrate is best-effort
            logger.debug("execution_state heartbeat failed", exc_info=True)

    def _check_fence(self) -> None:
        """Stand down if this pipeline's lease was reclaimed under us (#268).

        Raises :class:`~loony_dev.pipeline_lease.LeaseFencedError` when the
        on-disk lease no longer matches the token the orchestrator set for this
        dispatch — so a turn that unwedges *after* its lease was reclaimed never
        double-runs against the new holder. Unlike :meth:`_bump_heartbeat` this is
        **not** best-effort: ``LeaseFencedError`` must propagate out of the turn
        loop to the orchestrator's stand-down branch. A no-op when there is no
        active pipeline / fence token (bare/test/drive path).
        """
        coords = self._execution_coords()
        if coords is None:
            return
        base, repo, pkey = coords
        from loony_dev import pipeline_lease

        pipeline_lease.check_fence(base, repo, pkey)

    # ------------------------------------------------------------------
    # Shared Claude CLI runner with session continuity
    # ------------------------------------------------------------------

    def _session_id_for(self, task: Task) -> str | None:
        """Compute a deterministic session ID for *task*, or None."""
        key = task.session_key
        if not key or not self.repo:
            return None
        return session_id_for(self.repo, key)

    # ------------------------------------------------------------------
    # On-disk observe registry (#202)
    # ------------------------------------------------------------------
    # The ``-p`` agents drive one-shot ``claude -p --resume`` turns with no
    # long-lived PTY, so nothing publishes a ``session.json`` the way the
    # persistent-PTY ``SessionBridge`` did. The dashboard's JSONL-driven observe
    # surface needs that on-disk entry — with the ``cwd`` + the *resolved*
    # session id the turns actually ran under — to discover the transcript to
    # tail. These helpers write/refresh it under the orchestrator-threaded
    # ``self.base_dir`` (#285) — the *same* tree the web reads — rather than the
    # ``config.settings.base_dir`` property (which raises when base_dir is unset).
    # They stay strictly best-effort: registry trouble must never break (or fail)
    # a task. A genuine write failure is logged loudly (``warning``); only the
    # legitimate "no threaded base_dir" no-op (a bare/test agent) is silent.

    def _register_observe_session(
        self, task: Task, work_dir: Path, session_id: str | None, *, status: str = "running",
    ) -> None:
        """Record *task*'s session on disk so the dashboard can observe it.

        *session_id* must be the id the turns actually run under (for a fresh
        branch this is the random id resolved in ``_open_session``, not the
        deterministic one), or the JSONL path will not resolve. No-op when the
        task has no worktree key, no repo, no resolved session id, or no
        threaded ``base_dir`` (a bare/test agent, mirroring ``_execution_coords``).
        """
        task_key = task.worktree_key
        if not task_key or not self.repo or not session_id or self.base_dir is None:
            return
        try:
            from loony_dev import session_registry

            session_registry.register_task_session(
                self.base_dir,
                self.repo,
                task_key,
                session_id=session_id,
                cwd=work_dir,
                status=status,
            )
        except Exception:  # pragma: no cover - registry is best-effort
            logger.warning(
                "Could not register observe session for %s", task_key, exc_info=True
            )

    def _mark_observe_session(self, task: Task, status: str) -> None:
        """Update *task*'s on-disk session status (e.g. ``idle`` when parked).

        Leaves ``cwd``/``session_id`` intact so the session stays observable
        from its transcript after the turn ends (#202). Best-effort no-op when
        unregistered, when there is no threaded ``base_dir``, or on any error.
        """
        task_key = task.worktree_key
        if not task_key or not self.repo or self.base_dir is None:
            return
        try:
            from loony_dev import session_registry

            session_registry.set_task_session_status(
                self.base_dir, self.repo, task_key, status,
            )
        except Exception:  # pragma: no cover - registry is best-effort
            logger.warning(
                "Could not update observe session for %s", task_key, exc_info=True
            )

    def _command_turn(
        self,
        work_dir: Path,
        command: str,
        payload: dict,
        *,
        task_key: str | None,
    ) -> str:
        """Write *payload* to a context file and return ``/<command> <path>``.

        The turn injected into the session is a short slash-command invocation;
        Claude Code expands ``$ARGUMENTS`` to the context-file path and the
        command body (under ``<work_dir>/.claude/commands/``) reads the JSON.

        Raises :class:`CommandNotInstalledError` if the command is not installed
        in *work_dir* — #165 installs the bundled commands into every worker
        checkout, so its absence is config drift, surfaced loudly rather than
        falling back to an inline prompt.
        """
        command_file = Path(work_dir) / ".claude" / "commands" / f"{command}.md"
        if not command_file.is_file():
            raise CommandNotInstalledError(
                f"slash command '/{command}' is not installed at {command_file} — "
                f"run `loony-dev setup` (commands are installed at worker startup, #165)",
            )
        path = write_context_file(command, payload, task_key=task_key or command)
        return f"/{command} {path}"

    def _run_claude_cli(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        """Run the Claude CLI with optional session continuity.

        One call == one logical turn (the ``--resume`` → ``--session-id`` create
        retry is part of the same turn). This is the single chokepoint every real
        agent turn (planning + coding) flows through, so it is where the #267
        execution-state substrate records turn boundaries: ``turn_start`` before,
        ``turn_complete`` + a **progress-driven heartbeat** after a successful
        return, ``error`` on any non-zero rc (quota / timeout 124 / failure). The
        heartbeat advances only on real turn progress — a wedged turn stops
        heart-beating, which is exactly the reliability signal #268 needs.

        The heartbeat bumps at **turn start** as well as turn complete (#268):
        starting a turn *is* real progress, and an implement dispatch interleaves
        turns with a multi-minute ``coderabbit review`` subprocess that never
        bumps the heartbeat — bumping at start bounds the worst-case inter-
        heartbeat gap to a single turn cap rather than CodeRabbit + a full turn,
        so a healthy worker is never falsely reclaimed. Each boundary first calls
        :meth:`_check_fence`: a worker whose lease was reclaimed while wedged
        raises :class:`~loony_dev.pipeline_lease.LeaseFencedError` here and stands
        down before any further turn or GitHub mutation.
        """
        self._check_fence()
        self._emit_turn_event("turn_start", "active")
        self._bump_heartbeat()
        try:
            stdout, stderr, rc = self._run_claude_cli_inner(
                prompt, cwd=cwd, session_id=session_id, timeout=timeout,
            )
        except BaseException:
            self._emit_turn_event("error", "blocked")
            raise
        if rc == 0:
            self._check_fence()
            self._emit_turn_event("turn_complete", "active")
            self._bump_heartbeat()
        else:
            self._emit_turn_event("error", "blocked")
        return stdout, stderr, rc

    def _run_claude_cli_inner(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        """The raw ``claude -p`` runner (``--resume`` → ``--session-id`` retry).

        Split from :meth:`_run_claude_cli` so the turn-boundary instrumentation
        wraps exactly one logical turn without re-emitting on the internal create
        retry. When *session_id* is provided, attempts ``--resume`` first; if that
        fails because no matching session is found, retries with ``--session-id``
        to create a new session with the given UUID. *timeout* (seconds) bounds
        each invocation; on expiry the CLI process group is killed and the call
        returns rc ``124`` (a timeout is not a "session not found", so it is
        returned as-is without the create retry).
        """
        if session_id:
            stdout, stderr, rc = self._invoke_claude(
                prompt, cwd=cwd, extra_flags=["--resume", session_id], timeout=timeout,
            )
            if rc == 0 or not self._is_session_not_found(f"{stdout}\n{stderr}"):
                return stdout, stderr, rc
            logger.debug("Session %s not found — creating new session", session_id)
            return self._invoke_claude(
                prompt, cwd=cwd, extra_flags=["--session-id", session_id], timeout=timeout,
            )
        return self._invoke_claude(prompt, cwd=cwd, timeout=timeout)

    # Returncode used when a ``claude -p`` invocation is killed for exceeding its
    # timeout. Mirrors the shell convention (128 + SIGKILL? no — matches the
    # ``timeout(1)`` utility's 124) so it is recognisable and never 0.
    _CLI_TIMEOUT_RC = 124

    def _invoke_claude(
        self,
        prompt: str,
        *,
        cwd: Path,
        extra_flags: list[str] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        """Spawn ``claude -p`` and return (stdout, stderr, returncode).

        Prompt is passed via stdin to avoid OS ARG_MAX limits on large inputs.
        When *timeout* is set and the process overruns it, the whole process
        group is SIGKILLed (``start_new_session=True`` puts the CLI and any
        children in their own group) and ``(stdout_so_far, msg, 124)`` is
        returned rather than hanging the worker.
        """
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        if extra_flags:
            cmd.extend(extra_flags)

        with subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as proc:
            self._register_process(proc)
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):  # already gone
                    pass
                stdout, stderr = proc.communicate()
                return (stdout or ""), f"claude -p timed out after {timeout:.0f}s", self._CLI_TIMEOUT_RC
            finally:
                self._unregister_process(proc)
        return stdout, stderr, proc.returncode

    @staticmethod
    def _is_session_not_found(output: str) -> bool:
        """Return True if *output* indicates a missing/invalid session."""
        lower = output.lower()
        return any(p in lower for p in _SESSION_NOT_FOUND_PATTERNS)
