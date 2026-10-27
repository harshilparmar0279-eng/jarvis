"""
tests/test_integrated_upgrade.py — Automated verification suite for:
1. Resource Guardian & Diagnostics UI (100% solid, fully opaque background, contrast, no glassmorphism)
2. Centralized Browser Routing & Media Intent Detection (Rules A, B, C, D, E)
3. No silent fallback to Brave when Chrome is requested/defaulted
4. Playback verification & truthful outcome contract
5. Idempotent Windows Desktop Shortcut creation (Jarvis.lnk)
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

app = QApplication.instance() or QApplication(sys.argv)

import actions.browser_control as bc
from actions.browser_control import (
    YOUTUBE_URL,
    YOUTUBE_MUSIC_URL,
    DEFAULT_BROWSER,
    route_media_command,
    browser_control,
)
from core.resource_guardian import (
    ResourceGuardian,
    PROFILE_LOW_RAM,
    PROFILE_BALANCED,
    PROFILE_FULL_EXPERIENCE,
)
from ui_resource_guardian import (
    ResourceSparkline,
    ResourceDiagnosticsOverlay,
    PANEL_SOLID_BG,
    PANEL_SECTION_BG,
    COLOR_CYAN,
    COLOR_BRASS,
)


class TestResourceGuardianOpaqueUI(unittest.TestCase):
    """Verify Resource Guardian & Diagnostics UI has 100% solid, opaque dark theme."""

    def setUp(self):
        self.guardian = ResourceGuardian.get_instance()
        self.overlay = ResourceDiagnosticsOverlay()
        self.overlay.resize(self.overlay._OW, self.overlay._OH)

    def tearDown(self):
        self.overlay.hide()

    def test_opaque_widget_attributes(self):
        """Panel must enforce opaque painting attributes at Qt engine level."""
        self.assertTrue(
            self.overlay.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent),
            "WA_OpaquePaintEvent must be True to prevent underlying UI bleed-through"
        )
        self.assertTrue(
            self.overlay.testAttribute(Qt.WidgetAttribute.WA_StyledBackground),
            "WA_StyledBackground must be True to ensure stylesheet backgrounds are painted"
        )
        self.assertTrue(
            self.overlay.autoFillBackground(),
            "autoFillBackground must be True"
        )

    def test_solid_dark_background_no_transparency(self):
        """Panel background must be 100% solid with alpha = 255 (no rgba < 1)."""
        bg_color = QColor(PANEL_SOLID_BG)
        self.assertEqual(bg_color.alpha(), 255, "Panel background must be fully opaque (alpha=255)")
        # Must be deep navy/black theme (RGB values low)
        self.assertLess(bg_color.red(), 20, "Dark background red component must be <= 20")
        self.assertLess(bg_color.green(), 25, "Dark background green component must be <= 25")
        self.assertLess(bg_color.blue(), 35, "Dark background blue component must be <= 35")

    def test_section_contrast_and_solid_colors(self):
        """Inner sections must use solid card backgrounds and high contrast text."""
        sec_color = QColor(PANEL_SECTION_BG)
        self.assertEqual(sec_color.alpha(), 255, "Section card must be fully opaque")

        # Verify typography colors are bright and distinct
        cyan_col = QColor(COLOR_CYAN)
        brass_col = QColor(COLOR_BRASS)
        self.assertGreater(cyan_col.green(), 180, "Cyan header text must be bright")
        self.assertGreater(brass_col.red(), 180, "Brass header text must be bright")

    def test_profile_switching(self):
        """Profile switches must update guardian and button styling."""
        self.overlay._select_profile(PROFILE_LOW_RAM)
        self.assertEqual(self.guardian.get_profile(), PROFILE_LOW_RAM)

        self.overlay._select_profile(PROFILE_FULL_EXPERIENCE)
        self.assertEqual(self.guardian.get_profile(), PROFILE_FULL_EXPERIENCE)

        self.overlay._select_profile(PROFILE_BALANCED)
        self.assertEqual(self.guardian.get_profile(), PROFILE_BALANCED)

    def test_sparkline_widget(self):
        """Sparkline must initialize with proper height and respond to telemetry."""
        sparkline = ResourceSparkline()
        self.assertEqual(sparkline.height(), 44)
        self.assertGreaterEqual(sparkline.minimumWidth(), 80)
        # Snapshot capture
        self.guardian.capture_snapshot()
        sparkline.update()


class TestBrowserRoutingAndMediaIntents(unittest.TestCase):
    """Verify centralized browser routing and media command interpretation."""

    def test_default_constants(self):
        """Verify default configuration constants."""
        self.assertEqual(YOUTUBE_URL, "https://www.youtube.com/?authuser=0")
        self.assertEqual(YOUTUBE_MUSIC_URL, "https://music.youtube.com/")
        self.assertEqual(DEFAULT_BROWSER, "chrome")

    def test_rule_a_open_youtube(self):
        """'YouTube kholo' / 'Open YouTube' must open exact YOUTUBE_URL in Chrome."""
        for phrase in [
            "YouTube kholo",
            "Open YouTube",
            "YouTube open karo",
            "YouTube chalao",
            "kholo youtube",
        ]:
            res = route_media_command(phrase)
            self.assertEqual(res["action"], "go_to", f"Failed action for: {phrase}")
            self.assertEqual(res["url"], YOUTUBE_URL, f"Failed URL for: {phrase}")
            self.assertEqual(res["browser"], "chrome", f"Failed browser for: {phrase} (must never open Brave by default)")

    def test_rule_b_open_youtube_music(self):
        """'YouTube Music kholo' / 'Open YouTube Music' must open YOUTUBE_MUSIC_URL in Chrome."""
        for phrase in [
            "YouTube Music kholo",
            "Open YouTube Music",
            "YouTube music open karo",
            "YouTube music chala do",
        ]:
            res = route_media_command(phrase)
            self.assertEqual(res["action"], "go_to", f"Failed action for: {phrase}")
            self.assertEqual(res["url"], YOUTUBE_MUSIC_URL, f"Failed URL for: {phrase}")
            self.assertEqual(res["browser"], "chrome", f"Failed browser for: {phrase}")

    def test_rule_c_play_song_youtube_music(self):
        """'Play [song]' / '[song] music play karo' must open YouTube Music in Chrome."""
        test_queries = [
            ("Play Believer", "Believer", ""),
            ("Believer music play karo", "Believer", ""),
            ("Play Kesariya by Arijit Singh", "Kesariya", "Arijit Singh"),
            ("Tum Hi Ho gaana sunao", "Tum Hi Ho", ""),
            ("Shape of You play karo", "Shape of You", ""),
        ]
        for phrase, exp_query, exp_artist in test_queries:
            res = route_media_command(phrase)
            self.assertEqual(res["action"], "youtube_music_play", f"Failed action for: {phrase}")
            self.assertEqual(res["browser"], "chrome", f"Failed browser for: {phrase}")
            self.assertIn(exp_query.lower(), res["query"].lower(), f"Failed query extraction for: {phrase}")
            if exp_artist:
                self.assertEqual(res["artist"].lower(), exp_artist.lower(), f"Failed artist extraction for: {phrase}")

    def test_rule_d_chrome_mein_play_karo(self):
        """'Chrome mein [song] play karo' must open regular YouTube in Chrome, NOT YouTube Music."""
        phrases = [
            "Chrome mein Believer play karo",
            "Play Believer in Chrome",
            "Chrome me Kesariya chalao",
            "in Chrome play Starboy",
        ]
        for phrase in phrases:
            res = route_media_command(phrase)
            self.assertEqual(res["action"], "youtube_play", f"Rule D failed: {phrase} must target regular YouTube")
            self.assertEqual(res["browser"], "chrome", f"Rule D failed: {phrase} must use Chrome")
            self.assertIn("believer" if "believer" in phrase.lower() else ("kesariya" if "kesariya" in phrase.lower() else "starboy"), res["query"].lower())

    def test_rule_e_explicit_browser_requests(self):
        """Explicit browser requests override defaults."""
        res_brave = route_media_command("Brave mein YouTube kholo")
        self.assertEqual(res_brave["action"], "go_to")
        self.assertEqual(res_brave["url"], YOUTUBE_URL)
        self.assertEqual(res_brave["browser"], "brave")

        res_chrome = route_media_command("Chrome mein YouTube kholo")
        self.assertEqual(res_chrome["action"], "go_to")
        self.assertEqual(res_chrome["url"], YOUTUBE_URL)
        self.assertEqual(res_chrome["browser"], "chrome")

        res_brave_song = route_media_command("Brave mein Kesariya play karo")
        self.assertEqual(res_brave_song["action"], "youtube_play")
        self.assertEqual(res_brave_song["browser"], "brave")


class TestBrowserControlSafetyAndNoFallback(unittest.TestCase):
    """Verify that Chrome never silently falls back to Brave if unavailable."""

    @patch("actions.browser_control._resolve_browser")
    @patch("actions.browser_control._find_exe_windows", return_value=None)
    @patch("shutil.which", return_value=None)
    def test_no_silent_fallback_to_brave_when_chrome_missing(self, mock_which, mock_find, mock_resolve):
        """When Chrome is requested or defaulted for YouTube, failure must be returned instead of Brave fallback."""
        mock_resolve.return_value = {"engine": "chromium", "exe": None, "channel": None}

        # Calling browser_control with YouTube target and chrome
        res = browser_control({"action": "go_to", "url": YOUTUBE_URL, "browser": "chrome"})
        self.assertIn("failed", res.lower(), "Must report failed when Chrome is unavailable")
        self.assertIn("chrome is not installed", res.lower(), "Must clearly explain Chrome is missing")
        self.assertNotIn("brave", res.lower(), "Must never silently fall back to Brave")


class TestDesktopShortcutCreation(unittest.TestCase):
    """Verify idempotent creation and dynamic path detection for Jarvis.lnk shortcut."""

    def test_desktop_directory_resolution(self):
        """Desktop directory must resolve dynamically without hardcoding."""
        import ui
        desktop = ui.MainWindow._get_desktop_dir()
        self.assertTrue(desktop.is_dir(), f"Resolved desktop path must be a directory: {desktop}")
        self.assertTrue(os.path.exists(desktop), "Desktop directory must exist")

    def test_shortcut_creation_and_idempotence(self):
        """Shortcut creation must be successful and idempotent on Windows."""
        import ui
        desktop = ui.MainWindow._get_desktop_dir()
        lnk = desktop / "Jarvis.lnk"

        # Create shortcut
        ui.MainWindow._create_desktop_shortcut(None)
        self.assertTrue(lnk.exists(), f"Shortcut file must exist on desktop: {lnk}")

        # Verify shortcut properties via Windows Script Host
        import win32com.client
        sh = win32com.client.Dispatch("WScript.Shell")
        sc = sh.CreateShortCut(str(lnk))

        self.assertTrue(
            "python" in sc.TargetPath.lower(),
            f"Shortcut target must be python/pythonw, got: {sc.TargetPath}"
        )
        self.assertTrue(
            "main.py" in sc.Arguments,
            f"Shortcut arguments must target main.py, got: {sc.Arguments}"
        )
        self.assertEqual(
            Path(sc.WorkingDirectory).resolve(),
            BASE_DIR.resolve(),
            f"Shortcut working directory must be project base: {sc.WorkingDirectory}"
        )
        self.assertTrue(
            "jarvis.ico" in sc.IconLocation.lower(),
            f"Shortcut icon must use jarvis.ico, got: {sc.IconLocation}"
        )

        # Idempotent repeat run
        ui.MainWindow._create_desktop_shortcut(None)
        self.assertTrue(lnk.exists(), "Shortcut must still exist after repeated creation")


if __name__ == "__main__":
    unittest.main()
