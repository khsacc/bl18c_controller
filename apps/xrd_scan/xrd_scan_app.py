"""XRD DAC Scan window.

Scans Ch4 (X) and Ch5 (Y) over a user-defined grid, acquires a Rad-icon 2022
XRD image at every grid point, performs in-memory pyFAI azimuthal integration,
and maps user-defined 2θ ROI intensities.

Full 1D spectra are kept in memory (`_spectra[n_ch5, n_ch4, n_bins]`), so ROIs
can be freely redefined after the scan — the map is recomputed instantly without
re-scanning.  Clicking on the 2-D map pushes that point's spectrum into the ROI
dialog for interactive ROI adjustment.
"""
from __future__ import annotations

import json
import pathlib
import numpy as np
from datetime import datetime
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QRadioButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)
import pyqtgraph as pg
from scipy.optimize import curve_fit
from scipy.special import erf

try:
    from .xrd_scan_backend import (
        ROI_COLORS, RoiSpec, XrdScanWorker,
        CH_X, CH_Y, UM_PER_PULSE_CH4, UM_PER_PULSE_CH5,
    )
    from .roi_dialog import RoiDialog
    from settings.poni_state import PoniState
    from settings.settings_window import SettingsWindow
    from settings import log_prefs
    from settings.i18n import tr
    from utils.stage.qt_stop_watcher import StopProgressWatcher
except ImportError:
    import os, sys
    _root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    sys.path.insert(0, _root)
    from apps.xrd_scan.xrd_scan_backend import (
        ROI_COLORS, RoiSpec, XrdScanWorker,
        CH_X, CH_Y, UM_PER_PULSE_CH4, UM_PER_PULSE_CH5,
    )
    from apps.xrd_scan.roi_dialog import RoiDialog
    from settings.poni_state import PoniState
    from settings.settings_window import SettingsWindow
    from settings import log_prefs
    from settings.i18n import tr
    from utils.stage.qt_stop_watcher import StopProgressWatcher


# ---------------------------------------------------------------------------
# Fit models (copied from dac_scan_app so this module is self-contained)
# ---------------------------------------------------------------------------

def _gaussian(x, A, x0, sigma, C):
    return A * np.exp(-0.5 * ((x - x0) / sigma) ** 2) + C


def _aperture_model(x, A, x1, x2, w, bg):
    return A * (erf((x - x1) / w) - erf((x - x2) / w)) + bg


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


