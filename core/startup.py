"""
core/startup.py — Windows Startup, Single-Instance Lock & Reliability Infrastructure.

Key Capabilities:
  1. Single-Instance Enforcement (Named Windows Mutex) to prevent duplicate
     JARVIS instances, duplicate audio streams, and conflicting hotkeys.
  2. Windows Startup Registry Integration (HKCU Run key) with --background flag.
  3. Network Connectivity Monitor with bounded exponential backoff (2s → 30s)
     that never freezes the audio processing loop or UI thread.
  4. Audio Device Recovery that detects missing or unplugged microphones and
     recovers gracefully when devices become available.
"""

from __future__ import annotations

import ctypes
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

_OS = platform.system()
_MUTEX_NAME = "Global\\JARVIS_MARK_LVI_SINGLE_INSTANCE_MUTEX"
_REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_APP_NAME = "JARVIS_Assistant"

if _OS == "Windows":
    import winreg
    _ERROR_ALREADY_EXISTS = 183
else:
    winreg = None
    _ERROR_ALREADY_EXISTS = 0


# ── 1. Single-Instance Enforcement ───────────────────────────────────────────

class SingleInstanceLock:
    """Windows Named Mutex to prevent multiple competing JARVIS instances."""

    def __init__(self, mutex_name: str = _MUTEX_NAME):
        self._mutex_name = mutex_name
        self._handle = None
        self._acquired = False
        self._lock_file: Optional[Path] = None

    def acquire(self) -> bool:
        """Attempt to acquire exclusive ownership. Returns True if first instance."""
        if _OS == "Windows":
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.CreateMutexW(None, True, self._mutex_name)
                last_error = kernel32.GetLastError()
                if last_error == _ERROR_ALREADY_EXISTS:
                    if handle:
                        kernel32.CloseHandle(handle)
                    self._acquired = False
                    return False
                self._handle = handle
                self._acquired = True
                return True
            except Exception as e:
                print(f"[Startup] Windows mutex error ({e}) — falling back to lockfile")

        # Fallback / non-Windows file-based lock
        try:
            lock_dir = Path.home() / ".jarvis"
            lock_dir.mkdir(parents=True, exist_ok=True)
            self._lock_file = lock_dir / "jarvis.lock"

            if self._lock_file.exists():
                try:
                    old_pid = int(self._lock_file.read_text(encoding="utf-8").strip())
                    import psutil
                    if psutil.pid_exists(old_pid):
                        proc = psutil.Process(old_pid)
                        if "python" in proc.name().lower():
                            return False
                except Exception:
                    pass

            self._lock_file.write_text(str(os.getpid()), encoding="utf-8")
            self._acquired = True
            return True
        except Exception as e:
            print(f"[Startup] Lockfile acquisition error: {e}")
            return True

    def release(self) -> None:
        """Release the mutex/lockfile cleanly."""
        if _OS == "Windows" and self._handle:
            try:
                ctypes.windll.kernel32.ReleaseMutex(self._handle)
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

        if self._lock_file and self._lock_file.exists():
            try:
                self._lock_file.unlink()
            except Exception:
                pass
            self._lock_file = None

        self._acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._acquired


_GLOBAL_INSTANCE_LOCK: Optional[SingleInstanceLock] = None


def acquire_instance_lock() -> bool:
    global _GLOBAL_INSTANCE_LOCK
    if _GLOBAL_INSTANCE_LOCK is None:
        _GLOBAL_INSTANCE_LOCK = SingleInstanceLock()
    return _GLOBAL_INSTANCE_LOCK.acquire()


def release_instance_lock() -> None:
    global _GLOBAL_INSTANCE_LOCK
    if _GLOBAL_INSTANCE_LOCK is not None:
        _GLOBAL_INSTANCE_LOCK.release()


# ── 2. Windows Startup Registry Management ───────────────────────────────────

def is_startup_enabled() -> bool:
    """Check if JARVIS is registered in Windows startup (HKCU Run)."""
    if _OS != "Windows":
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, _REG_APP_NAME)
            return bool(val)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[Startup] Check startup error: {e}")
        return False


def enable_startup(background: bool = True) -> tuple[bool, str]:
    """Register JARVIS in Windows startup to launch automatically after sign-in."""
    if _OS != "Windows":
        return False, "Windows startup is only supported on Windows 10/11."

    try:
        base_dir = Path(__file__).resolve().parent.parent
        main_py = base_dir / "main.py"
        # Use pythonw.exe if available to start without console window
        python_exe = sys.executable
        pythonw = Path(python_exe).parent / "pythonw.exe"
        exe_to_use = str(pythonw) if pythonw.exists() else python_exe

        cmd_parts = [f'"{exe_to_use}"', f'"{main_py}"']
        if background:
            cmd_parts.append("--background")

        command_str = " ".join(cmd_parts)

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, _REG_APP_NAME, 0, winreg.REG_SZ, command_str)
            print(f"[Startup] Registered Windows startup: {command_str}")
            return True, "Start with Windows enabled (launches in background mode)."
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        msg = f"Failed to enable startup: {e}"
        print(f"[Startup] {msg}")
        return False, msg


