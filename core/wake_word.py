"""
Local wake-word detection for JARVIS ("Hey Jarvis").

Design goals:
  • ZERO cost when the feature is off — openwakeword is imported ONLY inside
    start()/install helpers, never at module load. If the user never enables
    wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); the actual model inference runs in
    this module's own background thread, so the real-time audio thread and the
    Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; there is no
    network call except the one-time model download the user triggers from the UI.

openwakeword ships small ONNX models (a few MB each) and runs comfortably on a
CPU. The pretrained wake phrase used here is "Hey Jarvis".

Wake-phrase notes
─────────────────
The bundled model is ``hey_jarvis`` which reliably detects the phrase
**"Hey Jarvis"**.  "Hello Jarvis" is NOT reliably detected by this model — the
phonetic patterns differ enough that the pretrained weights will either miss it
entirely or fire with very low confidence.  If you want "Hello Jarvis" as a wake
phrase you would need to train a custom openwakeword model on that phrase and
place the resulting ``.onnx`` file into the openwakeword resources directory.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# Pretrained openwakeword model that listens for "Hey Jarvis".
WAKE_MODEL = "hey_jarvis"
# The actual phrase the model is trained to detect.
WAKE_PHRASE = "Hey Jarvis"
# Score in [0,1]; above this counts as a detection. Tunable per environment.
DEFAULT_THRESHOLD = 0.5
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000
# After a detection fires, suppress further detections for this many seconds
# to prevent double-triggers from the same or closely-spaced utterances.
COOLDOWN_SECONDS = 2.0
# How many consecutive queue-full drops before we log a warning (avoids spam).
_QUEUE_DROP_LOG_INTERVAL = 50

_log = logging.getLogger(__name__)


def get_wake_phrase() -> str:
    """Return the wake phrase the current model listens for."""
    return WAKE_PHRASE


def is_installed() -> bool:
    """True if the openwakeword package is importable (no model check)."""
    try:
        import importlib.util
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """True if openwakeword is installed AND its model files are present on disk.

    This is a cheap, DETERMINISTIC file-existence check. It deliberately does NOT
    construct a Model to probe readiness — doing that is slow and, worse, can clash
    with the detector's own Model when it's already running, which intermittently
    returned False and made the UI flicker to 'not downloaded'. Never raises.
    """
    if not is_installed():
        return False
    try:
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        if not models_dir.is_dir():
            return False
        has_wake = (any(models_dir.glob(f"{WAKE_MODEL}*.onnx"))
                    or any(models_dir.glob(f"{WAKE_MODEL}*.tflite")))
        has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
                   or any(models_dir.glob("melspectrogram*.tflite")))
        has_emb = (any(models_dir.glob("embedding_model*.onnx"))
                   or any(models_dir.glob("embedding_model*.tflite")))
        return bool(has_wake and has_mel and has_emb)
    except Exception:
        return False


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing, then
    download the wake model. Returns (ok, message). Never raises — every failure
    is reported through the returned message and the logger.
    """
    _tell = notify or (lambda _msg: None)
    try:
        if not is_installed():
            logger("Wake word: installing openwakeword (one-time)…")
            _tell("Wake word: installing openwakeword (one-time)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"
        # Download the pretrained melspectrogram/embedding + wake models.
        logger("Wake word: downloading models…")
        _tell("Wake word: downloading models…")
        try:
            import openwakeword.utils as _u
            try:
                _u.download_models([WAKE_MODEL])
            except TypeError:
                _u.download_models()   # older signature downloads the default set
        except Exception as e:
            return False, f"model download failed: {e}"

        if not is_ready():
            return False, "installed, but the wake model could not be loaded."
        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except Exception as e:
        return False, f"setup error: {e}"


class WakeWordDetector:
    """
    Runs the wake model in a dedicated thread. The mic thread calls feed() with
    raw int16 frames; detections invoke on_detect() (called from this thread —
    the callback must marshal to whatever loop/UI it needs).
    """

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = threshold
        self._logger    = logger
        # See PluginRegistry: `logger` is the console and gets everything,
        # `notify` is the activity log and gets only what the user must act on.
        self._notify    = notify or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._ready = False

        # ── Cooldown & statistics ────────────────────────────────────────
        self._last_detection: float = 0.0       # time.monotonic() of last fire
        self._detections_count: int = 0
        self._cooldown: float = COOLDOWN_SECONDS

        # ── Queue-drop tracking (log periodically, not every frame) ──────
        self._drop_count: int = 0

        # ── Try to read threshold from user config (best-effort) ─────────
        try:
            from memory.config_manager import load_api_keys
            saved = load_api_keys().get("wake_threshold")
            if saved is not None:
                val = float(saved)
                if 0.1 <= val <= 0.95:
                    self._threshold = val
        except Exception:
            pass

    # ── Threshold management ─────────────────────────────────────────────

    def set_threshold(self, value: float) -> None:
        """Update the detection threshold at runtime.

        ``value`` is clamped to [0.1, 0.95]. The new value is also persisted to
        the config file so it survives restarts.
        """
        value = max(0.1, min(0.95, float(value)))
        self._threshold = value
        # Best-effort persist — never let a config write break audio.
        try:
            from memory.config_manager import _save_flag
            _save_flag("wake_threshold", value)
        except Exception:
            pass

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Load the model and spawn the inference thread. Returns True on success.
        Safe to call again — a no-op if already running. Never raises."""
        # Truly idempotent: check that the thread is *actually* alive, not just
        # the _running flag (the thread may have crashed or been joined).
        if self._running and self._thread is not None and self._thread.is_alive():
            return True
        # If the flag was stale, clean up before restarting.
        if self._running:
            self._running = False
        try:
            from openwakeword.model import Model
            self._model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
        except Exception as e:
            self._logger(f"Wake word: could not load model — {e}")
            self._notify("Wake word unavailable — use the WAKE NOW button.")
            self._model = None
            return False
        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger("Wake word: listening for 'Hey Jarvis'.")
        return True

    def stop(self) -> None:
        """Stop the detector thread and release the model.

        Joins the thread with a timeout to guarantee a clean shutdown even if
        the inference loop is blocked on a long prediction.
        """
        self._running = False
        # unblock the thread if it's waiting on the queue
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                self._logger("Wake word: thread did not exit within timeout.")
            self._thread = None
        self._model = None
        self._ready = False

    def restart(self) -> bool:
        """Stop the detector, then start it again. Returns True on success."""
        self.stop()
        return self.start()

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Status ───────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return a snapshot of detector state for the UI or diagnostics.

        Keys:
          listening        — is the detector loop running
          ready            — model loaded successfully
          phrase           — the wake phrase being listened for
          threshold        — current detection threshold
          last_detection   — monotonic timestamp of last detection (0 if never)
          detections_count — total detections since construction
        """
        return {
            "listening":        self._running and self._thread is not None
                                and self._thread.is_alive(),
            "ready":            self._ready,
            "phrase":           WAKE_PHRASE,
            "threshold":        self._threshold,
            "last_detection":   self._last_detection,
            "detections_count": self._detections_count,
        }

    # ── Feed (audio input) ───────────────────────────────────────────────

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap and
        never block — the frame is copied and dropped if the queue is backed up."""
        if not self._running:
            return
        try:
            # frame_int16 is a numpy int16 array (possibly 2-D mono) — flatten to 1-D
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            self._drop_count += 1
            if self._drop_count % _QUEUE_DROP_LOG_INTERVAL == 1:
                _log.warning(
                    "Wake word: audio queue full — dropped %d frame(s) so far "
                    "(inference may be slower than mic input).",
                    self._drop_count,
                )
        except Exception:
            pass

    # ── Inference loop ───────────────────────────────────────────────────

    def _loop(self) -> None:
        import numpy as np
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                scores = self._model.predict(np.asarray(frame, dtype=np.int16))
                score = 0.0
                if isinstance(scores, dict):
                    # match the jarvis model regardless of exact key suffix
                    for k, v in scores.items():
                        if "jarvis" in k.lower():
                            score = max(score, float(v))
                    if score == 0.0 and scores:
                        score = max(float(v) for v in scores.values())
                if score >= self._threshold:
                    # ── Cooldown guard ───────────────────────────────────
                    now = time.monotonic()
                    if (now - self._last_detection) < self._cooldown:
                        self._drain()
                        continue
                    self._last_detection = now
                    self._detections_count += 1

                    # drain any backlog so we don't double-fire on the same utterance
                    self._drain()
                    try:
                        self._on_detect()
                    except Exception as e:
                        self._logger(f"Wake word: on_detect error — {e}")
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass

    # ── Destructor ───────────────────────────────────────────────────────

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
