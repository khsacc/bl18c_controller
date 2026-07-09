"""1D Scan window.

Single-axis counterpart of :class:`Free2DScanWindow`
(``apps/scan2d/free_2d_scan_app.py``): the user picks *one* translation channel
(Ch1-Ch10 — Ch11 is a rotation stage and is excluded), scans it over
``current ± range`` in a user-defined number of grid points while reading the
transmitted X-ray intensity from the Keithley, fits the resulting profile with a
Gaussian or erf (aperture) model, and can move the channel to the fitted centre.

Reuse
-----
- Fit maths            : ``utils.fitting.fit_profile_1d`` (shared with scan2d).
- Scan worker          : ``Scan1DWorker`` in ``apps/scan2d/free_2d_scan_backend``.
- GPIB reader / sim    : ``GpibReader`` / ``GpibReaderSim`` (scan2d backend).
- Absolute-pulse / µm axes : ``_PulseAxisItem`` / ``_MicronAxisItem`` (scan2d app).

Layout
------
Left  : channel selection + scan parameter panel + status + fit result
Right : pyqtgraph profile plot (intensity vs channel pulse offset). The bottom
        axis shows absolute pulse values; the top axis shows µm from the centre.
"""
from __future__ import annotations

import json
import numpy as np
from datetime import datetime
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

