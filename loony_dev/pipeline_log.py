"""Per-pipeline worker logging (issue #220).

A worker emits a **single** process stream today: stdlib ``logging`` → stderr,
which the supervisor ``dup2``s onto ``<base>/.logs/<owner>/<repo>/loony-worker.log``.
That worker-scope log stays the full process stream (and every existing consumer
keeps reading it verbatim). This module adds a **second, parallel capture**: a
per-pipeline log keyed by the ``issue-N`` / ``pr-P`` worktree key, written under
the same ``.logs/<owner>/<repo>/`` tree, so the dashboard can tail a single
pipeline's activity independently and the Issue ▸ PR activity timeline (#225)
has a structured per-pipeline feed to read.

How records are routed
----------------------
Each task already runs in its own :class:`~concurrent.futures.ThreadPoolExecutor`
thread (``Orchestrator._run_task``). The active pipeline is tagged via a
:class:`contextvars.ContextVar` set at the top of that worker body; a custom
:class:`logging.Handler` on the root logger reads the var on every ``emit`` and
appends the record to that pipeline's file. Because contextvars are per-thread,
concurrent tasks never cross-contaminate, and all synchronous logging beneath
``_run_task`` (git prep, ``agent.execute``, the terminal callbacks) inherits the
key with no extra plumbing.

The log-path contract (source of truth for #225)
-------------------------------------------------
There is **one** way to locate a pipeline's log, and it is **forward-only**::

    pipeline_log_path(base_dir, owner, repo, pipeline_key)
      = repo_log_dir(...) / "pipelines" / f"{task_slug(pipeline_key)}.log"

``session_registry.task_slug`` appends an irreversible sha256 suffix, so **no
code ever reverses a ``*.log`` stem back to a key**. To still enumerate *which*
pipelines have logs, the handler records the raw key in a sibling ``<slug>.key``
sidecar on first write; listing reads sidecars, never stems.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TextIO

from loony_dev import session_registry

# The active pipeline key (``issue-N`` / ``pr-P``) for the current execution
# context, or ``None`` for worker-scope-only records (orchestrator ticks,
# no-worktree tasks). Per-thread by virtue of being a ContextVar, so concurrent
# task threads route independently.
current_pipeline: ContextVar[str | None] = ContextVar("current_pipeline", default=None)

PIPELINES_DIR_NAME = "pipelines"

# Same shape as the worker-log formatter (cli.py) so the #225 parser sees a
# stable line format across both scopes.
DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


@contextmanager
def pipeline_log_context(pipeline_key: str):
    """Bind *pipeline_key* as the active pipeline for the duration of the block.

    Token-based reset so nesting (a pipeline-scoped block inside another) restores
    the previous value rather than clobbering it to ``None``.
    """
    token = current_pipeline.set(pipeline_key)
    try:
        yield
    finally:
        current_pipeline.reset(token)


def pipeline_logs_dir(base_dir: Path, owner: str, repo: str) -> Path:
    """Return ``<base>/.logs/<owner>/<repo>/pipelines`` (a sibling of ``sessions``)."""
    return session_registry.repo_log_dir(base_dir, owner, repo) / PIPELINES_DIR_NAME


def pipeline_log_path(base_dir: Path, owner: str, repo: str, pipeline_key: str) -> Path:
    """The canonical, forward-only locator for a pipeline's log file.

    The single source of truth for both the writer (:class:`PipelineLogHandler`)
    and every reader (the web layer, #225). Slugs the key with the same
    deterministic :func:`session_registry.task_slug` used for the session
    registry, so the two layouts stay consistent.
    """
    return pipeline_logs_dir(base_dir, owner, repo) / f"{session_registry.task_slug(pipeline_key)}.log"


def pipeline_key_sidecar_path(base_dir: Path, owner: str, repo: str, pipeline_key: str) -> Path:
    """The ``<slug>.key`` sidecar holding the raw key for forward-only listing."""
    return pipeline_logs_dir(base_dir, owner, repo) / f"{session_registry.task_slug(pipeline_key)}.key"


class PipelineLogHandler(logging.Handler):
    """Route INFO+ records to the active pipeline's log file.

    Installed on the **root** logger so it captures every module logger
    (``agents.*``, ``tasks.*``, ``git``, ``coderabbit``, ``github.*``). On
    ``emit`` it reads :data:`current_pipeline`; a record with no active pipeline
    (worker-scope only) is dropped here — it still reaches the worker log via the
    other handlers. Open file handles are cached (lazy-open, append, flush each
    line) so a long-running worker does not churn syscalls per record.
    """

    def __init__(self, base_dir: Path, owner: str, repo: str, *, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.base_dir = Path(base_dir)
        self.owner = owner
        self.repo = repo
        self.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
        self._handles: dict[Path, TextIO] = {}
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        pipeline_key = current_pipeline.get()
        if pipeline_key is None:
            return  # worker-scope-only record; no pipeline to route to
        try:
            message = self.format(record)
            self._write(pipeline_key, message)
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    def _write(self, pipeline_key: str, message: str) -> None:
        path = pipeline_log_path(self.base_dir, self.owner, self.repo, pipeline_key)
        with self._lock:
            handle = self._handles.get(path)
            if handle is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Record the raw key in a sidecar the first time we open this
                # pipeline's log, so listing can recover the key without
                # reversing the irreversible slug (the forward-only contract).
                self._write_sidecar(pipeline_key)
                handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
                self._handles[path] = handle
            handle.write(message + "\n")
            handle.flush()

    def _write_sidecar(self, pipeline_key: str) -> None:
        sidecar = pipeline_key_sidecar_path(self.base_dir, self.owner, self.repo, pipeline_key)
        if not sidecar.exists():
            sidecar.write_text(pipeline_key, encoding="utf-8")

    def close(self) -> None:
        """Close every cached file handle, then the handler."""
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        super().close()
