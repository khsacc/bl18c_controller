"""2D Scan window.

Generalisation of ``DacScanWindow``: the user picks *any* two translation
channels (Ch1-Ch10 — Ch11 is a rotation stage and is excluded) via pulldowns
at the top of the parameter panel, then scans them over a user-defined grid
centred on the current stage position exactly like DAC Scan (Normal).

Layout
------
Left  : channel selection + scan parameter panel + status + fit result
Right : pyqtgraph GraphicsLayoutWidget
          [2D colour map]  |  [Y-channel profile]
          [X-channel profile]

Right-click on the 2-D map shows a "Go to this position" context menu.
All plots are non-interactive (no zoom / pan / context-menu).
"""
from __future__ import annotations

import json
import numpy as np
from datetime import datetime
from pathlib import Path
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QMessageBox, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

try:
    from .free_2d_scan_backend import (
        CHANNEL_CHOICES,
        Free2DScanWorker, GpibReader, GpibReaderSim,
        um_per_pulse,
    )
    from settings import log_prefs
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr
    from utils.fitting import fit_profile_1d
except ImportError:
    import os, sys
    _root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    sys.path.insert(0, _root)
    from apps.scan2d.free_2d_scan_backend import (
        CHANNEL_CHOICES,
        Free2DScanWorker, GpibReader, GpibReaderSim,
        um_per_pulse,
    )
    from settings import log_prefs
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr
    from utils.fitting import fit_profile_1d


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


# ---------------------------------------------------------------------------
# Secondary axis — shows absolute pulse values for the same tick positions
# ---------------------------------------------------------------------------

class _PulseAxisItem(pg.AxisItem):
    """AxisItem whose tick labels show absolute pulse values.

    The ViewBox coordinate space is in pulse offsets from the scan centre
    (integers).  This axis adds the centre-pulse so the labels show the
    absolute stage position.
    """

    def __init__(self, orientation: str, center_pulse: int = 0):
        super().__init__(orientation)
        self.center_pulse = center_pulse

    def tickStrings(self, values, scale, spacing):
        return [str(self.center_pulse + round(v)) for v in values]