try:
    from apps.scan2d.free_2d_scan_app import _MicronAxisItem, _PulseAxisItem
    from apps.scan2d.free_2d_scan_backend import (
        CHANNEL_CHOICES, GpibReader, GpibReaderSim, Scan1DWorker, um_per_pulse,
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
    from apps.scan2d.free_2d_scan_app import _MicronAxisItem, _PulseAxisItem
    from apps.scan2d.free_2d_scan_backend import (
        CHANNEL_CHOICES, GpibReader, GpibReaderSim, Scan1DWorker, um_per_pulse,
    )
    from settings import log_prefs
    from settings.notification_sound import play_current_sound
    from settings.i18n import tr
    from utils.fitting import fit_profile_1d


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    (now a QScrollArea) never silently changes a value the cursor happens to
    be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


# ---------------------------------------------------------------------------
# One-shot single-channel move worker
# ---------------------------------------------------------------------------

class _Move1DWorker(QThread):
    """Non-blocking absolute move of a single channel to *pulse*."""

    move_completed = pyqtSignal()
    move_failed    = pyqtSignal(str)

    def __init__(self, controller, ch: int, pulse: int, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.ch         = ch
        self.pulse      = pulse

    def run(self) -> None:
        try:
            self.controller.move_ch_absolute(self.ch, self.pulse)
            self.controller.wait_until_stop()
            self.move_completed.emit()
        except Exception as e:
            self.move_failed.emit(str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class Scan1DScanWindow(QMainWindow):
    """1D Scan — user picks one translation channel (Ch1-Ch10) to scan."""

    def __init__(
        self,
        controller=None,
        gpib_reader: GpibReader | None = None,
        debug: bool = False,
        parent=None,
        default_ch: int = 4,
        log_key: str = "scan1d",
        window_title: str = "1D Scan",
    ):
        super().__init__(parent)
        self.setWindowTitle(tr(window_title))
        self.resize(1100, 700)

        self._controller  = controller
        self._gpib_reader = gpib_reader
        self._debug       = debug
        self._scan_worker: Scan1DWorker | None = None
        self._move_worker: _Move1DWorker | None = None

        self._default_ch = default_ch
        self._log_key    = log_key

        # Channel captured at scan start (the combo is disabled while scanning).
        self._ch: int = default_ch

        # Scan state — pulse-primary
        self._n: int                          = 20
        self._half_um: float                  = 250.0
        self._center_pulse: int               = 0
        self._pulses_rel: np.ndarray | None   = None  # relative pulse offsets
        self._intensity:   np.ndarray | None  = None  # transmitted / incident
        self._transmitted: np.ndarray | None  = None
        self._incident:    np.ndarray | None  = None
        self._scan_speed:  str                = "H"
        self._scan_settle_ms: int             = 100
        self._scan_start_time: datetime | None = None
        self._fit_result: dict | None         = None
        self._suggested_pulse: int | None     = None

        self._setup_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        param_scroll = QScrollArea()
        param_scroll.setWidgetResizable(True)
        param_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        param_scroll.setFixedWidth(280)
        param_scroll.setWidget(self._build_param_panel())
        root.addWidget(param_scroll, 0)
        root.addWidget(self._build_plot_area(), 1)
        self._update_axis_labels()
        self._update_scan_preview()

    def _build_param_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Channel selection ────────────────────────────────────────────
        chsel_grp = QGroupBox(tr("Channel Selection"))
        chsel_lay = QVBoxLayout(chsel_grp)
        chsel_lay.addWidget(QLabel(tr("Channel:")))
        self._ch_combo = _no_wheel(QComboBox())
        self._ch_combo.addItems([f"Ch{c}" for c in CHANNEL_CHOICES])
        self._ch_combo.setCurrentIndex(CHANNEL_CHOICES.index(self._default_ch))
        chsel_lay.addWidget(self._ch_combo)
        layout.addWidget(chsel_grp)
        self._ch_combo.currentIndexChanged.connect(self._on_channel_selection_changed)

        # ── Scan parameters ──────────────────────────────────────────────
        self._scan_grp = QGroupBox(tr("Ch{ch} Scan", ch=self._selected_channel()))
        scan_lay = QVBoxLayout(self._scan_grp)
        scan_lay.addWidget(QLabel(tr("± range (µm):")))
        self._half_um_spin = _no_wheel(QDoubleSpinBox())
        self._half_um_spin.setRange(0.5, 5_000.0)
        self._half_um_spin.setValue(250.0)
        self._half_um_spin.setSuffix(" µm")
        self._half_um_spin.setSingleStep(10.0)
        scan_lay.addWidget(self._half_um_spin)
        scan_lay.addWidget(QLabel(tr("Grid points:")))
        self._grid_n_spin = _no_wheel(QSpinBox())
        self._grid_n_spin.setRange(2, 500)
        self._grid_n_spin.setValue(20)
        scan_lay.addWidget(self._grid_n_spin)
        layout.addWidget(self._scan_grp)

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
        self._accum_spin.setValue(1)
        self._accum_spin.setSingleStep(1)
        accum_lay.addWidget(self._accum_spin)
        accum_lay.addStretch()
        layout.addWidget(accum_grp)

        # ── Scan preview — range and step in pulses ──────────────────────
        self._scan_preview_label = QLabel()
        self._scan_preview_label.setWordWrap(True)
        self._scan_preview_label.setStyleSheet("font-size: 10px; color: #888;")
        layout.addWidget(self._scan_preview_label)

        for spin in (self._half_um_spin, self._grid_n_spin):
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

        layout.addStretch()

        # ── Fitting ──────────────────────────────────────────────────────
        fit_grp = QGroupBox(tr("Fitting"))
        fit_lay = QVBoxLayout(fit_grp)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel(tr("Model:")))
        self._fit_model_combo = _no_wheel(QComboBox())
        self._fit_model_combo.addItems(["Gaussian", "Aperture (erf)"])
        self._fit_model_combo.currentTextChanged.connect(self._on_fit_model_changed)
        model_row.addWidget(self._fit_model_combo)
        fit_lay.addLayout(model_row)
        self._fit_label = QLabel("—")
        self._fit_label.setWordWrap(True)
        fit_lay.addWidget(self._fit_label)

        self._goto_btn = QPushButton(tr("Go to fitted center"))
        self._goto_btn.setEnabled(False)
        self._goto_btn.setStyleSheet(
            "QPushButton:enabled { border: 2px solid #27ae60; font-weight: bold; font-size: 15px; }"
        )
        self._goto_btn.clicked.connect(self._on_goto_fitted)
        fit_lay.addWidget(self._goto_btn)

        layout.addWidget(fit_grp)
        return panel

    def _build_plot_area(self) -> QWidget:
        self._glw = pg.GraphicsLayoutWidget()

        # Bottom axis: absolute stage pulse values. Top axis: µm from centre.
        self._bottom_axis = _PulseAxisItem("bottom", center_pulse=0)
        self._top_axis    = _MicronAxisItem("top",   um_per_pulse=1.0)

        self._plot = self._glw.addPlot(
            row=0, col=0,
            title=tr("Intensity Profile"),
            axisItems={"bottom": self._bottom_axis, "top": self._top_axis},
        )
        self._plot.showAxis("top")
        self._plot.setLabel("left", tr("Intensity"))

        # Fitted first (renders behind the data points)
        self._curve_fit = self._plot.plot(pen=pg.mkPen("r", width=2))
        # Observed: black circles on top
        self._curve_data = self._plot.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=pg.mkBrush(0, 0, 0),
            symbolPen=pg.mkPen((80, 80, 80), width=1),
        )

        cross_pen = pg.mkPen("r", width=2, style=Qt.PenStyle.DashLine)
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=cross_pen)
        self._plot.addItem(self._vline)
        self._vline.setVisible(False)

        # No mouse interaction on the spatial (x) axis; keep it simple.
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()

        return self._glw

    # ── Channel selection ────────────────────────────────────────────────────

    def _selected_channel(self) -> int:
        return CHANNEL_CHOICES[self._ch_combo.currentIndex()]

    def _on_channel_selection_changed(self) -> None:
        ch = self._selected_channel()
        self._scan_grp.setTitle(tr("Ch{ch} Scan", ch=ch))
        self._update_scan_preview()
        self._update_axis_labels()

    def _update_axis_labels(self) -> None:
        ch = self._selected_channel()
        self._plot.setLabel("bottom", tr("Ch{ch} [pulse]", ch=ch))
        self._plot.setLabel("top",    tr("Ch{ch} [µm from centre]", ch=ch))
        self._top_axis.um_per_pulse = um_per_pulse(ch)

    def _update_scan_preview(self) -> None:
        ch      = self._selected_channel()
        half_um = self._half_um_spin.value()
        n       = self._grid_n_spin.value()
        half_p  = half_um / um_per_pulse(ch)
        step_p  = (2.0 * half_p / (n - 1)) if n > 1 else 0.0
        self._scan_preview_label.setText(
            tr("Ch{ch}: ±{half:.1f} pulses, step {step:.2f} p", ch=ch, half=half_p, step=step_p)
        )

    # ── Fit model change → refit ──────────────────────────────────────────────

    def _on_fit_model_changed(self) -> None:
        if (
            self._intensity is not None
            and np.any(~np.isnan(self._intensity))
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

        ch = self._selected_channel()
        try:
            self._center_pulse = int(self._controller.get_ch_pos(ch))
        except Exception as e:
            QMessageBox.warning(self, tr("Error"), tr("Cannot read current position:\n{error}", error=e))
            return

        self._ch = ch
        self._bottom_axis.center_pulse = self._center_pulse
        self._update_axis_labels()

        self._n       = self._grid_n_spin.value()
        self._half_um = self._half_um_spin.value()
        speed = next(
            btn.property("speed_val")
            for btn in self._speed_grp.buttons()
            if btn.isChecked()
        )

        # Relative pulse array (integer offsets from scan centre)
        umpp   = um_per_pulse(ch)
        half_p = self._half_um / umpp
        self._pulses_rel = np.round(
            np.linspace(-half_p, half_p, self._n)
        ).astype(int)
        pulses_abs = (self._center_pulse + self._pulses_rel).tolist()

        self._intensity   = np.full(self._n, np.nan)
        self._transmitted = np.full(self._n, np.nan)
        self._incident    = np.full(self._n, np.nan)
        self._scan_speed     = speed
        self._scan_settle_ms = self._settle_spin.value()
        self._scan_start_time = datetime.now()
        self._fit_result = None

        # Fixed x-range in pulse units, one half-step of padding each side.
        rp   = self._pulses_rel
        step = (rp[-1] - rp[0]) / max(self._n - 1, 1)
        self._plot.setXRange(
            float(rp[0]) - step / 2, float(rp[-1]) + step / 2, padding=0
        )
        self._plot.enableAutoRange(pg.ViewBox.YAxis)

        # Reset plot
        self._curve_data.setData([], [])
        self._curve_fit.setData([], [])
        self._vline.setVisible(False)
        self._suggested_pulse = None
        self._goto_btn.setEnabled(False)
        self._fit_label.setText("—")  # non-linguistic placeholder

        # GPIB reader — mirror Free2DScanWindow behaviour.
        if self._gpib_reader is not None:
            reader: GpibReader = self._gpib_reader
        elif self._debug:
            # Slice the 2-D simulator along y = 0 (peak line) so the profile is a
            # clean 1-D Gaussian along the chosen channel.
            reader = GpibReaderSim(
                um_per_pulse_x=umpp,
                um_per_pulse_y=1.0,
                center_x_pulse=self._center_pulse,
                center_y_pulse=0,
                peak_offset_y_um=0.0,
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

        self._scan_worker = Scan1DWorker(
            controller   = self._controller,
            gpib_reader  = reader,
            ch           = ch,
            pulses       = pulses_abs,
            center       = self._center_pulse,
            speed        = speed,
            settle_ms    = self._settle_spin.value(),
            accumulation = self._accum_spin.value(),
        )
        self._scan_worker.point_measured.connect(self._on_point_measured)
        self._scan_worker.scan_completed.connect(self._on_scan_completed)
        self._scan_worker.scan_aborted.connect(self._on_scan_aborted)
        self._scan_worker.status_message.connect(self._status_label.setText)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._ch_combo.setEnabled(False)
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
        self._ch_combo.setEnabled(True)
        self._status_label.setText(tr("EMERGENCY STOP — AESTP sent."))

    # ── Data reception ───────────────────────────────────────────────────────

    @pyqtSlot(int, float, float)
    def _on_point_measured(
        self, col: int, transmitted: float, incident: float
    ) -> None:
        intensity = transmitted / incident if incident > 0.0 else transmitted
        self._intensity[col]   = intensity
        self._transmitted[col] = transmitted
        self._incident[col]    = incident
        self._curve_data.setData(self._pulses_rel.astype(float), self._intensity)

    # ── Scan completion ───────────────────────────────────────────────────────

    def _on_scan_completed(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._ch_combo.setEnabled(True)
        self._status_label.setText(tr("Scan complete. Running fit…"))
        self._run_fit()
        if log_prefs.should_save(self._log_key):
            self._save_details("completed")
        play_current_sound()

    def _on_scan_aborted(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._ch_combo.setEnabled(True)
        if self._intensity is not None and np.any(~np.isnan(self._intensity)):
            self._status_label.setText(tr("Scan aborted. Fitting available data…"))
            self._run_fit()
        else:
            self._status_label.setText(tr("Scan aborted."))
        if log_prefs.should_save(self._log_key):
            self._save_details("aborted")

    # ── Fitting ───────────────────────────────────────────────────────────────

    def _run_fit(self) -> None:
        data = self._intensity
        if data is None or np.all(np.isnan(data)):
            self._status_label.setText(tr("No data available for fitting."))
            return

        ch    = self._ch
        model = self._fit_model_combo.currentText()
        xp    = self._pulses_rel.astype(float)

        self._curve_data.setData(xp, data)

        # Fit only the measured points (a partial scan leaves a NaN tail).
        valid = ~np.isnan(data)
        res = fit_profile_1d(xp[valid], data[valid], model)

        if res is None:
            self._fit_result = {f"ch{ch}": {"fit_ok": False}}
            self._suggested_pulse = None
            self._goto_btn.setEnabled(False)
            self._vline.setVisible(False)
            self._fit_label.setText(tr("Ch{ch}:  fit failed", ch=ch))
            self._status_label.setText(tr("Fit failed."))
            return

        center_rel = res.center
        self._curve_fit.setData(res.curve_x, res.curve_y)
        abs_p = self._center_pulse + round(center_rel)
        self._suggested_pulse = abs_p
        self._fit_label.setText(
            tr("Ch{ch}:  abs={abs_pulse} pulses\n"
               "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)",
               ch=ch, abs_pulse=abs_p, rel=center_rel,
               width_kind=res.width_kind, width=res.width)
        )
        width_key = "sigma_pulse" if res.model == "gaussian" else "width_pulse"
        self._fit_result = {
            f"ch{ch}": {
                "fit_ok": True,
                "model": res.model,
                "center_abs_pulse": abs_p,
                "center_rel_pulse": round(center_rel, 3),
                width_key: round(res.width, 3),
            }
        }

        self._vline.setPos(center_rel)
        self._vline.setVisible(True)
        self._goto_btn.setEnabled(True)
        self._status_label.setText(tr("Fit complete."))

    # ── Details save ──────────────────────────────────────────────────────────

    def _save_details(self, outcome: str) -> None:
        """Save scan arrays, metadata JSON, and plot PNG to the log directory."""
        if self._scan_start_time is None or self._intensity is None:
            return

        localdata = log_prefs.get_app_dir(self._log_key)
        ts   = self._scan_start_time.strftime("%Y%m%d_%H%M%S")
        stem = localdata / ts

        np.savez_compressed(
            str(stem) + ".npz",
            intensity   = self._intensity,
            transmitted = self._transmitted,
            incident    = self._incident,
            pulses_rel  = self._pulses_rel,
            pulses_abs  = self._center_pulse + self._pulses_rel,
        )

        meta = {
            "timestamp":   self._scan_start_time.isoformat(),
            "outcome":     outcome,
            "scan_params": {
                "ch":            self._ch,
                "center_pulse":  self._center_pulse,
                "half_um":       self._half_um,
                "n":             self._n,
                "um_per_pulse":  um_per_pulse(self._ch),
                "speed":         self._scan_speed,
                "settle_ms":     self._scan_settle_ms,
            },
            "fit":         self._fit_result,
            "arrays_file": ts + ".npz",
            "plot_file":   ts + ".png",
        }
        with (stem.parent / (ts + ".json")).open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        pixmap = self._glw.grab()
        pixmap.save(str(stem) + ".png")

        self._status_label.setText(
            tr("Saved → {path}  (.json / .npz / .png)", path=f"{localdata}/{ts}")
        )

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_goto_fitted(self) -> None:
        if self._suggested_pulse is None:
            return
        self._move_to(self._suggested_pulse, tr("Moving to fitted center…"))

    def _move_to(self, pulse: int, status_msg: str | None = None) -> None:
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

        self._move_worker = _Move1DWorker(self._controller, self._ch, pulse, parent=self)
        self._move_worker.move_completed.connect(self._on_move_completed)
        self._move_worker.move_failed.connect(self._on_move_failed)
        self._move_worker.start()

    @pyqtSlot()
    def _on_move_completed(self) -> None:
        self._status_label.setText(tr("Move complete."))
        if self._suggested_pulse is not None:
            self._goto_btn.setEnabled(True)

    @pyqtSlot(str)
    def _on_move_failed(self, err: str) -> None:
        self._status_label.setText(tr("Move failed: {error}", error=err))
        QMessageBox.warning(self, tr("Move Error"), err)
        if self._suggested_pulse is not None:
            self._goto_btn.setEnabled(True)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.abort()
            self._scan_worker.wait(3000)
        if self._move_worker is not None and self._move_worker.isRunning():
            self._move_worker.wait(3000)
        event.accept()


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    try:
        from utils.stage.control_stage_sim import PM16CControllerSim
    except ImportError:
        import os
        sys.path.insert(
            0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        from utils.stage.control_stage_sim import PM16CControllerSim

    app  = QApplication(sys.argv)
    ctrl = PM16CControllerSim()
    win  = Scan1DScanWindow(controller=ctrl, debug=True)
    win.show()
    sys.exit(app.exec())
