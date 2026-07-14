"""DAC Scan (Rotation Centre) window.

Scans Ch10 (translation) at multiple rotation angles (Ch11) to find the
aperture centre at each angle, then fits A·sin(θ)+B·cos(θ)+C to determine
suggested Ch3/Ch4 correction for aligning the rotation centre with the beam.

Layout
------
Left  : parameter panel + status + analysis results
Right : pyqtgraph GraphicsLayoutWidget
          [Ch10 scan profiles per θ  (top)]
          [θ vs aperture centre + sinusoidal fit  (bottom)]
"""
from __future__ import annotations

import json
import re

import numpy as np
from datetime import datetime
from pathlib import Path
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout, QWidget,
)
import pyqtgraph as pg
from scipy.optimize import curve_fit
from scipy.special import erf

try:
    from .dac_scan_rot_backend import (
        CH_ROT, CH_SCAN,
        DEG_PER_PULSE_CH11, UM_PER_PULSE_CH10, UM_PER_PULSE_CH3, UM_PER_PULSE_CH4,
        DacScanRotWorker, GpibReader, GpibReaderRotSim,
    )
    from settings import log_prefs
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr
except ImportError:
    import os, sys
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, _root)
    from apps.dac_scan.dac_scan_rot_backend import (
        CH_ROT, CH_SCAN,
        DEG_PER_PULSE_CH11, UM_PER_PULSE_CH10, UM_PER_PULSE_CH3, UM_PER_PULSE_CH4,
        DacScanRotWorker, GpibReader, GpibReaderRotSim,
    )
    from settings import log_prefs
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_THETA_COLORS = [
    (31, 119, 180),
    (255, 127, 14),
    (44, 160, 44),
    (214, 39, 40),
    (148, 103, 189),
    (140, 86, 75),
    (227, 119, 194),
    (127, 127, 127),
    (188, 189, 34),
    (23, 190, 207),
]


def _color(idx: int) -> tuple[int, int, int]:
    return _THETA_COLORS[idx % len(_THETA_COLORS)]


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


# ---------------------------------------------------------------------------
# Aperture model and fitting
# ---------------------------------------------------------------------------

def _aperture_model(x, A, x1, x2, w, bg):
    return A * (erf((x - x1) / w) - erf((x - x2) / w)) + bg