class _MicronAxisItem(pg.AxisItem):
    """AxisItem whose tick labels show physical distance in µm from the scan centre.

    The ViewBox coordinate space is in pulse offsets from the scan centre.
    This axis multiplies each offset by um_per_pulse to show physical distance.
    """

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
    """Non-blocking absolute move to (x_pulse, y_pulse)."""

    move_completed = pyqtSignal()
    move_failed    = pyqtSignal(str)

    def __init__(self, controller, ch_x: int, ch_y: int, x_pulse: int, y_pulse: int, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.ch_x       = ch_x
        self.ch_y       = ch_y
        self.x_pulse    = x_pulse
        self.y_pulse    = y_pulse

    def run(self) -> None:
        try:
            self.controller.move_ch_absolute(self.ch_x, self.x_pulse)
            self.controller.move_ch_absolute(self.ch_y, self.y_pulse)
            self.controller.wait_until_stop()
            self.move_completed.emit()
        except Exception as e:
            self.move_failed.emit(str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class Free2DScanWindow(QMainWindow):
    """2D Scan — user picks any two translation channels (Ch1-Ch10) to scan.

    ``default_ch_x`` / ``default_ch_y`` / ``allow_channel_change`` / ``log_key`` /
    ``window_title`` let a subclass turn this generic window into a fixed-axis
    scan app (e.g. DAC Scan on Ch4/Ch5) without duplicating any of the scan,
    fit, or plotting logic — see ``apps/dac_scan/dac_scan_app.py``.
    """

    def __init__(
        self,
        controller=None,
        gpib_reader: GpibReader | None = None,
        debug: bool = False,
        parent=None,
        default_ch_x: int = 4,
        default_ch_y: int = 5,
        allow_channel_change: bool = True,
        log_key: str = "free_2d_scan",
        window_title: str = "2D Scan",
    ):
        super().__init__(parent)
        self.setWindowTitle(tr(window_title))
        self.resize(1300, 800)

        self._controller   = controller
        self._gpib_reader  = gpib_reader
        self._debug        = debug
        self._scan_worker: Free2DScanWorker | None = None
        self._move_worker: _MoveWorker       | None = None

        self._default_ch_x         = default_ch_x
        self._default_ch_y         = default_ch_y
        self._allow_channel_change = allow_channel_change
        self._log_key              = log_key

        # Channels captured at scan start (may differ from live combo state
        # while a scan is running, since the combos are disabled during a scan).
        self._ch_x: int = default_ch_x
        self._ch_y: int = default_ch_y

        # Scan state — pulse-primary
        self._n_x: int                        = 10
        self._n_y: int                        = 10
        self._scan_size_um_x: float           = 500.0
        self._scan_size_um_y: float           = 500.0
        self._center_x_pulse: int             = 0
        self._center_y_pulse: int             = 0
        self._x_pulses_rel: np.ndarray | None = None  # relative X-channel pulse offsets
        self._y_pulses_rel: np.ndarray | None = None  # relative Y-channel pulse offsets
        self._transmitted_map: np.ndarray | None = None
        self._scan_speed:      str               = "H"
        self._scan_settle_ms:  int               = 100
        self._scan_accumulation: int             = 10
        self._scan_start_time: datetime | None   = None
        self._fit_results:     dict | None       = None
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
        self._update_axis_labels()
        self._update_scan_preview()

    def _build_param_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Channel selection ────────────────────────────────────────────
        chsel_grp = QGroupBox(tr("Channel Selection"))
        chsel_lay = QVBoxLayout(chsel_grp)
        chsel_lay.addWidget(QLabel(tr("X channel:")))
        self._ch_x_combo = _no_wheel(QComboBox())
        self._ch_x_combo.addItems([f"Ch{c}" for c in CHANNEL_CHOICES])
        self._ch_x_combo.setCurrentIndex(CHANNEL_CHOICES.index(self._default_ch_x))
        chsel_lay.addWidget(self._ch_x_combo)
        chsel_lay.addWidget(QLabel(tr("Y channel:")))
        self._ch_y_combo = _no_wheel(QComboBox())
        self._ch_y_combo.addItems([f"Ch{c}" for c in CHANNEL_CHOICES])
        self._ch_y_combo.setCurrentIndex(CHANNEL_CHOICES.index(self._default_ch_y))
        chsel_lay.addWidget(self._ch_y_combo)
        layout.addWidget(chsel_grp)
        self._ch_x_combo.currentIndexChanged.connect(self._on_channel_selection_changed)
        self._ch_y_combo.currentIndexChanged.connect(self._on_channel_selection_changed)
        if not self._allow_channel_change:
            chsel_grp.setVisible(False)
            self._ch_x_combo.setEnabled(False)
            self._ch_y_combo.setEnabled(False)

        # ── X-channel scan parameters ────────────────────────────────────
        self._x_grp = QGroupBox(
            tr("Ch{ch} (X) Scan", ch=CHANNEL_CHOICES[self._ch_x_combo.currentIndex()])
        )
        x_lay = QVBoxLayout(self._x_grp)
        x_lay.addWidget(QLabel(tr("Scan size (µm):")))
        self._scan_size_x_spin = _no_wheel(QDoubleSpinBox())
        self._scan_size_x_spin.setRange(1.0, 10_000.0)
        self._scan_size_x_spin.setValue(500.0)
        self._scan_size_x_spin.setSuffix(" µm")
        self._scan_size_x_spin.setSingleStep(10.0)
        x_lay.addWidget(self._scan_size_x_spin)
        x_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_x_spin = _no_wheel(QSpinBox())
        self._grid_n_x_spin.setRange(2, 200)
        self._grid_n_x_spin.setValue(10)
        x_lay.addWidget(self._grid_n_x_spin)
        layout.addWidget(self._x_grp)

        # ── Y-channel scan parameters ────────────────────────────────────
        self._y_grp = QGroupBox(
            tr("Ch{ch} (Y) Scan", ch=CHANNEL_CHOICES[self._ch_y_combo.currentIndex()])
        )
        y_lay = QVBoxLayout(self._y_grp)
        y_lay.addWidget(QLabel(tr("Scan size (µm):")))
        self._scan_size_y_spin = _no_wheel(QDoubleSpinBox())
        self._scan_size_y_spin.setRange(1.0, 10_000.0)
        self._scan_size_y_spin.setValue(500.0)
        self._scan_size_y_spin.setSuffix(" µm")
        self._scan_size_y_spin.setSingleStep(10.0)
        y_lay.addWidget(self._scan_size_y_spin)
        y_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_y_spin = _no_wheel(QSpinBox())
        self._grid_n_y_spin.setRange(2, 200)
        self._grid_n_y_spin.setValue(10)
        y_lay.addWidget(self._grid_n_y_spin)
        layout.addWidget(self._y_grp)

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

        # ── Settle time ──────────────────────────────────────────────────
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

        # ── Scan preview — range and step in pulses ──────────────────────
        self._scan_preview_label = QLabel()
        self._scan_preview_label.setWordWrap(True)
        self._scan_preview_label.setStyleSheet("font-size: 10px; color: #888;")
        layout.addWidget(self._scan_preview_label)

        for spin in (self._scan_size_x_spin, self._scan_size_y_spin,
                     self._grid_n_x_spin, self._grid_n_y_spin):
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

        # ── Fitting ──────────────────────────────────────────────────────
        fit_grp = QGroupBox(tr("Fitting"))
        fit_lay = QVBoxLayout(fit_grp)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel(tr("Model:")))
        self._fit_model_combo = _no_wheel(QComboBox())
        # Combo values double as fit_profile_1d() model keys (utils.fitting.GAUSSIAN /
        # APERTURE) — kept in English regardless of UI language, like the Speed L/M/H labels.
        self._fit_model_combo.addItems(["Gaussian", "Aperture (erf)"])
        self._fit_model_combo.currentTextChanged.connect(self._on_fit_model_changed)
        model_row.addWidget(self._fit_model_combo)
        fit_lay.addLayout(model_row)
        self._fit_x_label = QLabel(tr("X:  —"))
        self._fit_y_label = QLabel(tr("Y:  —"))
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

    # ── Channel selection ────────────────────────────────────────────────────

    def _selected_channels(self) -> tuple[int, int]:
        return (
            CHANNEL_CHOICES[self._ch_x_combo.currentIndex()],
            CHANNEL_CHOICES[self._ch_y_combo.currentIndex()],
        )

    def _on_channel_selection_changed(self) -> None:
        ch_x, ch_y = self._selected_channels()
        self._x_grp.setTitle(tr("Ch{ch} (X) Scan", ch=ch_x))
        self._y_grp.setTitle(tr("Ch{ch} (Y) Scan", ch=ch_y))
        self._update_scan_preview()
        self._update_axis_labels()

    def _update_axis_labels(self) -> None:
        ch_x, ch_y = self._selected_channels()
        self._plot_2d.setLabel("bottom", tr("Ch{ch} (X) [pulse]", ch=ch_x))
        self._plot_2d.setLabel("left",   tr("Ch{ch} (Y) [pulse]", ch=ch_y))
        self._plot_2d.setLabel("top",    tr("Ch{ch} (X) [µm from centre]", ch=ch_x))
        self._plot_2d.setLabel("right",  tr("Ch{ch} (Y) [µm from centre]", ch=ch_y))
        self._plot_y.setTitle(tr("Ch{ch} (Y) Profile", ch=ch_y))
        self._plot_y.setLabel("left",    tr("Ch offset"), units=tr("pulses"))
        self._plot_x.setTitle(tr("Ch{ch} (X) Profile", ch=ch_x))
        self._plot_x.setLabel("bottom",  tr("Ch offset"), units=tr("pulses"))
        self._top_axis.um_per_pulse   = um_per_pulse(ch_x)
        self._right_axis.um_per_pulse = um_per_pulse(ch_y)
        self._plot_2d.vb.setAspectLocked(True, ratio=um_per_pulse(ch_x) / um_per_pulse(ch_y))

    def _update_scan_preview(self) -> None:
        """Update the pulse range / step display above the Start button."""
        ch_x, ch_y = self._selected_channels()
        size_x = self._scan_size_x_spin.value()
        n_x    = self._grid_n_x_spin.value()
        size_y = self._scan_size_y_spin.value()
        n_y    = self._grid_n_y_spin.value()

        half_x = size_x / 2.0 / um_per_pulse(ch_x)
        half_y = size_y / 2.0 / um_per_pulse(ch_y)
        step_x = (2.0 * half_x / (n_x - 1)) if n_x > 1 else 0.0
        step_y = (2.0 * half_y / (n_y - 1)) if n_y > 1 else 0.0

        self._scan_preview_label.setText(
            tr("Ch{ch_x}: ±{half_x:.1f} pulses, step {step_x:.2f} p\n"
               "Ch{ch_y}: ±{half_y:.1f} pulses, step {step_y:.2f} p",
               ch_x=ch_x, half_x=half_x, step_x=step_x,
               ch_y=ch_y, half_y=half_y, step_y=step_y)
        )

    def _build_plot_area(self) -> QWidget:
        self._glw = pg.GraphicsLayoutWidget()

        # ── 2-D colour map ───────────────────────────────────────────────
        # Primary axes (bottom/left): absolute stage pulse values.
        # Secondary axes (top/right): physical distance in µm from scan centre.
        self._bottom_axis = _PulseAxisItem("bottom", center_pulse=0)
        self._left_axis   = _PulseAxisItem("left",   center_pulse=0)
        self._top_axis    = _MicronAxisItem("top",   um_per_pulse=1.0)
        self._right_axis  = _MicronAxisItem("right", um_per_pulse=1.0)

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

        self._img_item = pg.ImageItem()
        self._plot_2d.addItem(self._img_item)

        cmap = pg.colormap.get(self._cmap_combo.currentText())
        self._img_item.setColorMap(cmap)

        self._colorbar = pg.ColorBarItem(
            colorMap=cmap,
            label="Transmitted",
            interactive=False,
        )
        self._colorbar.setImageItem(self._img_item)  # placed in glw layout, not inside plot

        cross_pen = pg.mkPen("r", width=2, style=Qt.PenStyle.DashLine)
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=cross_pen)
        self._hline = pg.InfiniteLine(angle=0,  movable=False, pen=cross_pen)
        self._plot_2d.addItem(self._vline)
        self._plot_2d.addItem(self._hline)
        self._vline.setVisible(False)
        self._hline.setVisible(False)

        # ── Y-channel profile — right of the colorbar (col 2) ────────────
        self._plot_y = self._glw.addPlot(row=0, col=2, title=tr("Y Profile"))
        self._plot_y.setLabel("bottom", tr("Intensity"))
        self._plot_y.setYLink(self._plot_2d)
        # Fitted first (renders behind circles)
        self._curve_y_fit = self._plot_y.plot(pen=pg.mkPen("r", width=2))
        # Observed: black circles on top
        self._curve_y_data = self._plot_y.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=pg.mkBrush(0, 0, 0),
            symbolPen=pg.mkPen((80, 80, 80), width=1),
        )

        # ── X-channel profile — below the 2-D map ─────────────────────────
        self._plot_x = self._glw.addPlot(row=1, col=0, title=tr("X Profile"))
        self._plot_x.setLabel("left",   tr("Intensity"))
        self._plot_x.setXLink(self._plot_2d)
        # Fitted first
        self._curve_x_fit = self._plot_x.plot(pen=pg.mkPen("r", width=2))
        # Observed: black circles on top
        self._curve_x_data = self._plot_x.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=pg.mkBrush(0, 0, 0),
            symbolPen=pg.mkPen((80, 80, 80), width=1),
        )

        # Column / row stretch ratios.
        # Layout: col 0 = 2D map + X profile, col 1 = colorbar, col 2 = Y profile.
        # Placing the colorbar in its own column (between the 2D map and Y profile)
        # removes it from the 2D map's internal layout, so both the 2D map and the
        # X profile have only a right axis on their right edge — enabling exact width
        # matching with a fixed setWidth() value.
        ci = self._glw.ci
        ci.addItem(self._colorbar, row=0, col=1)   # standalone colorbar column
        ci.layout.setColumnStretchFactor(0, 3)
        ci.layout.setColumnStretchFactor(1, 0)     # colorbar: minimal, natural width
        ci.layout.setColumnStretchFactor(2, 1)
        ci.layout.setRowStretchFactor(0, 3)
        ci.layout.setRowStretchFactor(1, 1)

        # ── Disable all mouse interaction ────────────────────────────────
        for _p in (self._plot_2d, self._plot_y, self._plot_x):
            _p.vb.setMouseEnabled(x=False, y=False)
            _p.setMenuEnabled(False)
            _p.hideButtons()

        # ── Match top-axis height for Y profile ───────────────────────────
        # The 2D map has a labelled top axis; the Y profile gets a blank top
        # axis of the same pixel height so both ViewBoxes are equal in height
        # and setYLink aligns tick marks exactly.
        _TOP_H = 50
        self._plot_2d.getAxis("top").setHeight(_TOP_H)
        self._plot_y.showAxis("top")
        self._plot_y.getAxis("top").setHeight(_TOP_H)
        self._plot_y.getAxis("top").setStyle(showValues=False, tickLength=0)

        # ── Match right-axis widths: 2D map and X profile ────────────────
        # The colorbar is now in its own layout column (not inside the 2D map
        # PlotItem), so both plots have only a right axis on their right edge.
        # Fixing both to the same width ensures the ViewBoxes are equally wide
        # and X-linked tick marks land at the same horizontal pixel positions.
        # The left axes are also fixed to the same width to align the left edges.
        _LEFT_W       = 55
        _RIGHT_AXIS_W = 55
        self._plot_2d.getAxis("left").setWidth(_LEFT_W)
        self._plot_2d.getAxis("right").setWidth(_RIGHT_AXIS_W)
        self._plot_x.getAxis("left").setWidth(_LEFT_W)
        self._plot_x.showAxis("right")
        self._plot_x.getAxis("right").setWidth(_RIGHT_AXIS_W)
        self._plot_x.getAxis("right").setStyle(showValues=False, tickLength=0)

        # ── Disable autorange on spatially-linked axes ───────────────────
        # Y range of Y profile and X range of X profile are controlled
        # by the 2D map via setYLink / setXLink.  Autorange would override
        # the scan-derived range set in _on_start.
        self._plot_2d.vb.disableAutoRange()
        self._plot_y.vb.disableAutoRange(pg.ViewBox.YAxis)
        self._plot_x.vb.disableAutoRange(pg.ViewBox.XAxis)

        # Right-click on 2-D map
        self._plot_2d.scene().sigMouseClicked.connect(self._on_scene_clicked)

        return self._glw

    # ── Color map ─────────────────────────────────────────────────────────────

    def _on_cmap_changed(self, name: str) -> None:
        cmap = pg.colormap.get(name)
        self._img_item.setColorMap(cmap)
        self._colorbar.setColorMap(cmap)

    def _on_fit_model_changed(self) -> None:
        if (
            self._transmitted_map is not None
            and np.any(~np.isnan(self._transmitted_map))
            and (self._scan_worker is None or not self._scan_worker.isRunning())
        ):
            self._run_fit()

    # ── Scan control ─────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._controller is None:
            QMessageBox.warning(self, tr("Error"), tr("Stage controller not connected."))
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return

        ch_x, ch_y = self._selected_channels()
        if ch_x == ch_y:
            QMessageBox.warning(
                self, tr("Error"), tr("X channel and Y channel must be different.")
            )
            return

        try:
            self._center_x_pulse = int(self._controller.get_ch_pos(ch_x))
            self._center_y_pulse = int(self._controller.get_ch_pos(ch_y))
        except Exception as e:
            QMessageBox.warning(self, tr("Error"), tr("Cannot read current position:\n{error}", error=e))
            return

        self._ch_x = ch_x
        self._ch_y = ch_y

        # Update primary axes for absolute pulse labels
        self._bottom_axis.center_pulse = self._center_x_pulse
        self._left_axis.center_pulse   = self._center_y_pulse
        self._update_axis_labels()

        self._n_x            = self._grid_n_x_spin.value()
        self._n_y            = self._grid_n_y_spin.value()
        self._scan_size_um_x = self._scan_size_x_spin.value()
        self._scan_size_um_y = self._scan_size_y_spin.value()
        speed = next(
            btn.property("speed_val")
            for btn in self._speed_grp.buttons()
            if btn.isChecked()
        )

        # Compute relative pulse arrays (integer offsets from scan centre)
        um_per_pulse_x = um_per_pulse(ch_x)
        um_per_pulse_y = um_per_pulse(ch_y)
        half_x = self._scan_size_um_x / 2.0 / um_per_pulse_x
        half_y = self._scan_size_um_y / 2.0 / um_per_pulse_y
        self._x_pulses_rel = np.round(
            np.linspace(-half_x, half_x, self._n_x)
        ).astype(int)
        self._y_pulses_rel = np.round(
            np.linspace(-half_y, half_y, self._n_y)
        ).astype(int)

        x_pulses_abs = (self._center_x_pulse + self._x_pulses_rel).tolist()
        y_pulses_abs = (self._center_y_pulse + self._y_pulses_rel).tolist()

        self._transmitted_map = np.full((self._n_y, self._n_x), np.nan)
        self._scan_speed      = speed
        self._scan_settle_ms  = self._settle_spin.value()
        self._scan_accumulation = self._accum_spin.value()
        self._scan_start_time = datetime.now()
        self._fit_results     = None

        # Set fixed spatial ranges in pulse units (propagates to linked plots)
        xp, yp = self._x_pulses_rel, self._y_pulses_rel
        px_x = (xp[-1] - xp[0]) / max(self._n_x - 1, 1)
        px_y = (yp[-1] - yp[0]) / max(self._n_y - 1, 1)
        x_rng = (float(xp[0]) - px_x / 2, float(xp[-1]) + px_x / 2)
        y_rng = (float(yp[0]) - px_y / 2, float(yp[-1]) + px_y / 2)
        self._plot_2d.setRange(xRange=x_rng, yRange=y_rng, padding=0)
        # Re-enable autorange for the intensity (non-spatial) axes
        self._plot_y.vb.enableAutoRange(pg.ViewBox.XAxis)
        self._plot_x.vb.enableAutoRange(pg.ViewBox.YAxis)

        # Reset plots
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
        self._fit_x_label.setText(tr("Ch{ch}:  —", ch=ch_x))
        self._fit_y_label.setText(tr("Ch{ch}:  —", ch=ch_y))

        # GPIB reader
        if self._gpib_reader is not None:
            reader: GpibReader = self._gpib_reader
        elif self._debug:
            reader = GpibReaderSim(
                um_per_pulse_x=um_per_pulse_x,
                um_per_pulse_y=um_per_pulse_y,
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
                self._start_btn.setEnabled(True)
                self._status_label.setText(tr("Scan cancelled."))
                return
            reader = GpibReader()

        self._scan_worker = Free2DScanWorker(
            controller   = self._controller,
            gpib_reader  = reader,
            ch_x         = ch_x,
            ch_y         = ch_y,
            x_pulses     = x_pulses_abs,
            y_pulses     = y_pulses_abs,
            center_x     = self._center_x_pulse,
            center_y     = self._center_y_pulse,
            speed        = speed,
            settle_ms    = self._scan_settle_ms,
            accumulation = self._scan_accumulation,
        )
        self._scan_worker.point_measured.connect(self._on_point_measured)
        self._scan_worker.scan_completed.connect(self._on_scan_completed)
        self._scan_worker.scan_aborted.connect(self._on_scan_aborted)
        self._scan_worker.status_message.connect(self._status_label.setText)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._ch_x_combo.setEnabled(False)
        self._ch_y_combo.setEnabled(False)
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
        self._ch_x_combo.setEnabled(True)
        self._ch_y_combo.setEnabled(True)
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

        # Map pixel indices to pulse coordinates
        xp  = self._x_pulses_rel
        yp  = self._y_pulses_rel
        px_x = (xp[-1] - xp[0]) / max(self._n_x - 1, 1)
        px_y = (yp[-1] - yp[0]) / max(self._n_y - 1, 1)
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
        self._ch_x_combo.setEnabled(True)
        self._ch_y_combo.setEnabled(True)
        self._status_label.setText(tr("Scan complete. Running fit…"))
        self._run_fit()
        if log_prefs.should_save(self._log_key):
            self._save_details("completed")
        play_current_sound()

    def _on_scan_aborted(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._ch_x_combo.setEnabled(True)
        self._ch_y_combo.setEnabled(True)
        if (
            self._transmitted_map is not None
            and np.any(~np.isnan(self._transmitted_map))
        ):
            self._status_label.setText(tr("Scan aborted. Fitting available data…"))
            self._run_fit()
        else:
            self._status_label.setText(tr("Scan aborted."))
        if log_prefs.should_save(self._log_key):
            self._save_details("aborted")

    # ── Fitting ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fit_dict(res, abs_pulse: int) -> dict:
        """Build the JSON-serialisable fit-result dict for one channel."""
        width_key = "sigma_pulse" if res.model == "gaussian" else "width_pulse"
        return {
            "fit_ok": True,
            "model": res.model,
            "center_abs_pulse": abs_pulse,
            "center_rel_pulse": round(res.center, 3),
            width_key: round(res.width, 3),
        }

    def _run_fit(self) -> None:
        data = self._transmitted_map
        if data is None or np.all(np.isnan(data)):
            self._status_label.setText(tr("No data available for fitting."))
            return

        ch_x, ch_y = self._ch_x, self._ch_y
        model     = self._fit_model_combo.currentText()
        xp        = self._x_pulses_rel.astype(float)
        yp        = self._y_pulses_rel.astype(float)
        x_profile = np.nanmean(data, axis=0)   # (n_x,) — mean over Y rows
        y_profile = np.nanmean(data, axis=1)   # (n_y,) — mean over X cols

        self._curve_x_data.setData(xp, x_profile)
        self._curve_y_data.setData(y_profile, yp)

        x_center_rel = 0.0
        y_center_rel = 0.0

        # ── X channel (horizontal profile) ───────────────────────────────
        res_x = fit_profile_1d(xp, x_profile, model)
        if res_x is not None:
            x_center_rel = res_x.center
            self._curve_x_fit.setData(res_x.curve_x, res_x.curve_y)
            abs_x = self._center_x_pulse + round(x_center_rel)
            self._fit_x_label.setText(
                tr("Ch{ch}:  abs={abs_pulse} pulses\n"
                   "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
                   ch=ch_x, abs_pulse=abs_x, rel=x_center_rel,
                   width_kind=res_x.width_kind, width=res_x.width)
            )
            fit_x = self._fit_dict(res_x, abs_x)
        else:
            fit_x = {"fit_ok": False}
            self._fit_x_label.setText(tr("Ch{ch}:  fit failed", ch=ch_x))

        # ── Y channel (vertical profile — plotted with axes swapped) ──────
        res_y = fit_profile_1d(yp, y_profile, model)
        if res_y is not None:
            y_center_rel = res_y.center
            self._curve_y_fit.setData(res_y.curve_y, res_y.curve_x)
            abs_y = self._center_y_pulse + round(y_center_rel)
            self._fit_y_label.setText(
                tr("Ch{ch}:  abs={abs_pulse} pulses\n"
                   "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
                   ch=ch_y, abs_pulse=abs_y, rel=y_center_rel,
                   width_kind=res_y.width_kind, width=res_y.width)
            )
            fit_y = self._fit_dict(res_y, abs_y)
        else:
            fit_y = {"fit_ok": False}
            self._fit_y_label.setText(tr("Ch{ch}:  fit failed", ch=ch_y))

        self._fit_results = {f"ch{ch_x}": fit_x, f"ch{ch_y}": fit_y}
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
        """Save scan arrays, metadata JSON, and plot PNG to the configured log directory."""
        if self._scan_start_time is None or self._transmitted_map is None:
            return

        localdata = log_prefs.get_app_dir(self._log_key)
        ts   = self._scan_start_time.strftime("%Y%m%d_%H%M%S")
        stem = localdata / ts

        # ── numpy arrays ─────────────────────────────────────────────────
        np.savez_compressed(
            str(stem) + ".npz",
            transmitted_map = self._transmitted_map,
            x_pulses_rel    = self._x_pulses_rel,
            y_pulses_rel    = self._y_pulses_rel,
            x_pulses_abs    = self._center_x_pulse + self._x_pulses_rel,
            y_pulses_abs    = self._center_y_pulse + self._y_pulses_rel,
        )

        # ── metadata JSON ────────────────────────────────────────────────
        meta = {
            "timestamp":   self._scan_start_time.isoformat(),
            "outcome":     outcome,
            "scan_params": {
                "ch_x":             self._ch_x,
                "ch_y":             self._ch_y,
                "center_x_pulse":   self._center_x_pulse,
                "center_y_pulse":   self._center_y_pulse,
                "scan_size_um_x":   self._scan_size_um_x,
                "scan_size_um_y":   self._scan_size_um_y,
                "n_x":              self._n_x,
                "n_y":              self._n_y,
                "um_per_pulse_x":   um_per_pulse(self._ch_x),
                "um_per_pulse_y":   um_per_pulse(self._ch_y),
                "speed":            self._scan_speed,
                "settle_ms":        self._scan_settle_ms,
                "accumulation":     self._scan_accumulation,
            },
            "gaussian_fit": self._fit_results,
            "arrays_file":  ts + ".npz",
            "plot_file":    ts + ".png",
        }
        with (stem.parent / (ts + ".json")).open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        # ── plot image (full GraphicsLayoutWidget) ────────────────────────
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
            self._controller, self._ch_x, self._ch_y, x_pulse, y_pulse, parent=self
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
        x_rel = float(view_pos.x())   # X-channel pulse offset from centre
        y_rel = float(view_pos.y())   # Y-channel pulse offset from centre
        x_pulse = self._center_x_pulse + round(x_rel)
        y_pulse = self._center_y_pulse + round(y_rel)

        menu   = QMenu()
        action = menu.addAction(
            tr("Go to this position  (Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses)",
               ch_x=self._ch_x, x_pulse=x_pulse, ch_y=self._ch_y, y_pulse=y_pulse)
        )
        result = menu.exec(event.screenPos().toPoint())
        if result is action:
            self._move_to(
                x_pulse, y_pulse,
                tr("Moving to Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses…",
                   ch_x=self._ch_x, x_pulse=x_pulse, ch_y=self._ch_y, y_pulse=y_pulse),
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
