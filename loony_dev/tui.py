"""Terminal User Interface for monitoring the loony-dev supervisor and workers.

Run with: loony-dev ui [--base-dir PATH]
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Header, Label, ListItem, ListView, RichLog, Static


MAX_BUFFER_LINES = 5000


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

def is_running(pid_path: Path | None) -> bool | None:
    """Check whether the process recorded in *pid_path* is alive.

    Returns:
        True  — pid file exists, PID is valid, os.kill(pid, 0) succeeds.
        None  — os.kill raised PermissionError (process exists but unknown ownership).
        False — pid file missing, invalid content, or ProcessLookupError.
    """
    if pid_path is None:
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


# ---------------------------------------------------------------------------
# Log watcher
# ---------------------------------------------------------------------------

class LogWatcher:
    """Tails a single log file, returning only lines written after creation.

    On the first successful open, seeks to end-of-file so that historical
    content is skipped. Handles missing files gracefully until they appear.
    Accumulates received lines in ``self.buffer`` (capped at MAX_BUFFER_LINES).
    """

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._file = None
        self._seeked = False
        self.buffer: list[str] = []

    def poll(self) -> list[str]:
        """Return new lines appended to the file since the last call."""
        if self._file is None:
            if not self._path.exists():
                return []
            try:
                self._file = open(self._path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
                if not self._seeked:
                    self._file.seek(0, 2)  # Seek to end on first open
                    self._seeked = True
            except OSError:
                return []

        new_lines: list[str] = []
        try:
            while True:
                line = self._file.readline()
                if not line:
                    break
                new_lines.append(line.rstrip("\n"))
        except OSError:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

        if new_lines:
            self.buffer.extend(new_lines)
            excess = len(self.buffer) - MAX_BUFFER_LINES
            if excess > 0:
                del self.buffer[:excess]

        return new_lines

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SidebarEntry:
    label: str
    log_path: Path
    pid_path: Path | None = None


# ---------------------------------------------------------------------------
# Sidebar widget
# ---------------------------------------------------------------------------

class WorkerSidebar(Widget):
    """Left sidebar listing Supervisor and all discovered worker repos."""

    DEFAULT_CSS = """
    WorkerSidebar {
        width: 30;
        height: 100%;
        border-right: solid $panel-lighten-1;
    }
    WorkerSidebar > ListView {
        height: 1fr;
        background: transparent;
    }
    """

    def __init__(
        self,
        base_dir: Path,
        supervisor_log: Path,
        scan_interval: float = 5.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._base_dir = base_dir
        self._supervisor_log = supervisor_log
        self._scan_interval = scan_interval
        self._entries: list[SidebarEntry] = []

    @property
    def entries(self) -> list[SidebarEntry]:
        return self._entries

    def compose(self) -> ComposeResult:
        yield ListView()

    def on_mount(self) -> None:
        self._scan()
        self.set_interval(self._scan_interval, self._scan)

    def _scan(self) -> None:
        """Re-discover worker log directories and rebuild the list."""
        lv = self.query_one(ListView)
        logs_dir = self._base_dir / ".logs"

        # Preserve selected item by label across rebuilds
        old_index = lv.index
        old_label: str | None = None
        if old_index is not None and old_index < len(self._entries):
            old_label = self._entries[old_index].label

        # Supervisor is always first
        entries: list[SidebarEntry] = [
            SidebarEntry(
                label="Supervisor",
                log_path=self._supervisor_log,
                pid_path=logs_dir / "supervisor.pid",
            )
        ]

        # Discover worker log directories: .logs/<owner>/<repo>/
        if logs_dir.exists():
            for owner_dir in sorted(logs_dir.iterdir()):
                if not owner_dir.is_dir() or owner_dir.name.startswith("."):
                    continue
                for repo_dir in sorted(owner_dir.iterdir()):
                    if not repo_dir.is_dir():
                        continue
                    entries.append(SidebarEntry(
                        label=f"{owner_dir.name}/{repo_dir.name}",
                        log_path=repo_dir / "loony-worker.log",
                        pid_path=repo_dir / "loony-worker.pid",
                    ))

        self._entries = entries

        # Rebuild ListView, preserving the previously selected item
        lv.clear()
        new_selected_index = 0
        for i, entry in enumerate(entries):
            running = is_running(entry.pid_path)
            if running is True:
                badge = "[green]●[/green]"
            elif running is None:
                badge = "[yellow]●[/yellow]"
            else:
                badge = "[dim]○[/dim]"
            lv.append(ListItem(Label(f"{badge} {entry.label}", markup=True)))
            if entry.label == old_label:
                new_selected_index = i

        if entries:
            lv.index = new_selected_index


# ---------------------------------------------------------------------------
# Log pane widget
# ---------------------------------------------------------------------------

class LogPane(Widget):
    """Right pane that tails and displays log lines for the selected sidebar item."""

    DEFAULT_CSS = """
    LogPane {
        width: 1fr;
        height: 100%;
    }
    LogPane > RichLog {
        height: 1fr;
    }
    LogPane > #follow-banner {
        height: 1;
        background: $warning;
        color: $warning-text;
        text-align: center;
        display: none;
    }
    """

    follow: reactive[bool] = reactive(True)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._watcher: LogWatcher | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=False, markup=False, wrap=True)
        yield Static("Follow paused — press [f] to resume", id="follow-banner")

    def on_mount(self) -> None:
        self.set_interval(0.5, self._poll)

    def watch_follow(self, follow: bool) -> None:
        self.query_one("#follow-banner").display = not follow
        if follow:
            self.query_one(RichLog).scroll_end(animate=False)

    def switch_watcher(self, watcher: LogWatcher) -> None:
        """Switch to *watcher* and display its accumulated buffer."""
        self._watcher = watcher
        log = self.query_one(RichLog)
        log.clear()
        for line in watcher.buffer:
            log.write(line)
        if self.follow:
            log.scroll_end(animate=False)

    def _poll(self) -> None:
        if self._watcher is None:
            return
        new_lines = self._watcher.poll()
        if not new_lines:
            return
        log = self.query_one(RichLog)
        for line in new_lines:
            log.write(line)
        if self.follow:
            log.scroll_end(animate=False)


# ---------------------------------------------------------------------------
# Root application
# ---------------------------------------------------------------------------

class SupervisorApp(App):
    """Textual TUI for monitoring the loony-dev supervisor and workers."""

    TITLE = "loony-dev supervisor"

    DEFAULT_CSS = """
    Screen {
        layout: vertical;
    }
    #main-area {
        layout: horizontal;
        height: 1fr;
    }
    #hint-bar {
        height: 1;
        background: $panel;
        color: $text-muted;
        text-align: center;
    }
    """

    BINDINGS = [
        Binding("f", "toggle_follow", "Toggle follow"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        base_dir: Path,
        supervisor_log: Path,
        scan_interval: float = 5.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._base_dir = base_dir
        self._supervisor_log = supervisor_log
        self._scan_interval = scan_interval
        self._watchers: dict[str, LogWatcher] = {}
        self._current_label: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            yield WorkerSidebar(
                base_dir=self._base_dir,
                supervisor_log=self._supervisor_log,
                scan_interval=self._scan_interval,
            )
            yield LogPane()
        yield Static(
            "[b]↑/↓[/b] navigate  [b]j/k[/b] vi-nav  [b]f[/b] toggle follow  [b]q[/b] quit",
            id="hint-bar",
        )

    def on_mount(self) -> None:
        self._select_by_index(0)

    def _get_watcher(self, entry: SidebarEntry) -> LogWatcher:
        """Get or create a persistent LogWatcher for the given entry."""
        if entry.label not in self._watchers:
            self._watchers[entry.label] = LogWatcher(entry.log_path)
        return self._watchers[entry.label]

    def _select_by_index(self, index: int) -> None:
        """Switch the log pane to the sidebar item at *index*."""
        sidebar = self.query_one(WorkerSidebar)
        if not sidebar.entries or index >= len(sidebar.entries):
            return
        entry = sidebar.entries[index]
        if entry.label == self._current_label:
            return  # Same item — no need to reset the pane
        self._current_label = entry.label
        watcher = self._get_watcher(entry)
        self.query_one(LogPane).switch_watcher(watcher)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None or event.list_view.index is None:
            return
        self._select_by_index(event.list_view.index)

    def action_toggle_follow(self) -> None:
        log_pane = self.query_one(LogPane)
        log_pane.follow = not log_pane.follow

    def on_unmount(self) -> None:
        for watcher in self._watchers.values():
            watcher.close()