def disable_startup() -> tuple[bool, str]:
    """Remove JARVIS from Windows startup."""
    if _OS != "Windows":
        return False, "Not on Windows."

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, _REG_APP_NAME)
            print("[Startup] Removed Windows startup entry.")
            return True, "Start with Windows disabled."
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return True, "Startup entry was not present."
    except Exception as e:
        msg = f"Failed to disable startup: {e}"
        print(f"[Startup] {msg}")
        return False, msg


# ── 3. Non-Blocking Network Connectivity Monitor ─────────────────────────────

class NetworkMonitor:
    """Monitors internet connectivity in a background thread with bounded exponential backoff.
    Ensures network failures never freeze the UI or the wake-word audio thread.
    """

    def __init__(
        self,
        on_connected: Optional[Callable[[], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        logger: Callable[[str], None] = print,
    ):
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._logger = logger
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._is_online: bool = True
        self._backoff: float = 2.0
        self._lock = threading.Lock()

    @property
    def is_online(self) -> bool:
        with self._lock:
            return self._is_online

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="NetworkMonitorThread",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def check_connection(self) -> bool:
        """Fast low-overhead check via socket connection to standard DNS root/public IPs."""
        hosts = [("8.8.8.8", 53), ("1.1.1.1", 53), ("208.67.222.222", 53)]
        for host, port in hosts:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((host, port))
                sock.close()
                return True
            except (socket.timeout, OSError):
                continue
            except Exception:
                pass
        return False

    def _monitor_loop(self) -> None:
        # Initial check
        current_state = self.check_connection()
        with self._lock:
            self._is_online = current_state

        while self._running:
            online = self.check_connection()
            prev_state = self._is_online

            if online != prev_state:
                with self._lock:
                    self._is_online = online

                if online:
                    self._backoff = 2.0
                    self._logger("[Network] 🌐 Internet connectivity restored.")
                    if self._on_connected:
                        try:
                            self._on_connected()
                        except Exception as e:
                            self._logger(f"[Network] on_connected error: {e}")
                else:
                    self._logger("[Network] ⚠️ Internet connectivity lost. Local wake-word remains active.")
                    if self._on_disconnected:
                        try:
                            self._on_disconnected()
                        except Exception as e:
                            self._logger(f"[Network] on_disconnected error: {e}")

            # Sleep interval: short when online (10s), bounded backoff when offline (2s -> 30s)
            if online:
                sleep_time = 10.0
            else:
                sleep_time = self._backoff
                self._backoff = min(self._backoff * 1.5, 30.0)

            step = 0.5
            elapsed = 0.0
            while self._running and elapsed < sleep_time:
                time.sleep(step)
                elapsed += step


# ── 4. Audio Device Recovery ─────────────────────────────────────────────────

class AudioDeviceMonitor:
    """Monitors microphone availability and recovers from unplugged/changed audio hardware."""

    def __init__(self, logger: Callable[[str], None] = print):
        self._logger = logger
        self._last_status: dict = {"ok": True, "device": "default", "error": ""}

    def probe_microphone(self, device_name_or_idx=None) -> tuple[bool, str]:
        """Test whether the microphone can open a stream without raising."""
        try:
            import sounddevice as sd
            # Query device info
            dev = sd.query_devices(device_name_or_idx, kind="input")
            if not dev or dev.get("max_input_channels", 0) <= 0:
                return False, f"Device '{device_name_or_idx}' has 0 input channels."

            # Attempt a minimal test stream
            with sd.InputStream(
                device=device_name_or_idx,
                channels=1,
                samplerate=16000,
                blocksize=1024,
            ):
                pass
            return True, dev.get("name", "Default Microphone")
        except Exception as e:
            return False, str(e)

    def recover_stream(self, create_stream_fn: Callable[[], any], max_retries: int = 3) -> any:
        """Retry stream creation with bounded backoff on temporary device failure."""
        backoff = 1.0
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                stream = create_stream_fn()
                if stream is not None:
                    self._logger(f"[AudioRecovery] Successfully initialized audio stream on attempt {attempt}.")
                    return stream
            except Exception as e:
                last_exc = e
                self._logger(f"[AudioRecovery] Attempt {attempt}/{max_retries} failed: {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)

        self._logger(f"[AudioRecovery] Failed to recover audio stream after {max_retries} attempts: {last_exc}")
        return None
