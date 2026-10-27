"""
ui_resource_guardian.py — Smart Resource Guardian UI components:
- ResourceSparkline: Compact 60-sample sparkline widget for the HUD telemetry column.
- ResourceDiagnosticsOverlay: Solid, fully opaque floating diagnostics, leak analysis, and profile manager.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPainterPath
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QScrollArea
)

from core.resource_guardian import (
    ResourceGuardian,
    PROFILE_LOW_RAM,
    PROFILE_BALANCED,
    PROFILE_FULL_EXPERIENCE,
    PROFILE_DISPLAY_NAMES,
)
from memory.config_manager import save_resource_profile


# Solid theme colors for opaque Resource Guardian panel (strictly no transparency/blur)
PANEL_SOLID_BG       = "#020b14"  # Solid dark navy/black background
PANEL_SECTION_BG     = "#051524"  # Solid inner card background
PANEL_BOX_BG         = "#071c30"  # Solid sub-box background
PANEL_BORDER         = "#1a3d54"  # Solid oceanic cyan border
PANEL_BORDER_BRASS   = "#d4af37"  # Solid brass border
COLOR_CYAN           = "#00e5ff"  # Futuristic cyan
COLOR_AMBER          = "#ffaa22"  # Amber accent
COLOR_BRASS          = "#d4af37"  # Brass gold
COLOR_TEXT_BRIGHT    = "#ffffff"  # Crisp white
COLOR_TEXT_CYAN      = "#8ffcff"  # Cyan text
COLOR_TEXT_MED       = "#89aab8"  # High-contrast readable medium text
COLOR_GREEN          = "#00ffaa"  # Stable green
COLOR_RED            = "#ff4d6d"  # Error red


class ResourceSparkline(QWidget):
    """
    Compact 60-sample telemetry sparkline widget integrated into the HUD sidebar.
    Displays Process RAM (RSS in MB) trend over time with hover highlights.
    Clicking it opens the ResourceDiagnosticsOverlay.
    """

    def __init__(self, parent=None, on_click=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setMinimumWidth(80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._on_click = on_click
        self._guardian = ResourceGuardian.get_instance()

    def enterEvent(self, _):
        self._hovered = True
        self.update()

    def leaveEvent(self, _):
        self._hovered = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and callable(self._on_click):
            self._on_click()
        super().mousePressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        # Solid background
        bg_col = QColor("#051624" if self._hovered else "#03101c")
        p.fillRect(0, 0, w, h, bg_col)

        # Border
        b_col = QColor(COLOR_CYAN if self._hovered else PANEL_BORDER)
        p.setPen(QPen(b_col, 1))
        p.drawRect(0, 0, w - 1, h - 1)

        history = self._guardian.get_history()
        rss_values = [snap.process_rss_mb for snap in history]

        # Draw Title and Current value
        cur_rss = rss_values[-1] if rss_values else self._guardian._read_rss_mb()
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QColor(COLOR_CYAN if self._hovered else COLOR_TEXT_MED))
        p.drawText(4, 11, "PROC RAM")

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QColor(COLOR_TEXT_BRIGHT))
        val_str = f"{cur_rss:.0f}M"
        p.drawText(w - p.fontMetrics().horizontalAdvance(val_str) - 4, 11, val_str)

        # Sparkline curve
        chart_top = 16
        chart_h = h - chart_top - 4
        chart_w = w - 8

        if len(rss_values) >= 2 and chart_h > 4:
            min_v = min(rss_values)
            max_v = max(rss_values)
            span = max(max_v - min_v, 5.0)

            pts: list[QPointF] = []
            for i, val in enumerate(rss_values):
                x = 4 + (i / max(1, len(rss_values) - 1)) * chart_w
                norm = (val - min_v) / span
                y = (chart_top + chart_h) - (norm * chart_h)
                pts.append(QPointF(x, y))

            # Sparkline stroke
            path = QPainterPath()
            path.moveTo(pts[0])
            for pt in pts[1:]:
                path.lineTo(pt)

            stroke_col = QColor(COLOR_AMBER if cur_rss > 350.0 else COLOR_CYAN)
            p.setPen(QPen(stroke_col, 1.5))
            p.drawPath(path)

            # Dot at current head
            p.setBrush(QBrush(stroke_col))
            p.drawEllipse(pts[-1], 2, 2)
        else:
            # Placeholder flat line
            p.setPen(QPen(QColor(PANEL_BORDER), 1, Qt.PenStyle.DashLine))
            p.drawLine(4, chart_top + chart_h // 2, w - 4, chart_top + chart_h // 2)


class ResourceDiagnosticsOverlay(QWidget):
    """
    Floating Resource Guardian & Diagnostics overlay.
    SOLID, 100% OPAQUE dark background matching Jarvis's black/navy theme.
    Zero glassmorphism, zero transparency, zero underlying UI bleed-through.
    """

    _OW = 600
    _OH = 540

    def __init__(self, parent=None, on_profile_changed=None):
        super().__init__(parent)
        self.setFixedSize(self._OW, self._OH)
        self._on_profile_changed = on_profile_changed
        self._guardian = ResourceGuardian.get_instance()

        # Enforce 100% solid, fully opaque painting at the Qt widget engine level
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)

        self._init_ui()

    def showEvent(self, e):
        super().showEvent(e)
        self.raise_()

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
            e.accept()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        """Paint an impenetrable, 100% solid opaque dark navy/black background."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # 100% solid opaque background fill (Alpha = 255)
        p.fillRect(self.rect(), QColor(PANEL_SOLID_BG))

        # Solid crisp outer border
        p.setPen(QPen(QColor(COLOR_CYAN), 1.5))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

        # Subtle gold accent line across top
        p.setPen(QPen(QColor(COLOR_BRASS), 1))
        p.drawLine(1, 1, self.width() - 2, 1)

    def _init_ui(self):
        # Master stylesheet with strictly SOLID colors (no rgba with alpha < 1)
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {PANEL_SOLID_BG};
                color: {COLOR_TEXT_CYAN};
                font-family: 'Courier New';
            }}
            QScrollArea {{
                background-color: {PANEL_SOLID_BG};
                border: none;
            }}
            QScrollBar:vertical {{
                background: {PANEL_SECTION_BG};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {PANEL_BORDER};
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {COLOR_CYAN};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(14, 12, 14, 12)
        main_lay.setSpacing(8)

        # ── Header ───────────────────────────────────────────────────────────
        hdr_row = QHBoxLayout()
        title_lbl = QLabel("◈ RESOURCE GUARDIAN & DIAGNOSTICS")
        title_lbl.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {COLOR_BRASS}; background-color: {PANEL_SOLID_BG}; border: none;")
        hdr_row.addWidget(title_lbl)

        hdr_row.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(26, 26)
        close_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {PANEL_SECTION_BG};
                color: {COLOR_TEXT_MED};
                border: 1px solid {PANEL_BORDER};
                border-radius: 3px;
            }}
            QPushButton:hover {{
                background-color: {COLOR_RED};
                color: #ffffff;
                border: 1px solid {COLOR_RED};
            }}
        """)
        close_btn.clicked.connect(self.hide)
        hdr_row.addWidget(close_btn)
        main_lay.addLayout(hdr_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background-color: {PANEL_BORDER}; max-height: 1px; border: none; margin: 1px 0;")
        main_lay.addWidget(sep)

        # ── Profile Selector (Solid Section) ──────────────────────────────────
        prof_box = QWidget()
        prof_box.setStyleSheet(f"""
            background-color: {PANEL_SECTION_BG};
            border: 1px solid {PANEL_BORDER};
            border-radius: 4px;
        """)
        pb_lay = QVBoxLayout(prof_box)
        pb_lay.setContentsMargins(10, 8, 10, 8)
        pb_lay.setSpacing(5)

        pb_hdr = QLabel("RESOURCE PROFILE")
        pb_hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        pb_hdr.setStyleSheet(f"color: {COLOR_CYAN}; background-color: {PANEL_SECTION_BG}; border: none;")
        pb_lay.addWidget(pb_hdr)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._prof_btns: dict[str, QPushButton] = {}

        for pid, label in [
            (PROFILE_LOW_RAM, "LOW RAM"),
            (PROFILE_BALANCED, "BALANCED"),
            (PROFILE_FULL_EXPERIENCE, "FULL EXPERIENCE"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, p=pid: self._select_profile(p))
            btn_row.addWidget(b)
            self._prof_btns[pid] = b

        pb_lay.addLayout(btn_row)

        self._prof_desc_lbl = QLabel()
        self._prof_desc_lbl.setFont(QFont("Courier New", 7))
        self._prof_desc_lbl.setStyleSheet(f"color: {COLOR_TEXT_MED}; background-color: {PANEL_SECTION_BG}; border: none;")
        self._prof_desc_lbl.setWordWrap(True)
        pb_lay.addWidget(self._prof_desc_lbl)

        main_lay.addWidget(prof_box)

        # ── Scrollable Diagnostics Content ────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"background-color: {PANEL_SOLID_BG}; border: none;")

        content = QWidget()
        content.setObjectName("DiagContent")
        content.setStyleSheet(f"background-color: {PANEL_SOLID_BG};")
        c_lay = QVBoxLayout(content)
        c_lay.setContentsMargins(0, 0, 4, 0)
        c_lay.setSpacing(8)

        # 1. Live Metrics Grid (Solid Section)
        grid_w = QWidget()
        grid_w.setStyleSheet(f"""
            background-color: {PANEL_SECTION_BG};
            border: 1px solid {PANEL_BORDER};
            border-radius: 4px;
        """)
        glay = QGridLayout(grid_w)
        glay.setContentsMargins(10, 8, 10, 8)
        glay.setSpacing(6)

        self._lbl_proc_ram = QLabel()
        self._lbl_sys_ram = QLabel()
        self._lbl_cpu = QLabel()
        self._lbl_state = QLabel()

        for col_idx, (title, lbl) in enumerate([
            ("JARVIS PROCESS RAM", self._lbl_proc_ram),
            ("SYSTEM RAM (OS)", self._lbl_sys_ram),
            ("CPU UTILISATION", self._lbl_cpu),
            ("RUNTIME STATE", self._lbl_state),
        ]):
            box = QWidget()
            box.setStyleSheet(f"""
                background-color: {PANEL_BOX_BG};
                border: 1px solid {PANEL_BORDER};
                border-radius: 3px;
            """)
            b_lay = QVBoxLayout(box)
            b_lay.setContentsMargins(8, 6, 8, 6)
            b_lay.setSpacing(3)

            th = QLabel(title)
            th.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
            th.setStyleSheet(f"color: {COLOR_CYAN}; border: none; background-color: {PANEL_BOX_BG};")
            b_lay.addWidget(th)

            lbl.setFont(QFont("Courier New", 7))
            lbl.setStyleSheet(f"color: {COLOR_TEXT_BRIGHT}; border: none; background-color: {PANEL_BOX_BG};")
            lbl.setWordWrap(True)
            b_lay.addWidget(lbl)

            glay.addWidget(box, col_idx // 2, col_idx % 2)

        c_lay.addWidget(grid_w)

        # 2. Leak Diagnostics & Recommendation (Solid Section)
        leak_box = QWidget()
        leak_box.setStyleSheet(f"""
            background-color: {PANEL_SECTION_BG};
            border: 1px solid {PANEL_BORDER};
            border-radius: 4px;
        """)
        lb_lay = QVBoxLayout(leak_box)
        lb_lay.setContentsMargins(10, 8, 10, 8)
        lb_lay.setSpacing(4)

        self._leak_status_lbl = QLabel("◈ MEMORY HEALTH: ANALYZING...")
        self._leak_status_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._leak_status_lbl.setStyleSheet(f"color: {COLOR_GREEN}; border: none; background-color: {PANEL_SECTION_BG};")
        lb_lay.addWidget(self._leak_status_lbl)

        self._leak_rec_lbl = QLabel()
        self._leak_rec_lbl.setFont(QFont("Courier New", 7))
        self._leak_rec_lbl.setStyleSheet(f"color: {COLOR_TEXT_MED}; border: none; background-color: {PANEL_SECTION_BG};")
        self._leak_rec_lbl.setWordWrap(True)
        lb_lay.addWidget(self._leak_rec_lbl)
        c_lay.addWidget(leak_box)

        # 3. Startup & Module Breakdown (Solid Section)
        start_box = QWidget()
        start_box.setStyleSheet(f"""
            background-color: {PANEL_SECTION_BG};
            border: 1px solid {PANEL_BORDER};
            border-radius: 4px;
        """)
        sb_lay = QVBoxLayout(start_box)
        sb_lay.setContentsMargins(10, 8, 10, 8)
        sb_lay.setSpacing(4)

        sb_hdr = QLabel("STARTUP BREAKDOWN & OPTIONAL MODULES")
        sb_hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        sb_hdr.setStyleSheet(f"color: {COLOR_CYAN}; border: none; background-color: {PANEL_SECTION_BG};")
        sb_lay.addWidget(sb_hdr)

        self._startup_lbl = QLabel()
        self._startup_lbl.setFont(QFont("Courier New", 7))
        self._startup_lbl.setStyleSheet(f"color: {COLOR_TEXT_CYAN}; border: none; background-color: {PANEL_SECTION_BG};")
        self._startup_lbl.setWordWrap(True)
        sb_lay.addWidget(self._startup_lbl)
        c_lay.addWidget(start_box)

        # 4. Background Workers (Solid Section)
        work_box = QWidget()
        work_box.setStyleSheet(f"""
            background-color: {PANEL_SECTION_BG};
            border: 1px solid {PANEL_BORDER};
            border-radius: 4px;
        """)
        wb_lay = QVBoxLayout(work_box)
        wb_lay.setContentsMargins(10, 8, 10, 8)
        wb_lay.setSpacing(4)

        wb_hdr = QLabel("ACTIVE BACKGROUND WORKERS")
        wb_hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        wb_hdr.setStyleSheet(f"color: {COLOR_CYAN}; border: none; background-color: {PANEL_SECTION_BG};")
        wb_lay.addWidget(wb_hdr)

        self._workers_lbl = QLabel()
        self._workers_lbl.setFont(QFont("Courier New", 7))
        self._workers_lbl.setStyleSheet(f"color: {COLOR_TEXT_CYAN}; border: none; background-color: {PANEL_SECTION_BG};")
        self._workers_lbl.setWordWrap(True)
        wb_lay.addWidget(self._workers_lbl)
        c_lay.addWidget(work_box)

        scroll.setWidget(content)
        main_lay.addWidget(scroll, stretch=1)

        self.refresh()

    def _select_profile(self, profile: str):
        clean = self._guardian.set_profile(profile)
        save_resource_profile(clean)
        if callable(self._on_profile_changed):
            try:
                self._on_profile_changed(clean)
            except Exception:
                pass
        self.refresh()

    def refresh(self):
        """Update all telemetry and diagnostic fields with crisp, readable styling."""
        cur_prof = self._guardian.get_profile()

        desc_map = {
            PROFILE_LOW_RAM: "LOW RAM: Nonessential visual particles paused, telemetry slowed to 5s, heavy optional modules deferred.",
            PROFILE_BALANCED: "BALANCED: Full visualizer ribbons, responsive voice interaction, 2s telemetry interval.",
            PROFILE_FULL_EXPERIENCE: "FULL EXPERIENCE: Maximum visual particles, unconstrained visualizer fidelity, 1.5s telemetry.",
        }
        self._prof_desc_lbl.setText(desc_map.get(cur_prof, ""))

        for pid, btn in self._prof_btns.items():
            if pid == cur_prof:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #0e3352;
                        color: #ffffff;
                        border: 1.5px solid {COLOR_BRASS};
                        border-radius: 3px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {PANEL_BOX_BG};
                        color: {COLOR_TEXT_MED};
                        border: 1px solid {PANEL_BORDER};
                        border-radius: 3px;
                    }}
                    QPushButton:hover {{
                        background-color: #0c2a44;
                        color: {COLOR_TEXT_BRIGHT};
                        border: 1px solid {COLOR_CYAN};
                    }}
                """)

        # Analyze memory
        diag = self._guardian.analyze_memory()
        history = self._guardian.get_history()
        latest = history[-1] if history else None

        # Metrics
        p_rss = diag["current_rss_mb"]
        p_base = diag["baseline_rss_mb"]
        p_peak = diag["peak_rss_mb"]
        p_delta = diag["delta_from_baseline_mb"]
        sign = "+" if p_delta >= 0 else ""
        self._lbl_proc_ram.setText(
            f"RSS: {p_rss:.1f} MB (Delta: {sign}{p_delta:.1f} MB)\nPeak: {p_peak:.1f} MB | Boot: {p_base:.1f} MB"
        )

        if latest:
            self._lbl_sys_ram.setText(
                f"{latest.system_ram_percent:.0f}% Used\n{latest.system_ram_available_gb:.1f} GB Free of {latest.system_ram_total_gb:.1f} GB"
            )
            self._lbl_cpu.setText(
                f"JARVIS App: {latest.process_cpu_percent:.1f}%\nTotal System: {latest.system_cpu_percent:.1f}%"
            )
            self._lbl_state.setText(f"Status: {latest.state}\nSamples: {len(history)} / 60")
        else:
            self._lbl_sys_ram.setText("Measuring...")
            self._lbl_cpu.setText("Measuring...")
            self._lbl_state.setText("IDLE")

        # Health status
        st = diag["growth_status"]
        if st == "SUSTAINED_GROWTH":
            col = COLOR_RED
            lbl = "⚠️ SUSTAINED GROWTH DETECTED"
        elif st == "MODERATE_ELEVATION":
            col = COLOR_AMBER
            lbl = "⚡ MODERATE MEMORY FLUCTUATION"
        else:
            col = COLOR_GREEN
            lbl = "✓ MEMORY STABLE & HEALTHY"

        self._leak_status_lbl.setText(f"◈ {lbl} ({diag['growth_rate_mb_min']:+.2f} MB/min)")
        self._leak_status_lbl.setStyleSheet(f"color: {col}; border: none; background-color: {PANEL_SECTION_BG};")
        self._leak_rec_lbl.setText(diag["recommendation"])

        # Startup summary
        st_sum = self._guardian.startup_tracker.get_summary()
        stages_str = " | ".join(f"{s['name']}: {s['duration_s']}s" for s in st_sum["stages"]) if st_sum["stages"] else "Initialized"
        mods_str = ", ".join(f"{m} ({st_m})" for m, st_m in st_sum["optional_modules"].items())
        self._startup_lbl.setText(f"Stages: {stages_str}\nOptional Modules: {mods_str}")

        # Workers
        workers = self._guardian.get_workers_status()
        if workers:
            w_lines = [f"• {w['name']}: {'ALIVE' if w['is_alive'] else 'STOPPED'} ({w['description']})" for w in workers]
            self._workers_lbl.setText("\n".join(w_lines))
        else:
            self._workers_lbl.setText("• Main Qt GUI Thread\n• Telemetry Poller (_SysMetrics)\n• Background Audio Runner")
