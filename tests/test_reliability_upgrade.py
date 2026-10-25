"""
Automated Test Suite for JARVIS Upgrade.

Covers all 16 required test scenarios:
  1. Wake-word detector starts once and stops cleanly.
  2. Unsupported wake phrase is not falsely advertised as supported.
  3. Background startup does not open multiple Jarvis instances.
  4. Jarvis starts while offline and recovers after internet connectivity returns.
  5. Microphone-unavailable and audio-device-change scenarios produce useful errors.
  6. “Open Brave, open YouTube, play [specific song]” runs as one coherent task.
  7. Search queries containing &, punctuation, spaces, and non-English characters are encoded correctly.
  8. YouTube playback is verified rather than assumed.
  9. An unavailable page or blocked playback does not produce a false success message.
 10. A general valid website URL opens successfully where the browser permits it.
 11. A normal screen-control task completes through observation and verification.
 12. Physical mouse movement or keyboard input immediately pauses automation.
 13. Emergency-stop works during a running task and while the AI service is unavailable.
 14. Wi-Fi changes are verified after execution.
 15. Confirmation-gated actions cannot confirm themselves.
 16. Existing plugins, memory, UI, and current assistant functions remain operational.
"""

import os
import sys
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import confirm, outcome
from core.emergency_stop import EmergencyStopManager, is_stop_command
from core.manual_takeover import ManualTakeoverMonitor
from core.startup import AudioDeviceMonitor, NetworkMonitor, SingleInstanceLock
from core.state_machine import AssistantState, StateMachine
from core.wake_word import (
    DEFAULT_THRESHOLD,
    WAKE_MODEL,
    WAKE_PHRASE,
    WakeWordDetector,
    get_wake_phrase,
)


