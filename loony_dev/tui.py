"""Terminal User Interface for monitoring the loony-dev supervisor and workers.

Run with: loony-dev ui [--base-dir PATH]
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Header, RichLog, Static, Tab, Tabs

from loony_dev import config


# ---------------------------------------------------------------------------
# inotify helpers (Linux only; graceful no-op on other platforms)
# ---------------------------------------------------------------------------

_IN_MODIFY = 0x00000002  # File was modified
_IN_CLOSE_WRITE = 0x00000008  # Writable file was closed

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    _INOTIFY_AVAILABLE = (
        hasattr(_libc, "inotify_init1")
        and hasattr(_libc, "inotify_add_watch")
        and hasattr(_libc, "inotify_rm_watch")
    )
except OSError:
    _INOTIFY_AVAILABLE = False




# ---------------------------------------------------------------------------
# Log watcher
# ---------------------------------------------------------------------------

class LogWatcher:
    """Tails a single log file, returning lines from the beginning on first open.

    On the first successful open, reads from the beginning of the file so that
    existing log content is loaded into the buffer. Handles missing files
    gracefully until they appear. Accumulates received lines in ``self.buffer``
    (capped at config.settings.UI.MAX_BUFFER_LINES).

    Uses inotify on Linux to detect modifications without spinning; falls back
    to unconditional polling on platforms where inotify is unavailable.
    """

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._file = None
        self.buffer: list[str] = []
        # inotify state (-1 means not in use)
        self._inotify_fd: int = -1
        self._inotify_wd: int = -1
        if _INOTIFY_AVAILABLE:
            try:
                fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
                if fd >= 0:
                    self._inotify_fd = fd
            except OSError:
                pass

    def _inotify_add_watch(self) -> None:
        """Register an inotify watch on the log file (called once it exists)."""
        if self._inotify_fd < 0 or self._inotify_wd >= 0:
            return
        try:
            wd = _libc.inotify_add_watch(
                self._inotify_fd,
                str(self._path).encode(),
                _IN_MODIFY | _IN_CLOSE_WRITE,
            )
            if wd >= 0:
                self._inotify_wd = wd
        except OSError:
            pass

    def _inotify_has_events(self) -> bool:
        """Return True if inotify reports the file was modified; drain the fd."""
        if self._inotify_fd < 0 or self._inotify_wd < 0:
            return False
        try:
            r, _, _ = select.select([self._inotify_fd], [], [], 0)
            if r:
                os.read(self._inotify_fd, 4096)  # drain pending events
                return True
        except OSError:
            pass
        return False

    def poll(self) -> list[str]:
        """Return new lines appended to the file since the last call.

        When inotify is available the read is skipped entirely if no
        IN_MODIFY / IN_CLOSE_WRITE event has been received, avoiding a
        tight-polling loop. On platforms without inotify the method always
        attempts a read (original polling behaviour).
        """
        if self._file is None:
            if not self._path.exists():
                return []
            try:
                self._file = open(self._path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
                self._inotify_add_watch()
            except OSError:
                return []
        elif self._inotify_fd >= 0 and self._inotify_wd >= 0:
            # inotify is active — skip the read if nothing has changed
            if not self._inotify_has_events():
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
            excess = len(self.buffer) - config.settings.UI.MAX_BUFFER_LINES
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
        if self._inotify_fd >= 0:
            try:
                os.close(self._inotify_fd)
            except OSError:
                pass
            self._inotify_fd = -1
            self._inotify_wd = -1


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TabBarEntry:
    label: str
    log_path: Path
    pid_path: Path | None = None

    @property
    def tab_id(self) -> str:
        """Convert the worker label to a valid CSS identifier for use as a tab ID."""
        return self.label.replace("/", "-").replace(" ", "-")

    def _is_running(self) -> bool | None:
        """Check whether the process recorded in *pid_path* is alive.

        Returns:
            True  — pid file exists, PID is valid, os.kill(pid, 0) succeeds.
            None  — os.kill raised PermissionError (process exists but unknown ownership).
            False — pid file missing, invalid content, or ProcessLookupError.
        """
        if self.pid_path is None:
            return False
        try:
            pid = int(self.pid_path.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return None

    @property
    def tab_label(self) -> str:
        """Return the Rich-markup label shown on the tab (badge + name)."""
        running = self._is_running()
        if running is True:
            badge = "[green]\u25cf[/green]"
        elif running is None:
            badge = "[yellow]\u25cf[/yellow]"
        else:
            badge = "[dim]\u25cb[/dim]"
        return f"{badge} {self.label}"


# ---------------------------------------------------------------------------
# Tab bar widget
# ---------------------------------------------------------------------------

class WorkerTabBar(Widget):
    """Horizontal tab bar listing Supervisor and all discovered worker repos."""

    DEFAULT_CSS = """
    WorkerTabBar {
        height: auto;
        border-top: solid $panel-lighten-1;
    }
    """

    can_focus = False

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
        self._entries: list[TabBarEntry] = []

    @property
    def entries(self) -> list[TabBarEntry]:
        return self._entries

    def compose(self) -> ComposeResult:
        yield Tabs()

    async def on_mount(self) -> None:
        self.query_one(Tabs).can_focus = False
        await self._scan()
        self.set_interval(self._scan_interval, self._scan)

    def _discover_entries(self) -> list[TabBarEntry]:
        """Build the list of tab-bar entries by scanning the logs directory."""
        logs_dir = self._base_dir / ".logs"

        # Supervisor is always first
        entries: list[TabBarEntry] = [
            TabBarEntry(
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
                    entries.append(TabBarEntry(
                        label=f"{owner_dir.name}/{repo_dir.name}",
                        log_path=repo_dir / "loony-worker.log",
                        pid_path=repo_dir / "loony-worker.pid",
                    ))

        return entries

    async def _scan(self) -> None:
        """Re-discover worker log directories and rebuild the tab bar."""
        tabs = self.query_one(Tabs)
        old_active = tabs.active

        self._entries = self._discover_entries()

        await tabs.clear()
        new_active = old_active if old_active else ""
        valid_ids = set()
        for entry in self._entries:
            tid = entry.tab_id
            valid_ids.add(tid)
            tabs.add_tab(Tab(entry.tab_label, id=tid))

        if new_active not in valid_ids:
            new_active = self._entries[0].tab_id if self._entries else ""
        if new_active:
            self.call_after_refresh(setattr, tabs, "active", new_active)


# ---------------------------------------------------------------------------
# Log pane widget
# ---------------------------------------------------------------------------

class LogPane(Widget):
    """Right pane that tails and displays log lines for the selected TabBar item.

    Maintains one RichLog per tab and toggles visibility, so tab switches are
    instantaneous instead of replaying the entire buffer.  Only the last
    config.settings.UI.TAIL_LINES are written initially; the full buffer is loaded lazily when
    the user scrolls to the top.
    """

    DEFAULT_CSS = """
    LogPane {
        width: 1fr;
        height: 1fr;
    }
    LogPane > RichLog {
        height: 1fr;
        display: none;
    }
    LogPane > #follow-banner {
        height: 1;
        background: $warning;
        color: $warning-muted;
        text-align: center;
        display: none;
    }
    """

    follow: reactive[bool] = reactive(True)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._watcher: LogWatcher | None = None
        self._logs: dict[str, RichLog] = {}
        self._watchers: dict[str, LogWatcher] = {}  # key -> watcher for lazy load
        self._fully_loaded: set[str] = set()
        self._active_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Follow paused \u2014 press [f] to resume", id="follow-banner")

    def on_mount(self) -> None:
        self.set_interval(0.5, self._poll)
        self.set_interval(0.25, self._check_scroll)

    def watch_follow(self, follow: bool) -> None:
        self.query_one("#follow-banner").display = not follow
        if follow and self._active_key and self._active_key in self._logs:
            self._logs[self._active_key].scroll_end(animate=False)

    def preload(self, key: str, watcher: LogWatcher) -> None:
        """Create a hidden RichLog for *key* with the tail of the buffer."""
        if key in self._logs:
            return
        log = RichLog(highlight=False, markup=False, wrap=True)
        self._logs[key] = log
        self._watchers[key] = watcher
        self.mount(log, before=self.query_one("#follow-banner"))
        tail = watcher.buffer[-config.settings.UI.TAIL_LINES:] if len(watcher.buffer) > config.settings.UI.TAIL_LINES else watcher.buffer
        for line in tail:
            log.write(line)
        if len(watcher.buffer) <= config.settings.UI.TAIL_LINES:
            self._fully_loaded.add(key)

    def switch_watcher(self, key: str, watcher: LogWatcher) -> None:
        """Switch to the log for *key*."""
        # Hide the currently visible log
        if self._active_key and self._active_key in self._logs:
            self._logs[self._active_key].display = False

        self._watcher = watcher
        self._active_key = key
        self._watchers[key] = watcher

        if key not in self._logs:
            self.preload(key, watcher)

        log = self._logs[key]
        log.display = True
        log.scroll_end(animate=False)
        log.focus()

    def _check_scroll(self) -> None:
        """If the active log is scrolled to the top and not fully loaded, load all lines."""
        if self._active_key is None or self._active_key in self._fully_loaded:
            return
        log = self._logs.get(self._active_key)
        if log is None:
            return
        if log.scroll_offset.y > 0:
            return
        # Scrolled to top — load the full buffer
        watcher = self._watchers.get(self._active_key)
        if watcher is None:
            return
        # The tail that was loaded starts at this offset in the full buffer
        prepended = max(0, len(watcher.buffer) - config.settings.UI.TAIL_LINES)
        log.clear()
        for line in watcher.buffer:
            log.write(line)
        # Restore scroll so the view stays on the same content
        log.scroll_to(y=prepended, animate=False)
        self._fully_loaded.add(self._active_key)

    def _poll(self) -> None:
        if self._watcher is None or self._active_key is None:
            return
        new_lines = self._watcher.poll()
        if not new_lines:
            return
        log = self._logs.get(self._active_key)
        if log is None:
            return
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
    #hint-bar {
        height: 1;
        background: $panel;
        color: $text-muted;
        text-align: center;
    }
    """

    BINDINGS = [
        Binding("left",  "prev_tab",      "Prev tab"),
        Binding("right", "next_tab",      "Next tab"),
        Binding("f",     "toggle_follow", "Toggle follow"),
        Binding("q",     "quit",          "Quit"),
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
        yield LogPane()
        yield WorkerTabBar(
            base_dir=self._base_dir,
            supervisor_log=self._supervisor_log,
            scan_interval=self._scan_interval,
        )
        yield Static(
            "[b]←/→[/b] switch tab  [b]↑/↓/PgUp/PgDn[/b] scroll  [b]f[/b] follow  [b]q[/b] quit",
            id="hint-bar",
        )

    def on_mount(self) -> None:
        # Pre-load all tabs so even the first switch is instant
        log_pane = self.query_one(LogPane)
        tab_bar = self.query_one(WorkerTabBar)
        for entry in tab_bar.entries:
            watcher = self._get_watcher(entry)
            log_pane.preload(entry.label, watcher)
        self._select_by_index(0)

    def _get_watcher(self, entry: TabBarEntry) -> LogWatcher:
        """Get or create a persistent LogWatcher for the given entry."""
        if entry.label not in self._watchers:
            w = LogWatcher(entry.log_path)
            w.poll()  # pre-load buffer before first display
            self._watchers[entry.label] = w
        return self._watchers[entry.label]

    def _select_by_index(self, index: int) -> None:
        """Switch the log pane to the tab bar item at *index*."""
        tab_bar = self.query_one(WorkerTabBar)
        if not tab_bar.entries or index >= len(tab_bar.entries):
            return
        entry = tab_bar.entries[index]
        if entry.label == self._current_label:
            return  # Same item — no need to reset the pane
        self._current_label = entry.label
        watcher = self._get_watcher(entry)
        self.query_one(LogPane).switch_watcher(entry.label, watcher)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab is None:
            return
        tab_bar = self.query_one(WorkerTabBar)
        activated_id = event.tab.id
        for i, entry in enumerate(tab_bar.entries):
            if entry.tab_id == activated_id:
                self._select_by_index(i)
                return

    def action_prev_tab(self) -> None:
        tab_bar = self.query_one(WorkerTabBar)
        entries = tab_bar.entries
        if not entries:
            return
        current = next(
            (i for i, e in enumerate(entries) if e.label == self._current_label), 0
        )
        new_index = (current - 1) % len(entries)
        new_entry = entries[new_index]
        self.query_one(Tabs).active = new_entry.tab_id
        self._select_by_index(new_index)

    def action_next_tab(self) -> None:
        tab_bar = self.query_one(WorkerTabBar)
        entries = tab_bar.entries
        if not entries:
            return
        current = next(
            (i for i, e in enumerate(entries) if e.label == self._current_label), 0
        )
        new_index = (current + 1) % len(entries)
        new_entry = entries[new_index]
        self.query_one(Tabs).active = new_entry.tab_id
        self._select_by_index(new_index)

    def action_toggle_follow(self) -> None:
        log_pane = self.query_one(LogPane)
        log_pane.follow = not log_pane.follow

    def on_unmount(self) -> None:
        for watcher in self._watchers.values():
            watcher.close()