def _fit_aperture(
    pulses: np.ndarray, intensities: np.ndarray
) -> tuple[float, float, float, np.ndarray, bool]:
    """Fit erf aperture model.

    Returns (center_pulse, left_edge, right_edge, popt, was_flipped).
    *was_flipped* is True when the raw intensity was inverted before fitting so
    the caller can reconstruct the fit curve in the original coordinate space.
    """
    x = pulses.astype(float)
    y = intensities.astype(float)

    was_flipped = bool(y[0] > y[len(y) // 2])
    if was_flipped:
        y = float(y.max()) - y

    A0  = float(y.max() - y.min())
    thr = float(y.min() + 0.5 * (y.max() - y.min()))
    idx = np.where(y > thr)[0]
    x1_0 = float(x[idx[0]])  if len(idx) > 0 else float(x[len(x) // 4])
    x2_0 = float(x[idx[-1]]) if len(idx) > 0 else float(x[3 * len(x) // 4])

    p0 = [A0, x1_0, x2_0, 5.0, float(y.min())]
    popt, _ = curve_fit(_aperture_model, x, y, p0=p0, maxfev=10_000)
    _, x1, x2, _, _ = popt
    center = (float(x1) + float(x2)) / 2.0
    return center, float(x1), float(x2), popt, was_flipped


# ---------------------------------------------------------------------------
# One-shot correction move worker
# ---------------------------------------------------------------------------

CH_CH3 = 3
CH_CH4 = 4


class _CorrectionMoveWorker(QThread):
    """Move Ch3, Ch4, and optionally Ch10 by the given relative pulse amounts, then stop."""

    move_completed = pyqtSignal()
    move_failed    = pyqtSignal(str)

    def __init__(
        self, controller, delta_ch3: int, delta_ch4: int,
        delta_ch10: int = 0, parent=None,
    ):
        super().__init__(parent)
        self.controller  = controller
        self.delta_ch3   = delta_ch3
        self.delta_ch4   = delta_ch4
        self.delta_ch10  = delta_ch10

    def run(self) -> None:
        try:
            if self.delta_ch3 != 0:
                self.controller.move_ch_relative(CH_CH3, self.delta_ch3)
                self.controller.wait_until_stop(stay_in_rem=True)
            if self.delta_ch4 != 0:
                self.controller.move_ch_relative(CH_CH4, self.delta_ch4)
                self.controller.wait_until_stop(stay_in_rem=True)
            if self.delta_ch10 != 0:
                self.controller.move_ch_relative(CH_SCAN, self.delta_ch10)
                self.controller.wait_until_stop(stay_in_rem=True)
            self.controller.switch_to_loc()
            self.move_completed.emit()
        except Exception as e:
            try:
                self.controller.switch_to_loc()
            except Exception:
                pass
            self.move_failed.emit(str(e))


# ---------------------------------------------------------------------------
# Post-scan move worker (return to 0° and optionally centre Ch10)
# ---------------------------------------------------------------------------

class _PostScanMoveWorker(QThread):
    move_completed  = pyqtSignal()
    move_failed     = pyqtSignal(str)
    status_message  = pyqtSignal(str)

    def __init__(
        self,
        controller,
        ch10_center: int | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.controller   = controller
        self.ch10_center  = ch10_center  # None → skip Ch10 move

    def run(self) -> None:
        try:
            self.status_message.emit(tr("Returning to θ=0°…"))
            self.controller.move_ch_absolute(CH_ROT, 0)
            self.controller.wait_until_stop(stay_in_rem=True)

            if self.ch10_center is not None:
                self.status_message.emit(
                    tr("Moving Ch10 to centre ({n} pulse)…", n=self.ch10_center)
                )
                self.controller.move_ch_absolute(CH_SCAN, self.ch10_center)
                self.controller.wait_until_stop(stay_in_rem=True)

            self.controller.switch_to_loc()
            self.move_completed.emit()
        except Exception as e:
            try:
                self.controller.switch_to_loc()
            except Exception:
                pass
            self.move_failed.emit(str(e))


# ---------------------------------------------------------------------------
# Rotation model and fitting
# ---------------------------------------------------------------------------

def _rot_model(theta_deg, A, B, C):
    rad = np.deg2rad(theta_deg)
    return A * np.sin(rad) + B * np.cos(rad) + C


def _fit_rotation(thetas_deg: np.ndarray, centers: np.ndarray) -> tuple[float, float, float]:
    """Fit centers = A·sin(θ) + B·cos(θ) + C.  Returns (A, B, C)."""
    p0 = [0.0, 0.0, float(np.mean(centers))]
    popt, _ = curve_fit(_rot_model, thetas_deg, centers, p0=p0, maxfev=10_000)
    return float(popt[0]), float(popt[1]), float(popt[2])


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class DacScanRotWindow(QMainWindow):
    """DAC Scan (Rotation Centre) — transmission aperture scan at multiple angles."""

    def __init__(
        self,
        controller=None,
        gpib_reader: GpibReader | None = None,
        debug: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("DAC Scan (Rotation Centre)"))
        self.resize(1300, 850)

        self._controller  = controller
        self._gpib_reader = gpib_reader
        self._debug       = debug
        self._worker: DacScanRotWorker | None = None

        # {theta_deg: (pulses_array, transmitted_array)}
        self._scan_data:        dict[float, tuple[np.ndarray, np.ndarray]] = {}
        self._theta_order: list[float] = []

        self._aperture_centers: dict[float, float] = {}

        self._data_curves:   dict[float, pg.PlotDataItem]  = {}
        self._fit_curves:    dict[float, pg.PlotDataItem]  = {}
        self._center_vlines: dict[float, pg.InfiniteLine]  = {}
        self._theta_colors:  dict[float, tuple[int,...]]   = {}

        self._fit_A: float | None = None
        self._fit_B: float | None = None
        self._fit_C: float | None = None

        self._move_ch3: int = 0   # pending correction (pulses)
        self._move_ch4: int = 0
        self._move_ch10: int = 0  # Ch10 compensation (= Ch4 move)
        self._move_worker: _CorrectionMoveWorker | None = None
        self._post_scan_worker: _PostScanMoveWorker | None = None
        self._correction_applied: bool = False

        # Details-save state
        self._scan_start_time:  datetime | None = None
        self._scan_theta_list:  list[float]     = []
        self._ch10_pulses:      list[int]        = []
        self._scan_speed:       str              = "M"
        self._scan_settle_ms:   int              = 100
        self._scan_accumulation: int             = 10
        self._scan_half_range_um: float          = 300.0
        self._scan_step_um:     float            = 20.0
        self._analysis_results: dict | None      = None

        self._setup_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 6, 8, 8)
        outer.setSpacing(4)

        self._banner_label = QLabel("")
        self._banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner_label.setMinimumHeight(32)
        self._banner_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; padding: 4px 12px; "
            "border-radius: 4px; color: #888;"
        )
        outer.addWidget(self._banner_label)

        content = QWidget()
        root = QHBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addWidget(self._build_param_panel(), 0)
        root.addWidget(self._build_plot_area(),   1)
        outer.addWidget(content, 1)

    def _build_param_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(285)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Rotation angles ───────────────────────────────────────────────
        theta_grp = QGroupBox(tr("Rotation Angles — Ch11"))
        theta_lay = QVBoxLayout(theta_grp)
        theta_lay.addWidget(QLabel(tr("θ list (degrees), comma-separated:")))
        self._theta_edit = QLineEdit("-5, 0, 10, 20, 30")
        self._theta_edit.setPlaceholderText(tr("e.g. 0, 10, 20, 30, -6"))
        theta_lay.addWidget(self._theta_edit)
        layout.addWidget(theta_grp)

        # ── Ch10 scan range (µm, relative to current Ch10 position) ──────
        scan_grp = QGroupBox(tr("Ch10 Scan Range  (centred on current position)"))
        scan_lay = QVBoxLayout(scan_grp)

        row_half = QHBoxLayout()
        lbl_half = QLabel(tr("Half-range (µm):"))
        lbl_half.setFixedWidth(120)
        row_half.addWidget(lbl_half)
        self._half_range_spin = _no_wheel(QDoubleSpinBox())
        self._half_range_spin.setRange(1.0, 100_000.0)
        self._half_range_spin.setValue(300.0)
        self._half_range_spin.setSuffix(" µm")
        self._half_range_spin.setSingleStep(10.0)
        row_half.addWidget(self._half_range_spin)
        scan_lay.addLayout(row_half)

        row_step = QHBoxLayout()
        lbl_step = QLabel(tr("Step (µm):"))
        lbl_step.setFixedWidth(120)
        row_step.addWidget(lbl_step)
        self._step_spin = _no_wheel(QDoubleSpinBox())
        self._step_spin.setRange(UM_PER_PULSE_CH10, 10_000.0)
        self._step_spin.setValue(20.0)
        self._step_spin.setSuffix(" µm")
        self._step_spin.setSingleStep(2.0)
        row_step.addWidget(self._step_spin)
        scan_lay.addLayout(row_step)

        self._scan_preview_label = QLabel()
        self._scan_preview_label.setWordWrap(True)
        self._scan_preview_label.setStyleSheet("font-size: 10px; color: #888;")
        scan_lay.addWidget(self._scan_preview_label)
        self._update_scan_preview()
        layout.addWidget(scan_grp)

        self._half_range_spin.valueChanged.connect(self._update_scan_preview)
        self._step_spin.valueChanged.connect(self._update_scan_preview)

        # ── Speed ─────────────────────────────────────────────────────────
        speed_grp = QGroupBox(tr("Speed"))
        speed_lay = QHBoxLayout(speed_grp)
        self._speed_grp = QButtonGroup(self)
        for label in ("L", "M", "H"):
            rb = QRadioButton(label)
            rb.setProperty("speed_val", label)
            self._speed_grp.addButton(rb)
            speed_lay.addWidget(rb)
            if label == "M":
                rb.setChecked(True)
        layout.addWidget(speed_grp)

        # ── Settle time ───────────────────────────────────────────────────
        settle_grp = QGroupBox(tr("Settle time after move"))
        settle_lay = QHBoxLayout(settle_grp)
        self._settle_spin = _no_wheel(QSpinBox())
        self._settle_spin.setRange(0, 9999)
        self._settle_spin.setValue(100)
        self._settle_spin.setSuffix(" ms")
        self._settle_spin.setSingleStep(10)
        settle_lay.addWidget(self._settle_spin)
        settle_lay.addStretch()
        layout.addWidget(settle_grp)

        # ── Accumulation ─────────────────────────────────────────────────
        accum_grp = QGroupBox(tr("Accumulation"))
        accum_lay = QHBoxLayout(accum_grp)
        accum_lay.addWidget(QLabel(tr("Reads per point:")))
        self._accum_spin = _no_wheel(QSpinBox())
        self._accum_spin.setRange(1, 100)
        self._accum_spin.setValue(10)
        self._accum_spin.setSingleStep(1)
        accum_lay.addWidget(self._accum_spin)
        accum_lay.addStretch()
        layout.addWidget(accum_grp)

        # ── Post-scan actions ─────────────────────────────────────────────
        post_grp = QGroupBox(tr("Post-scan actions"))
        post_lay = QVBoxLayout(post_grp)
        self._return_zero_chk = QCheckBox(tr("Return to θ=0° after scan"))
        self._return_zero_chk.setChecked(True)
        self._center_ch10_chk = QCheckBox(tr("Move Ch10 to centre at θ=0°"))
        self._center_ch10_chk.setChecked(False)
        self._center_ch10_chk.setEnabled(False)
        post_lay.addWidget(self._return_zero_chk)
        post_lay.addWidget(self._center_ch10_chk)
        layout.addWidget(post_grp)

        self._return_zero_chk.toggled.connect(self._update_post_scan_ui)
        self._theta_edit.textChanged.connect(self._update_post_scan_ui)

        # ── Control buttons ───────────────────────────────────────────────
        self._start_btn = QPushButton(tr("Start Scan"))
        self._start_btn.setStyleSheet(
            "QPushButton:enabled { background-color: #27ae60; color: white;"
            " font-weight: bold; font-size: 13px; }"
        )
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn = QPushButton(tr("Stop"))
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton:enabled { border: 2px solid #e67e22; color: #e67e22; font-weight: bold; }"
        )
        self._stop_btn.clicked.connect(self._on_stop)
        self._estop_btn = QPushButton(tr("Emergency Stop"))
        self._estop_btn.setStyleSheet(
            "QPushButton { background-color: #FF3333; color: white; font-weight: bold;"
            " font-size: 16px; border-radius: 4px; }"
            " QPushButton:pressed { background-color: #CC0000; }"
        )
        self._estop_btn.clicked.connect(self._on_emergency_stop)
        layout.addWidget(self._start_btn)
        layout.addWidget(self._stop_btn)
        layout.addWidget(self._estop_btn)

        # ── Status ────────────────────────────────────────────────────────
        self._status_label = QLabel(tr("Ready"))
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        layout.addStretch()

        # ── Analysis result ───────────────────────────────────────────────
        ana_grp = QGroupBox(tr("Analysis Result"))
        ana_lay = QVBoxLayout(ana_grp)
        formula_lbl = QLabel("centre(θ) = A·sin(θ) + B·cos(θ) + C")
        formula_lbl.setStyleSheet(
            "font-style: italic; font-size: 11px; color: #555;"
            "padding: 2px 0 4px 0;"
        )
        formula_lbl.setWordWrap(True)
        ana_lay.addWidget(formula_lbl)

        self._ana_A_label    = QLabel(tr("A  [X eccentricity] = —"))
        self._ana_B_label    = QLabel(tr("B  [Y eccentricity] = —"))
        self._ana_C_label    = QLabel(tr("C  [global offset]  = —"))
        self._ana_ch3_label  = QLabel(tr("Ch3: —"))
        self._ana_ch4_label  = QLabel(tr("Ch4: —"))
        self._ana_ch10_label = QLabel(tr("Ch10 compensation: —"))
        for lbl in (self._ana_A_label, self._ana_B_label, self._ana_C_label,
                    self._ana_ch3_label, self._ana_ch4_label, self._ana_ch10_label):
            lbl.setWordWrap(True)
        ana_lay.addWidget(self._ana_A_label)
        ana_lay.addWidget(self._ana_B_label)
        ana_lay.addWidget(self._ana_C_label)
        sep = QLabel(tr("── Suggested Motion ──"))
        sep.setStyleSheet("color: #888; font-size: 11px;")
        ana_lay.addWidget(sep)
        ana_lay.addWidget(self._ana_ch3_label)
        ana_lay.addWidget(self._ana_ch4_label)
        ana_lay.addWidget(self._ana_ch10_label)
        self._also_move_ch10_chk = QCheckBox(tr("Also compensate Ch10 (= Ch4 move)"))
        self._also_move_ch10_chk.setChecked(True)
        ana_lay.addWidget(self._also_move_ch10_chk)
        self._return_centre_btn = QPushButton(tr("Return to θ=0° && Centre Ch10"))
        self._return_centre_btn.setEnabled(False)
        self._return_centre_btn.setStyleSheet(
            "QPushButton:enabled { background-color: #388E3C; color: white; font-weight: bold; }"
        )
        self._return_centre_btn.clicked.connect(self._on_return_and_centre)
        ana_lay.addWidget(self._return_centre_btn)
        self._apply_btn = QPushButton(tr("Apply Correction (Move Ch3, Ch4 & Ch10)"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.setStyleSheet(
            "QPushButton:enabled { background-color: #1976D2; color: white; font-weight: bold; }"
        )
        self._apply_btn.clicked.connect(self._on_apply_correction)
        ana_lay.addWidget(self._apply_btn)
        self._run_analysis_btn = QPushButton(tr("Re-run Analysis"))
        self._run_analysis_btn.setEnabled(False)
        self._run_analysis_btn.clicked.connect(self._run_analysis)
        ana_lay.addWidget(self._run_analysis_btn)
        layout.addWidget(ana_grp)

        return panel

    def _update_scan_preview(self) -> None:
        half_um = self._half_range_spin.value()
        step_um = self._step_spin.value()
        half_pulse = int(half_um / UM_PER_PULSE_CH10)
        step_pulse = max(1, int(step_um / UM_PER_PULSE_CH10))
        if half_pulse > 0:
            n = len(range(-half_pulse, half_pulse + 1, step_pulse))
            self._scan_preview_label.setText(
                tr(
                    "±{half_pulse} pulse (±{half_um:.0f} µm) / "
                    "step {step_pulse} pulse ({step_um:.0f} µm) / "
                    "{n} pts",
                    half_pulse=half_pulse, half_um=half_pulse * UM_PER_PULSE_CH10,
                    step_pulse=step_pulse, step_um=step_pulse * UM_PER_PULSE_CH10, n=n,
                )
            )
        else:
            self._scan_preview_label.setText(tr("(half-range too small)"))

    def _set_banner(self, text: str, color: str = "#888") -> None:
        self._banner_label.setStyleSheet(
            f"font-size: 15px; font-weight: bold; padding: 4px 12px; "
            f"border-radius: 4px; color: {color};"
        )
        self._banner_label.setText(text)

    def _update_post_scan_ui(self) -> None:
        return_checked = self._return_zero_chk.isChecked()
        has_zero = self._theta_list_has_zero()
        enabled = return_checked and has_zero
        self._center_ch10_chk.setEnabled(enabled)
        if not enabled:
            self._center_ch10_chk.setChecked(False)

    def _theta_list_has_zero(self) -> bool:
        raw = self._theta_edit.text().strip()
        parts = re.split(r"[,\s]+", raw)
        try:
            return any(abs(float(p)) < 1e-9 for p in parts if p)
        except ValueError:
            return False

    def _get_center_at_zero(self) -> float | None:
        for theta, center in self._aperture_centers.items():
            if abs(theta) < 1e-9:
                return center
        return None

    def _build_plot_area(self) -> QWidget:
        self._glw = pg.GraphicsLayoutWidget()

        self._plot_top = self._glw.addPlot(row=0, col=0, title=tr("Ch10 Transmission Scans"))
        self._plot_top.setLabel("bottom", tr("Ch10 pulse"))
        self._plot_top.setLabel("left",   tr("Intensity (a.u.)"))
        self._legend_top = self._plot_top.addLegend(offset=(10, 10))

        self._plot_bot = self._glw.addPlot(row=1, col=0, title=tr("θ vs Aperture Centre"))
        self._plot_bot.setLabel("bottom", tr("θ (degrees)"))
        self._plot_bot.setLabel("left",   tr("Aperture centre (Ch10 pulse)"))

        self._fit_curve_bot = self._plot_bot.plot(
            pen=pg.mkPen("r", width=2), name=tr("A·sin+B·cos+C fit")
        )
        self._center_scatter = self._plot_bot.plot(
            pen=None, symbol="o", symbolSize=8,
            symbolBrush=pg.mkBrush(255, 255, 255),
            symbolPen=pg.mkPen("k", width=1.5),
        )

        self._glw.ci.layout.setRowStretchFactor(0, 3)
        self._glw.ci.layout.setRowStretchFactor(1, 2)

        return self._glw

    # ── Scan control ──────────────────────────────────────────────────────────

    def _parse_theta_list(self) -> list[float] | None:
        raw = self._theta_edit.text().strip()
        parts = re.split(r"[,\s]+", raw)
        try:
            result = [float(p) for p in parts if p]
        except ValueError:
            QMessageBox.warning(
                self, tr("Input Error"),
                tr("Cannot parse theta list.\n"
                   "Use comma-separated numbers, e.g.: 0, 10, 20, 30, -6"),
            )
            return None
        if not result:
            QMessageBox.warning(self, tr("Input Error"), tr("Theta list is empty."))
            return None
        return result

    def _on_start(self) -> None:
        if self._controller is None:
            QMessageBox.warning(self, tr("Error"), tr("Stage controller not connected."))
            return
        if self._worker is not None and self._worker.isRunning():
            return

        theta_list = self._parse_theta_list()
        if theta_list is None:
            return

        reply = QMessageBox.question(
            self, tr("Safety Check"),
            tr("Have you confirmed that the stage will not collide "
               "within the specified rotation range?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Read current Ch10 position and compute absolute pulse range
        try:
            center_ch10 = int(self._controller.get_ch_pos(CH_SCAN))
        except Exception as e:
            QMessageBox.warning(self, tr("Error"), tr("Cannot read Ch10 position:\n{error}", error=e))
            return

        half_pulse = int(self._half_range_spin.value() / UM_PER_PULSE_CH10)
        step_pulse = max(1, int(self._step_spin.value() / UM_PER_PULSE_CH10))
        if half_pulse < 1:
            QMessageBox.warning(self, tr("Input Error"), tr("Half-range is too small (< 1 pulse)."))
            return

        ch10_start = center_ch10 - half_pulse
        ch10_stop  = center_ch10 + half_pulse

        speed = next(
            btn.property("speed_val")
            for btn in self._speed_grp.buttons()
            if btn.isChecked()
        )

        # Keithley check — must pass before resetting state
        reader: GpibReader
        if self._gpib_reader is not None:
            reader = self._gpib_reader
        elif self._debug:
            reader = GpibReaderRotSim(true_C=float(center_ch10))
        else:
            QMessageBox.warning(
                self,
                tr("Keithley 2000 not connected"),
                tr("Keithley 2000 is not connected.\n"
                   "Please connect the Keithley 2000 from the main window "
                   "before starting the scan."),
            )
            return

        self._reset_state(theta_list)
        self._scan_start_time    = datetime.now()
        self._scan_theta_list    = list(theta_list)
        self._ch10_pulses        = list(range(ch10_start, ch10_stop + 1, step_pulse))
        self._scan_speed         = speed
        self._scan_settle_ms     = self._settle_spin.value()
        self._scan_accumulation  = self._accum_spin.value()
        self._scan_half_range_um = self._half_range_spin.value()
        self._scan_step_um       = self._step_spin.value()
        self._status_label.setText(
            tr("Ch10 centre: {center}  range: {start}…{stop}  step: {step}",
               center=center_ch10, start=ch10_start, stop=ch10_stop, step=step_pulse)
        )

        self._worker = DacScanRotWorker(
            controller=self._controller,
            gpib_reader=reader,
            theta_deg_list=theta_list,
            ch10_start=ch10_start,
            ch10_stop=ch10_stop,
            ch10_step=step_pulse,
            speed=speed,
            settle_ms=self._scan_settle_ms,
            accumulation=self._scan_accumulation,
        )
        self._worker.point_measured.connect(self._on_point_measured)
        self._worker.theta_completed.connect(self._on_theta_completed)
        self._worker.scan_completed.connect(self._on_scan_completed)
        self._worker.scan_aborted.connect(self._on_scan_aborted)
        self._worker.status_message.connect(self._status_label.setText)
        self._worker.status_message.connect(
            lambda msg: self._set_banner(msg, "#1565C0")
        )

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._set_banner(tr("Scanning…"), "#1565C0")
        self._worker.start()

    def _reset_state(self, theta_list: list[float]) -> None:
        self._scan_data.clear()
        self._theta_order = list(theta_list)
        self._aperture_centers.clear()
        self._fit_A = self._fit_B = self._fit_C = None
        self._analysis_results = None
        self._data_curves.clear()
        self._fit_curves.clear()
        self._center_vlines.clear()
        self._theta_colors.clear()

        self._plot_top.clear()
        self._legend_top = self._plot_top.addLegend(offset=(10, 10))
        self._plot_bot.clear()
        self._fit_curve_bot = self._plot_bot.plot(
            pen=pg.mkPen("r", width=2), name=tr("A·sin+B·cos+C fit")
        )
        self._center_scatter = self._plot_bot.plot(
            pen=None, symbol="o", symbolSize=8,
            symbolBrush=pg.mkBrush(255, 255, 255),
            symbolPen=pg.mkPen("k", width=1.5),
        )

        self._ana_A_label.setText(tr("A  [X eccentricity] = —"))
        self._ana_B_label.setText(tr("B  [Y eccentricity] = —"))
        self._ana_C_label.setText(tr("C  [global offset]  = —"))
        self._ana_ch3_label.setText(tr("Ch3: —"))
        self._ana_ch4_label.setText(tr("Ch4: —"))
        self._ana_ch10_label.setText(tr("Ch10 compensation: —"))
        self._return_centre_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._run_analysis_btn.setEnabled(False)
        self._correction_applied = False

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.abort()
            if self._controller is not None:
                self._controller.normal_stop()
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Aborting…"))

    def _on_emergency_stop(self) -> None:
        if self._worker is not None:
            self._worker.abort()
        if self._controller is not None:
            self._controller.emergency_stop()
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("EMERGENCY STOP — AESTP sent."))

    # ── Data reception ────────────────────────────────────────────────────────

    @pyqtSlot(float, int, float)
    def _on_point_measured(
        self, theta_deg: float, pulse_ch10: int, transmitted: float
    ) -> None:
        if theta_deg not in self._scan_data:
            self._scan_data[theta_deg] = (
                np.array([], dtype=float),
                np.array([], dtype=float),
            )
            idx   = len(self._scan_data) - 1
            color = _color(idx)
            self._theta_colors[theta_deg] = color
            label = f"θ={theta_deg:.1f}°"
            self._data_curves[theta_deg] = self._plot_top.plot(
                pen=None, symbol="o", symbolSize=4,
                symbolBrush=pg.mkBrush(*color),
                symbolPen=pg.mkPen(color, width=0),
                name=label,
            )
            self._fit_curves[theta_deg] = self._plot_top.plot(
                pen=pg.mkPen(color, width=2),
            )

        pulses, trans = self._scan_data[theta_deg]
        self._scan_data[theta_deg] = (
            np.append(pulses, float(pulse_ch10)),
            np.append(trans,  transmitted),
        )
        self._data_curves[theta_deg].setData(
            self._scan_data[theta_deg][0],
            self._scan_data[theta_deg][1],
        )

    @pyqtSlot(float)
    def _on_theta_completed(self, theta_deg: float) -> None:
        if theta_deg not in self._scan_data:
            return
        pulses, ints = self._scan_data[theta_deg]
        if len(pulses) < 5:
            return
        try:
            center, _x1, _x2, popt, was_flipped = _fit_aperture(pulses, ints)
            self._aperture_centers[theta_deg] = center

            xx = np.linspace(float(pulses.min()), float(pulses.max()), 500)
            yy = _aperture_model(xx, *popt)
            if was_flipped:
                yy = float(ints.max()) - yy
            self._fit_curves[theta_deg].setData(xx, yy)

            # Vertical dashed line at aperture centre
            color = self._theta_colors.get(theta_deg, (128, 128, 128))
            if theta_deg in self._center_vlines:
                self._plot_top.removeItem(self._center_vlines[theta_deg])
            vline = pg.InfiniteLine(
                pos=center, angle=90, movable=False,
                pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
            )
            self._plot_top.addItem(vline)
            self._center_vlines[theta_deg] = vline

            # Update legend label to include centre pulse value
            if theta_deg in self._data_curves:
                new_label = f"θ={theta_deg:.1f}°  c={center:.0f}"
                self._legend_top.removeItem(self._data_curves[theta_deg])
                self._legend_top.addItem(self._data_curves[theta_deg], new_label)
        except Exception:
            pass

        thetas  = np.array(list(self._aperture_centers.keys()))
        centers = np.array(list(self._aperture_centers.values()))
        self._center_scatter.setData(thetas, centers)

    # ── Scan completion ────────────────────────────────────────────────────────

    def _on_scan_completed(self) -> None:
        self._stop_btn.setEnabled(False)
        self._set_banner(tr("Scan complete — analyzing…"), "#2E7D32")
        self._status_label.setText(tr("Scan complete. Running analysis…"))
        self._run_analysis()
        if log_prefs.should_save("dac_scan_rot"):
            self._save_details("completed")
        play_current_sound()
        if self._return_zero_chk.isChecked() and self._controller is not None:
            self._start_post_scan_moves()
            self._update_action_btns()
        else:
            self._start_btn.setEnabled(True)

    def _on_scan_aborted(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._set_banner(tr("Scan aborted"), "#E65100")
        if len(self._aperture_centers) >= 3:
            self._status_label.setText(tr("Scan aborted. Fitting available data…"))
            self._run_analysis()
        else:
            self._status_label.setText(
                tr("Scan aborted ({n} angle(s) completed; need ≥ 3 for rotation fit).",
                   n=len(self._aperture_centers))
            )
            self._update_action_btns()
        if log_prefs.should_save("dac_scan_rot"):
            self._save_details("aborted")

    # ── Post-scan moves ───────────────────────────────────────────────────────

    def _update_action_btns(self) -> None:
        """Enable/disable action buttons based on whether any worker is running."""
        any_moving = (
            (self._worker is not None and self._worker.isRunning()) or
            (self._move_worker is not None and self._move_worker.isRunning()) or
            (self._post_scan_worker is not None and self._post_scan_worker.isRunning())
        )
        has_results  = self._fit_A is not None
        has_zero_ctr = self._get_center_at_zero() is not None

        self._return_centre_btn.setEnabled(has_zero_ctr and not any_moving)
        self._apply_btn.setEnabled(has_results and not any_moving)
        self._run_analysis_btn.setEnabled(
            len(self._aperture_centers) >= 3 and not any_moving
        )

    def _on_return_and_centre(self) -> None:
        if self._controller is None:
            QMessageBox.warning(self, tr("Error"), tr("Stage controller not connected."))
            return

        ch10_center: int | None = None
        raw = self._get_center_at_zero()
        if raw is not None:
            ch10_center = round(raw)

        self._post_scan_worker = _PostScanMoveWorker(
            self._controller, ch10_center=ch10_center, parent=self
        )
        self._post_scan_worker.status_message.connect(self._status_label.setText)
        self._post_scan_worker.status_message.connect(
            lambda msg: self._set_banner(msg, "#1565C0")
        )
        self._post_scan_worker.move_completed.connect(self._on_post_scan_completed)
        self._post_scan_worker.move_failed.connect(self._on_post_scan_failed)
        self._post_scan_worker.start()
        self._set_banner(tr("Moving to θ=0°…"), "#1565C0")
        self._update_action_btns()

    def _start_post_scan_moves(self) -> None:
        ch10_center: int | None = None
        if self._center_ch10_chk.isChecked() and self._center_ch10_chk.isEnabled():
            raw = self._get_center_at_zero()
            if raw is not None:
                ch10_center = round(raw)

        self._post_scan_worker = _PostScanMoveWorker(
            self._controller, ch10_center=ch10_center, parent=self
        )
        self._post_scan_worker.status_message.connect(self._status_label.setText)
        self._post_scan_worker.status_message.connect(
            lambda msg: self._set_banner(msg, "#1565C0")
        )
        self._post_scan_worker.move_completed.connect(self._on_post_scan_completed)
        self._post_scan_worker.move_failed.connect(self._on_post_scan_failed)
        self._set_banner(tr("Moving to θ=0°…"), "#1565C0")
        self._post_scan_worker.start()

    @pyqtSlot()
    def _on_post_scan_completed(self) -> None:
        self._status_label.setText(tr("Ready."))
        self._set_banner(tr("Scan complete"), "#2E7D32")
        self._start_btn.setEnabled(True)
        self._update_action_btns()

    @pyqtSlot(str)
    def _on_post_scan_failed(self, err: str) -> None:
        self._status_label.setText(tr("Post-scan move failed: {error}", error=err))
        self._set_banner(tr("Move Error"), "#C62828")
        QMessageBox.warning(self, tr("Move Error"), err)
        self._start_btn.setEnabled(True)
        self._update_action_btns()

    # ── Analysis ──────────────────────────────────────────────────────────────

    def _run_analysis(self) -> None:
        if len(self._aperture_centers) < 3:
            self._status_label.setText(tr("Need ≥ 3 completed angles for rotation fit."))
            return

        thetas  = np.array(list(self._aperture_centers.keys()))
        centers = np.array(list(self._aperture_centers.values()))

        try:
            A, B, C = _fit_rotation(thetas, centers)
        except Exception as e:
            self._status_label.setText(tr("Rotation fit failed: {error}", error=e))
            return

        self._fit_A, self._fit_B, self._fit_C = A, B, C

        theta_fine = np.linspace(float(thetas.min()), float(thetas.max()), 500)
        self._fit_curve_bot.setData(theta_fine, _rot_model(theta_fine, A, B, C))
        self._center_scatter.setData(thetas, centers)

        A_um = A * UM_PER_PULSE_CH10
        B_um = B * UM_PER_PULSE_CH10
        self._ana_A_label.setText(
            tr("A  [X eccentricity] = {A:+.2f} pulse  ({A_um:+.1f} µm)", A=A, A_um=A_um)
        )
        self._ana_B_label.setText(
            tr("B  [Y eccentricity] = {B:+.2f} pulse  ({B_um:+.1f} µm)", B=B, B_um=B_um)
        )
        self._ana_C_label.setText(tr("C  [global offset]  = {C:.2f} pulse", C=C))

        move_ch3 = A
        move_ch4 = B
        self._move_ch3  = round(move_ch3)
        self._move_ch4  = round(move_ch4)
        self._move_ch10 = self._move_ch4
        self._ana_ch3_label.setText(
            tr("Ch3: {pulse:+d} pulse  ({um:+.1f} µm)",
               pulse=self._move_ch3, um=self._move_ch3 * UM_PER_PULSE_CH3)
        )
        self._ana_ch4_label.setText(
            tr("Ch4: {pulse:+d} pulse  ({um:+.1f} µm)",
               pulse=self._move_ch4, um=self._move_ch4 * UM_PER_PULSE_CH4)
        )
        self._ana_ch10_label.setText(
            tr("Ch10: {pulse:+d} pulse  ({um:+.1f} µm)  (= Ch4)",
               pulse=self._move_ch10, um=self._move_ch10 * UM_PER_PULSE_CH10)
        )
        self._update_action_btns()
        self._analysis_results = {
            "A_pulse": round(A, 4),
            "B_pulse": round(B, 4),
            "C_pulse": round(C, 4),
            "A_um":    round(A_um, 3),
            "B_um":    round(B_um, 3),
            "suggested_ch3_pulse": self._move_ch3,
            "suggested_ch4_pulse": self._move_ch4,
            "suggested_ch3_um":    round(self._move_ch3 * UM_PER_PULSE_CH3, 3),
            "suggested_ch4_um":    round(self._move_ch4 * UM_PER_PULSE_CH4, 3),
            "aperture_centers_pulse": {
                str(theta): round(center, 4)
                for theta, center in self._aperture_centers.items()
            },
        }
        self._status_label.setText(tr("Analysis complete."))

    # ── Details save ──────────────────────────────────────────────────────────

    def _save_details(self, outcome: str) -> None:
        """Save scan arrays, metadata JSON, and plot PNG to the configured log directory."""
        if self._scan_start_time is None or not self._scan_theta_list:
            return

        localdata = log_prefs.get_app_dir("dac_scan_rot")
        ts   = self._scan_start_time.strftime("%Y%m%d_%H%M%S_rot")
        stem = localdata / ts

        # ── Build 2D arrays (n_theta × n_ch10) ───────────────────────────
        n_theta = len(self._scan_theta_list)
        n_ch10  = len(self._ch10_pulses)
        pulse_to_idx = {p: i for i, p in enumerate(self._ch10_pulses)}

        transmitted_map = np.full((n_theta, n_ch10), np.nan)

        for t_idx, theta in enumerate(self._scan_theta_list):
            if theta not in self._scan_data:
                continue
            pulses, trans = self._scan_data[theta]
            for k, p in enumerate(pulses.astype(int)):
                if p in pulse_to_idx:
                    transmitted_map[t_idx, pulse_to_idx[p]] = trans[k]

        aperture_centers_arr = np.array([
            self._aperture_centers.get(theta, np.nan)
            for theta in self._scan_theta_list
        ])

        # ── numpy arrays ─────────────────────────────────────────────────
        np.savez_compressed(
            str(stem) + ".npz",
            theta_deg_list      = np.array(self._scan_theta_list),
            ch10_pulses_abs     = np.array(self._ch10_pulses),
            transmitted_map     = transmitted_map,
            aperture_centers    = aperture_centers_arr,
        )

        # ── metadata JSON ────────────────────────────────────────────────
        meta = {
            "timestamp": self._scan_start_time.isoformat(),
            "outcome":   outcome,
            "scan_params": {
                "theta_deg_list":      self._scan_theta_list,
                "half_range_um":       self._scan_half_range_um,
                "step_um":             self._scan_step_um,
                "um_per_pulse_ch10":   UM_PER_PULSE_CH10,
                "ch10_pulses_abs":     self._ch10_pulses,
                "speed":               self._scan_speed,
                "settle_ms":           self._scan_settle_ms,
                "accumulation":        self._scan_accumulation,
            },
            "analysis_results": self._analysis_results,
            "arrays_file": ts + ".npz",
            "plot_file":   ts + ".png",
        }
        with (stem.parent / (ts + ".json")).open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        # ── plot image ────────────────────────────────────────────────────
        pixmap = self._glw.grab()
        pixmap.save(str(stem) + ".png")

        self._status_label.setText(
            tr("Saved → {path}  (.json / .npz / .png)", path=f"{localdata}/{ts}")
        )

    # ── Correction move ───────────────────────────────────────────────────────

    def _on_apply_correction(self) -> None:
        if self._controller is None:
            QMessageBox.warning(self, tr("Error"), tr("Stage controller not connected."))
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, tr("Busy"), tr("A scan is in progress."))
            return
        if self._move_worker is not None and self._move_worker.isRunning():
            QMessageBox.information(self, tr("Busy"), tr("A move is already in progress."))
            return
        delta_ch10 = self._move_ch4 if self._also_move_ch10_chk.isChecked() else 0
        if self._move_ch3 == 0 and self._move_ch4 == 0:
            QMessageBox.information(self, tr("No correction needed"), tr("Both corrections are 0 pulse."))
            return

        ch10_line = (
            tr("\nMove Ch10 by {pulse:+d} pulse  ({um:+.1f} µm)  (compensation)",
               pulse=delta_ch10, um=delta_ch10 * UM_PER_PULSE_CH10)
            if delta_ch10 != 0 else ""
        )
        reply = QMessageBox.question(
            self, tr("Apply Correction"),
            tr(
                "Move Ch3 by {ch3_pulse:+d} pulse  ({ch3_um:+.1f} µm)\n"
                "Move Ch4 by {ch4_pulse:+d} pulse  ({ch4_um:+.1f} µm)"
                "{ch10_line}\n\nProceed?",
                ch3_pulse=self._move_ch3, ch3_um=self._move_ch3 * UM_PER_PULSE_CH3,
                ch4_pulse=self._move_ch4, ch4_um=self._move_ch4 * UM_PER_PULSE_CH4,
                ch10_line=ch10_line,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._status_label.setText(tr("Applying correction…"))

        self._move_worker = _CorrectionMoveWorker(
            self._controller, self._move_ch3, self._move_ch4,
            delta_ch10=delta_ch10, parent=self,
        )
        self._move_worker.move_completed.connect(self._on_correction_completed)
        self._move_worker.move_failed.connect(self._on_correction_failed)
        self._move_worker.start()
        self._update_action_btns()

    @pyqtSlot()
    def _on_correction_completed(self) -> None:
        self._correction_applied = True
        ch10_note = ""
        if self._move_worker is not None and self._move_worker.delta_ch10 != 0:
            ch10_note = tr(", Ch10 {pulse:+d} pulse", pulse=self._move_worker.delta_ch10)
        self._status_label.setText(
            tr("Correction applied: Ch3 {ch3:+d} pulse, Ch4 {ch4:+d} pulse{ch10_note}.",
               ch3=self._move_ch3, ch4=self._move_ch4, ch10_note=ch10_note)
        )
        self._update_action_btns()

    @pyqtSlot(str)
    def _on_correction_failed(self, err: str) -> None:
        self._status_label.setText(tr("Move failed: {error}", error=err))
        QMessageBox.warning(self, tr("Move Error"), err)
        self._update_action_btns()

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._fit_A is not None and not self._correction_applied:
            reply = QMessageBox.warning(
                self, tr("Unapplied Correction"),
                tr("The scan analysis is complete, but the Ch3/Ch4 correction "
                   "has not been applied yet.\n"
                   "Close the window anyway?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        scan_running      = self._worker is not None and self._worker.isRunning()
        move_running      = self._move_worker is not None and self._move_worker.isRunning()
        post_scan_running = self._post_scan_worker is not None and self._post_scan_worker.isRunning()
        if not (scan_running or move_running or post_scan_running):
            event.accept()
            return

        # Setting the abort flag alone only stops the *next* queued move — the
        # move currently in flight keeps going until the hardware is told to
        # stop. Without normal_stop(), a slow move could outlast wait()'s
        # timeout and the window would close while the stage is still moving.
        if scan_running:
            self._worker.abort()
        if self._controller is not None:
            try:
                self._controller.normal_stop()
            except Exception:
                pass

        scan_done = self._worker.wait(15000) if scan_running else True
        move_done = self._move_worker.wait(15000) if move_running else True
        post_done = self._post_scan_worker.wait(15000) if post_scan_running else True
        if not (scan_done and move_done and post_done):
            QMessageBox.warning(
                self, tr("Stage Still Moving"),
                tr("The stage has not confirmed that it stopped yet.\n"
                   "Please wait a moment and try closing the window again."),
            )
            event.ignore()
            return
        event.accept()