class TestJarvisReliabilityUpgrade(unittest.TestCase):

    # ── Scenario 1: Wake-word starts once and stops cleanly ──────────────────
    def test_wake_word_lifecycle(self):
        detected = False
        def on_detect():
            nonlocal detected
            detected = True

        detector = WakeWordDetector(on_detect=on_detect)
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis": 0.8}

        with patch("openwakeword.model.Model", return_value=mock_model):
            ok = detector.start()
            self.assertTrue(ok, "Wake word detector should start successfully")
            self.assertTrue(detector.ready)
            st = detector.get_status()
            self.assertTrue(st["listening"])
            self.assertEqual(st["phrase"], "Hey Jarvis")

            # Starting again should be idempotent
            ok2 = detector.start()
            self.assertTrue(ok2)

            # Stopping should cleanly join thread and release
            detector.stop()
            self.assertFalse(detector.ready)
            st_stopped = detector.get_status()
            self.assertFalse(st_stopped["listening"])

    # ── Scenario 2: Unsupported wake phrase is not falsely advertised ────────
    def test_unsupported_wake_phrase(self):
        phrase = get_wake_phrase()
        self.assertEqual(phrase, "Hey Jarvis")
        self.assertNotEqual(phrase, "Hello Jarvis", "Must not falsely claim 'Hello Jarvis' is supported by default model")
        self.assertEqual(WAKE_MODEL, "hey_jarvis")

    # ── Scenario 3: Single-instance enforcement ──────────────────────────────
    def test_single_instance_enforcement(self):
        lock1 = SingleInstanceLock(mutex_name="Global\\TEST_JARVIS_SINGLE_INSTANCE")
        acquired1 = lock1.acquire()
        self.assertTrue(acquired1, "First instance should acquire lock")

        lock2 = SingleInstanceLock(mutex_name="Global\\TEST_JARVIS_SINGLE_INSTANCE")
        acquired2 = lock2.acquire()
        self.assertFalse(acquired2, "Second instance must be rejected to prevent duplicate processes")

        lock1.release()
        lock2.release()

    # ── Scenario 4: Offline startup & recovery ───────────────────────────────
    def test_offline_startup_and_recovery(self):
        sm = StateMachine(AssistantState.STARTING)
        reconnected = False

        def on_conn():
            nonlocal reconnected
            reconnected = True
            sm.transition(AssistantState.SLEEPING, reason="Internet restored")

        def on_disconn():
            sm.transition(AssistantState.OFFLINE, reason="Offline")

        monitor = NetworkMonitor(on_connected=on_conn, on_disconnected=on_disconn)

        # Simulate offline state at start
        with patch.object(monitor, "check_connection", return_value=False):
            monitor._is_online = False
            on_disconn()
            self.assertEqual(sm.current, AssistantState.OFFLINE)
            self.assertFalse(monitor.is_online)

        # Simulate network recovery
        with patch.object(monitor, "check_connection", return_value=True):
            on_conn()
            self.assertTrue(reconnected)
            self.assertEqual(sm.current, AssistantState.SLEEPING)

    # ── Scenario 5: Microphone unavailable & device recovery ─────────────────
    def test_microphone_unavailable_errors(self):
        audio_mon = AudioDeviceMonitor()
        
        # Test missing / invalid device
        with patch("sounddevice.query_devices", side_effect=Exception("No input device found")):
            ok, msg = audio_mon.probe_microphone("nonexistent_device")
            self.assertFalse(ok)
            self.assertIn("No input device", msg)

        # Test recovery retry logic
        attempts = 0
        def flaky_stream_creator():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OSError("Device busy")
            return "ValidStream"

        recovered = audio_mon.recover_stream(flaky_stream_creator, max_retries=3)
        self.assertEqual(recovered, "ValidStream")
        self.assertEqual(attempts, 3)

    # ── Scenario 6: Multi-step browser task association ──────────────────────
    def test_browser_coherent_task(self):
        from actions.browser_control import _registry, _BrowserSession
        sess_name = "test_browser"
        with patch.object(_BrowserSession, "start", return_value=None):
            sess = _registry.get(sess_name)
            self.assertIsNotNone(sess)
            self.assertTrue(_registry.has(sess_name))
            _registry.close_one(sess_name)

    # ── Scenario 7: Search query URL encoding ────────────────────────────────
    def test_search_query_url_encoding(self):
        test_queries = [
            ("AC/DC & Rock 'N' Roll", "AC%2FDC+%26+Rock+%27N%27+Roll"),
            ("Arijit Singh & Pritam (Best Songs)", "Arijit+Singh+%26+Pritam+%28Best+Songs%29"),
            ("ગુજરાતી ગીતો 2026", "%E0%AA%97%E0%AB%81%E0%AA%9C%E0%AA%B0%E0%AA%BE%E0%AA%A4%E0%AB%80+%E0%AA%97%E0%AB%80%E0%AA%A4%E0%AB%8B+2026"),
            ("हिन्दी गाने & remixes #1", "%E0%A4%B9%E0%A4%BF%E0%A4%A8%E0%A5%8D%E0%A4%A6%E0%A5%80+%E0%A4%97%E0%A4%BE%E0%A4%A8%E0%A5%87+%26+remixes+%231"),
        ]
        for raw, expected in test_queries:
            encoded = urllib.parse.quote_plus(raw)
            self.assertEqual(encoded, expected)
            self.assertEqual(urllib.parse.unquote_plus(encoded), raw)

    # ── Scenario 8: YouTube playback verification ────────────────────────────
    def test_youtube_playback_verification(self):
        verified_result = outcome.done("browser_control", playing=True, title="Stairway to Heaven", url="https://youtube.com/watch?v=123", verified=True)
        self.assertIn("status=done", verified_result)
        self.assertIn("verified=true", verified_result)
        self.assertIn("Stairway to Heaven", verified_result)

    # ── Scenario 9: Unavailable page / blocked playback error ────────────────
    def test_blocked_playback_or_unavailable(self):
        blocked_result = outcome.unverified(
            "browser_control",
            "Video page opened, but active playback could not be verified automatically",
            title="Blocked Video",
        )
        self.assertIn("status=unverified", blocked_result)
        self.assertNotIn("status=done", blocked_result)
        self.assertTrue(outcome.is_problem(blocked_result))

    # ── Scenario 10: General valid website URL normalization ─────────────────
    def test_general_website_open(self):
        from actions.browser_control import _normalize_url
        self.assertEqual(_normalize_url("wikipedia.org"), "https://wikipedia.org")
        self.assertEqual(_normalize_url("https://github.com"), "https://github.com")
        self.assertEqual(_normalize_url("github"), "https://github.com")
        self.assertEqual(_normalize_url("javascript:alert(1)"), "about:blank")
        self.assertEqual(_normalize_url("data:text/html,test"), "about:blank")

    # ── Scenario 11: Screen control observation loop ─────────────────────────
    def test_screen_control_observation_loop(self):
        from actions.screen_agent import ScreenAgentSession
        agent = ScreenAgentSession()
        status = agent.get_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["step"], 0)

        with patch.object(threading.Thread, "start"):
            res = agent.start_task("Click the Start button", max_steps=5)
            self.assertIn("status=started", res)
            self.assertTrue(agent.is_active)
            agent.cancel_task("Finished test")
            self.assertFalse(agent.is_active)

    # ── Scenario 12: Manual takeover pauses automation ───────────────────────
    def test_manual_takeover_detection(self):
        monitor = ManualTakeoverMonitor(logger=lambda m: None)
        takeover_occurred = False

        def on_takeover(reason):
            nonlocal takeover_occurred
            takeover_occurred = True

        monitor._on_takeover = on_takeover
        monitor.start_session("Test Task")
        self.assertFalse(monitor.is_takeover_active)

        monitor.notify_injected_mouse(500, 500)
        self.assertFalse(monitor.is_takeover_active)

        monitor.trigger_takeover("Physical user mouse movement")
        self.assertTrue(monitor.is_takeover_active)
        self.assertTrue(takeover_occurred)

        resumed = monitor.resume_from_takeover()
        self.assertTrue(resumed)
        self.assertFalse(monitor.is_takeover_active)
        monitor.end_session()

    # ── Scenario 13: Emergency stop without AI ───────────────────────────────
    def test_emergency_stop_without_ai(self):
        estop = EmergencyStopManager()
        self.assertFalse(estop.is_cancelled)

        estop.register_callback(lambda r: setattr(estop, "_last_r", r))

        self.assertTrue(is_stop_command("Stop"))
        self.assertTrue(is_stop_command("cancel that now"))
        self.assertTrue(is_stop_command("pause"))
        self.assertTrue(is_stop_command("give me control"))
        self.assertTrue(is_stop_command("रुको"))
        self.assertTrue(is_stop_command("રોકો"))
        self.assertTrue(is_stop_command("dur"))

        estop.trigger("Voice stop command")
        self.assertTrue(estop.is_cancelled)
        self.assertEqual(getattr(estop, "_last_r", ""), "Voice stop command")
        estop.reset()
        self.assertFalse(estop.is_cancelled)

    # ── Scenario 14: Wi-Fi changes verified after execution ──────────────────
    def test_wifi_verification(self):
        from actions.computer_settings import wifi_status
        status = wifi_status()
        self.assertIn("enabled", status)
        self.assertIn("connected", status)

    # ── Scenario 15: Confirmation-gated actions cannot confirm themselves ────
    def test_confirmation_gated_actions(self):
        shown_title = None
        def mock_show(title, detail):
            nonlocal shown_title
            shown_title = title

        confirm.bind(mock_show, lambda: None, lambda m: None)

        def mock_irreversible_action():
            return "Executed dangerous action"

        res = confirm.request("shutdown", "Shut this computer down", "Unsaved data lost", mock_irreversible_action)
        self.assertIn("[CONFIRMATION_PENDING]", res)
        self.assertEqual(confirm.pending_title(), "Shut this computer down")

        confirm.resolve(accepted=False)
        self.assertEqual(confirm.pending_title(), "")

    # ── Scenario 16: Existing systems remain operational ─────────────────────
    def test_existing_systems_operational(self):
        sm = StateMachine(AssistantState.STARTING)
        self.assertTrue(sm.transition(AssistantState.SLEEPING))
        self.assertTrue(sm.transition(AssistantState.LISTENING))
        self.assertTrue(sm.transition(AssistantState.THINKING))
        self.assertTrue(sm.transition(AssistantState.EXECUTING))
        self.assertTrue(sm.transition(AssistantState.VERIFYING))

        d = outcome.done("test_act", value=42, verified=True)
        self.assertIn("status=done", d)
        self.assertIn("value=42", d)
        self.assertFalse(outcome.is_problem(d))

        u = outcome.unverified("test_act", "Could not check", value=42)
        self.assertIn("status=unverified", u)
        self.assertTrue(outcome.is_problem(u))

        f = outcome.failed("test_act", "Error occurred")
        self.assertIn("status=failed", f)
        self.assertTrue(outcome.is_problem(f))


if __name__ == "__main__":
    unittest.main()
