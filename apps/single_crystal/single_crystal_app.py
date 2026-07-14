"""Single Crystal XRD Oscillation Scan window.

Rotates Ch11 (rotation stage) over a user-defined angular range while
acquiring one Rad-icon 2022 frame per step.  Sub-pulse SPDL commands keep
Ch11 sweeping continuously during each exposure.

Each frame is saved as an uncompressed TIFF with JSON metadata (Tag 270)
containing the omega start angle, step, exposure, etc.  A CSV log with
one row per frame is written to the save directory on completion.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

try:
    from ..Rad_icon_2022.radicon_backend import (
        RadiconBackend, RadiconError,
        XrdOscillationWorker, _DEG_PER_PULSE_CH11,
    )
    from ..Rad_icon_2022.radicon_ui import (
        _save_tiff, _read_tiff_metadata,
        _parse_defect_file, _build_defect_mask, _apply_defect_correction,
        _ImageLabel, _DEFAULT_DEFECT_FILE,
    )
except ImportError:
    import sys as _sys
    _pkg = str(Path(__file__).parent.parent.parent)
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from apps.Rad_icon_2022.radicon_backend import (
        RadiconBackend, RadiconError,
        XrdOscillationWorker, _DEG_PER_PULSE_CH11,
    )
    from apps.Rad_icon_2022.radicon_ui import (
        _save_tiff, _read_tiff_metadata,
        _parse_defect_file, _build_defect_mask, _apply_defect_correction,
        _ImageLabel, _DEFAULT_DEFECT_FILE,
    )

try:
    from settings.i18n import tr
except ImportError:
    import sys as _sys
    _pkg = str(Path(__file__).parent.parent.parent)
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.i18n import tr

_HERE      = Path(__file__).parent
_LOCALDATA = _HERE / "__localdata"
_PREFS_FILE = _LOCALDATA / "prefs.json"


def _deg_to_pulse(deg: float) -> int:
    return round(deg / _DEG_PER_PULSE_CH11)


def _pulse_to_deg(pulse: int) -> float:
    return pulse * _DEG_PER_PULSE_CH11


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SingleCrystalWindow(QtWidgets.QMainWindow):

    def __init__(
        self,
        backend: RadiconBackend,
        controller,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Single Crystal Measurements (XRD Oscillation)"))
        self._backend    = backend
        self._controller = controller
        self._prefs      = self._load_prefs()

        self._worker: XrdOscillationWorker | None = None
        self._scan_id: str = ""
        self._csv_path: Path | None = None
        self._csv_rows: list[dict] = []

        # image / correction state
        self._img_arr: np.ndarray | None = None
        self._dark_img: np.ndarray | None = None
        self._dark_path: Path | None = None
        self._dark_exposure_ms: int | None = None
        self._dark_flip_v: bool | None = None
        self._dark_flip_h: bool | None = None
        self._defect_mask: np.ndarray | None = None
        self._defect_file_path: Path | None = None

        self._build_ui()
        self._update_preview()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self._scan_banner = QtWidgets.QLabel(tr("Idle"))
        self._scan_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scan_banner.setStyleSheet(
            "font-size: 18px; font-weight: bold; padding: 8px; border-radius: 4px;"
            "background: #2a2a2a; color: #888888;"
        )
        self._scan_banner.setFixedHeight(46)
        outer.addWidget(self._scan_banner)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_param_panel())
        splitter.addWidget(self._build_image_panel())
        splitter.setSizes([380, 700])
        outer.addWidget(splitter, 1)

        self.resize(1120, 820)

    def _build_param_panel(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QtWidgets.QWidget()
        root  = QtWidgets.QVBoxLayout(inner)
        root.setSpacing(8)
        scroll.setWidget(inner)

        # ── Oscillation parameters ──────────────────────────────────────
        osc_box = QtWidgets.QGroupBox(tr("Oscillation (Ch11)"))
        osc_form = QtWidgets.QFormLayout(osc_box)

        self._min_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._min_spin.setRange(-720.0, 720.0)
        self._min_spin.setDecimals(3)
        self._min_spin.setSuffix(" deg")
        self._min_spin.setSingleStep(0.5)
        self._min_spin.setValue(self._prefs.get("min_deg", -5.0))
        osc_form.addRow(tr("Min angle:"), self._min_spin)

        self._max_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._max_spin.setRange(-720.0, 720.0)
        self._max_spin.setDecimals(3)
        self._max_spin.setSuffix(" deg")
        self._max_spin.setSingleStep(0.5)
        self._max_spin.setValue(self._prefs.get("max_deg", 30.0))
        osc_form.addRow(tr("Max angle:"), self._max_spin)

        self._step_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._step_spin.setRange(_DEG_PER_PULSE_CH11, 180.0)
        self._step_spin.setDecimals(3)
        self._step_spin.setSuffix(" deg")
        self._step_spin.setSingleStep(0.1)
        self._step_spin.setValue(self._prefs.get("step_deg", 0.5))
        osc_form.addRow(tr("Step:"), self._step_spin)

        self._preview_label = QtWidgets.QLabel()
        self._preview_label.setStyleSheet("color: gray; font-size: 11px;")
        self._preview_label.setWordWrap(True)
        osc_form.addRow("", self._preview_label)

        root.addWidget(osc_box)

        # ── Detector ────────────────────────────────────────────────────
        det_box  = QtWidgets.QGroupBox(tr("Detector settings"))
        det_form = QtWidgets.QFormLayout(det_box)

        binning_label = tr("2 × 2") if self._backend.width < 2000 else tr("None")
        info = QtWidgets.QLabel(
            tr("{width} × {height} px  (binning: {binning})",
               width=self._backend.width, height=self._backend.height, binning=binning_label)
        )
        info.setStyleSheet("color: gray;")
        det_form.addRow(tr("Resolution:"), info)

        self._exp_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._exp_spin.setRange(0.001, 60.0)
        self._exp_spin.setDecimals(3)
        self._exp_spin.setSuffix(" s")
        self._exp_spin.setSingleStep(0.5)
        self._exp_spin.setValue(self._prefs.get("exposure_s", 1.0))
        det_form.addRow(tr("Exposure time:"), self._exp_spin)

        flip_w = QtWidgets.QWidget()
        flip_h = QtWidgets.QHBoxLayout(flip_w)
        flip_h.setContentsMargins(0, 0, 0, 0)
        self._flip_v_chk = QtWidgets.QCheckBox(tr("Vertical"))
        self._flip_v_chk.setChecked(self._prefs.get("flip_v", True))
        self._flip_h_chk = QtWidgets.QCheckBox(tr("Horizontal"))
        self._flip_h_chk.setChecked(self._prefs.get("flip_h", False))
        flip_h.addWidget(self._flip_v_chk)
        flip_h.addWidget(self._flip_h_chk)
        flip_h.addStretch()
        det_form.addRow(tr("Flip:"), flip_w)

        root.addWidget(det_box)

        # ── Corrections ─────────────────────────────────────────────────
        corr_box  = QtWidgets.QGroupBox(tr("Corrections"))
        corr_vbox = QtWidgets.QVBoxLayout(corr_box)

        # Dark
        self._dark_chk = QtWidgets.QCheckBox(tr("Dark-current correction"))
        self._dark_chk.setChecked(self._prefs.get("dark_enabled", True))
        corr_vbox.addWidget(self._dark_chk)

        dark_row = QtWidgets.QHBoxLayout()
        self._dark_edit = QtWidgets.QLineEdit()
        self._dark_edit.setReadOnly(True)
        self._dark_edit.setPlaceholderText(tr("No dark-current file selected"))
        dark_row.addWidget(self._dark_edit)
        dark_browse = QtWidgets.QPushButton(tr("Browse..."))
        dark_browse.setFixedWidth(60)
        dark_browse.clicked.connect(self._browse_dark_file)
        dark_row.addWidget(dark_browse)
        corr_vbox.addLayout(dark_row)

        self._dark_status = QtWidgets.QLabel()
        self._dark_status.setStyleSheet("color: gray; font-size: 11px;")
        self._dark_status.setWordWrap(True)
        corr_vbox.addWidget(self._dark_status)

        # Defect
        defect_row1 = QtWidgets.QHBoxLayout()
        self._defect_chk = QtWidgets.QCheckBox(tr("Pixel-defect correction (median)"))
        self._defect_chk.setChecked(self._prefs.get("defect_enabled", True))
        defect_row1.addWidget(self._defect_chk)
        self._defect_kernel_combo = _no_wheel(QtWidgets.QComboBox())
        self._defect_kernel_combo.addItems(["3×3", "4×4", "5×5", "6×6"])
        self._defect_kernel_combo.setCurrentText(self._prefs.get("defect_kernel", "3×3"))
        self._defect_kernel_combo.setEnabled(self._defect_chk.isChecked())
        defect_row1.addWidget(self._defect_kernel_combo)
        defect_row1.addStretch()
        corr_vbox.addLayout(defect_row1)

        defect_row2 = QtWidgets.QHBoxLayout()
        self._defect_edit = QtWidgets.QLineEdit()
        self._defect_edit.setReadOnly(True)
        self._defect_edit.setPlaceholderText(tr("No defect file selected"))
        defect_row2.addWidget(self._defect_edit)
        defect_browse = QtWidgets.QPushButton(tr("Browse..."))
        defect_browse.setFixedWidth(60)
        defect_browse.clicked.connect(self._browse_defect_file)
        defect_row2.addWidget(defect_browse)
        corr_vbox.addLayout(defect_row2)

        self._defect_status = QtWidgets.QLabel()
        self._defect_status.setStyleSheet("color: gray; font-size: 11px;")
        corr_vbox.addWidget(self._defect_status)

        self._defect_chk.toggled.connect(self._defect_kernel_combo.setEnabled)
        root.addWidget(corr_box)

        # ── Save directory ───────────────────────────────────────────────
        save_box = QtWidgets.QGroupBox(tr("Save directory"))
        save_row = QtWidgets.QHBoxLayout(save_box)
        self._dir_edit = QtWidgets.QLineEdit(
            self._prefs.get("save_dir", str(Path.home()))
        )
        save_row.addWidget(self._dir_edit)
        dir_browse = QtWidgets.QPushButton(tr("Browse..."))
        dir_browse.setFixedWidth(60)
        dir_browse.clicked.connect(self._browse_save_dir)
        save_row.addWidget(dir_browse)
        root.addWidget(save_box)

        # ── Buttons ──────────────────────────────────────────────────────
        btn_box = QtWidgets.QWidget()
        btn_lay = QtWidgets.QVBoxLayout(btn_box)
        btn_lay.setSpacing(4)

        self._start_btn = QtWidgets.QPushButton(tr("Start Scan"))
        self._start_btn.setFixedHeight(36)
        self._start_btn.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._start_btn.clicked.connect(self._on_start)
        btn_lay.addWidget(self._start_btn)

        self._stop_btn = QtWidgets.QPushButton(tr("Stop"))
        self._stop_btn.setFixedHeight(32)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_lay.addWidget(self._stop_btn)

        self._estop_btn = QtWidgets.QPushButton(tr("Emergency Stop"))
        self._estop_btn.setFixedHeight(32)
        self._estop_btn.setStyleSheet(
            "background-color: #c0392b; color: white; font-weight: bold;"
        )
        self._estop_btn.clicked.connect(self._on_emergency_stop)
        btn_lay.addWidget(self._estop_btn)

        root.addWidget(btn_box)

        # ── Progress ─────────────────────────────────────────────────────
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        root.addWidget(self._progress_bar)

        self._status_label = QtWidgets.QLabel(tr("Idle"))
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_label)

        root.addStretch()
        inner.setMinimumWidth(360)

        # Wire live-preview update
        for w in (self._min_spin, self._max_spin, self._step_spin, self._exp_spin):
            w.valueChanged.connect(self._update_preview)

        # Re-evaluate dark warning when exposure or flip settings change
        self._exp_spin.valueChanged.connect(lambda _: self._refresh_dark_warning())
        self._flip_v_chk.toggled.connect(lambda _: self._refresh_dark_warning())
        self._flip_h_chk.toggled.connect(lambda _: self._refresh_dark_warning())

        # Terminal logging for parameter changes
        self._exp_spin.valueChanged.connect(
            lambda v: _log.info("Exposure time changed: %.3f s (%d ms)", v, max(1, round(v * 1000)))
        )
        self._dark_chk.toggled.connect(
            lambda checked: _log.info("Dark correction: %s", "enabled" if checked else "disabled")
        )
        self._defect_chk.toggled.connect(
            lambda checked: _log.info("Defect correction: %s", "enabled" if checked else "disabled")
        )

        # Auto-load defect file
        saved_defect = self._prefs.get("defect_file", "")
        try:
            if saved_defect and Path(saved_defect).exists():
                self._load_defect_file(Path(saved_defect))
            elif _DEFAULT_DEFECT_FILE.exists():
                self._load_defect_file(_DEFAULT_DEFECT_FILE)
        except Exception as exc:
            self._defect_status.setText(tr("Load error: {error}", error=exc))

        return scroll

    def _build_image_panel(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setStyleSheet("background: #111;")
        vbox = QtWidgets.QVBoxLayout(w)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(4)

        self._img_label = _ImageLabel()
        vbox.addWidget(self._img_label, 1)

        # Min/Max sliders
        slider_w = QtWidgets.QWidget()
        slider_w.setStyleSheet("background: #1e1e1e; color: #ccc; font-size: 11px;")
        sg = QtWidgets.QGridLayout(slider_w)
        sg.setContentsMargins(6, 4, 6, 4)
        sg.setSpacing(4)

        sg.addWidget(QtWidgets.QLabel(tr("Min")), 0, 0)
        self._min_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._min_slider.setRange(0, 65535)
        self._min_slider.setValue(0)
        sg.addWidget(self._min_slider, 0, 1)
        self._min_val_spin = _no_wheel(QtWidgets.QSpinBox())
        self._min_val_spin.setRange(0, 65535)
        self._min_val_spin.setFixedWidth(68)
        sg.addWidget(self._min_val_spin, 0, 2)

        sg.addWidget(QtWidgets.QLabel(tr("Max")), 1, 0)
        self._max_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._max_slider.setRange(0, 65535)
        self._max_slider.setValue(65535)
        sg.addWidget(self._max_slider, 1, 1)
        self._max_val_spin = _no_wheel(QtWidgets.QSpinBox())
        self._max_val_spin.setRange(0, 65535)
        self._max_val_spin.setValue(65535)
        self._max_val_spin.setFixedWidth(68)
        sg.addWidget(self._max_val_spin, 1, 2)

        auto_btn = QtWidgets.QPushButton(tr("Auto"))
        auto_btn.setFixedWidth(48)
        auto_btn.clicked.connect(self._auto_levels)
        sg.addWidget(auto_btn, 0, 3, 2, 1)

        self._min_slider.valueChanged.connect(self._min_val_spin.setValue)
        self._min_val_spin.valueChanged.connect(self._min_slider.setValue)
        self._max_slider.valueChanged.connect(self._max_val_spin.setValue)
        self._max_val_spin.valueChanged.connect(self._max_slider.setValue)
        self._min_slider.valueChanged.connect(self._render_preview)
        self._max_slider.valueChanged.connect(self._render_preview)

        vbox.addWidget(slider_w)

        self._img_info = QtWidgets.QLabel("—")
        self._img_info.setStyleSheet("color: #aaa; font-size: 11px; padding: 2px 4px; background: #1e1e1e;")
        self._img_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_info.setWordWrap(True)
        vbox.addWidget(self._img_info)

        return w

    # ------------------------------------------------------------------
    # Preview label (step count + estimated time)
    # ------------------------------------------------------------------

    def _update_preview(self) -> None:
        min_deg  = self._min_spin.value()
        max_deg  = self._max_spin.value()
        step_deg = self._step_spin.value()
        exp_s    = self._exp_spin.value()

        if max_deg <= min_deg or step_deg <= 0:
            self._preview_label.setText(tr("⚠ Invalid range or step"))
            return

        n_steps     = max(1, round((max_deg - min_deg) / step_deg))
        step_pulses = _deg_to_pulse(step_deg)
        total_s     = n_steps * exp_s
        m, s        = divmod(int(total_s), 60)

        warn = ""
        if step_pulses < 1:
            warn = tr("\n⚠ Step is too small (min 0.004 deg)")

        self._preview_label.setText(
            tr("{n_steps} steps  |  step size: {step_pulses} pulse\n"
               "Estimated time: {m}m{s:02d}s{warn}",
               n_steps=n_steps, step_pulses=step_pulses, m=m, s=s, warn=warn)
        )

    # ------------------------------------------------------------------
    # File browse
    # ------------------------------------------------------------------

    def _browse_save_dir(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, tr("Select save folder"), self._dir_edit.text() or str(Path.home())
        )
        if d:
            self._dir_edit.setText(d)
            self._save_prefs()

    def _browse_dark_file(self) -> None:
        start = str(self._dark_path.parent) if self._dark_path else self._dir_edit.text()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select dark-current file"), start,
            "TIFF images (*.tif *.tiff);;All files (*)"
        )
        if not path:
            return
        try:
            import cv2
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None or img.ndim != 2:
                raise ValueError(tr("Please select a grayscale TIFF"))
            self._dark_img  = img.astype(np.float64)
            self._dark_path = Path(path)
            meta = _read_tiff_metadata(path)
            self._dark_exposure_ms = meta.get("exposure_ms")
            self._dark_flip_v = meta.get("flip_v")
            self._dark_flip_h = meta.get("flip_h")
            self._dark_edit.setText(Path(path).name)
            _log.info(
                "Dark file loaded: %s  (exposure_ms=%s, flip_v=%s, flip_h=%s)",
                Path(path).name,
                self._dark_exposure_ms,
                self._dark_flip_v,
                self._dark_flip_h,
            )
            self._refresh_dark_warning()
            self._save_prefs()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Load error"), str(exc))

    def _refresh_dark_warning(self) -> None:
        if self._dark_img is None:
            return
        name = self._dark_path.name if self._dark_path else tr("unknown")
        current_ms = max(1, round(self._exp_spin.value() * 1000))
        warnings: list[str] = []

        if self._dark_exposure_ms is not None and self._dark_exposure_ms != current_ms:
            warnings.append(
                tr("Exposure mismatch (dark: {dark_ms} ms / current: {cur_ms} ms)",
                   dark_ms=self._dark_exposure_ms, cur_ms=current_ms)
            )
        if self._dark_flip_v is not None and self._dark_flip_v != self._flip_v_chk.isChecked():
            warnings.append(
                tr("Vertical flip mismatch (dark: {dark_v} / current: {cur_v})",
                   dark_v=self._dark_flip_v, cur_v=self._flip_v_chk.isChecked())
            )
        if self._dark_flip_h is not None and self._dark_flip_h != self._flip_h_chk.isChecked():
            warnings.append(
                tr("Horizontal flip mismatch (dark: {dark_h} / current: {cur_h})",
                   dark_h=self._dark_flip_h, cur_h=self._flip_h_chk.isChecked())
            )

        if warnings:
            self._dark_status.setText(
                tr("Loaded: {name}\n[Warning] {warning}", name=name, warning=" / ".join(warnings))
            )
            self._dark_status.setStyleSheet("color: orange; font-size: 11px;")
        else:
            suffix = tr("  ({ms} ms)", ms=self._dark_exposure_ms) if self._dark_exposure_ms else ""
            self._dark_status.setText(tr("Loaded: {name}{suffix}", name=name, suffix=suffix))
            self._dark_status.setStyleSheet("color: green; font-size: 11px;")

    def _browse_defect_file(self) -> None:
        start = str(self._defect_file_path.parent) if self._defect_file_path else self._dir_edit.text()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select pixel-defect file"), start,
            "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            self._load_defect_file(Path(path))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Defect file load error"), str(exc))

    def _load_defect_file(self, path: Path) -> None:
        binning = "2x2" if self._backend.width < 2000 else "1x1"
        defects = _parse_defect_file(
            str(path), binning,
            self._backend._h_blank,
            self._backend.width, self._backend.height,
        )
        self._defect_mask      = _build_defect_mask(defects, self._backend.height, self._backend.width)
        self._defect_file_path = path
        self._defect_edit.setText(path.name)
        self._defect_status.setText(tr("Defect pixels: {n} px", n=len(defects)))
        self._save_prefs()

    # ------------------------------------------------------------------
    # Scan control
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        # Validate inputs
        min_deg  = self._min_spin.value()
        max_deg  = self._max_spin.value()
        step_deg = self._step_spin.value()
        exp_s    = self._exp_spin.value()

        if max_deg <= min_deg:
            QtWidgets.QMessageBox.warning(self, tr("Input error"),
                                          tr("Max angle must be greater than Min angle."))
            return

        step_pulses = _deg_to_pulse(step_deg)
        if step_pulses < 1:
            QtWidgets.QMessageBox.warning(
                self, tr("Input error"),
                tr("Step is too small (min {min_deg} deg = 1 pulse).", min_deg=_DEG_PER_PULSE_CH11)
            )
            return

        n_steps  = max(1, round((max_deg - min_deg) / step_deg))
        min_pulse = _deg_to_pulse(min_deg)
        exp_ms   = max(1, round(exp_s * 1000))

        save_dir = Path(self._dir_edit.text())
        if not save_dir.is_dir():
            QtWidgets.QMessageBox.warning(self, tr("Input error"), tr("Save directory does not exist."))
            return

        total_s = n_steps * exp_s
        m, s    = divmod(int(total_s), 60)

        reply = QtWidgets.QMessageBox.question(
            self, tr("Confirm Scan Start"),
            tr(
                "The scan will start with the following settings.\n\n"
                "  Angle range:  {min_deg:.3f} → {max_deg:.3f} deg\n"
                "  Step:  {step_deg:.3f} deg  ({step_pulses} pulse)\n"
                "  Frame count: {n_steps}\n"
                "  Exposure time:  {exp_s:.3f} s\n"
                "  Estimated time:  {m}m{s:02d}s\n\n"
                "Ch11 will first move to {min_deg:.3f} deg ({min_pulse} pulse).\n"
                "Continue?",
                min_deg=min_deg, max_deg=max_deg, step_deg=step_deg, step_pulses=step_pulses,
                n_steps=n_steps, exp_s=exp_s, m=m, s=s, min_pulse=min_pulse,
            ),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self._save_prefs()
        self._scan_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._csv_rows = []
        self._csv_path = save_dir / f"scan_{self._scan_id}.csv"

        _log.info(
            "Scan started: omega %.3f → %.3f deg, step %.3f deg (%d steps), "
            "exposure %d ms, save_dir=%s",
            min_deg, max_deg, step_deg, n_steps, exp_ms, save_dir,
        )

        self._worker = XrdOscillationWorker(
            backend     = self._backend,
            controller  = self._controller,
            min_pulse   = min_pulse,
            step_pulses = step_pulses,
            n_steps     = n_steps,
            exposure_ms = exp_ms,
            parent      = self,
        )
        self._worker.frame_acquired.connect(self._on_frame_acquired)
        self._worker.progress.connect(self._on_progress)
        self._worker.scan_finished.connect(self._on_scan_finished)
        self._worker.scan_aborted.connect(self._on_scan_aborted)
        self._worker.error.connect(self._on_scan_error)
        self._worker.overrun_warning.connect(self._on_overrun_warning)

        self._progress_bar.setRange(0, n_steps)
        self._progress_bar.setValue(0)
        self._set_busy(True)
        moving_msg = tr("Moving Ch11 to {min_deg:.3f} deg…", min_deg=min_deg)
        self._set_banner(moving_msg, "moving")
        self._status_label.setText(moving_msg)
        self._worker.start()

    def _on_stop(self) -> None:
        if self._worker:
            self._worker.abort()
            if self._controller:
                self._controller.normal_stop()
        self._stop_btn.setEnabled(False)
        self._status_label.setText(tr("Stopping…"))

    def _on_emergency_stop(self) -> None:
        if self._worker:
            self._worker.abort(emergency=True)
        if self._controller:
            self._controller.emergency_stop()
        self._set_busy(False)
        _log.warning("Emergency stop triggered (AESTP sent)")
        self._status_label.setText(tr("EMERGENCY STOP — AESTP sent."))
        self._set_banner(tr("EMERGENCY STOP — AESTP sent."), "error")

    # ------------------------------------------------------------------
    # Worker signals
    # ------------------------------------------------------------------

    def _on_frame_acquired(self, step_i: int, omega_start_deg: float, frame: np.ndarray) -> None:
        step_deg = self._step_spin.value()
        omega_end_deg = omega_start_deg + step_deg
        exp_ms = max(1, round(self._exp_spin.value() * 1000))

        frame = self._apply_flip(frame)
        frame, _ = self._dark_correct(frame)
        frame, _ = self._defect_correct(frame)

        save_dir = Path(self._dir_edit.text())
        save_dir.mkdir(parents=True, exist_ok=True)
        suffix   = "2x2" if self._backend.width < 2000 else "1x1"
        fname    = save_dir / f"xrd_{self._scan_id}_{step_i + 1:04d}_{suffix}.tif"

        meta = {
            "image_type":     "xrd_oscillation",
            "scan_id":        self._scan_id,
            "step_index":     step_i + 1,
            "omega_start_deg": round(omega_start_deg, 4),
            "omega_end_deg":  round(omega_end_deg, 4),
            "step_deg":       round(step_deg, 4),
            "exposure_ms":    exp_ms,
            "binning":        "2x2" if self._backend.width < 2000 else "1x1",
            "flip_v":         self._flip_v_chk.isChecked(),
            "flip_h":         self._flip_h_chk.isChecked(),
            "dark_corrected": self._dark_chk.isChecked() and self._dark_img is not None,
            "dark_source":    self._dark_path.name if self._dark_path else None,
            "defect_corrected": self._defect_chk.isChecked() and self._defect_mask is not None,
            "detector":       "Rad-icon 2022",
            "beamline":       "BL-18C",
            "datetime":       datetime.now().isoformat(timespec="seconds"),
        }
        _save_tiff(fname, frame, meta)

        self._csv_rows.append({
            "step_index":      step_i + 1,
            "omega_start_deg": round(omega_start_deg, 4),
            "omega_end_deg":   round(omega_end_deg, 4),
            "filename":        fname.name,
            "datetime":        meta["datetime"],
            "exposure_ms":     exp_ms,
        })
        self._display_image(frame, fname.name)

    def _on_progress(self, done: int, total: int, omega_deg: float) -> None:
        self._progress_bar.setValue(done)
        exp_s    = self._exp_spin.value()
        remain_s = (total - done) * exp_s
        m, s     = divmod(int(remain_s), 60)
        detail = tr(
            "Scanning… {done}/{total}  |  current: {omega:.3f} deg  |  remaining: {m}m{s:02d}s",
            done=done, total=total, omega=omega_deg, m=m, s=s,
        )
        self._status_label.setText(detail)
        self._set_banner(
            tr(
                "Capturing  {done}/{total}  frames  |  {omega:.3f} deg  |  remaining {m}m{s:02d}s",
                done=done + 1, total=total, omega=omega_deg, m=m, s=s,
            ),
            "scanning",
        )

    def _on_scan_finished(self) -> None:
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._set_busy(False)
        self._write_csv()
        _log.info("Scan finished: %d frames saved → %s", len(self._csv_rows), self._dir_edit.text())
        msg = tr("Done: {n} frames saved  →  {dir}/", n=len(self._csv_rows), dir=Path(self._dir_edit.text()).name)
        self._status_label.setText(msg)
        self._set_banner(tr("Done  —  {n} frames saved", n=len(self._csv_rows)), "done")

    def _on_scan_aborted(self) -> None:
        self._set_busy(False)
        self._write_csv()
        _log.info("Scan aborted: %d frames saved", len(self._csv_rows))
        msg = tr("Aborted: {n} frames saved", n=len(self._csv_rows))
        self._status_label.setText(msg)
        self._set_banner(tr("Aborted  —  {n} frames saved", n=len(self._csv_rows)), "aborted")

    def _on_scan_error(self, msg: str) -> None:
        self._set_busy(False)
        self._write_csv()
        _log.error("Scan error: %s", msg)
        self._status_label.setText(tr("Error: {msg}", msg=msg))
        self._set_banner(tr("An error occurred"), "error")
        QtWidgets.QMessageBox.critical(self, tr("Scan Error"), msg)

    def _on_overrun_warning(self, step_i: int, overrun_s: float) -> None:
        self._status_label.setText(
            tr("[Warning] Step {n}: exposure overrun {overrun:.2f}s", n=step_i + 1, overrun=overrun_s)
        )

    # ------------------------------------------------------------------
    # Image processing
    # ------------------------------------------------------------------

    def _apply_flip(self, img: np.ndarray) -> np.ndarray:
        v = self._flip_v_chk.isChecked()
        h = self._flip_h_chk.isChecked()
        if v and h:
            return img[::-1, ::-1]
        if v:
            return img[::-1, :]
        if h:
            return img[:, ::-1]
        return img

    def _dark_correct(self, img: np.ndarray) -> tuple[np.ndarray, str]:
        if not self._dark_chk.isChecked() or self._dark_img is None:
            return img, ""
        if img.shape != self._dark_img.shape:
            return img, tr("Dark-current image size does not match ({dark_shape} vs {img_shape})",
                            dark_shape=self._dark_img.shape, img_shape=img.shape)
        corrected = (img.astype(np.float64) - self._dark_img).clip(0, 65535).astype(np.uint16)
        return corrected, ""

    def _defect_correct(self, img: np.ndarray) -> tuple[np.ndarray, str]:
        if not self._defect_chk.isChecked() or self._defect_mask is None:
            return img, ""
        if img.shape != self._defect_mask.shape:
            return img, tr("Defect mask size does not match ({mask_shape} vs {img_shape})",
                            mask_shape=self._defect_mask.shape, img_shape=img.shape)
        kernel = int(self._defect_kernel_combo.currentText()[0])
        return _apply_defect_correction(img, self._defect_mask, kernel), ""

    def _display_image(self, img: np.ndarray, filename: str = "") -> None:
        self._img_arr = img
        h, w = img.shape
        info = f"{w}×{h}  min={img.min()}  max={img.max()}  mean={img.mean():.0f}"
        if filename:
            info = f"{filename}  |  {info}"
        self._img_info.setText(info)
        self._render_preview()

    def _render_preview(self) -> None:
        if self._img_arr is None:
            return
        lo = self._min_slider.value()
        hi = self._max_slider.value()
        if hi <= lo:
            hi = lo + 1
        arr8 = ((self._img_arr.astype(np.float32) - lo) / (hi - lo) * 255).clip(0, 255).astype(np.uint8)
        h, w = arr8.shape
        qimg = QtGui.QImage(arr8.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8)
        self._img_label.setSourcePixmap(QtGui.QPixmap.fromImage(qimg))

    def _auto_levels(self) -> None:
        if self._img_arr is None:
            return
        self._min_slider.setValue(int(self._img_arr.min()))
        self._max_slider.setValue(int(self._img_arr.max()))

    # ------------------------------------------------------------------
    # CSV log
    # ------------------------------------------------------------------

    def _write_csv(self) -> None:
        if not self._csv_rows or self._csv_path is None:
            return
        try:
            with self._csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["step_index", "omega_start_deg", "omega_end_deg",
                                "filename", "datetime", "exposure_ms"],
                )
                writer.writeheader()
                writer.writerows(self._csv_rows)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Banner helper
    # ------------------------------------------------------------------

    def _set_banner(self, text: str, state: str = "idle") -> None:
        _styles = {
            "idle":     "background: #2a2a2a; color: #888888;",
            "moving":   "background: #1565c0; color: #ffffff;",
            "scanning": "background: #1565c0; color: #ffffff;",
            "done":     "background: #1b5e20; color: #ffffff;",
            "aborted":  "background: #e65100; color: #ffffff;",
            "error":    "background: #b71c1c; color: #ffffff;",
        }
        extra = _styles.get(state, _styles["idle"])
        self._scan_banner.setStyleSheet(
            f"font-size: 18px; font-weight: bold; padding: 8px; border-radius: 4px; {extra}"
        )
        self._scan_banner.setText(text)

    # ------------------------------------------------------------------
    # Busy state
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        enabled = not busy
        self._start_btn.setEnabled(enabled)
        self._stop_btn.setEnabled(busy)
        self._min_spin.setEnabled(enabled)
        self._max_spin.setEnabled(enabled)
        self._step_spin.setEnabled(enabled)
        self._exp_spin.setEnabled(enabled)
        self._flip_v_chk.setEnabled(enabled)
        self._flip_h_chk.setEnabled(enabled)
        self._dark_chk.setEnabled(enabled)
        self._defect_chk.setEnabled(enabled)
        self._dir_edit.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _load_prefs(self) -> dict:
        try:
            return json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_prefs(self) -> None:
        _LOCALDATA.mkdir(exist_ok=True)
        prefs = {
            "min_deg":       self._min_spin.value(),
            "max_deg":       self._max_spin.value(),
            "step_deg":      self._step_spin.value(),
            "exposure_s":    self._exp_spin.value(),
            "flip_v":        self._flip_v_chk.isChecked(),
            "flip_h":        self._flip_h_chk.isChecked(),
            "dark_enabled":  self._dark_chk.isChecked(),
            "defect_enabled": self._defect_chk.isChecked(),
            "defect_kernel": self._defect_kernel_combo.currentText(),
            "defect_file":   str(self._defect_file_path) if self._defect_file_path else "",
            "save_dir":      self._dir_edit.text(),
        }
        _PREFS_FILE.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._worker and self._worker.isRunning():
            reply = QtWidgets.QMessageBox.question(
                self, tr("Scan in Progress"),
                tr("A scan is in progress. Abort and close?"),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.abort()
            if self._controller:
                try:
                    self._controller.normal_stop()
                except Exception:
                    pass
            if not self._worker.wait(5000):
                QtWidgets.QMessageBox.warning(
                    self, tr("Still Stopping"),
                    tr("The scan has not finished stopping yet. Please wait and try closing again."),
                )
                event.ignore()
                return
        self._save_prefs()
        super().closeEvent(event)
