"""
core/emergency_stop.py — Global Emergency Stop & Interruption Manager for JARVIS.

Features:
  • Global hotkey monitor (running in low-overhead background thread).
  • Works even if cloud AI service is disconnected or unresponsive.
  • Voice interruption parser for multilingual stop commands (English, Hindi, Gujarati, Turkish, etc.).
  • Immediate task cancellation and control handover to the user.
"""

from __future__ import annotations

import platform
import re
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

# Default hotkey chords
# Windows Virtual-Key codes
_VK_CODES = {
    "ctrl": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "escape": 0x1B,
    "pause": 0x13,
    "f12": 0x7B,
    "s": 0x53,
    "q": 0x51,
    "x": 0x58,
}

# Recognized voice stop commands in English, Hindi, Gujarati, and other supported languages
_VOICE_STOP_PATTERNS = [
    # English
    r"\bstop\b",
    r"\bcancel\b",
    r"\bpause\b",
    r"\bgive me control\b",
    r"\bhalt\b",
    r"\bshut up\b",
    r"\bquiet\b",
    r"\bemergency stop\b",
    r"\babort\b",
    r"\blet me take over\b",
    r"\bhand over\b",
    # Hindi (Latin & Devanagari)
    r"\bruko\b",
    r"\broko\b",
    r"\bband karo\b",
    r"\bchup\b",
    r"रुको",
    r"रोको",
    r"बंद करो",
    r"चुप",
    # Gujarati (Latin & Gujarati script)
    r"\botho\b",
    r"\bthobho\b",
    r"\bmane control aapo\b",
    r"રોકો",
    r"થોભો",
    r"મને કંટ્રોલ આપો",
    # Turkish (already supported in repo)
    r"\bdur\b",
    r"\biptal\b",
    r"\bsus\b",
    r"\bdurdur\b",
    r"\bbana ver\b",
]

_STOP_REGEX = re.compile("|".join(_VOICE_STOP_PATTERNS), re.IGNORECASE | re.UNICODE)


def is_stop_command(text: str) -> bool:
    """Check if an utterance or transcript represents an emergency stop/pause command."""
    if not text:
        return False
    cleaned = text.strip()
    return bool(_STOP_REGEX.search(cleaned))


class EmergencyStopManager:
    """Global emergency stop system that coordinates hotkey, voice, and manual aborts."""

    def __init__(self, chord: tuple[str, ...] = ("ctrl", "alt", "s")):
        self._chord = tuple(k.lower() for k in chord)
        self._callbacks: list[Callable[[str], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._active_cancellation = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._active_cancellation.is_set()

    def reset(self) -> None:
        self._active_cancellation.clear()
        get_state_machine().reset_cancellation()

    def register_callback(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def trigger(self, reason: str = "Emergency stop triggered") -> None:
        """Trigger an emergency stop immediately across all subsystems."""
        try:
            print(f"[EmergencyStop] [STOP] Triggered: {reason}")
        except Exception:
            pass
        self._active_cancellation.set()
        
        # Transition global state machine
        sm = get_state_machine()
        sm.emergency_stop(reason=reason)

        # Notify callbacks
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(reason)
            except Exception as e:
                print(f"[EmergencyStop] Callback error: {e}")

    def start_hotkey_listener(self) -> bool:
        """Start global background hotkey listener on Windows without polling heavily."""
        if _OS != "Windows":
            return False
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._thread = threading.Thread(
                target=self._hotkey_loop,
                name="EmergencyStopListener",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop_hotkey_listener(self) -> None:
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def _hotkey_loop(self) -> None:
        import ctypes
        user32 = ctypes.windll.user32
        vk_keys = [_VK_CODES[k] for k in self._chord if k in _VK_CODES]
        if not vk_keys:
            return

        # Poll at 25 Hz (40 ms), negligible CPU impact (~0.01%)
        while self._running:
            try:
                # Check if all keys in the chord are currently held down
                pressed = all(user32.GetAsyncKeyState(k) & 0x8000 for k in vk_keys)
                if pressed:
                    self.trigger(f"Global hotkey ({'+'.join(self._chord)}) pressed")
                    # Wait until released to avoid repeated triggers
                    while self._running and all(user32.GetAsyncKeyState(k) & 0x8000 for k in vk_keys):
                        time.sleep(0.1)
            except Exception:
                pass
            time.sleep(0.04)


# Global singleton
_GLOBAL_EMERGENCY_STOP: Optional[EmergencyStopManager] = None
_EMERGENCY_LOCK = threading.Lock()


def get_emergency_stop() -> EmergencyStopManager:
    global _GLOBAL_EMERGENCY_STOP
    with _EMERGENCY_LOCK:
        if _GLOBAL_EMERGENCY_STOP is None:
            _GLOBAL_EMERGENCY_STOP = EmergencyStopManager()
            _GLOBAL_EMERGENCY_STOP.start_hotkey_listener()
        return _GLOBAL_EMERGENCY_STOP
