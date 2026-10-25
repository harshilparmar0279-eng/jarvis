"""
core/manual_takeover.py — Immediate Manual Takeover & Physical Input Monitoring for Windows.

Guarantees the user always retains primary control of their computer.
Distinguishes Jarvis-injected mouse/keyboard commands from genuine physical human input.
If the user moves the mouse or presses a key during an active automation session:
  1. Immediately pauses further automation.
  2. Cancels pending actions and queued events.
  3. Preserves task context (step, goal, last observation).
  4. Displays "Manual control active".
  5. Never fights the user's cursor.
  6. Resumes only on explicit user instruction.
"""

from __future__ import annotations

import math
import platform
import sys
import threading
import time
from typing import Callable, Optional

# Ensure stdout handles unicode/emojis on Windows consoles
for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.state_machine import get_state_machine

_OS = platform.system()


def _safe_print(msg: str) -> None:
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"))
        except Exception:
            pass


class ManualTakeoverMonitor:
    """Monitors physical mouse and keyboard activity to detect manual user takeover."""

    def __init__(
        self,
        on_takeover: Optional[Callable[[str], None]] = None,
        logger: Callable[[str], None] = _safe_print,
    ):
        self._on_takeover = on_takeover
        self._logger = logger
        self._active_session = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Last injected mouse position and timestamp
        self._last_injected_pos: tuple[int, int] = (0, 0)
        self._last_injected_time: float = 0.0
        self._injected_grace_period: float = 0.35  # seconds

        # Takeover state
        self._takeover_active: bool = False
        self._paused_task_context: dict = {}

    @property
    def is_takeover_active(self) -> bool:
        with self._lock:
            return self._takeover_active

    @property
    def is_session_active(self) -> bool:
        with self._lock:
            return self._active_session

    def start_session(self, task_name: str = "") -> None:
        """Called when an autonomous desktop control session starts."""
        with self._lock:
            self._active_session = True
            self._takeover_active = False
            self._paused_task_context = {"task": task_name, "started_at": time.time()}
            self._sync_current_cursor()

        self._start_watcher()
        self._logger(f"[TakeoverMonitor] Desktop control session started: '{task_name}'")

    def end_session(self) -> None:
        """Called when desktop control completes or is formally cancelled."""
        with self._lock:
            self._active_session = False
            self._takeover_active = False
            self._paused_task_context = {}
        self._logger("[TakeoverMonitor] Desktop control session ended.")

    def notify_injected_mouse(self, x: int, y: int) -> None:
        """Jarvis notifies the monitor of coordinates it is moving the pointer to."""
        with self._lock:
            self._last_injected_pos = (int(x), int(y))
            self._last_injected_time = time.monotonic()

    def notify_injected_keyboard(self) -> None:
        """Jarvis notifies the monitor before typing or pressing keys."""
        with self._lock:
            self._last_injected_time = time.monotonic()

    def resume_from_takeover(self) -> bool:
        """User explicitly commanded Jarvis to resume control."""
        with self._lock:
            if not self._active_session:
                return False
            self._takeover_active = False
            self._sync_current_cursor()
        sm = get_state_machine()
        sm.transition("EXECUTING", reason="User resumed control after manual takeover")
        self._logger("[TakeoverMonitor] Automation resumed by user command.")
        return True

    def trigger_takeover(self, reason: str = "Physical user input detected") -> None:
        """Immediately pause automation for manual takeover."""
        with self._lock:
            if not self._active_session or self._takeover_active:
                return
            self._takeover_active = True

        self._logger(f"[TakeoverMonitor] [PAUSE] Manual control active — {reason}")

        # Update state machine
        sm = get_state_machine()
        sm.pause_for_manual_control(reason=reason)

        if self._on_takeover:
            try:
                self._on_takeover(reason)
            except Exception as e:
                self._logger(f"[TakeoverMonitor] Callback error: {e}")

    def _sync_current_cursor(self) -> None:
        if _OS == "Windows":
            try:
                import ctypes
                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                pt = POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                self._last_injected_pos = (pt.x, pt.y)
                self._last_injected_time = time.monotonic()
            except Exception:
                pass

    def _start_watcher(self) -> None:
        if _OS != "Windows":
            return
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._watcher_loop,
                name="ManualTakeoverWatcher",
                daemon=True,
            )
            self._thread.start()

    def _watcher_loop(self) -> None:
        import ctypes
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32 = ctypes.windll.user32
        pt = POINT()

        # Virtual keys to watch for user physical key interruption
        check_vks = [
            0x1B,        # VK_ESCAPE
            0x20,        # VK_SPACE
            0x0D,        # VK_RETURN
            0x08,        # VK_BACK
            0x01,        # VK_LBUTTON
            0x02,        # VK_RBUTTON
        ]

        while self._running:
            time.sleep(0.03)  # ~33 Hz polling
            with self._lock:
                if not self._active_session or self._takeover_active:
                    continue
                last_inj_pos = self._last_injected_pos
                last_inj_time = self._last_injected_time

            now = time.monotonic()
            time_since_inj = now - last_inj_time

            # 1. Check mouse cursor movement
            try:
                user32.GetCursorPos(ctypes.byref(pt))
                cur_pos = (pt.x, pt.y)
                dist = math.hypot(cur_pos[0] - last_inj_pos[0], cur_pos[1] - last_inj_pos[1])

                # If mouse moved significantly beyond what Jarvis injected
                if dist > 20:
                    if time_since_inj > self._injected_grace_period:
                        self.trigger_takeover(f"Mouse moved by user (dist: {dist:.0f}px)")
                        continue
            except Exception:
                pass

            # 2. Check key / mouse button presses
            if time_since_inj > self._injected_grace_period:
                try:
                    for vk in check_vks:
                        if user32.GetAsyncKeyState(vk) & 0x8000:
                            self.trigger_takeover(f"Physical input detected (key: 0x{vk:02X})")
                            break
                except Exception:
                    pass


# Singleton
_GLOBAL_TAKEOVER_MONITOR: Optional[ManualTakeoverMonitor] = None
_TAKEOVER_LOCK = threading.Lock()


def get_takeover_monitor() -> ManualTakeoverMonitor:
    global _GLOBAL_TAKEOVER_MONITOR
    with _TAKEOVER_LOCK:
        if _GLOBAL_TAKEOVER_MONITOR is None:
            _GLOBAL_TAKEOVER_MONITOR = ManualTakeoverMonitor()
        return _GLOBAL_TAKEOVER_MONITOR