def _fit_aperture_1d(xp, profile):
    x = xp.astype(float)
    y = np.where(np.isnan(profile), np.nanmean(profile), profile).astype(float)
    was_flipped = bool(
        np.nanmean(y[: max(1, len(y) // 4)])
        > np.nanmean(y[len(y) // 4 : 3 * len(y) // 4])
    )
    if was_flipped:
        y = float(np.nanmax(y)) - y
    A0  = float(np.nanmax(y) - np.nanmin(y))
    thr = float(np.nanmin(y) + 0.5 * A0)
    idx = np.where(y > thr)[0]
    x1_0 = float(x[idx[0]])  if len(idx) > 0 else float(x[len(x) // 4])
    x2_0 = float(x[idx[-1]]) if len(idx) > 0 else float(x[3 * len(x) // 4])
    w0   = max(abs(x2_0 - x1_0) * 0.1, (x[-1] - x[0]) / max(len(x), 1), 0.1)
    p0   = [A0, x1_0, x2_0, w0, float(np.nanmin(y))]
    popt, _ = curve_fit(_aperture_model, x, y, p0=p0, maxfev=10_000)
    _, x1, x2, _, _ = popt
    return (float(x1) + float(x2)) / 2.0, abs(float(x2) - float(x1)), popt, was_flipped


# ---------------------------------------------------------------------------
# Custom axis items
# ---------------------------------------------------------------------------

class _PulseAxisItem(pg.AxisItem):
    def __init__(self, orientation: str, center_pulse: int = 0):
        super().__init__(orientation)
        self.center_pulse = center_pulse

    def tickStrings(self, values, scale, spacing):
        return [str(self.center_pulse + round(v)) for v in values]


class _MicronAxisItem(pg.AxisItem):
    def __init__(self, orientation: str, um_per_pulse: float = 1.0):
        super().__init__(orientation)
        self.um_per_pulse = um_per_pulse

    def tickStrings(self, values, scale, spacing):
        um_vals = [v * self.um_per_pulse for v in values]
        mag = max((abs(u) for u in um_vals), default=1.0)
        fmt = "{:.0f}" if mag >= 100 else "{:.1f}" if mag >= 10 else "{:.2f}"
        return [fmt.format(u) for u in um_vals]


# ---------------------------------------------------------------------------
# One-shot move worker
# ---------------------------------------------------------------------------

class _MoveWorker(QThread):
    move_completed = pyqtSignal()
    move_failed    = pyqtSignal(str)

    def __init__(self, controller, x_pulse: int, y_pulse: int, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.x_pulse    = x_pulse
        self.y_pulse    = y_pulse

    def run(self) -> None:
        try:
            with self.controller.motion_session(
                owner="DAC Scan (XRD)",
                operation="Go to suggested position",
            ) as motion:
                self.controller.move_ch_absolute(CH_X, self.x_pulse, motion=motion)
                self.controller.move_ch_absolute(CH_Y, self.y_pulse, motion=motion)
                self.controller.wait_until_stop(motion=motion)
            self.move_completed.emit()
        except Exception as e:
            self.move_failed.emit(str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class XrdScanWindow(QMainWindow):
    """DAC Scan (XRD) — 2-D XRD intensity mapping with Gaussian/erf fit.

    Full 1D spectra are retained in _spectra so ROIs can be changed at any
    time (before, during, or after the scan) without re-scanning.
    """

    def __init__(
        self,
        controller=None,
        backend=None,           # RadiconBackend
        poni_state: "PoniState | None" = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("DAC Scan (XRD)"))
        self.resize(1360, 820)
        self._clamp_to_screen_height()

        self._controller  = controller
        self._backend     = backend
        self._poni_state  = poni_state

        self._scan_worker: XrdScanWorker | None = None
        self._move_worker: _MoveWorker   | None = None
        self._roi_dialog:  RoiDialog     | None = None
        self._settings_window: SettingsWindow | None = None

        # pyFAI state — kept in sync with PoniState via _on_poni_changed
        self._ai = None

        # Dark current
        self._dark: np.ndarray | None = None   # float32, same shape as detector image

        # ROI state
        self._roi_list: list[RoiSpec] = []
        self._n_bins:   int           = 1000

        # Scan state
        self._n_ch4: int             = 10
        self._n_ch5: int             = 10
        self._center_x_pulse: int    = 0
        self._center_y_pulse: int    = 0
        self._x_pulses_rel: np.ndarray | None = None
        self._y_pulses_rel: np.ndarray | None = None
        # intensity_maps: (n_roi, n_ch5, n_ch4) — derived from _spectra
        self._intensity_maps: np.ndarray | None = None
        # _spectra: (n_ch5, n_ch4, n_bins) — raw 1D spectra, retained for post-scan ROI
        self._spectra: np.ndarray | None        = None
        self._radial:  np.ndarray | None        = None   # shared radial axis
        self._scan_start_time: datetime | None  = None
        self._fit_results: dict | None          = None
        self._suggested_x_pulse: int | None     = None
        self._suggested_y_pulse: int | None     = None

        self._setup_ui()

        if poni_state is not None:
            poni_state.poni_changed.connect(self._on_poni_changed)
        self._on_poni_changed()   # initialise status panel from current state

    # ── UI construction ──────────────────────────────────────────────────────

    def _clamp_to_screen_height(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        max_h = screen.availableGeometry().height()
        self.setMaximumHeight(max_h)
        if self.height() > max_h:
            self.resize(self.width(), max_h)

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        param_scroll = QScrollArea()
        param_scroll.setWidgetResizable(True)
        param_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        param_scroll.setFixedWidth(300)
        param_scroll.setWidget(self._build_param_panel())
        root.addWidget(param_scroll, 0)
        root.addWidget(self._build_plot_area(), 1)
        self._cmap_combo.currentTextChanged.connect(self._on_cmap_changed)

    def _build_param_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Calibration status (from PoniState) ──────────────────────────────
        calib_grp = QGroupBox(tr("Calibration"))
        calib_lay = QVBoxLayout(calib_grp)
        self._calib_status_label = QLabel(tr("Not calibrated"))
        self._calib_status_label.setWordWrap(True)
        calib_lay.addWidget(self._calib_status_label)
        self._calib_details_label = QLabel("")
        self._calib_details_label.setWordWrap(True)
        self._calib_details_label.setStyleSheet("font-size: 10px; color: #555;")
        calib_lay.addWidget(self._calib_details_label)
        layout.addWidget(calib_grp)

        # ── Dark current ──────────────────────────────────────────────────────
        dark_grp = QGroupBox(tr("Dark current"))
        dark_lay = QVBoxLayout(dark_grp)
        dark_row = QHBoxLayout()
        self._dark_label = QLabel(tr("Not loaded"))
        self._dark_label.setWordWrap(True)
        self._dark_label.setStyleSheet("font-size: 10px; color: #888;")
        dark_btn_col = QVBoxLayout()
        self._dark_browse_btn = QPushButton(tr("Browse…"))
        self._dark_browse_btn.clicked.connect(self._on_browse_dark)
        self._dark_clear_btn = QPushButton(tr("Clear"))
        self._dark_clear_btn.clicked.connect(self._on_clear_dark)
        self._dark_clear_btn.setEnabled(False)
        dark_btn_col.addWidget(self._dark_browse_btn)
        dark_btn_col.addWidget(self._dark_clear_btn)
        dark_row.addWidget(self._dark_label, 1)
        dark_row.addLayout(dark_btn_col, 0)
        dark_lay.addLayout(dark_row)
        layout.addWidget(dark_grp)

        # ── Integration / ROI ─────────────────────────────────────────────
        integ_grp = QGroupBox(tr("Integration / ROI"))
        integ_lay = QVBoxLayout(integ_grp)
        bins_row = QHBoxLayout()
        bins_row.addWidget(QLabel(tr("Bins:")))
        self._n_bins_spin = _no_wheel(QSpinBox())
        self._n_bins_spin.setRange(1, 10_000)
        self._n_bins_spin.setValue(1000)
        self._n_bins_spin.setSingleStep(100)
        bins_row.addWidget(self._n_bins_spin)
        bins_row.addStretch()
        integ_lay.addLayout(bins_row)
        self._roi_btn = QPushButton(tr("Set ROI…"))
        self._roi_btn.clicked.connect(self._on_open_roi_dialog)
        integ_lay.addWidget(self._roi_btn)
        self._roi_summary_lbl = QLabel(tr("No ROI defined"))
        self._roi_summary_lbl.setWordWrap(True)
        self._roi_summary_lbl.setStyleSheet("font-size: 10px; color: #888;")
        integ_lay.addWidget(self._roi_summary_lbl)
        layout.addWidget(integ_grp)

        # ── Exposure time ────────────────────────────────────────────────────
        exp_grp = QGroupBox(tr("Exposure"))
        exp_lay = QHBoxLayout(exp_grp)
        exp_lay.addWidget(QLabel(tr("Exposure:")))
        self._exposure_spin = _no_wheel(QSpinBox())
        self._exposure_spin.setRange(1, 60_000)
        self._exposure_spin.setValue(1000)
        self._exposure_spin.setSuffix(" ms")
        self._exposure_spin.setSingleStep(100)
        exp_lay.addWidget(self._exposure_spin)
        exp_lay.addStretch()
        layout.addWidget(exp_grp)

        # ── Ch4 scan ─────────────────────────────────────────────────────────
        ch4_grp = QGroupBox(tr("Ch{ch} (X) Scan", ch=4))
        ch4_lay = QVBoxLayout(ch4_grp)
        ch4_lay.addWidget(QLabel(tr("Scan size (µm):")))
        self._scan_size_ch4_spin = _no_wheel(QDoubleSpinBox())
        self._scan_size_ch4_spin.setRange(1.0, 10_000.0)
        self._scan_size_ch4_spin.setValue(100.0)
        self._scan_size_ch4_spin.setSuffix(" µm")
        self._scan_size_ch4_spin.setSingleStep(10.0)
        ch4_lay.addWidget(self._scan_size_ch4_spin)
        ch4_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_ch4_spin = _no_wheel(QSpinBox())
        self._grid_n_ch4_spin.setRange(2, 200)
        self._grid_n_ch4_spin.setValue(10)
        ch4_lay.addWidget(self._grid_n_ch4_spin)
        layout.addWidget(ch4_grp)

        # ── Ch5 scan ─────────────────────────────────────────────────────────
        ch5_grp = QGroupBox(tr("Ch{ch} (Y) Scan", ch=5))
        ch5_lay = QVBoxLayout(ch5_grp)
        ch5_lay.addWidget(QLabel(tr("Scan size (µm):")))
        self._scan_size_ch5_spin = _no_wheel(QDoubleSpinBox())
        self._scan_size_ch5_spin.setRange(1.0, 10_000.0)
        self._scan_size_ch5_spin.setValue(100.0)
        self._scan_size_ch5_spin.setSuffix(" µm")
        self._scan_size_ch5_spin.setSingleStep(10.0)
        ch5_lay.addWidget(self._scan_size_ch5_spin)
        ch5_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_ch5_spin = _no_wheel(QSpinBox())
        self._grid_n_ch5_spin.setRange(2, 200)
        self._grid_n_ch5_spin.setValue(10)
        ch5_lay.addWidget(self._grid_n_ch5_spin)
        layout.addWidget(ch5_grp)

        # ── Speed ─────────────────────────────────────────────────────────────
        speed_grp = QGroupBox(tr("Speed"))
        speed_lay = QHBoxLayout(speed_grp)
        self._speed_grp = QButtonGroup(self)
        for label in ("L", "M", "H"):
            rb = QRadioButton(label)
            rb.setProperty("speed_val", label)
            self._speed_grp.addButton(rb)
            speed_lay.addWidget(rb)
            if label == "H":
                rb.setChecked(True)
        layout.addWidget(speed_grp)

        # ── Settle time ───────────────────────────────────────────────────────
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

        # ── Save TIFFs ────────────────────────────────────────────────────────
        self._save_tiff_cb = QCheckBox(tr("Save TIFF images"))
        layout.addWidget(self._save_tiff_cb)

        # ── Scan preview ──────────────────────────────────────────────────────
        self._scan_preview_label = QLabel()
        self._scan_preview_label.setWordWrap(True)
        self._scan_preview_label.setStyleSheet("font-size: 10px; color: #888;")
        layout.addWidget(self._scan_preview_label)
        self._update_scan_preview()
        for spin in (self._scan_size_ch4_spin, self._scan_size_ch5_spin,
                     self._grid_n_ch4_spin, self._grid_n_ch5_spin):
            spin.valueChanged.connect(self._update_scan_preview)

        # ── Control buttons ───────────────────────────────────────────────────
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

        # ── Status ────────────────────────────────────────────────────────────
        self._status_label = QLabel(tr("Ready"))
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # ── Color map ─────────────────────────────────────────────────────────
        cmap_row = QHBoxLayout()
        cmap_row.addWidget(QLabel(tr("Color map:")))
        self._cmap_combo = _no_wheel(QComboBox())
        self._cmap_combo.addItems(["inferno", "viridis", "plasma", "magma"])
        cmap_row.addWidget(self._cmap_combo)
        layout.addLayout(cmap_row)

        layout.addStretch()

        # ── Fitting ───────────────────────────────────────────────────────────
        fit_grp = QGroupBox(tr("Fitting"))
        fit_lay = QVBoxLayout(fit_grp)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel(tr("Model:")))
        self._fit_model_combo = _no_wheel(QComboBox())
        self._fit_model_combo.addItems(["Gaussian", "Aperture (erf)"])
        self._fit_model_combo.currentTextChanged.connect(self._on_fit_model_changed)
        model_row.addWidget(self._fit_model_combo)
        fit_lay.addLayout(model_row)
        self._fit_x_label = QLabel(tr("Ch{ch}:  —", ch=4))
        self._fit_y_label = QLabel(tr("Ch{ch}:  —", ch=5))
        self._fit_x_label.setWordWrap(True)
        self._fit_y_label.setWordWrap(True)
        fit_lay.addWidget(self._fit_x_label)
        fit_lay.addWidget(self._fit_y_label)
        self._goto_btn = QPushButton(tr("Go to suggested position"))
        self._goto_btn.setEnabled(False)
        self._goto_btn.setStyleSheet(
            "QPushButton:enabled { border: 2px solid #27ae60; font-weight: bold; font-size: 15px; }"
        )
        self._goto_btn.clicked.connect(self._on_goto_suggested)
        fit_lay.addWidget(self._goto_btn)
        layout.addWidget(fit_grp)

        return panel

    def _update_scan_preview(self) -> None:
        size_ch4 = self._scan_size_ch4_spin.value()
        n_ch4    = self._grid_n_ch4_spin.value()
        size_ch5 = self._scan_size_ch5_spin.value()
        n_ch5    = self._grid_n_ch5_spin.value()
        half_ch4 = size_ch4 / 2.0 / UM_PER_PULSE_CH4
        half_ch5 = size_ch5 / 2.0 / UM_PER_PULSE_CH5
        step_ch4 = (2.0 * half_ch4 / (n_ch4 - 1)) if n_ch4 > 1 else 0.0
        step_ch5 = (2.0 * half_ch5 / (n_ch5 - 1)) if n_ch5 > 1 else 0.0
        self._scan_preview_label.setText(
            tr(
                "Ch{ch_x}: ±{half_x:.1f} pulses, step {step_x:.2f} p\n"
                "Ch{ch_y}: ±{half_y:.0f} pulses, step {step_y:.1f} p",
                ch_x=4, half_x=half_ch4, step_x=step_ch4,
                ch_y=5, half_y=half_ch5, step_y=step_ch5,
            )
        )

    def _build_plot_area(self) -> QWidget:
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)

        # ROI selector combobox
        roi_row = QHBoxLayout()
        roi_row.addWidget(QLabel(tr("Displayed ROI:")))
        self._roi_display_combo = _no_wheel(QComboBox())
        self._roi_display_combo.setMinimumWidth(160)
        self._roi_display_combo.currentIndexChanged.connect(self._on_roi_display_changed)
        roi_row.addWidget(self._roi_display_combo)
        roi_row.addStretch()
        vbox.addLayout(roi_row)

        self._glw = pg.GraphicsLayoutWidget()
        vbox.addWidget(self._glw, 1)

        self._bottom_axis = _PulseAxisItem("bottom", center_pulse=0)
        self._left_axis   = _PulseAxisItem("left",   center_pulse=0)
        self._top_axis    = _MicronAxisItem("top",   um_per_pulse=UM_PER_PULSE_CH4)
        self._right_axis  = _MicronAxisItem("right", um_per_pulse=UM_PER_PULSE_CH5)

        self._plot_2d = self._glw.addPlot(
            row=0, col=0,
            title=tr("XRD Intensity Map"),
            axisItems={
                "bottom": self._bottom_axis,
                "left":   self._left_axis,
                "top":    self._top_axis,
                "right":  self._right_axis,
            },
        )
        self._plot_2d.showAxis("top")
        self._plot_2d.showAxis("right")
        self._plot_2d.setLabel("bottom", tr("Ch{ch} (X) [pulse]", ch=4))
        self._plot_2d.setLabel("left",   tr("Ch{ch} (Y) [pulse]", ch=5))
        self._plot_2d.setLabel("top",    tr("Ch{ch} (X) [µm from centre]", ch=4))
        self._plot_2d.setLabel("right",  tr("Ch{ch} (Y) [µm from centre]", ch=5))

        self._img_item = pg.ImageItem()
        self._plot_2d.addItem(self._img_item)

        cmap = pg.colormap.get(self._cmap_combo.currentText())
        self._img_item.setColorMap(cmap)

        self._colorbar = pg.ColorBarItem(
            colorMap=cmap,
            label=tr("ROI intensity"),
            interactive=False,
        )
        self._colorbar.setImageItem(self._img_item)

        cross_pen = pg.mkPen("r", width=2, style=Qt.PenStyle.DashLine)
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=cross_pen)
        self._hline = pg.InfiniteLine(angle=0,  movable=False, pen=cross_pen)
        self._plot_2d.addItem(self._vline)
        self._plot_2d.addItem(self._hline)
        self._vline.setVisible(False)
        self._hline.setVisible(False)

        self._plot_y = self._glw.addPlot(row=0, col=2, title=tr("Ch{ch} (Y) Profile", ch=5))
        self._plot_y.setLabel("bottom", tr("Intensity"))
        self._plot_y.setLabel("left",   tr("Ch{ch} offset", ch=5), units="pulses")
        self._plot_y.setYLink(self._plot_2d)
        self._curve_y_fit  = self._plot_y.plot(pen=pg.mkPen("r", width=2))
        self._curve_y_data = self._plot_y.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=pg.mkBrush(0, 0, 0),
            symbolPen=pg.mkPen((80, 80, 80), width=1),
        )

        self._plot_x = self._glw.addPlot(row=1, col=0, title=tr("Ch{ch} (X) Profile", ch=4))
        self._plot_x.setLabel("bottom", tr("Ch{ch} offset", ch=4), units="pulses")
        self._plot_x.setLabel("left",   tr("Intensity"))
        self._plot_x.setXLink(self._plot_2d)
        self._curve_x_fit  = self._plot_x.plot(pen=pg.mkPen("r", width=2))
        self._curve_x_data = self._plot_x.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=pg.mkBrush(0, 0, 0),
            symbolPen=pg.mkPen((80, 80, 80), width=1),
        )

        ci = self._glw.ci
        ci.addItem(self._colorbar, row=0, col=1)
        ci.layout.setColumnStretchFactor(0, 3)
        ci.layout.setColumnStretchFactor(1, 0)
        ci.layout.setColumnStretchFactor(2, 1)
        ci.layout.setRowStretchFactor(0, 3)
        ci.layout.setRowStretchFactor(1, 1)

        for _p in (self._plot_2d, self._plot_y, self._plot_x):
            _p.vb.setMouseEnabled(x=False, y=False)
            _p.setMenuEnabled(False)
            _p.hideButtons()

        _TOP_H = 50
        self._plot_2d.getAxis("top").setHeight(_TOP_H)
        self._plot_y.showAxis("top")
        self._plot_y.getAxis("top").setHeight(_TOP_H)
        self._plot_y.getAxis("top").setStyle(showValues=False, tickLength=0)

        _LEFT_W       = 55
        _RIGHT_AXIS_W = 55
        self._plot_2d.getAxis("left").setWidth(_LEFT_W)
        self._plot_2d.getAxis("right").setWidth(_RIGHT_AXIS_W)
        self._plot_x.getAxis("left").setWidth(_LEFT_W)
        self._plot_x.showAxis("right")
        self._plot_x.getAxis("right").setWidth(_RIGHT_AXIS_W)
        self._plot_x.getAxis("right").setStyle(showValues=False, tickLength=0)

        self._plot_2d.vb.disableAutoRange()
        self._plot_y.vb.disableAutoRange(pg.ViewBox.YAxis)
        self._plot_x.vb.disableAutoRange(pg.ViewBox.XAxis)

        self._plot_2d.vb.setAspectLocked(True, ratio=UM_PER_PULSE_CH4 / UM_PER_PULSE_CH5)

        self._plot_2d.scene().sigMouseClicked.connect(self._on_scene_clicked)

        return container

    # ── Calibration state sync ────────────────────────────────────────────────

    def _on_poni_changed(self) -> None:
        """Sync local _ai and status panel from PoniState."""
        s = self._poni_state
        if s is None or not s.is_calibrated():
            self._ai = None
            self._calib_status_label.setText(tr("✕ Not calibrated"))
            self._calib_status_label.setStyleSheet("color: #a00; font-weight: bold;")
            self._calib_details_label.setText(
                tr("Please calibrate via Tools → Calibrate poni (IPAnalyzer + CeO2).")
            )
            return

        self._ai = s.ai
        self._calib_status_label.setText(tr("● Calibrated"))
        self._calib_status_label.setStyleSheet("color: green; font-weight: bold;")

        lines = []
        if s.prm_path:
            lines.append(tr("IPA:  {name}", name=s.prm_path.name))
        if s.ceo2_path:
            lines.append(tr("CeO2: {name}", name=s.ceo2_path.name))
        if s.chi2_before is not None and s.chi2_after is not None:
            lines.append(tr("chi²: {before:.5f} → {after:.5f}",
                             before=s.chi2_before, after=s.chi2_after))
        if s.n_control_pts is not None:
            lines.append(tr("Control points: {n}", n=s.n_control_pts))
        self._calib_details_label.setText("\n".join(lines))

    # ── Dark current loading ──────────────────────────────────────────────────

    def _on_browse_dark(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Select dark current TIFF"), "",
            "TIFF files (*.tif *.tiff);;All files (*)"
        )
        if not path:
            return
        try:
            import tifffile
            dark = tifffile.imread(path).astype(np.float32)
        except Exception as exc:
            QMessageBox.critical(self, tr("Dark load error"), str(exc))
            return
        self._dark = dark
        self._dark_label.setText(pathlib.Path(path).name)
        self._dark_label.setStyleSheet("font-size: 10px; color: #080;")
        self._dark_clear_btn.setEnabled(True)
        self._status_label.setText(
            tr("Dark loaded: {name}  ({w}×{h} px)",
               name=pathlib.Path(path).name, w=dark.shape[1], h=dark.shape[0])
        )

    def _on_clear_dark(self) -> None:
        self._dark = None
        self._dark_label.setText(tr("Not loaded"))
        self._dark_label.setStyleSheet("font-size: 10px; color: #888;")
        self._dark_clear_btn.setEnabled(False)
        self._status_label.setText(tr("Dark cleared."))

    # ── ROI dialog ────────────────────────────────────────────────────────────

    def _on_open_roi_dialog(self) -> None:
        if self._backend is None:
            QMessageBox.warning(self, tr("No camera"),
                                tr("Rad-icon 2022 is not connected."))
            return
        if self._roi_dialog is None:
            self._roi_dialog = RoiDialog(
                backend=self._backend,
                params_getter=self._get_roi_dialog_params,
                open_settings_callback=self._open_detector_calibration,
                parent=None,
            )
            self._roi_dialog.roi_list_changed.connect(self._on_roi_list_changed)

        # Push mean spectrum from last scan if available
        self._push_mean_spectrum_to_dialog()

        self._roi_dialog.show()
        self._roi_dialog.raise_()
        self._roi_dialog.activateWindow()

    def _open_detector_calibration(self) -> None:
        """Open Settings on the Detector Calibration page (default page 0)."""
        if self._settings_window is None:
            self._settings_window = SettingsWindow(
                poni_state=self._poni_state,
                parent=self,
            )
            self._settings_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._settings_window.destroyed.connect(
                lambda: setattr(self, "_settings_window", None)
            )
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _get_roi_dialog_params(self) -> tuple:
        return (
            self._n_bins_spin.value(),
            self._exposure_spin.value(),
            self._ai,
            self._dark,
        )

    def _push_mean_spectrum_to_dialog(self) -> None:
        """Send the mean spectrum from _spectra to the ROI dialog if available."""
        if (
            self._roi_dialog is None
            or self._spectra is None
            or self._radial is None
        ):
            return
        flat = self._spectra.reshape(-1, self._spectra.shape[-1])
        valid = flat[~np.any(np.isnan(flat), axis=1)]
        if valid.shape[0] == 0:
            return
        mean_spectrum = valid.mean(axis=0)
        self._roi_dialog.set_spectrum(self._radial, mean_spectrum)

    @pyqtSlot(list)
    def _on_roi_list_changed(self, roi_list: list[RoiSpec]) -> None:
        self._roi_list = roi_list
        self._update_roi_summary()
        self._update_roi_display_combo()
        # Recompute maps from stored spectra (works during scan too)
        self._recompute_maps_from_spectra()

    def _update_roi_summary(self) -> None:
        if not self._roi_list:
            self._roi_summary_lbl.setText(tr("No ROI defined"))
            return
        parts = [
            tr("#{n} {label} [{tmin:.1f}–{tmax:.1f}°, {mode}]",
               n=i + 1, label=r.label, tmin=r.tth_min, tmax=r.tth_max, mode=r.mode)
            for i, r in enumerate(self._roi_list)
        ]
        self._roi_summary_lbl.setText("\n".join(parts))

    def _update_roi_display_combo(self) -> None:
        prev_idx = self._roi_display_combo.currentIndex()
        self._roi_display_combo.blockSignals(True)
        self._roi_display_combo.clear()
        for i, roi in enumerate(self._roi_list):
            self._roi_display_combo.addItem(tr("ROI#{n}: {label}", n=i + 1, label=roi.label))
        self._roi_display_combo.blockSignals(False)
        if prev_idx < self._roi_display_combo.count():
            self._roi_display_combo.setCurrentIndex(prev_idx)
        elif self._roi_display_combo.count() > 0:
            self._roi_display_combo.setCurrentIndex(0)
        self._refresh_map_display()

    # ── Spectrum storage and ROI recomputation ────────────────────────────────

    def _recompute_maps_from_spectra(self) -> None:
        """Rebuild intensity_maps from stored spectra using the current ROI list."""
        if self._spectra is None or self._radial is None or not self._roi_list:
            return

        n_ch5, n_ch4, _ = self._spectra.shape
        n_roi = len(self._roi_list)
        new_maps = np.full((n_roi, n_ch5, n_ch4), np.nan)

        for row in range(n_ch5):
            for col in range(n_ch4):
                spectrum = self._spectra[row, col, :]
                if np.any(np.isnan(spectrum)):
                    continue
                for i, roi in enumerate(self._roi_list):
                    new_maps[i, row, col] = roi.compute(self._radial, spectrum)

        self._intensity_maps = new_maps
        self._refresh_map_display()

        # Auto-refit if not scanning and data exists
        if (
            not (self._scan_worker is not None and self._scan_worker.isRunning())
            and np.any(~np.isnan(new_maps))
        ):
            self._run_fit()

    # ── ROI display switch ────────────────────────────────────────────────────

    def _on_roi_display_changed(self, _idx: int) -> None:
        self._refresh_map_display()
        data = self._current_roi_map()
        if (
            data is not None
            and np.any(~np.isnan(data))
            and not (self._scan_worker is not None and self._scan_worker.isRunning())
        ):
            self._run_fit()

    def _current_roi_map(self) -> np.ndarray | None:
        idx = self._roi_display_combo.currentIndex()
        if (
            self._intensity_maps is None
            or idx < 0
            or idx >= self._intensity_maps.shape[0]
        ):
            return None
        return self._intensity_maps[idx]

    def _refresh_map_display(self) -> None:
        data = self._current_roi_map()
        if data is None:
            return
        display = np.nan_to_num(data, nan=0.0)
        valid = data[~np.isnan(data)]
        if valid.size == 0:
            return
        vmin, vmax = float(valid.min()), float(valid.max())
        if vmin == vmax:
            vmax = vmin + 1.0
        self._img_item.setImage(display.T, levels=(vmin, vmax))
        self._colorbar.setLevels(low=vmin, high=vmax)
        if self._x_pulses_rel is not None and self._y_pulses_rel is not None:
            xp, yp = self._x_pulses_rel, self._y_pulses_rel
            n_ch4  = len(xp)
            n_ch5  = len(yp)
            px_x = (xp[-1] - xp[0]) / max(n_ch4 - 1, 1)
            px_y = (yp[-1] - yp[0]) / max(n_ch5 - 1, 1)
            self._img_item.setRect(
                float(xp[0])  - px_x / 2.0,
                float(yp[0])  - px_y / 2.0,
                float(xp[-1] - xp[0]) + px_x,
                float(yp[-1] - yp[0]) + px_y,
            )
        roi_idx = self._roi_display_combo.currentIndex()
        if 0 <= roi_idx < len(self._roi_list):
            self._plot_2d.setTitle(
                tr("XRD Intensity Map — ROI#{n}: {label}",
                   n=roi_idx + 1, label=self._roi_list[roi_idx].label)
            )

    # ── Color map ─────────────────────────────────────────────────────────────

    def _on_cmap_changed(self, name: str) -> None:
        cmap = pg.colormap.get(name)
        self._img_item.setColorMap(cmap)
        self._colorbar.setColorMap(cmap)

    def _on_fit_model_changed(self) -> None:
        data = self._current_roi_map()
        if (
            data is not None
            and np.any(~np.isnan(data))
            and not (self._scan_worker is not None and self._scan_worker.isRunning())
        ):
            self._run_fit()

    # ── Scan control ──────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._controller is None:
            QMessageBox.warning(self, tr("Error"), tr("Stage controller not connected."))
            return
        if self._backend is None:
            QMessageBox.warning(self, tr("Error"), tr("Rad-icon 2022 not connected."))
            return
        if self._ai is None:
            QMessageBox.warning(
                self, tr("Not calibrated"),
                tr("No calibration available.\n"
                   "Please calibrate via Tools → Calibrate poni (IPAnalyzer + CeO2)."),
            )
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return

        try:
            self._center_x_pulse = int(self._controller.get_ch_pos(CH_X))
            self._center_y_pulse = int(self._controller.get_ch_pos(CH_Y))
        except Exception as e:
            QMessageBox.warning(self, tr("Error"), tr("Cannot read current position:\n{error}", error=e))
            return

        self._bottom_axis.center_pulse = self._center_x_pulse
        self._left_axis.center_pulse   = self._center_y_pulse

        self._n_ch4  = self._grid_n_ch4_spin.value()
        self._n_ch5  = self._grid_n_ch5_spin.value()
        self._n_bins = self._n_bins_spin.value()
        speed = next(
            btn.property("speed_val")
            for btn in self._speed_grp.buttons()
            if btn.isChecked()
        )

        half_ch4 = self._scan_size_ch4_spin.value() / 2.0 / UM_PER_PULSE_CH4
        half_ch5 = self._scan_size_ch5_spin.value() / 2.0 / UM_PER_PULSE_CH5
        self._x_pulses_rel = np.round(
            np.linspace(-half_ch4, half_ch4, self._n_ch4)
        ).astype(int)
        self._y_pulses_rel = np.round(
            np.linspace(-half_ch5, half_ch5, self._n_ch5)
        ).astype(int)

        x_pulses_abs = (self._center_x_pulse + self._x_pulses_rel).tolist()
        y_pulses_abs = (self._center_y_pulse + self._y_pulses_rel).tolist()

        # Allocate storage — ROIs may be undefined yet; maps are built on receipt
        n_roi = max(len(self._roi_list), 1)
        self._intensity_maps = np.full((n_roi, self._n_ch5, self._n_ch4), np.nan)
        self._spectra        = np.full(
            (self._n_ch5, self._n_ch4, self._n_bins), np.nan, dtype=np.float32
        )
        self._radial         = None
        self._scan_start_time = datetime.now()
        self._fit_results     = None

        xp, yp = self._x_pulses_rel, self._y_pulses_rel
        px_x = (xp[-1] - xp[0]) / max(self._n_ch4 - 1, 1)
        px_y = (yp[-1] - yp[0]) / max(self._n_ch5 - 1, 1)
        x_rng = (float(xp[0]) - px_x / 2, float(xp[-1]) + px_x / 2)
        y_rng = (float(yp[0]) - px_y / 2, float(yp[-1]) + px_y / 2)
        self._plot_2d.setRange(xRange=x_rng, yRange=y_rng, padding=0)
        self._plot_y.vb.enableAutoRange(pg.ViewBox.XAxis)
        self._plot_x.vb.enableAutoRange(pg.ViewBox.YAxis)

        self._img_item.clear()
        self._curve_x_data.setData([], [])
        self._curve_x_fit.setData([], [])
        self._curve_y_data.setData([], [])
        self._curve_y_fit.setData([], [])
        self._vline.setVisible(False)
        self._hline.setVisible(False)
        self._suggested_x_pulse = None
        self._suggested_y_pulse = None
        self._goto_btn.setEnabled(False)
        self._fit_x_label.setText(tr("Ch{ch}:  —", ch=4))
        self._fit_y_label.setText(tr("Ch{ch}:  —", ch=5))

        tiff_dir = None
        if self._save_tiff_cb.isChecked():
            ts = self._scan_start_time.strftime("%Y%m%d_%H%M%S")
            tiff_dir = pathlib.Path(__file__).parent / "__localdata" / ts
            tiff_dir.mkdir(parents=True, exist_ok=True)

        self._scan_worker = XrdScanWorker(
            controller  = self._controller,
            backend     = self._backend,
            ai          = self._ai,
            dark        = self._dark,
            x_pulses    = x_pulses_abs,
            y_pulses    = y_pulses_abs,
            center_x    = self._center_x_pulse,
            center_y    = self._center_y_pulse,
            n_bins      = self._n_bins,
            exposure_ms = self._exposure_spin.value(),
            speed       = speed,
            settle_ms   = self._settle_spin.value(),
            save_tiff   = self._save_tiff_cb.isChecked(),
            tiff_dir    = tiff_dir,
        )
        self._scan_worker.point_measured.connect(self._on_point_measured)
        self._scan_worker.scan_completed.connect(self._on_scan_completed)
        self._scan_worker.scan_aborted.connect(self._on_scan_aborted)
        self._scan_worker.scan_could_not_start.connect(self._on_scan_could_not_start)
        self._scan_worker.status_message.connect(self._status_label.setText)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_label.setText(tr("Starting scan…"))
        self._scan_worker.start()

    def _on_stop(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.abort()
            if self._controller is not None:
                self._request_stop(emergency=False)
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Aborting…"))

    def _on_emergency_stop(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.abort()
        if self._controller is not None:
            self._request_stop(emergency=True)
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    def _request_stop(self, *, emergency: bool) -> None:
        if emergency:
            future = self._controller.request_emergency_stop(source="DAC Scan (XRD)")
        else:
            future = self._controller.request_normal_stop(source="DAC Scan (XRD)")
        self._stop_watcher = StopProgressWatcher(self._controller, future, self)
        self._stop_watcher.progress_changed.connect(self._on_stop_progress)

    def _on_stop_progress(self, state: str) -> None:
        text = {
            "queued": tr("Stop requested…"),
            "sent_confirming": tr("Stop command sent. Confirming all motors stopped…"),
            "confirmed": tr("All motors stopped."),
            "failed": tr("Stop could not be confirmed — check the controller."),
        }.get(state)
        if text:
            self._status_label.setText(text)

    # ── Data reception ────────────────────────────────────────────────────────

    @pyqtSlot(int, int, object, object)
    def _on_point_measured(
        self, row: int, col: int,
        radial: np.ndarray, intensity: np.ndarray,
    ) -> None:
        if self._spectra is None:
            return

        # Store raw spectrum
        self._radial = radial
        n = min(len(intensity), self._spectra.shape[2])
        self._spectra[row, col, :n] = intensity[:n]

        # Compute ROI values with the current ROI list (may differ from scan-start)
        if self._roi_list and self._intensity_maps is not None:
            n_roi = len(self._roi_list)
            if self._intensity_maps.shape[0] != n_roi:
                # ROI count changed mid-scan — reallocate maps
                new_maps = np.full(
                    (n_roi, self._n_ch5, self._n_ch4), np.nan
                )
                k = min(self._intensity_maps.shape[0], n_roi)
                new_maps[:k] = self._intensity_maps[:k]
                self._intensity_maps = new_maps
                self._update_roi_display_combo()
            for i, roi_spec in enumerate(self._roi_list):
                self._intensity_maps[i, row, col] = roi_spec.compute(radial, intensity)

        self._refresh_map_display()

    # ── Scan completion ───────────────────────────────────────────────────────

    def _on_scan_completed(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Scan complete. Running fit…"))
        self._run_fit()
        self._push_mean_spectrum_to_dialog()
        if log_prefs.should_save("xrd_scan"):
            self._save_details("completed")

    def _on_scan_could_not_start(self, message: str) -> None:
        # The stage lease could not be acquired at all — no move was ever
        # sent, so this must be a visible failure, not a status-line flash
        # that looks indistinguishable from an instant "completed".
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Scan could not start."))
        QMessageBox.warning(self, tr("Stage Busy"), message)

    def _on_scan_aborted(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        data = self._current_roi_map()
        if data is not None and np.any(~np.isnan(data)):
            self._status_label.setText(tr("Scan aborted. Fitting available data…"))
            self._run_fit()
        else:
            self._status_label.setText(tr("Scan aborted."))
        self._push_mean_spectrum_to_dialog()
        if log_prefs.should_save("xrd_scan"):
            self._save_details("aborted")

    # ── Fitting ───────────────────────────────────────────────────────────────

    def _run_fit(self) -> None:
        data = self._current_roi_map()
        if data is None or np.all(np.isnan(data)):
            return

        model     = self._fit_model_combo.currentText()
        xp        = self._x_pulses_rel.astype(float)
        yp        = self._y_pulses_rel.astype(float)
        x_profile = np.nanmean(data, axis=0)
        y_profile = np.nanmean(data, axis=1)

        self._curve_x_data.setData(xp, x_profile)
        self._curve_y_data.setData(y_profile, yp)

        xp_fine = np.linspace(xp[0], xp[-1], 300)
        yp_fine = np.linspace(yp[0], yp[-1], 300)

        x_center_rel = 0.0
        y_center_rel = 0.0
        fit_ch4: dict = {"fit_ok": False}
        fit_ch5: dict = {"fit_ok": False}

        if model == "Gaussian":
            try:
                p0 = [
                    float(np.nanmax(x_profile) - np.nanmin(x_profile)),
                    float(xp[int(np.nanargmax(x_profile))]),
                    (xp[-1] - xp[0]) / 4.0,
                    float(np.nanmin(x_profile)),
                ]
                popt_x, _ = curve_fit(_gaussian, xp, x_profile, p0=p0, maxfev=10_000)
                x_center_rel = float(popt_x[1])
                sigma_x      = abs(float(popt_x[2]))
                self._curve_x_fit.setData(xp_fine, _gaussian(xp_fine, *popt_x))
                abs_x = self._center_x_pulse + round(x_center_rel)
                self._fit_x_label.setText(
                    tr("Ch{ch}:  abs={abs_pulse} pulses\n"
                       "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
                       ch=4, abs_pulse=abs_x, rel=x_center_rel, width_kind="σ", width=sigma_x)
                )
                fit_ch4 = {"fit_ok": True, "model": "gaussian",
                           "center_abs_pulse": abs_x,
                           "center_rel_pulse": round(x_center_rel, 3),
                           "sigma_pulse":      round(sigma_x, 3)}
            except Exception:
                self._fit_x_label.setText(tr("Ch{ch}:  fit failed", ch=4))
            try:
                p0 = [
                    float(np.nanmax(y_profile) - np.nanmin(y_profile)),
                    float(yp[int(np.nanargmax(y_profile))]),
                    (yp[-1] - yp[0]) / 4.0,
                    float(np.nanmin(y_profile)),
                ]
                popt_y, _ = curve_fit(_gaussian, yp, y_profile, p0=p0, maxfev=10_000)
                y_center_rel = float(popt_y[1])
                sigma_y      = abs(float(popt_y[2]))
                self._curve_y_fit.setData(_gaussian(yp_fine, *popt_y), yp_fine)
                abs_y = self._center_y_pulse + round(y_center_rel)
                self._fit_y_label.setText(
                    tr("Ch{ch}:  abs={abs_pulse} pulses\n"
                       "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
                       ch=5, abs_pulse=abs_y, rel=y_center_rel, width_kind="σ", width=sigma_y)
                )
                fit_ch5 = {"fit_ok": True, "model": "gaussian",
                           "center_abs_pulse": abs_y,
                           "center_rel_pulse": round(y_center_rel, 3),
                           "sigma_pulse":      round(sigma_y, 3)}
            except Exception:
                self._fit_y_label.setText(tr("Ch{ch}:  fit failed", ch=5))

        else:  # Aperture (erf)
            try:
                cx, wx, popt_x, flipped_x = _fit_aperture_1d(xp, x_profile)
                x_center_rel = cx
                yy = _aperture_model(xp_fine, *popt_x)
                if flipped_x:
                    yy = float(np.nanmax(x_profile)) - yy
                self._curve_x_fit.setData(xp_fine, yy)
                abs_x = self._center_x_pulse + round(cx)
                self._fit_x_label.setText(
                    tr("Ch{ch}:  abs={abs_pulse} pulses\n"
                       "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
                       ch=4, abs_pulse=abs_x, rel=cx, width_kind="width", width=wx)
                )
                fit_ch4 = {"fit_ok": True, "model": "aperture",
                           "center_abs_pulse": abs_x,
                           "center_rel_pulse": round(cx, 3),
                           "width_pulse":      round(wx, 3)}
            except Exception:
                self._fit_x_label.setText(tr("Ch{ch}:  fit failed", ch=4))
            try:
                cy, wy, popt_y, flipped_y = _fit_aperture_1d(yp, y_profile)
                y_center_rel = cy
                yy = _aperture_model(yp_fine, *popt_y)
                if flipped_y:
                    yy = float(np.nanmax(y_profile)) - yy
                self._curve_y_fit.setData(yy, yp_fine)
                abs_y = self._center_y_pulse + round(cy)
                self._fit_y_label.setText(
                    tr("Ch{ch}:  abs={abs_pulse} pulses\n"
                       "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
                       ch=5, abs_pulse=abs_y, rel=cy, width_kind="width", width=wy)
                )
                fit_ch5 = {"fit_ok": True, "model": "aperture",
                           "center_abs_pulse": abs_y,
                           "center_rel_pulse": round(cy, 3),
                           "width_pulse":      round(wy, 3)}
            except Exception:
                self._fit_y_label.setText(tr("Ch{ch}:  fit failed", ch=5))

        self._fit_results = {"ch4": fit_ch4, "ch5": fit_ch5}
        self._suggested_x_pulse = self._center_x_pulse + round(x_center_rel)
        self._suggested_y_pulse = self._center_y_pulse + round(y_center_rel)

        self._vline.setPos(x_center_rel)
        self._hline.setPos(y_center_rel)
        self._vline.setVisible(True)
        self._hline.setVisible(True)
        self._goto_btn.setEnabled(True)
        self._status_label.setText(tr("Fit complete."))

    # ── Details save ──────────────────────────────────────────────────────────

    def _save_details(self, outcome: str) -> None:
        if self._scan_start_time is None or self._intensity_maps is None:
            return
        localdata = log_prefs.get_app_dir("xrd_scan")
        ts   = self._scan_start_time.strftime("%Y%m%d_%H%M%S")
        stem = localdata / ts

        save_kw: dict = dict(
            intensity_maps = self._intensity_maps,
            x_pulses_rel   = self._x_pulses_rel,
            y_pulses_rel   = self._y_pulses_rel,
            x_pulses_abs   = self._center_x_pulse + self._x_pulses_rel,
            y_pulses_abs   = self._center_y_pulse + self._y_pulses_rel,
        )
        if self._spectra is not None:
            save_kw["spectra"] = self._spectra
        if self._radial is not None:
            save_kw["radial"]  = self._radial
        np.savez_compressed(str(stem) + ".npz", **save_kw)

        roi_dicts = [
            {"label": r.label, "tth_min": r.tth_min,
             "tth_max": r.tth_max, "mode": r.mode}
            for r in self._roi_list
        ]
        s = self._poni_state
        poni_source = {}
        if s is not None:
            if s.prm_path:
                poni_source["prm_file"] = str(s.prm_path)
            if s.ceo2_path:
                poni_source["ceo2_file"] = str(s.ceo2_path)
            if s.poni_path:
                poni_source["poni_file"] = str(s.poni_path)
            if s.chi2_before is not None:
                poni_source["chi2_before"] = s.chi2_before
                poni_source["chi2_after"]  = s.chi2_after
        meta = {
            "timestamp":   self._scan_start_time.isoformat(),
            "outcome":     outcome,
            "poni_source": poni_source,
            "n_bins":      self._n_bins,
            "roi_list":    roi_dicts,
            "scan_params": {
                "center_x_pulse":   self._center_x_pulse,
                "center_y_pulse":   self._center_y_pulse,
                "n_ch4":            self._n_ch4,
                "n_ch5":            self._n_ch5,
                "um_per_pulse_ch4": UM_PER_PULSE_CH4,
                "um_per_pulse_ch5": UM_PER_PULSE_CH5,
            },
            "fit_results":  self._fit_results,
            "arrays_file":  ts + ".npz",
            "plot_file":    ts + ".png",
        }
        with (stem.parent / (ts + ".json")).open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        pixmap = self._glw.grab()
        pixmap.save(str(stem) + ".png")
        self._status_label.setText(
            tr("Saved → {path}  (.json / .npz / .png)", path=f"{localdata}/{ts}")
        )

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_goto_suggested(self) -> None:
        if self._suggested_x_pulse is None or self._suggested_y_pulse is None:
            return
        self._move_to(
            self._suggested_x_pulse, self._suggested_y_pulse,
            tr("Moving to suggested position…"),
        )

    def _move_to(self, x_pulse: int, y_pulse: int,
                 status_msg: str | None = None) -> None:
        if status_msg is None:
            status_msg = tr("Moving…")
        if self._controller is None:
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            QMessageBox.information(self, tr("Busy"), tr("A scan is in progress."))
            return
        if self._move_worker is not None and self._move_worker.isRunning():
            QMessageBox.information(self, tr("Busy"), tr("A move is already in progress."))
            return
        self._goto_btn.setEnabled(False)
        self._status_label.setText(status_msg)
        self._move_worker = _MoveWorker(
            self._controller, x_pulse, y_pulse, parent=self
        )
        self._move_worker.move_completed.connect(self._on_move_completed)
        self._move_worker.move_failed.connect(self._on_move_failed)
        self._move_worker.start()

    @pyqtSlot()
    def _on_move_completed(self) -> None:
        self._status_label.setText(tr("Move complete."))
        if self._suggested_x_pulse is not None:
            self._goto_btn.setEnabled(True)

    @pyqtSlot(str)
    def _on_move_failed(self, err: str) -> None:
        self._status_label.setText(tr("Move failed: {error}", error=err))
        QMessageBox.warning(self, tr("Move Error"), err)
        if self._suggested_x_pulse is not None:
            self._goto_btn.setEnabled(True)

    # ── Map mouse events ──────────────────────────────────────────────────────

    def _on_scene_clicked(self, event) -> None:
        pos = event.scenePos()
        if not self._plot_2d.vb.sceneBoundingRect().contains(pos):
            return

        view_pos = self._plot_2d.vb.mapSceneToView(pos)
        x_rel    = float(view_pos.x())
        y_rel    = float(view_pos.y())

        if event.button() == Qt.MouseButton.LeftButton:
            self._on_map_left_click(x_rel, y_rel, event)
        elif event.button() == Qt.MouseButton.RightButton:
            self._on_map_right_click(x_rel, y_rel, event)

    def _on_map_left_click(self, x_rel: float, y_rel: float, event) -> None:
        """Show the spectrum of the clicked grid point in the ROI dialog."""
        if (
            self._spectra is None
            or self._radial is None
            or self._x_pulses_rel is None
            or self._y_pulses_rel is None
            or self._roi_dialog is None
            or not self._roi_dialog.isVisible()
        ):
            return
        event.accept()
        col = int(np.argmin(np.abs(self._x_pulses_rel - x_rel)))
        row = int(np.argmin(np.abs(self._y_pulses_rel - y_rel)))
        spectrum = self._spectra[row, col, :]
        if np.any(np.isnan(spectrum)):
            return
        self._roi_dialog.set_spectrum(self._radial, spectrum)

    def _on_map_right_click(self, x_rel: float, y_rel: float, event) -> None:
        """Context menu: go to clicked position."""
        if self._intensity_maps is None:
            return
        event.accept()
        x_pulse = self._center_x_pulse + round(x_rel)
        y_pulse = self._center_y_pulse + round(y_rel)
        menu   = QMenu()
        action = menu.addAction(
            tr("Go to this position  (Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses)",
               ch_x=4, x_pulse=x_pulse, ch_y=5, y_pulse=y_pulse)
        )
        result = menu.exec(event.screenPos().toPoint())
        if result is action:
            self._move_to(
                x_pulse, y_pulse,
                tr("Moving to Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses…",
                   ch_x=4, x_pulse=x_pulse, ch_y=5, y_pulse=y_pulse),
            )

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        scan_running = self._scan_worker is not None and self._scan_worker.isRunning()
        move_running = self._move_worker is not None and self._move_worker.isRunning()
        if not scan_running and not move_running:
            if self._roi_dialog is not None:
                self._roi_dialog.close()
            event.accept()
            return

        # Setting the abort flag alone only stops the *next* queued move — the
        # move currently in flight keeps going until the hardware is told to
        # stop. Without normal_stop(), a slow move could outlast wait()'s
        # timeout and the window would close while the stage is still moving.
        if scan_running:
            self._scan_worker.abort()
        if self._controller is not None:
            try:
                self._controller.normal_stop()
            except Exception:
                pass

        scan_done = self._scan_worker.wait(15000) if scan_running else True
        move_done = self._move_worker.wait(15000) if move_running else True
        if not (scan_done and move_done):
            QMessageBox.warning(
                self, tr("Stage Still Moving"),
                tr("The stage has not confirmed that it stopped yet.\n"
                   "Please wait a moment and try closing the window again."),
            )
            event.ignore()
            return

        if self._roi_dialog is not None:
            self._roi_dialog.close()
        event.accept()
