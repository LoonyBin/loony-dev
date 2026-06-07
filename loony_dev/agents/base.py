from __future__ import annotations

import subprocess
import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from loony_dev.models import TaskResult
    from loony_dev.tasks.base import Task


class Agent(ABC):
    """Base class for all agents. Different agents use different tools."""

    name: str

    def _ensure_registry(self) -> None:
        """Lazily create the per-instance subprocess registry.

        A single agent instance may run several tasks concurrently (one per
        thread-pool worker), so the set of live subprocesses is shared across
        threads and guarded by a lock. ``setdefault`` is atomic under the GIL,
        so this is safe even if two workers race on first use.
        """
        self.__dict__.setdefault("_proc_lock", threading.Lock())
        self.__dict__.setdefault("_active_processes", set())

    def _register_process(self, proc: subprocess.Popen) -> None:
        """Record a freshly spawned subprocess so terminate() can reach it."""
        self._ensure_registry()
        with self._proc_lock:
            self._active_processes.add(proc)

    def _unregister_process(self, proc: subprocess.Popen) -> None:
        """Drop a finished subprocess from the registry."""
        self._ensure_registry()
        with self._proc_lock:
            self._active_processes.discard(proc)

    @abstractmethod
    def can_handle(self, task: Task) -> bool:
        """Whether this agent can handle the given task type.

        Multiple agents may be able to handle the same task type (e.g. a
        Claude coding agent and a Gemini coding agent). Only one will be
        configured with valid credentials in a given deployment, so the
        orchestrator checks can_handle on each agent in turn and uses the
        first affirmative response.
        """
        ...

    @abstractmethod
    def execute(self, task: Task, work_dir: Path) -> TaskResult:
        """Execute a task in *work_dir*. Blocking. Returns result.

        *work_dir* is the directory (a per-task git worktree, or the base
        checkout for tasks without a worktree) in which the agent runs all
        of its git and CLI operations.
        """
        ...

    def terminate(self) -> None:
        """Terminate every currently active subprocess, if any.

        With concurrent dispatch a single agent instance may have several
        Claude subprocesses in flight at once, so this terminates all of them
        (SIGTERM, then SIGKILL after a grace period).
        """
        self._ensure_registry()
        with self._proc_lock:
            procs = list(self._active_processes)
        for proc in procs:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass
