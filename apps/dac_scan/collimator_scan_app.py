"""Collimator Scan window.

Scans Ch1 (X) and Ch2 (Y) over a user-defined grid centred on the current
stage position, reads transmitted X-ray intensity from GPIB, and displays
the result as a live 2-D colour map with Gaussian-fit marginal profiles.

Layout
------
Left  : scan parameter panel + status + Gaussian-fit result
Right : pyqtgraph GraphicsLayoutWidget
          [2D colour map]  |  [Ch2 (Y) profile]
          [Ch1 (X) profile]

Right-click on the 2-D map shows a "Go to this position" context menu.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QMessageBox, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout, QWidget,
)
import pyqtgraph as pg
from scipy.optimize import curve_fit

try:
    from .collimator_scan_backend import (
        CH_X, CH_Y,
        CollimatorScanWorker, GpibReader, GpibReaderSim,
        UM_PER_PULSE_CH1, UM_PER_PULSE_CH2,
    )
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr
except ImportError:
    import os, sys
    _root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    sys.path.insert(0, _root)
    from apps.dac_scan.collimator_scan_backend import (
        CH_X, CH_Y,
        CollimatorScanWorker, GpibReader, GpibReaderSim,
        UM_PER_PULSE_CH1, UM_PER_PULSE_CH2,
    )
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr


# ---------------------------------------------------------------------------
# Gaussian model
# ---------------------------------------------------------------------------

def _gaussian(x, A, x0, sigma, C):
    return A * np.exp(-0.5 * ((x - x0) / sigma) ** 2) + C


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


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
            self.controller.move_ch_absolute(CH_X, self.x_pulse)
            self.controller.move_ch_absolute(CH_Y, self.y_pulse)
            self.controller.wait_until_stop()
            self.move_completed.emit()
        except Exception as e:
            self.move_failed.emit(str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class CollimatorScanWindow(QMainWindow):
    """Collimator Scan — 2-D transmission mapping on Ch1/Ch2 with Gaussian fit."""

    def __init__(
        self,
        controller=None,
        gpib_reader: GpibReader | None = None,
        debug: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("Collimator Scan"))
        self.resize(1300, 800)

        self._controller   = controller
        self._gpib_reader  = gpib_reader
        self._debug        = debug
        self._scan_worker: CollimatorScanWorker | None = None
        self._move_worker: _MoveWorker          | None = None

        self._n_ch1: int                      = 10
        self._n_ch2: int                      = 10
        self._scan_size_um_ch1: float         = 1000.0
        self._scan_size_um_ch2: float         = 1000.0
        self._center_x_pulse: int             = 0
        self._center_y_pulse: int             = 0
        self._x_pulses_rel: np.ndarray | None = None
        self._y_pulses_rel: np.ndarray | None = None
        self._transmitted_map: np.ndarray | None = None
        self._suggested_x_pulse: int | None   = None
        self._suggested_y_pulse: int | None   = None

        self._setup_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)
        root.addWidget(self._build_param_panel(), 0)
        root.addWidget(self._build_plot_area(),   1)
        self._cmap_combo.currentTextChanged.connect(self._on_cmap_changed)

    def _build_param_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Ch1 scan parameters ──────────────────────────────────────────
        ch1_grp = QGroupBox(tr("Ch{ch} (X) Scan", ch=1))
        ch1_lay = QVBoxLayout(ch1_grp)
        ch1_lay.addWidget(QLabel(tr("Scan size (µm):")))
        self._scan_size_ch1_spin = _no_wheel(QDoubleSpinBox())
        self._scan_size_ch1_spin.setRange(1.0, 10_000.0)
        self._scan_size_ch1_spin.setValue(1000.0)
        self._scan_size_ch1_spin.setSuffix(" µm")
        self._scan_size_ch1_spin.setSingleStep(10.0)
        ch1_lay.addWidget(self._scan_size_ch1_spin)
        ch1_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_ch1_spin = _no_wheel(QSpinBox())
        self._grid_n_ch1_spin.setRange(2, 200)
        self._grid_n_ch1_spin.setValue(10)
        ch1_lay.addWidget(self._grid_n_ch1_spin)
        layout.addWidget(ch1_grp)

        # ── Ch2 scan parameters ──────────────────────────────────────────
        ch2_grp = QGroupBox(tr("Ch{ch} (Y) Scan", ch=2))
        ch2_lay = QVBoxLayout(ch2_grp)
        ch2_lay.addWidget(QLabel(tr("Scan size (µm):")))
        self._scan_size_ch2_spin = _no_wheel(QDoubleSpinBox())
        self._scan_size_ch2_spin.setRange(1.0, 10_000.0)
        self._scan_size_ch2_spin.setValue(1000.0)
        self._scan_size_ch2_spin.setSuffix(" µm")
        self._scan_size_ch2_spin.setSingleStep(10.0)
        ch2_lay.addWidget(self._scan_size_ch2_spin)
        ch2_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_ch2_spin = _no_wheel(QSpinBox())
        self._grid_n_ch2_spin.setRange(2, 200)
        self._grid_n_ch2_spin.setValue(10)
        ch2_lay.addWidget(self._grid_n_ch2_spin)
        layout.addWidget(ch2_grp)

        # ── Speed ────────────────────────────────────────────────────────
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

        # ── Scan preview ─────────────────────────────────────────────────
        self._scan_preview_label = QLabel()
        self._scan_preview_label.setWordWrap(True)
        self._scan_preview_label.setStyleSheet("font-size: 10px; color: #888;")
        layout.addWidget(self._scan_preview_label)
        self._update_scan_preview()

        for spin in (self._scan_size_ch1_spin, self._scan_size_ch2_spin,
                     self._grid_n_ch1_spin, self._grid_n_ch2_spin):
            spin.valueChanged.connect(self._update_scan_preview)

        # ── Control buttons ──────────────────────────────────────────────
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

        # ── Status ───────────────────────────────────────────────────────
        self._status_label = QLabel(tr("Ready"))
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # ── Color map ─────────────────────────────────────────────────────
        cmap_row = QHBoxLayout()
        cmap_row.addWidget(QLabel(tr("Color map:")))
        self._cmap_combo = _no_wheel(QComboBox())
        self._cmap_combo.addItems(
            ["plasma", "inferno", "viridis", "magma"]
        )
        cmap_row.addWidget(self._cmap_combo)
        layout.addLayout(cmap_row)

        layout.addStretch()

        # ── Gaussian fit results ─────────────────────────────────────────
        fit_grp = QGroupBox(tr("Gaussian Fit Result"))
        fit_lay = QVBoxLayout(fit_grp)
        self._fit_x_label = QLabel(tr("Ch{ch}:  —", ch=1))
        self._fit_y_label = QLabel(tr("Ch{ch}:  —", ch=2))
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
        size_ch1 = self._scan_size_ch1_spin.value()
        n_ch1    = self._grid_n_ch1_spin.value()
        size_ch2 = self._scan_size_ch2_spin.value()
        n_ch2    = self._grid_n_ch2_spin.value()

        half_ch1 = size_ch1 / 2.0 / UM_PER_PULSE_CH1
        half_ch2 = size_ch2 / 2.0 / UM_PER_PULSE_CH2
        step_ch1 = (2.0 * half_ch1 / (n_ch1 - 1)) if n_ch1 > 1 else 0.0
        step_ch2 = (2.0 * half_ch2 / (n_ch2 - 1)) if n_ch2 > 1 else 0.0

        self._scan_preview_label.setText(
            tr(
                "Ch{ch_x}: ±{half_x:.0f} pulses, step {step_x:.2f} p\n"
                "Ch{ch_y}: ±{half_y:.0f} pulses, step {step_y:.2f} p",
                ch_x=1, half_x=half_ch1, step_x=step_ch1,
                ch_y=2, half_y=half_ch2, step_y=step_ch2,
            )
        )

    def _build_plot_area(self) -> QWidget:
        self._glw = pg.GraphicsLayoutWidget()

        self._bottom_axis = _PulseAxisItem("bottom", center_pulse=0)
        self._left_axis   = _PulseAxisItem("left",   center_pulse=0)
        self._top_axis    = _MicronAxisItem("top",   um_per_pulse=UM_PER_PULSE_CH1)
        self._right_axis  = _MicronAxisItem("right", um_per_pulse=UM_PER_PULSE_CH2)

        self._plot_2d = self._glw.addPlot(
            row=0, col=0,
            title=tr("Transmission Map"),
            axisItems={
                "bottom": self._bottom_axis,
                "left":   self._left_axis,
                "top":    self._top_axis,
                "right":  self._right_axis,
            },
        )
        self._plot_2d.showAxis("top")
        self._plot_2d.showAxis("right")
        self._plot_2d.setLabel("bottom", tr("Ch{ch} (X) [pulse]", ch=1))
        self._plot_2d.setLabel("left",   tr("Ch{ch} (Y) [pulse]", ch=2))
        self._plot_2d.setLabel("top",    tr("Ch{ch} (X) [µm from centre]", ch=1))
        self._plot_2d.setLabel("right",  tr("Ch{ch} (Y) [µm from centre]", ch=2))

        self._img_item = pg.ImageItem()
        self._plot_2d.addItem(self._img_item)

        cmap = pg.colormap.get(self._cmap_combo.currentText())
        self._img_item.setColorMap(cmap)

        self._colorbar = pg.ColorBarItem(
            colorMap=cmap,
            label=tr("Transmitted"),
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

        self._plot_y = self._glw.addPlot(row=0, col=2, title=tr("Ch{ch} (Y) Profile", ch=2))
        self._plot_y.setLabel("bottom", tr("Intensity"))
        self._plot_y.setLabel("left",   tr("Ch{ch} offset", ch=2), units="pulses")
        self._plot_y.setYLink(self._plot_2d)
        self._curve_y_fit  = self._plot_y.plot(pen=pg.mkPen("r", width=2))
        self._curve_y_data = self._plot_y.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=pg.mkBrush(0, 0, 0),
            symbolPen=pg.mkPen((80, 80, 80), width=1),
        )

        self._plot_x = self._glw.addPlot(row=1, col=0, title=tr("Ch{ch} (X) Profile", ch=1))
        self._plot_x.setLabel("bottom", tr("Ch{ch} offset", ch=1), units="pulses")
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

        self._plot_2d.vb.setAspectLocked(True, ratio=UM_PER_PULSE_CH1 / UM_PER_PULSE_CH2)

        self._plot_2d.scene().sigMouseClicked.connect(self._on_scene_clicked)

        return self._glw

    # ── Color map ─────────────────────────────────────────────────────────────

    def _on_cmap_changed(self, name: str) -> None:
        cmap = pg.colormap.get(name)
        self._img_item.setColorMap(cmap)
        self._colorbar.setColorMap(cmap)

    # ── Scan control ─────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._controller is None:
            QMessageBox.warning(self, tr("Error"), tr("Stage controller not connected."))
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

        self._n_ch1            = self._grid_n_ch1_spin.value()
        self._n_ch2            = self._grid_n_ch2_spin.value()
        self._scan_size_um_ch1 = self._scan_size_ch1_spin.value()
        self._scan_size_um_ch2 = self._scan_size_ch2_spin.value()
        speed = next(
            btn.property("speed_val")
            for btn in self._speed_grp.buttons()
            if btn.isChecked()
        )

        half_ch1 = self._scan_size_um_ch1 / 2.0 / UM_PER_PULSE_CH1
        half_ch2 = self._scan_size_um_ch2 / 2.0 / UM_PER_PULSE_CH2
        self._x_pulses_rel = np.round(
            np.linspace(-half_ch1, half_ch1, self._n_ch1)
        ).astype(int)
        self._y_pulses_rel = np.round(
            np.linspace(-half_ch2, half_ch2, self._n_ch2)
        ).astype(int)

        x_pulses_abs = (self._center_x_pulse + self._x_pulses_rel).tolist()
        y_pulses_abs = (self._center_y_pulse + self._y_pulses_rel).tolist()

        self._transmitted_map = np.full((self._n_ch2, self._n_ch1), np.nan)

        xp, yp = self._x_pulses_rel, self._y_pulses_rel
        px_x = (xp[-1] - xp[0]) / max(self._n_ch1 - 1, 1)
        px_y = (yp[-1] - yp[0]) / max(self._n_ch2 - 1, 1)
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
        self._fit_x_label.setText(tr("Ch{ch}:  —", ch=1))
        self._fit_y_label.setText(tr("Ch{ch}:  —", ch=2))

        if self._gpib_reader is not None:
            reader: GpibReader = self._gpib_reader
        elif self._debug:
            reader = GpibReaderSim(
                center_x_pulse=self._center_x_pulse,
                center_y_pulse=self._center_y_pulse,
            )
        else:
            reply = QMessageBox.warning(
                self,
                tr("Keithley 2000 not connected"),
                tr("Keithley 2000 is not connected.\n"
                   "The scan will record zero intensity for all points.\n\n"
                   "Connect the Keithley 2000 from the main window before starting the scan.\n\n"
                   "Continue anyway?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._status_label.setText(tr("Scan cancelled."))
                return
            reader = GpibReader()

        self._scan_worker = CollimatorScanWorker(
            controller   = self._controller,
            gpib_reader  = reader,
            x_pulses     = x_pulses_abs,
            y_pulses     = y_pulses_abs,
            center_x     = self._center_x_pulse,
            center_y     = self._center_y_pulse,
            speed        = speed,
            accumulation = self._accum_spin.value(),
        )
        self._scan_worker.point_measured.connect(self._on_point_measured)
        self._scan_worker.scan_completed.connect(self._on_scan_completed)
        self._scan_worker.scan_aborted.connect(self._on_scan_aborted)
        self._scan_worker.status_message.connect(self._status_label.setText)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_label.setText(tr("Starting scan…"))
        self._scan_worker.start()

    def _on_stop(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.abort()
            if self._controller is not None:
                self._controller.normal_stop()
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Aborting…"))

    def _on_emergency_stop(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.abort()
        if self._controller is not None:
            self._controller.emergency_stop()
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("EMERGENCY STOP — AESTP sent."))

    # ── Data reception ───────────────────────────────────────────────────────

    @pyqtSlot(int, int, float)
    def _on_point_measured(
        self, row: int, col: int, transmitted: float
    ) -> None:
        self._transmitted_map[row, col] = transmitted
        self._update_2d_map()

    def _update_2d_map(self) -> None:
        data    = self._transmitted_map
        display = np.nan_to_num(data, nan=0.0)

        valid = data[~np.isnan(data)]
        if valid.size == 0:
            return
        vmin, vmax = float(valid.min()), float(valid.max())
        if vmin == vmax:
            vmax = vmin + 1.0

        self._img_item.setImage(display.T, levels=(vmin, vmax))
        self._colorbar.setLevels(low=vmin, high=vmax)

        xp  = self._x_pulses_rel
        yp  = self._y_pulses_rel
        px_x = (xp[-1] - xp[0]) / max(self._n_ch1 - 1, 1)
        px_y = (yp[-1] - yp[0]) / max(self._n_ch2 - 1, 1)
        self._img_item.setRect(
            float(xp[0])  - px_x / 2.0,
            float(yp[0])  - px_y / 2.0,
            float(xp[-1] - xp[0]) + px_x,
            float(yp[-1] - yp[0]) + px_y,
        )

    # ── Scan completion ───────────────────────────────────────────────────────

    def _on_scan_completed(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Scan complete. Running fit…"))
        self._run_gaussian_fit()
        play_current_sound()

    def _on_scan_aborted(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if (
            self._transmitted_map is not None
            and np.any(~np.isnan(self._transmitted_map))
        ):
            self._status_label.setText(tr("Scan aborted. Fitting available data…"))
            self._run_gaussian_fit()
        else:
            self._status_label.setText(tr("Scan aborted."))

    # ── Gaussian fitting ──────────────────────────────────────────────────────

    def _run_gaussian_fit(self) -> None:
        data = self._transmitted_map
        if data is None or np.all(np.isnan(data)):
            self._status_label.setText(tr("No data available for fitting."))
            return

        xp = self._x_pulses_rel.astype(float)
        yp = self._y_pulses_rel.astype(float)

        x_profile = np.nanmean(data, axis=0)
        y_profile = np.nanmean(data, axis=1)

        self._curve_x_data.setData(xp, x_profile)
        self._curve_y_data.setData(y_profile, yp)

        xp_fine = np.linspace(xp[0], xp[-1], 300)
        yp_fine = np.linspace(yp[0], yp[-1], 300)

        x_center_rel = 0.0
        y_center_rel = 0.0

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
                   ch=1, abs_pulse=abs_x, rel=x_center_rel, width_kind="σ", width=sigma_x)
            )
        except Exception:
            self._fit_x_label.setText(tr("Ch{ch}:  fit failed", ch=1))
            abs_x = self._center_x_pulse

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
                   ch=2, abs_pulse=abs_y, rel=y_center_rel, width_kind="σ", width=sigma_y)
            )
        except Exception:
            self._fit_y_label.setText(tr("Ch{ch}:  fit failed", ch=2))
            abs_y = self._center_y_pulse

        self._suggested_x_pulse = self._center_x_pulse + round(x_center_rel)
        self._suggested_y_pulse = self._center_y_pulse + round(y_center_rel)

        self._vline.setPos(x_center_rel)
        self._hline.setPos(y_center_rel)
        self._vline.setVisible(True)
        self._hline.setVisible(True)

        self._goto_btn.setEnabled(True)
        self._status_label.setText(tr("Fit complete."))

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_goto_suggested(self) -> None:
        if self._suggested_x_pulse is None or self._suggested_y_pulse is None:
            return
        self._move_to(
            self._suggested_x_pulse,
            self._suggested_y_pulse,
            tr("Moving to suggested position…"),
        )

    def _move_to(self, x_pulse: int, y_pulse: int, status_msg: str | None = None) -> None:
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

    # ── Right-click on 2-D map ────────────────────────────────────────────────

    def _on_scene_clicked(self, event) -> None:
        if event.button() != Qt.MouseButton.RightButton:
            return
        if self._transmitted_map is None:
            return
        pos = event.scenePos()
        if not self._plot_2d.vb.sceneBoundingRect().contains(pos):
            return
        event.accept()

        view_pos = self._plot_2d.vb.mapSceneToView(pos)
        x_rel = float(view_pos.x())
        y_rel = float(view_pos.y())
        x_pulse = self._center_x_pulse + round(x_rel)
        y_pulse = self._center_y_pulse + round(y_rel)

        menu   = QMenu()
        action = menu.addAction(
            tr("Go to this position  (Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses)",
               ch_x=1, x_pulse=x_pulse, ch_y=2, y_pulse=y_pulse)
        )
        result = menu.exec(event.screenPos().toPoint())
        if result is action:
            self._move_to(
                x_pulse, y_pulse,
                tr("Moving to Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses…",
                   ch_x=1, x_pulse=x_pulse, ch_y=2, y_pulse=y_pulse),
            )

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        scan_running = self._scan_worker is not None and self._scan_worker.isRunning()
        move_running = self._move_worker is not None and self._move_worker.isRunning()
        if not scan_running and not move_running:
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
        event.accept()
