"""
core/resource_guardian.py — Lightweight resource monitoring, diagnostics, and profile control.

Zero external dependencies beyond standard library and psutil.
Maintains a bounded 60-sample telemetry history, tracks startup stages,
detects sustained memory growth, registers background workers, and
manages three distinct resource profiles (LOW RAM, BALANCED, FULL EXPERIENCE).
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import psutil

# ── Resource Profiles ────────────────────────────────────────────────────────
PROFILE_LOW_RAM         = "low_ram"
PROFILE_BALANCED        = "balanced"
PROFILE_FULL_EXPERIENCE = "full"

VALID_PROFILES = (PROFILE_LOW_RAM, PROFILE_BALANCED, PROFILE_FULL_EXPERIENCE)
DEFAULT_PROFILE = PROFILE_BALANCED

PROFILE_DISPLAY_NAMES = {
    PROFILE_LOW_RAM:         "LOW RAM",
    PROFILE_BALANCED:        "BALANCED",
    PROFILE_FULL_EXPERIENCE: "FULL EXPERIENCE",
}

# ── Memory Growth Thresholds & Cooldowns ──────────────────────────────────────
# A temporary spike (e.g. image processing, model buffer) is ordinary.
# We flag sustained growth only when memory increases monotonically over a
# significant portion of the bounded window by at least 35 MB.
SUSTAINED_GROWTH_MIN_MB = 35.0
GROWTH_ALERT_COOLDOWN_SEC = 300.0  # 5 minutes cooldown between repeated alerts
HISTORY_MAX_SAMPLES = 60


@dataclass
class ResourceSnapshot:
    timestamp: float
    process_rss_mb: float
    process_cpu_percent: float
    system_cpu_percent: float
    system_ram_percent: float
    system_ram_available_gb: float
    system_ram_total_gb: float
    state: str = "IDLE"  # "IDLE" | "VOICE_ACTIVE" | "BACKGROUND_WORK" | "OPTIONAL_FEATURES"


@dataclass
class StartupStage:
    name: str
    duration_s: float
    timestamp: float
    details: str = ""


@dataclass
class WorkerInfo:
    name: str
    description: str
    thread_ref: Optional[threading.Thread] = None
    is_alive_fn: Optional[Callable[[], bool]] = None
    is_essential: bool = True
    started_at: float = field(default_factory=time.time)

    def is_alive(self) -> bool:
        if self.is_alive_fn is not None:
            try:
                return bool(self.is_alive_fn())
            except Exception:
                return False
        if self.thread_ref is not None:
            try:
                return bool(self.thread_ref.is_alive())
            except Exception:
                return False
        return True


class StartupTracker:
    """Tracks startup stages and optional module load state."""

    def __init__(self):
        self._stages: list[StartupStage] = []
        self._optional_modules: dict[str, str] = {
            "openwakeword": "Deferred",
            "faster_whisper": "Deferred",
            "piper": "Deferred",
            "playwright": "Deferred",
            "cv2": "Deferred",
            "pynvml": "Deferred",
        }
        self._lock = threading.Lock()
        self._boot_time: float = time.time()

    def reset(self):
        with self._lock:
            self._boot_time = time.time()

    def record_stage(self, name: str, duration_s: float, details: str = ""):
        with self._lock:
            self._stages.append(StartupStage(
                name=name,
                duration_s=round(duration_s, 4),
                timestamp=time.time(),
                details=details
            ))

    def update_module_status(self, module_name: str, status: str):
        with self._lock:
            self._optional_modules[module_name] = status

    def check_loaded_modules(self):
        """Inspect sys.modules to see if optional heavy libraries were loaded."""
        with self._lock:
            for mod in list(self._optional_modules.keys()):
                if mod in sys.modules:
                    self._optional_modules[mod] = "Loaded"

    def get_summary(self) -> dict:
        self.check_loaded_modules()
        with self._lock:
            total_duration = sum(s.duration_s for s in self._stages)
            return {
                "boot_time": self._boot_time,
                "total_tracked_duration_s": round(total_duration, 3),
                "stages": [
                    {"name": s.name, "duration_s": s.duration_s, "details": s.details}
                    for s in self._stages
                ],
                "optional_modules": dict(self._optional_modules),
            }


class ResourceGuardian:
    """Central engine for telemetry, leak detection, profiles, and diagnostics."""

    _instance: Optional[ResourceGuardian] = None

    @classmethod
    def get_instance(cls) -> ResourceGuardian:
        if cls._instance is None:
            cls._instance = ResourceGuardian()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._process = psutil.Process(os.getpid())
        self._history: collections.deque[ResourceSnapshot] = collections.deque(maxlen=HISTORY_MAX_SAMPLES)
        self._startup_tracker = StartupTracker()
        self._workers: dict[str, WorkerInfo] = {}

        # Baseline & Diagnostics
        self._baseline_rss_mb: float = self._read_rss_mb()
        self._peak_rss_mb: float = self._baseline_rss_mb
        self._last_alert_time: float = 0.0
        self._last_alert_msg: str = ""

        # Current profile
        self._current_profile: str = DEFAULT_PROFILE
        self._profile_callbacks: list[Callable[[str], None]] = []

        # State tags
        self._state: str = "IDLE"
        self._active_optional_features: set[str] = set()

    # ── Process & System Metrics ─────────────────────────────────────────────

    def _read_rss_mb(self) -> float:
        try:
            return round(self._process.memory_info().rss / (1024 * 1024), 2)
        except Exception:
            return 0.0

    def capture_snapshot(self, state: Optional[str] = None) -> ResourceSnapshot:
        """Capture one resource snapshot and append to bounded history."""
        rss_mb = self._read_rss_mb()
        try:
            p_cpu = self._process.cpu_percent(interval=None)
        except Exception:
            p_cpu = 0.0

        try:
            s_cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            s_ram_pct = vm.percent
            s_ram_avail_gb = round(vm.available / (1024 ** 3), 2)
            s_ram_total_gb = round(vm.total / (1024 ** 3), 2)
        except Exception:
            s_cpu = 0.0
            s_ram_pct = 0.0
            s_ram_avail_gb = 0.0
            s_ram_total_gb = 0.0

        with self._lock:
            if state is not None:
                self._state = state
            current_state = self._state

            if rss_mb > self._peak_rss_mb:
                self._peak_rss_mb = rss_mb

            snap = ResourceSnapshot(
                timestamp=time.time(),
                process_rss_mb=rss_mb,
                process_cpu_percent=round(p_cpu, 1),
                system_cpu_percent=round(s_cpu, 1),
                system_ram_percent=round(s_ram_pct, 1),
                system_ram_available_gb=s_ram_avail_gb,
                system_ram_total_gb=s_ram_total_gb,
                state=current_state,
            )
            self._history.append(snap)
            return snap

    def get_history(self) -> list[ResourceSnapshot]:
        with self._lock:
            return list(self._history)

    def set_state(self, state: str):
        with self._lock:
            self._state = state

    def set_optional_feature_active(self, feature_name: str, active: bool):
        with self._lock:
            if active:
                self._active_optional_features.add(feature_name)
            else:
                self._active_optional_features.discard(feature_name)
            if self._active_optional_features and self._state == "IDLE":
                self._state = "OPTIONAL_FEATURES"
            elif not self._active_optional_features and self._state == "OPTIONAL_FEATURES":
                self._state = "IDLE"

    # ── Memory Growth & Leak Diagnostics ─────────────────────────────────────

    def analyze_memory(self) -> dict:
        """Analyze bounded history for sustained memory growth and return diagnostics."""
        with self._lock:
            history = list(self._history)
            baseline = self._baseline_rss_mb
            peak = self._peak_rss_mb
            current = history[-1].process_rss_mb if history else baseline

        delta_from_baseline = round(current - baseline, 2)
        n = len(history)

        growth_status = "STABLE"
        growth_rate_mb_min = 0.0
        recommendation = "Memory consumption is within normal operating parameters."

        if n >= 15:
            # Check slope across the bounded history
            earliest = history[0]
            latest = history[-1]
            elapsed_min = max(0.1, (latest.timestamp - earliest.timestamp) / 60.0)
            window_delta = latest.process_rss_mb - earliest.process_rss_mb
            growth_rate_mb_min = round(window_delta / elapsed_min, 2)

            # Check if majority of steps are upward
            increases = sum(1 for i in range(1, n) if history[i].process_rss_mb > history[i - 1].process_rss_mb)
            is_monotonically_rising = (increases / (n - 1)) > 0.75

            if window_delta >= SUSTAINED_GROWTH_MIN_MB and is_monotonically_rising:
                growth_status = "SUSTAINED_GROWTH"
                recommendation = (
                    f"Process RAM has increased by {window_delta:.1f} MB over {elapsed_min:.1f}m. "
                    "If idle, this may indicate memory retention. Safe action: consider toggling the "
                    "LOW RAM profile or closing unused optional plugins."
                )
            elif window_delta > 15.0:
                growth_status = "MODERATE_ELEVATION"
                recommendation = (
                    "Temporary memory elevation observed, typical during audio synthesis, vision capture, "
                    "or large document indexing. No action required."
                )

        return {
            "current_rss_mb": current,
            "baseline_rss_mb": baseline,
            "peak_rss_mb": peak,
            "delta_from_baseline_mb": delta_from_baseline,
            "sample_count": n,
            "growth_status": growth_status,
            "growth_rate_mb_min": growth_rate_mb_min,
            "recommendation": recommendation,
        }

    # ── Startup & Workers ────────────────────────────────────────────────────

    @property
    def startup_tracker(self) -> StartupTracker:
        return self._startup_tracker

    def register_worker(self, name: str, description: str, thread_ref=None,
                        is_alive_fn=None, is_essential=True):
        with self._lock:
            self._workers[name] = WorkerInfo(
                name=name,
                description=description,
                thread_ref=thread_ref,
                is_alive_fn=is_alive_fn,
                is_essential=is_essential,
                started_at=time.time(),
            )

    def get_workers_status(self) -> list[dict]:
        with self._lock:
            out = []
            for w in self._workers.values():
                out.append({
                    "name": w.name,
                    "description": w.description,
                    "is_alive": w.is_alive(),
                    "is_essential": w.is_essential,
                    "uptime_s": round(time.time() - w.started_at, 1),
                })
            return out

    # ── Profile Management ───────────────────────────────────────────────────

    def get_profile(self) -> str:
        with self._lock:
            return self._current_profile

    def set_profile(self, profile: str, notify: bool = True) -> str:
        clean = str(profile or "").strip().lower()
        if clean not in VALID_PROFILES:
            clean = DEFAULT_PROFILE

        with self._lock:
            prev = self._current_profile
            self._current_profile = clean
            callbacks = list(self._profile_callbacks)

        if notify and prev != clean:
            for cb in callbacks:
                try:
                    cb(clean)
                except Exception as e:
                    print(f"[ResourceGuardian] Profile callback error: {e}")

        return clean

    def add_profile_listener(self, cb: Callable[[str], None]):
        with self._lock:
            if cb not in self._profile_callbacks:
                self._profile_callbacks.append(cb)

    def remove_profile_listener(self, cb: Callable[[str], None]):
        with self._lock:
            if cb in self._profile_callbacks:
                self._profile_callbacks.remove(cb)

    def get_telemetry_interval_sec(self) -> float:
        prof = self.get_profile()
        if prof == PROFILE_LOW_RAM:
            return 5.0
        elif prof == PROFILE_FULL_EXPERIENCE:
            return 1.5
        return 2.0  # Balanced

    def get_dust_particles_count(self) -> int:
        prof = self.get_profile()
        if prof == PROFILE_LOW_RAM:
            return 0
        return 35

    def get_wave_steps(self) -> int:
        prof = self.get_profile()
        if prof == PROFILE_LOW_RAM:
            return 36
        return 72
