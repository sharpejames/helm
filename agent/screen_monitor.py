"""
agent/screen_monitor.py — Low-cost local screen state monitor.

Uses Win32/UIA APIs (no vision model) to track:
- Foreground window title and process
- List of open visible windows
- Modal/dialog detection
- State change detection between steps

Runs in ~10ms per check. No API calls.
"""

import ctypes
import ctypes.wintypes
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    visible: bool
    is_foreground: bool = False


@dataclass
class ScreenState:
    """Snapshot of the current screen state."""
    timestamp: float
    foreground_title: str
    foreground_pid: int
    visible_windows: list[WindowInfo] = field(default_factory=list)
    window_count: int = 0

    def summary(self) -> str:
        """Human-readable summary for LLM context."""
        lines = [f"Foreground: {self.foreground_title or '(none)'}"]
        lines.append(f"Open windows ({self.window_count}):")
        for w in self.visible_windows[:15]:  # cap at 15
            marker = " ← ACTIVE" if w.is_foreground else ""
            lines.append(f"  - {w.title}{marker}")
        return "\n".join(lines)


class ScreenMonitor:
    """
    Monitors screen state cheaply using Win32 APIs.
    Call check() each step to get current state and detect changes.
    """

    def __init__(self):
        self._last_state: ScreenState | None = None
        self._last_foreground: str = ""

    def check(self) -> tuple[ScreenState, list[str]]:
        """
        Check current screen state. Returns (state, changes).
        changes is a list of human-readable change descriptions (empty if nothing changed).
        """
        state = self._capture()
        changes = self._diff(state)
        self._last_state = state
        self._last_foreground = state.foreground_title
        return state, changes

    def _capture(self) -> ScreenState:
        """Capture current screen state via Win32."""
        # Get foreground window
        fg_hwnd = user32.GetForegroundWindow()
        fg_title = self._get_title(fg_hwnd)
        fg_pid = self._get_pid(fg_hwnd)

        # Enumerate visible windows
        windows = []
        def enum_callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                title = self._get_title(hwnd)
                if title and len(title.strip()) > 0:
                    pid = self._get_pid(hwnd)
                    windows.append(WindowInfo(
                        hwnd=hwnd,
                        title=title,
                        pid=pid,
                        visible=True,
                        is_foreground=(hwnd == fg_hwnd),
                    ))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)

        return ScreenState(
            timestamp=time.time(),
            foreground_title=fg_title,
            foreground_pid=fg_pid,
            visible_windows=windows,
            window_count=len(windows),
        )

    def _diff(self, current: ScreenState) -> list[str]:
        """Detect changes between last state and current."""
        if self._last_state is None:
            return []

        changes = []
        prev = self._last_state

        # Foreground changed
        if current.foreground_title != prev.foreground_title:
            changes.append(f"Window focus changed: '{prev.foreground_title}' → '{current.foreground_title}'")

        # New windows appeared
        prev_titles = {w.title for w in prev.visible_windows}
        curr_titles = {w.title for w in current.visible_windows}
        new_windows = curr_titles - prev_titles
        closed_windows = prev_titles - curr_titles

        for t in new_windows:
            changes.append(f"New window opened: '{t}'")
        for t in closed_windows:
            if t:  # skip empty titles
                changes.append(f"Window closed: '{t}'")

        return changes

    def detect_modal(self) -> str | None:
        """
        Check if a modal dialog is blocking the foreground app.
        Returns the modal title if detected, None otherwise.
        """
        fg_hwnd = user32.GetForegroundWindow()
        fg_title = self._get_title(fg_hwnd)

        # Check if the foreground window looks like a dialog
        # (small window, owned by another window, or has dialog-like title)
        style = user32.GetWindowLongW(fg_hwnd, -16)  # GWL_STYLE
        is_dialog = bool(style & 0x80000000)  # WS_POPUP
        owner = user32.GetWindow(fg_hwnd, 4)  # GW_OWNER

        if owner and fg_title:
            # This window is owned by another — likely a modal dialog
            owner_title = self._get_title(owner)
            if owner_title and owner_title != fg_title:
                return fg_title

        # Check for common dialog keywords
        dialog_keywords = ["save", "open", "confirm", "error", "warning", "alert",
                          "dialog", "properties", "replace", "overwrite", "delete"]
        if fg_title and any(kw in fg_title.lower() for kw in dialog_keywords):
            return fg_title

        return None

    @staticmethod
    def _get_title(hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    @staticmethod
    def _get_pid(hwnd) -> int:
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
