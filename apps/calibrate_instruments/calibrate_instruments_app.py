"""Calibrate Detector Geometry — guided, multi-detector-position pyFAI geometry
calibration for the Rad-icon 2022 detector.

See SPEC.md in this directory for the full design rationale.
"""
from __future__ import annotations

import json
import math
import pathlib
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal

try:
    from .calibrate_instruments_backend import (
        CalibrationPosition, ManualInitialParams, FreeParamStages,
        MultiPositionCalibrationWorker, PYFAI_AVAILABLE,
        build_initial_ai, calibrant_names,
        detect_binning, pixel_size_um_for_binning, RAD_ICON_PIXEL_SIZE_1X1_UM,
        detect_beam_center,
    )
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from apps.calibrate_instruments.calibrate_instruments_backend import (
        CalibrationPosition, ManualInitialParams, FreeParamStages,
        MultiPositionCalibrationWorker, PYFAI_AVAILABLE,
        build_initial_ai, calibrant_names,
        detect_binning, pixel_size_um_for_binning, RAD_ICON_PIXEL_SIZE_1X1_UM,
        detect_beam_center,
    )

try:
    from pyFAI.calibrant import get_calibrant as _get_calibrant
except ImportError:
    _get_calibrant = None

try:
    from utils.poni_io import write_poni
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from utils.poni_io import write_poni

try:
    from settings.i18n import tr
    from settings.pages.detector_calibration import remember_poni_path
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from settings.i18n import tr
    from settings.pages.detector_calibration import remember_poni_path

_HERE = pathlib.Path(__file__).parent
_LOCALDATA = _HERE / "__localdata"
_PREFS_FILE = _LOCALDATA / "calibrate_instruments_prefs.json"


def _no_wheel(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the page
    (this window is one long QScrollArea) never silently changes a value
    the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------

class _PositionImageView(QtWidgets.QWidget):
    """Grayscale image + extracted-control-point scatter overlay for one position."""

    beam_centre_picked = pyqtSignal(float, float)  # (x_px, y_px), right-click only

    def __init__(self, label: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if label:
            title = QtWidgets.QLabel(f"<b>{label}</b>")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title)
        self.plot = pg.PlotWidget(background="w")
        self.plot.setAspectLocked(True)
        self.plot.invertY(True)
        for axis in ("bottom", "left"):
            self.plot.getAxis(axis).setTextPen("k")
            self.plot.getAxis(axis).setPen("k")
        # Static full-image display only — no pan/zoom/scroll inside the view.
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.getViewBox().setMouseEnabled(x=False, y=False)
        self.plot.setMinimumHeight(220)
        self._img_item = pg.ImageItem()
        self.plot.addItem(self._img_item)
        self._scatter = pg.ScatterPlotItem(
            size=4, brush=pg.mkBrush(220, 40, 40, 200), pen=None,
        )
        self.plot.addItem(self._scatter)
        layout.addWidget(self.plot)
        self.plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)

    def _on_mouse_clicked(self, event) -> None:
        if event.button() != Qt.MouseButton.RightButton:
            return
        if self._img_item.image is None:
            return
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        view_pos = vb.mapSceneToView(event.scenePos())
        self.beam_centre_picked.emit(view_pos.x(), view_pos.y())

    def set_image(self, img: np.ndarray) -> None:
        self._img_item.setImage(img.T, autoLevels=True)
        height, width = img.shape[:2]
        self.plot.getViewBox().setLimits(xMin=0, xMax=width, yMin=0, yMax=height)
        self.plot.setRange(xRange=(0, width), yRange=(0, height), padding=0)

    def set_control_points(self, rows_cols: np.ndarray | None) -> None:
        if rows_cols is None or len(rows_cols) == 0:
            self._scatter.setData([], [])
            return
        self._scatter.setData(rows_cols[:, 1], rows_cols[:, 0])


class _AutoHeightStackedWidget(QtWidgets.QStackedWidget):
    """A QStackedWidget that sizes itself to the *current* page only.

    Plain QStackedWidget reserves enough height for its tallest page even
    while a shorter page is shown, leaving a lot of empty space (e.g. the
    one-line prm/poni browse pages next to the multi-row manual-entry form).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.currentChanged.connect(lambda _: self.updateGeometry())

    def sizeHint(self) -> QtCore.QSize:
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self) -> QtCore.QSize:
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()


class _PositionRowWidget(QtWidgets.QFrame):
    """One row (card) in the detector-position list: mgs entry + Take XRD / Load image."""

    take_xrd_requested  = pyqtSignal(object)   # emits self
    load_image_requested = pyqtSignal(object)  # emits self
    mgs_changed         = pyqtSignal()

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Raised)
        self.position = CalibrationPosition(label=label)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        name_label = QtWidgets.QLabel(f"<b>{label}</b>")
        name_label.setFixedWidth(170)
        layout.addWidget(name_label)

        mgs_label = QtWidgets.QLabel(tr("mgs (mm):"))
        mgs_label.setFixedWidth(70)
        layout.addWidget(mgs_label)
        self.mgs_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self.mgs_spin.setRange(-100_000.0, 100_000.0)
        self.mgs_spin.setDecimals(3)
        self.mgs_spin.setValue(0.0)
        self.mgs_spin.setMinimumWidth(110)
        self.mgs_spin.valueChanged.connect(self._on_mgs_value_changed)
        layout.addWidget(self.mgs_spin)

        self.take_btn = QtWidgets.QPushButton(tr("Take XRD"))
        self.take_btn.clicked.connect(lambda: self.take_xrd_requested.emit(self))
        layout.addWidget(self.take_btn)

        self.load_btn = QtWidgets.QPushButton(tr("Load image…"))
        self.load_btn.clicked.connect(lambda: self.load_image_requested.emit(self))
        layout.addWidget(self.load_btn)

        self.status_label = QtWidgets.QLabel(tr("not captured"))
        self.status_label.setStyleSheet("color: #888;")
        layout.addWidget(self.status_label, 1)

        # mgs defaults to 0.0 for every row, so seed the position from it —
        # otherwise the very first row would fail the "has mgs" check even
        # though the spinbox already shows a value the user could take at face value.
        self.position.mgs_mm = self.mgs_spin.value()

    def _on_mgs_value_changed(self, value: float) -> None:
        self.position.mgs_mm = value
        self.mgs_changed.emit()

    def mark_captured(self, ch9_pulse: int | None, n_points: int | None = None) -> None:
        if n_points is None:
            text = tr("✓ captured")
        else:
            text = tr("✓ {n} control points", n=n_points)
        if ch9_pulse is not None:
            text += f"   (Ch9@{ch9_pulse})"
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: green;")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class CalibrateInstrumentsWindow(QtWidgets.QWidget):
    """Guided multi-position pyFAI calibration for the Rad-icon 2022 detector.

    Can be opened standalone (from main.py) or as a Rad-icon sub-app. Either
    way, pass `get_radicon_window=` — a callable returning the live
    RadiconWindow instance if it is currently open, else None. It is used
    both to share flip settings (so calibration images use the same
    orientation as real measurements) and to require that the Rad-icon window
    be open (and its exposure configured there) before Take XRD is allowed.
    """

    def __init__(
        self,
        backend=None,
        controller=None,
        poni_state=None,
        get_radicon_window=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Calibrate Detector Geometry"))
        self.resize(1050, 850)

        self._backend            = backend
        self._controller         = controller
        self._poni_state         = poni_state
        self._get_radicon_window = get_radicon_window

        self._rows: list[_PositionRowWidget] = []
        self._image_views: dict[str, _PositionImageView] = {}
        self._position_counter = 0

        self._prm_path: pathlib.Path | None = None
        self._poni_path: pathlib.Path | None = None
        self._last_prm_dir  = ""
        self._last_poni_dir = ""
        self._last_save_dir = ""
        self._last_load_dir = ""

        self._worker: MultiPositionCalibrationWorker | None = None
        self._ai_result = None
        self._detected_binning: str | None = None
        self._detected_pixel_size_um: float | None = None
        self._previous_mode_idx = 2   # 0=prm, 1=poni, 2=manual (matches _manual_radio default checked)

        self._build_ui()
        self._load_prefs()
        self._add_position_row()
        self._add_position_row()

        self._ch9_timer = QtCore.QTimer(self)
        self._ch9_timer.timeout.connect(self._poll_ch9)
        if self._controller is not None:
            self._ch9_timer.start(300)

        if not PYFAI_AVAILABLE:
            self._log_append(tr("⚠ pyFAI is not installed — calibration cannot run."))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        root = QtWidgets.QVBoxLayout(content)
        root.setSpacing(10)

        scope_notice = QtWidgets.QLabel(tr(
            "⚠ This tool only works with images captured by the Rad-icon 2022 "
            "detector — the pixel size (99 µm at 1x1 / 198 µm at 2x2) is "
            "hardcoded for that specific detector and is not configurable."
        ))
        scope_notice.setWordWrap(True)
        scope_notice.setStyleSheet("color: #a05a00; font-weight: bold;")
        root.addWidget(scope_notice)

        root.addWidget(self._build_calibrant_group())
        root.addWidget(self._build_positions_group())
        root.addWidget(self._build_initial_source_group())
        root.addWidget(self._build_run_group())
        root.addWidget(self._build_results_group(), 1)

    def _build_calibrant_group(self) -> QtWidgets.QGroupBox:
        grp = QtWidgets.QGroupBox(tr("Calibrant"))
        row = QtWidgets.QHBoxLayout(grp)

        names = calibrant_names()
        self._calibrant_combo = _no_wheel(QtWidgets.QComboBox())
        self._calibrant_combo.setEditable(True)
        self._calibrant_combo.addItems(names if names else ["CeO2", "LaB6", "Si"])
        completer = QtWidgets.QCompleter(names if names else ["CeO2", "LaB6", "Si"], self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._calibrant_combo.setCompleter(completer)
        self._calibrant_combo.currentTextChanged.connect(self._on_calibrant_combo_changed)

        quick_group = QtWidgets.QButtonGroup(self)
        self._calibrant_quick_radios: dict[str, QtWidgets.QRadioButton] = {}
        for name in ("CeO2", "LaB6", "Si"):
            radio = QtWidgets.QRadioButton(name)
            radio.toggled.connect(
                lambda checked, n=name: checked and self._calibrant_combo.setCurrentText(n)
            )
            radio.toggled.connect(self._update_calibrant_combo_enabled)
            quick_group.addButton(radio)
            self._calibrant_quick_radios[name] = radio
            row.addWidget(radio)

        self._calibrant_other_radio = QtWidgets.QRadioButton(tr("Other:"))
        self._calibrant_other_radio.toggled.connect(self._update_calibrant_combo_enabled)
        quick_group.addButton(self._calibrant_other_radio)
        row.addWidget(self._calibrant_other_radio)
        row.addWidget(self._calibrant_combo, 1)

        idx = self._calibrant_combo.findText("CeO2")
        self._calibrant_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._calibrant_quick_radios["CeO2"].setChecked(True)
        self._update_calibrant_combo_enabled()
        return grp

    def _update_calibrant_combo_enabled(self, *_args) -> None:
        self._calibrant_combo.setEnabled(self._calibrant_other_radio.isChecked())

    def _on_calibrant_combo_changed(self, text: str) -> None:
        radio = self._calibrant_quick_radios.get(text)
        if radio is not None:
            radio.setChecked(True)
        else:
            self._calibrant_other_radio.setChecked(True)

    def _build_initial_source_group(self) -> QtWidgets.QGroupBox:
        grp = QtWidgets.QGroupBox(tr("Initial Parameters"))
        vbox = QtWidgets.QVBoxLayout(grp)

        self._binning_label = QtWidgets.QLabel(
            tr("Binning: not detected yet (capture or load Position 1's image)")
        )
        self._binning_label.setStyleSheet("color: #666;")
        vbox.addWidget(self._binning_label)

        radio_row = QtWidgets.QHBoxLayout()
        self._prm_radio    = QtWidgets.QRadioButton(tr("IPA prm file"))
        self._poni_radio   = QtWidgets.QRadioButton(tr("Existing poni file"))
        self._manual_radio = QtWidgets.QRadioButton(tr("Manual entry"))
        self._manual_radio.setChecked(True)
        mode_group = QtWidgets.QButtonGroup(self)
        for i, radio in enumerate((self._prm_radio, self._poni_radio, self._manual_radio)):
            mode_group.addButton(radio, i)
            radio_row.addWidget(radio)
        radio_row.addStretch()
        mode_group.idClicked.connect(self._on_mode_changed)
        vbox.addLayout(radio_row)

        self._init_stack = _AutoHeightStackedWidget()
        self._init_stack.addWidget(self._build_prm_page())
        self._init_stack.addWidget(self._build_poni_page())
        self._init_stack.addWidget(self._build_manual_page())
        self._init_stack.setCurrentIndex(2)
        vbox.addWidget(self._init_stack)
        return grp

    def _build_prm_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        self._prm_path_edit = QtWidgets.QLineEdit()
        self._prm_path_edit.setReadOnly(True)
        browse = QtWidgets.QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse_prm)
        row.addWidget(self._prm_path_edit, 1)
        row.addWidget(browse)
        return page

    def _build_poni_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        self._poni_path_edit = QtWidgets.QLineEdit()
        self._poni_path_edit.setReadOnly(True)
        browse = QtWidgets.QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse_poni)
        row.addWidget(self._poni_path_edit, 1)
        row.addWidget(browse)
        return page

    def _build_manual_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self._manual_distance_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._manual_distance_spin.setRange(0.1, 10_000.0)
        self._manual_distance_spin.setDecimals(3)
        self._manual_distance_spin.setValue(150.0)
        form.addRow(tr("Distance (mm):"), self._manual_distance_spin)

        beam_centre_hint = QtWidgets.QLabel(tr(
            "Tip: right-click near the beam centre on the Position 1 image "
            "above to fill in X/Y below automatically."
        ))
        beam_centre_hint.setWordWrap(True)
        beam_centre_hint.setStyleSheet("color: #666; font-size: 11px;")
        form.addRow(beam_centre_hint)

        self._auto_beam_centre_btn = QtWidgets.QPushButton(
            tr("Auto-detect beam centre in the primary image")
        )
        self._auto_beam_centre_btn.clicked.connect(self._on_auto_detect_beam_centre)
        form.addRow(self._auto_beam_centre_btn)

        self._manual_bcx_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._manual_bcx_spin.setRange(0.0, 10_000.0)
        self._manual_bcx_spin.setDecimals(2)
        form.addRow(tr("Beam centre X (px):"), self._manual_bcx_spin)

        self._manual_bcy_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._manual_bcy_spin.setRange(0.0, 10_000.0)
        self._manual_bcy_spin.setDecimals(2)
        form.addRow(tr("Beam centre Y (px):"), self._manual_bcy_spin)

        # Pixel size is never entered manually — it always comes from the
        # detected Rad-icon binning (see _update_detected_binning), applied
        # uniformly regardless of which initial-parameter source is chosen.

        self._manual_rot1_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._manual_rot1_spin.setRange(-90.0, 90.0)
        self._manual_rot1_spin.setDecimals(3)
        form.addRow(tr("Rot1 (deg):"), self._manual_rot1_spin)

        self._manual_rot2_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._manual_rot2_spin.setRange(-90.0, 90.0)
        self._manual_rot2_spin.setDecimals(3)
        form.addRow(tr("Rot2 (deg):"), self._manual_rot2_spin)

        wl_row = QtWidgets.QHBoxLayout()
        self._wl_mode_combo = _no_wheel(QtWidgets.QComboBox())
        self._wl_mode_combo.addItems([tr("Wavelength (Å)"), tr("Energy (keV)")])
        self._wl_value_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._wl_value_spin.setRange(0.0001, 1000.0)
        self._wl_value_spin.setDecimals(6)
        self._wl_value_spin.setValue(0.61)
        wl_row.addWidget(self._wl_mode_combo)
        wl_row.addWidget(self._wl_value_spin)
        form.addRow(tr("Wavelength / Energy:"), wl_row)

        return page

    def _on_mode_changed(self, idx: int) -> None:
        if idx == 2 and self._previous_mode_idx != 2:
            self._autofill_manual_from_source("prm" if self._previous_mode_idx == 0 else "poni")
        self._previous_mode_idx = idx
        self._init_stack.setCurrentIndex(idx)

    def _autofill_manual_from_source(self, mode: str) -> None:
        """Switching to Manual entry from IPA prm / existing poni carries over
        that source's values as the manual entry's starting point."""
        path = self._prm_path if mode == "prm" else self._poni_path
        if path is None:
            return
        dummy_px_um = self._detected_pixel_size_um or RAD_ICON_PIXEL_SIZE_1X1_UM
        try:
            if mode == "prm":
                ai = build_initial_ai(mode, dummy_px_um, prm_path=path)
            else:
                ai = build_initial_ai(mode, dummy_px_um, poni_path=path)
        except Exception:
            return

        self._manual_distance_spin.setValue(ai.dist * 1e3)
        self._manual_rot1_spin.setValue(math.degrees(ai.rot1))
        self._manual_rot2_spin.setValue(math.degrees(ai.rot2))
        self._wl_mode_combo.setCurrentIndex(0)   # Wavelength (Å)
        self._wl_value_spin.setValue(ai.wavelength * 1e10)

        if self._detected_pixel_size_um is not None:
            pixel_size_m = self._detected_pixel_size_um * 1e-6
            self._manual_bcx_spin.setValue(ai.poni2 / pixel_size_m)
            self._manual_bcy_spin.setValue(ai.poni1 / pixel_size_m)

    def _build_positions_group(self) -> QtWidgets.QGroupBox:
        grp = QtWidgets.QGroupBox(tr("Detector Positions"))
        vbox = QtWidgets.QVBoxLayout(grp)

        note = QtWidgets.QLabel(tr(
            "Move the detector stage (Ch9) using another stage-control app, "
            "read the magnescale (mgs) value off the physical scale, enter it "
            "below, then press \"Take XRD\". Position 1 is the primary position "
            "— the geometry actually saved is evaluated there."
        ))
        note.setWordWrap(True)
        note.setStyleSheet("color: #666; font-size: 11px;")
        vbox.addWidget(note)

        self._rows_layout = QtWidgets.QVBoxLayout()
        self._rows_layout.setSpacing(6)
        vbox.addLayout(self._rows_layout)

        btn_row = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton(tr("+ Add position"))
        add_btn.clicked.connect(lambda: self._add_position_row())
        remove_btn = QtWidgets.QPushButton(tr("− Remove last"))
        remove_btn.clicked.connect(self._remove_last_position_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        vbox.addLayout(btn_row)

        self._image_grid_container = QtWidgets.QWidget()
        self._image_grid = QtWidgets.QGridLayout(self._image_grid_container)
        self._image_grid.setSpacing(6)
        self._image_grid.setColumnStretch(0, 1)
        self._image_grid.setColumnStretch(1, 1)
        vbox.addWidget(self._image_grid_container)
        return grp

    def _reflow_image_grid(self) -> None:
        """Lay out position images 2-per-row (row count grows as positions are added)."""
        while self._image_grid.count():
            self._image_grid.takeAt(0)
        cols = 2
        for i, row in enumerate(self._rows):
            view = self._image_views.get(row.position.label)
            if view is None:
                continue
            r, c = divmod(i, cols)
            self._image_grid.addWidget(view, r, c)

    def _build_run_group(self) -> QtWidgets.QGroupBox:
        grp = QtWidgets.QGroupBox(tr("Calibration Settings and Run"))
        vbox = QtWidgets.QVBoxLayout(grp)

        chk_row = QtWidgets.QHBoxLayout()
        chk_row.addWidget(QtWidgets.QLabel(tr("Free parameters (dist0 is always fit):")))
        self._fit_poni1_chk = QtWidgets.QCheckBox(tr("Poni1 (beam Y)"))
        self._fit_poni2_chk = QtWidgets.QCheckBox(tr("Poni2 (beam X)"))
        self._fit_rot1_chk = QtWidgets.QCheckBox(tr("Rot1"))
        self._fit_rot2_chk = QtWidgets.QCheckBox(tr("Rot2"))
        self._fit_wavelength_chk = QtWidgets.QCheckBox(tr("Wavelength/Energy"))
        for chk in (self._fit_poni1_chk, self._fit_poni2_chk, self._fit_rot1_chk,
                    self._fit_rot2_chk, self._fit_wavelength_chk):
            chk.setChecked(True)
            chk_row.addWidget(chk)
        chk_row.addStretch()
        vbox.addLayout(chk_row)

        run_btn_row = QtWidgets.QHBoxLayout()
        run_btn_row.addWidget(QtWidgets.QLabel(tr("Optimisation cycles:")))
        self._cycles_spin = _no_wheel(QtWidgets.QSpinBox())
        self._cycles_spin.setRange(1, 50)
        self._cycles_spin.setValue(15)
        self._cycles_spin.setToolTip(tr(
            "Number of times to repeat the full extract+refine pipeline, each "
            "cycle starting from the previous cycle's result (same as pressing "
            "\"Repeat optimisation\" this many times)."
        ))
        run_btn_row.addWidget(self._cycles_spin)

        self._calibrate_btn = QtWidgets.QPushButton(tr("Calibrate parameters"))
        self._calibrate_btn.setStyleSheet(
            "font-weight: bold; background-color: #4CAF50; color: white; padding: 6px 12px;"
        )
        self._calibrate_btn.clicked.connect(self._on_calibrate_clicked)
        run_btn_row.addWidget(self._calibrate_btn)

        self._repeat_btn = QtWidgets.QPushButton(tr("Repeat optimisation"))
        self._repeat_btn.setEnabled(False)
        self._repeat_btn.setToolTip(tr(
            "Re-run the optimisation using the just-obtained poni parameters "
            "as the new starting point (instead of the Initial Parameters source)."
        ))
        self._repeat_btn.clicked.connect(self._on_repeat_optimisation_clicked)
        run_btn_row.addWidget(self._repeat_btn)
        vbox.addLayout(run_btn_row)

        self._log_view = QtWidgets.QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(120)
        vbox.addWidget(self._log_view)

        self._result_plot = pg.PlotWidget(background="w")
        self._result_plot.setLabel("bottom", tr("2θ (deg)"))
        self._result_plot.setLabel("left", tr("Intensity (a.u.)"))
        self._result_plot.showGrid(x=False, y=False)
        for axis in ("bottom", "left"):
            self._result_plot.getAxis(axis).setTextPen("k")
            self._result_plot.getAxis(axis).setPen("k")
        # Static display only — no pan/zoom/scroll inside the view.
        self._result_plot.setMouseEnabled(x=False, y=False)
        self._result_plot.hideButtons()
        self._result_plot.setMenuEnabled(False)
        self._result_plot.getViewBox().setMouseEnabled(x=False, y=False)
        self._result_plot.setMinimumHeight(220)
        self._result_curve = self._result_plot.plot(pen=pg.mkPen((40, 80, 160), width=1))
        self._result_peak_lines: list = []
        vbox.addWidget(self._result_plot)
        return grp

    def _build_results_group(self) -> QtWidgets.QGroupBox:
        grp = QtWidgets.QGroupBox(tr("Result"))
        vbox = QtWidgets.QVBoxLayout(grp)

        self._save_btn = QtWidgets.QPushButton(tr("Save and apply poni…"))
        self._save_btn.setEnabled(False)
        self._save_btn.setStyleSheet(
            "font-weight: bold; background-color: #4CAF50; color: white; padding: 6px 12px;"
        )
        self._save_btn.clicked.connect(self._on_save_poni)
        vbox.addWidget(self._save_btn)

        self._result_label = QtWidgets.QLabel(tr("Not calibrated yet."))
        self._result_label.setWordWrap(True)
        self._result_label.setStyleSheet("font-family: monospace; font-size: 1em;")
        vbox.addWidget(self._result_label)

        poni_diagram_path = _HERE.parent.parent / "assets" / "img" / "PONI.webp"
        poni_pixmap = QtGui.QPixmap(str(poni_diagram_path))
        if not poni_pixmap.isNull():
            diagram_label = QtWidgets.QLabel()
            diagram_label.setPixmap(
                poni_pixmap.scaledToWidth(700, Qt.TransformationMode.SmoothTransformation)
            )
            diagram_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vbox.addWidget(diagram_label)
        return grp

    # ------------------------------------------------------------------
    # Position rows
    # ------------------------------------------------------------------

    def _add_position_row(self) -> None:
        self._position_counter += 1
        if self._position_counter == 1:
            label = tr("Position 1 (primary)")
        else:
            label = tr("Position {n}", n=self._position_counter)
        row = _PositionRowWidget(label)
        row.take_xrd_requested.connect(self._on_take_xrd)
        row.load_image_requested.connect(self._on_load_image)
        row.mgs_changed.connect(self._update_calibrate_btn)
        self._rows_layout.addWidget(row)
        self._rows.append(row)

        view = _PositionImageView(label)
        if self._position_counter == 1:
            view.beam_centre_picked.connect(self._on_beam_centre_picked)
        self._image_views[row.position.label] = view
        self._reflow_image_grid()

        self._update_calibrate_btn()

    def _remove_last_position_row(self) -> None:
        if len(self._rows) <= 2:
            QtWidgets.QMessageBox.information(
                self, tr("Cannot remove"), tr("At least 2 positions are required."),
            )
            return
        row = self._rows.pop()
        view = self._image_views.pop(row.position.label, None)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        if view is not None:
            view.setParent(None)
            view.deleteLater()
        self._reflow_image_grid()
        self._update_calibrate_btn()

    def _update_calibrate_btn(self) -> None:
        ready = len(self._rows) >= 2 and all(
            row.position.image is not None for row in self._rows
        )
        self._calibrate_btn.setEnabled(ready and PYFAI_AVAILABLE)

    def _on_beam_centre_picked(self, x: float, y: float) -> None:
        """Right-click on the Position 1 image -> confirm -> fill manual beam centre.
        Matches the pyFAI tutorial's approach of visually reading the beam
        centre off the primary image rather than auto-detecting it."""
        reply = QtWidgets.QMessageBox.question(
            self, tr("Set beam centre?"),
            tr("Set beam centre to (x={x:.1f}, y={y:.1f}) px?", x=x, y=y),
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._manual_bcx_spin.setValue(x)
        self._manual_bcy_spin.setValue(y)
        self._manual_radio.setChecked(True)
        self._previous_mode_idx = 2
        self._init_stack.setCurrentIndex(2)

    def _on_auto_detect_beam_centre(self) -> None:
        """Estimate the beam centre from Position 1's image via the ring
        pattern's point symmetry (see detect_beam_center) -> confirm -> fill
        manual beam centre. Only an initial guess; full geometry refinement
        still happens afterwards."""
        img = self._rows[0].position.image
        if img is None:
            QtWidgets.QMessageBox.warning(
                self, tr("No image yet"),
                tr("Take XRD or load an image for Position 1 first."),
            )
            return
        try:
            x, y, confidence = detect_beam_center(img)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Detection Error"), str(exc))
            return

        msg = tr("Set beam centre to (x={x:.1f}, y={y:.1f}) px?", x=x, y=y)
        if confidence < 0.3:
            msg += "\n\n" + tr(
                "⚠ Confidence is low ({confidence:.2f}) — the ring pattern may "
                "be faint, asymmetric, or have too few rings. Please verify "
                "visually (e.g. with right-click) before trusting this.",
                confidence=confidence,
            )
        else:
            msg += "\n\n" + tr("Confidence: {confidence:.2f}", confidence=confidence)
        reply = QtWidgets.QMessageBox.question(self, tr("Set beam centre?"), msg)
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._manual_bcx_spin.setValue(x)
        self._manual_bcy_spin.setValue(y)

    # ------------------------------------------------------------------
    # Ch9 reference polling
    # ------------------------------------------------------------------

    def _poll_ch9(self) -> None:
        if self._controller is None:
            return
        try:
            pos_str = self._controller.get_ch_pos(9)
            if pos_str is None:
                return
            self._current_ch9 = int(pos_str)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Take XRD / Load image
    # ------------------------------------------------------------------

    def _apply_flip(self, img: np.ndarray) -> np.ndarray:
        radicon_window = self._get_radicon_window() if self._get_radicon_window else None
        return radicon_window._apply_flip(img) if radicon_window is not None else img

    def _check_duplicate_mgs(self, row: _PositionRowWidget) -> bool:
        """Warn if another position already uses this row's mgs value.
        Returns False if the caller should abort (user declined to continue)."""
        mgs = row.position.mgs_mm
        for other in self._rows:
            if other is not row and other.position.mgs_mm is not None \
                    and abs(other.position.mgs_mm - mgs) < 1e-9:
                reply = QtWidgets.QMessageBox.question(
                    self, tr("Duplicate mgs value"),
                    tr("Position '{other}' already used mgs={mgs} mm. Did you "
                       "forget to move the stage?", other=other.position.label, mgs=mgs),
                )
                if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                    return False
                break
        return True

    def _commit_position_image(
        self, row: _PositionRowWidget, image: np.ndarray, ch9_pulse: int | None,
    ) -> None:
        row.position.ch9_pulse = ch9_pulse
        row.position.image     = image
        row.mark_captured(ch9_pulse)
        self._image_views[row.position.label].set_image(image)
        if row is self._rows[0]:
            self._update_detected_binning(image)
        self._update_calibrate_btn()

    def _update_detected_binning(self, image: np.ndarray) -> None:
        """Re-detect binning/pixel size from Position 1 (primary)'s image.
        Pixel size is always derived this way — never from a prm/poni file
        or manual entry — since those may have been calibrated at a
        different binning than what is currently in use."""
        width = image.shape[1]
        self._detected_binning = detect_binning(width)
        self._detected_pixel_size_um = pixel_size_um_for_binning(self._detected_binning)
        self._binning_label.setText(
            tr("Binning: {binning}  (pixel size = {px:.1f} µm, from image width {w} px)",
               binning=self._detected_binning, px=self._detected_pixel_size_um, w=width)
        )

    def _on_take_xrd(self, row: _PositionRowWidget) -> None:
        radicon_window = self._get_radicon_window() if self._get_radicon_window else None
        if radicon_window is None:
            QtWidgets.QMessageBox.warning(
                self, tr("Rad-icon 2022 not open"),
                tr("Please open the Rad-icon 2022 (FPD) Controller window first "
                   "and configure the exposure settings there, then try again."),
            )
            return
        if self._backend is None:
            QtWidgets.QMessageBox.warning(
                self, tr("Not connected"), tr("Rad-icon backend is not connected."),
            )
            return
        if not self._check_duplicate_mgs(row):
            return

        ch9_pulse = getattr(self, "_current_ch9", None)
        if ch9_pulse is None and self._controller is not None:
            try:
                pos_str = self._controller.get_ch_pos(9)
                ch9_pulse = int(pos_str) if pos_str is not None else None
            except Exception:
                ch9_pulse = None

        try:
            raw = self._backend.snap()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Acquisition Error"), str(exc))
            return

        img = self._apply_flip(raw)
        self._commit_position_image(row, img, ch9_pulse)

    def _on_load_image(self, row: _PositionRowWidget) -> None:
        if not self._check_duplicate_mgs(row):
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select XRD image"), self._last_load_dir,
            "TIFF files (*.tif *.tiff);;All files (*)",
        )
        if not path:
            return
        try:
            import tifffile
            img = tifffile.imread(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Load Error"), str(exc))
            return

        self._last_load_dir = str(pathlib.Path(path).parent)
        self._save_prefs()
        # Loaded files are assumed to already be flip-corrected (they were
        # saved that way by RadiconWindow / this app's own Take XRD) — no
        # ch9 value is known for a file loaded from disk.
        self._commit_position_image(row, img, ch9_pulse=None)

    # ------------------------------------------------------------------
    # File browsing (initial geometry source)
    # ------------------------------------------------------------------

    def _browse_prm(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select IPAnalyzer parameter file"), self._last_prm_dir,
            "IPA parameter files (*.prm);;All files (*)",
        )
        if not path:
            return
        self._prm_path = pathlib.Path(path)
        self._prm_path_edit.setText(str(self._prm_path))
        self._last_prm_dir = str(self._prm_path.parent)
        self._save_prefs()

    def _browse_poni(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select poni file"), self._last_poni_dir,
            "poni files (*.poni);;All files (*)",
        )
        if not path:
            return
        self._poni_path = pathlib.Path(path)
        self._poni_path_edit.setText(str(self._poni_path))
        self._last_poni_dir = str(self._poni_path.parent)
        self._save_prefs()

    # ------------------------------------------------------------------
    # Calibration run
    # ------------------------------------------------------------------

    def _read_manual_params(self) -> ManualInitialParams:
        wavelength_ang = None
        energy_kev = None
        if self._wl_mode_combo.currentIndex() == 0:
            wavelength_ang = self._wl_value_spin.value()
        else:
            energy_kev = self._wl_value_spin.value()
        return ManualInitialParams(
            distance_mm=self._manual_distance_spin.value(),
            beam_center_x_px=self._manual_bcx_spin.value(),
            beam_center_y_px=self._manual_bcy_spin.value(),
            rot1_deg=self._manual_rot1_spin.value(),
            rot2_deg=self._manual_rot2_spin.value(),
            wavelength_ang=wavelength_ang,
            energy_kev=energy_kev,
        )

    def _build_current_initial_ai(self):
        if self._detected_pixel_size_um is None:
            raise ValueError(
                "Binning not yet detected — capture or load Position 1's image first."
            )
        px = self._detected_pixel_size_um
        if self._prm_radio.isChecked():
            return build_initial_ai("prm", px, prm_path=self._prm_path)
        if self._poni_radio.isChecked():
            return build_initial_ai("poni", px, poni_path=self._poni_path)
        return build_initial_ai("manual", px, manual=self._read_manual_params())

    def _log_append(self, text: str) -> None:
        self._log_view.appendPlainText(text)

    def _on_calibrate_clicked(self) -> None:
        try:
            ai_initial = self._build_current_initial_ai()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Initial geometry error"), str(exc))
            return
        self._start_calibration(ai_initial)

    def _on_repeat_optimisation_clicked(self) -> None:
        if self._ai_result is None:
            return
        self._start_calibration(self._ai_result)

    def _start_calibration(self, ai_initial) -> None:
        self._total_cycles = self._cycles_spin.value()
        self._remaining_cycles = self._total_cycles
        self._log_view.clear()
        self._run_one_cycle(ai_initial)

    def _run_one_cycle(self, ai_initial) -> None:
        # Position 1 is always the primary — the worker falls back to
        # positions[0] when no position has is_primary set.
        positions = [row.position for row in self._rows]

        stages = FreeParamStages(
            fit_poni1=self._fit_poni1_chk.isChecked(),
            fit_poni2=self._fit_poni2_chk.isChecked(),
            fit_rot1=self._fit_rot1_chk.isChecked(),
            fit_rot2=self._fit_rot2_chk.isChecked(),
            fit_wavelength=self._fit_wavelength_chk.isChecked(),
        )

        if self._total_cycles > 1:
            cycle_no = self._total_cycles - self._remaining_cycles + 1
            self._log_append(f"===== Optimisation cycle {cycle_no}/{self._total_cycles} =====")

        self._calibrate_btn.setEnabled(False)
        self._repeat_btn.setEnabled(False)
        self._save_btn.setEnabled(False)

        self._worker = MultiPositionCalibrationWorker(
            positions, self._calibrant_combo.currentText().strip(), ai_initial, stages,
        )
        self._worker.progress.connect(self._log_append)
        self._worker.ring_extracted.connect(self._on_ring_extracted)
        self._worker.stage_completed.connect(self._on_stage_completed)
        self._worker.completed.connect(self._on_cycle_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_cycle_completed(self, ai_primary, results: dict) -> None:
        self._update_result_plot(ai_primary)   # redraw after every cycle, not just the last
        self._remaining_cycles -= 1
        if self._remaining_cycles > 0:
            self._run_one_cycle(ai_primary)
        else:
            self._on_completed(ai_primary, results)

    def _on_ring_extracted(self, label: str, sg, n_points: int) -> None:
        for row in self._rows:
            if row.position.label == label:
                row.mark_captured(row.position.ch9_pulse, n_points)
        view = self._image_views.get(label)
        if view is None:
            return
        try:
            data = sg.geometry_refinement.data
            pts = data[:, :2] if data is not None and len(data) else None
        except Exception:
            pts = None
        view.set_control_points(pts)

    def _on_stage_completed(self, stage_name: str, chi2: float) -> None:
        self._log_append(f"[{stage_name}] chi2 = {chi2:.6g}")

    def _on_completed(self, ai_primary, results: dict) -> None:
        self._ai_result = ai_primary
        self._calibrate_btn.setEnabled(True)
        self._repeat_btn.setEnabled(True)
        self._save_btn.setEnabled(True)

        p = results["params"]
        # getFit2D() gives the true beam centre in pixels (Fit2D convention),
        # which accounts for rot1/rot2 tilt — distinct from poni1/poni2/pixel
        # size (the PONI, foot of the perpendicular from sample to detector
        # plane) whenever the detector is tilted. See assets/img/PONI.webp.
        try:
            f2d = ai_primary.getFit2D()
            centre_line = (
                f"  beam centre (Fit2D) = x={f2d['centerX']:.2f} px, "
                f"y={f2d['centerY']:.2f} px\n"
            )
        except Exception:
            centre_line = ""
        text = (
            f"chi2 = {results['chi2']:.6g}\n"
            f"dist0 = {p['dist0']*1e3:.4f} mm   scale0 = {p['scale0']:.6e} m/mm\n"
            f"poni1 = {p['poni1']*1e3:.4f} mm   poni2 = {p['poni2']*1e3:.4f} mm\n"
            f"rot1 = {math.degrees(p['rot1']):.4f}°   rot2 = {math.degrees(p['rot2']):.4f}°\n"
            f"energy = {p['energy']:.4f} keV\n\n"
            f"Effective geometry at primary ({results['primary_label']}, "
            f"mgs={results['primary_mgs']} mm):\n"
            f"  dist = {ai_primary.dist*1e3:.4f} mm\n"
            f"  poni1 = {ai_primary.poni1*1e3:.4f} mm   poni2 = {ai_primary.poni2*1e3:.4f} mm\n"
            f"{centre_line}"
            f"  wavelength = {ai_primary.wavelength*1e10:.6f} Å"
        )
        self._result_label.setText(text)
        # Already redrawn per-cycle by _on_cycle_completed; no need to repeat here.

    def _compute_npt_for_bin_width(self, ai, img_shape: tuple, bin_width_deg: float) -> int:
        try:
            tth = ai.center_array(img_shape, unit="2th_deg", scale=True)
            tth_min, tth_max = float(np.nanmin(tth)), float(np.nanmax(tth))
            return max(10, int(np.ceil((tth_max - tth_min) / bin_width_deg)))
        except Exception:
            return 2000

    def _update_result_plot(self, ai) -> None:
        """1D-reduce the primary position's image with the fitted geometry
        (0.01 deg/bin) and plot it, with the chosen calibrant's expected
        ring positions overlaid as reference lines."""
        img = self._rows[0].position.image
        if img is None:
            return
        npt = self._compute_npt_for_bin_width(ai, img.shape, 0.01)
        try:
            result = ai.integrate1d(
                img.astype(np.float32), npt=npt, unit="2th_deg",
                method=("no", "histogram", "cython"), correctSolidAngle=True,
            )
        except Exception:
            return
        self._result_curve.setData(result.radial, result.intensity)
        tth_min, tth_max = float(result.radial[0]), float(result.radial[-1])
        self._result_plot.getViewBox().setLimits(xMin=tth_min, xMax=tth_max)
        self._result_plot.setRange(xRange=(tth_min, tth_max), padding=0.02)

        for line in self._result_peak_lines:
            self._result_plot.removeItem(line)
        self._result_peak_lines.clear()
        if _get_calibrant is not None:
            try:
                cal = _get_calibrant(self._calibrant_combo.currentText().strip())
                cal.wavelength = ai.wavelength
                for tth_rad in cal.get_2th():
                    if tth_rad is None:
                        continue
                    tth_deg = math.degrees(tth_rad)
                    if tth_min <= tth_deg <= tth_max:
                        line = pg.InfiniteLine(
                            pos=tth_deg, angle=90, movable=False,
                            pen=pg.mkPen("r", width=0.8, style=Qt.PenStyle.DashLine),
                        )
                        self._result_plot.addItem(line)
                        self._result_peak_lines.append(line)
            except Exception:
                pass

    def _on_failed(self, msg: str) -> None:
        self._calibrate_btn.setEnabled(True)
        self._repeat_btn.setEnabled(self._ai_result is not None)
        self._log_append(tr("✕ Calibration failed"))
        QtWidgets.QMessageBox.critical(self, tr("Calibration Error"), msg)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _default_poni_filename(self) -> str:
        primary = self._rows[0].position
        parts = [datetime.now().strftime("%Y%m%d_%H%M%S")]
        if primary.mgs_mm is not None:
            parts.append(f"mgs{primary.mgs_mm:g}")
        if primary.ch9_pulse is not None:
            parts.append(f"ch9{primary.ch9_pulse}")
        if self._detected_binning:
            parts.append(f"bin{self._detected_binning}")
        return "_".join(parts) + ".poni"

    def _on_save_poni(self) -> None:
        if self._ai_result is None:
            return
        start_dir = pathlib.Path(self._last_save_dir) if self._last_save_dir else pathlib.Path.cwd()
        suggested_path = str(start_dir / self._default_poni_filename())
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save poni file"), suggested_path,
            "poni files (*.poni);;All files (*)",
        )
        if not path:
            return
        p = pathlib.Path(path)
        if p.suffix.lower() != ".poni":
            p = p.with_suffix(".poni")
        try:
            write_poni(
                self._ai_result, p,
                comments=["# Calibrate Detector Geometry — multi-position calibration"],
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Save Error"), str(exc))
            return
        self._last_save_dir = str(p.parent)
        self._save_prefs()
        # Two-step "save and apply": the poni file is now on disk (above);
        # registering it with Settings -> Detector Calibration (both the
        # live PoniState and its persisted last-used-file prefs) ensures
        # there is never a state where calibrated poni parameters exist in
        # memory without a corresponding registered file on disk.
        if self._poni_state is not None:
            self._poni_state.update(ai=self._ai_result, poni_path=p)
        remember_poni_path(p)
        self._result_label.setText(
            self._result_label.text()
            + f"\n\nSaved and applied → {p}"
        )

    # ------------------------------------------------------------------
    # Prefs persistence
    # ------------------------------------------------------------------

    def _save_prefs(self) -> None:
        _LOCALDATA.mkdir(parents=True, exist_ok=True)
        data = {
            "last_prm_dir":  self._last_prm_dir,
            "last_poni_dir": self._last_poni_dir,
            "last_save_dir": self._last_save_dir,
            "last_load_dir": self._last_load_dir,
            "calibrant":     self._calibrant_combo.currentText(),
        }
        _PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_prefs(self) -> None:
        if not _PREFS_FILE.exists():
            return
        try:
            data = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self._last_prm_dir  = data.get("last_prm_dir", "")
        self._last_poni_dir = data.get("last_poni_dir", "")
        self._last_save_dir = data.get("last_save_dir", "")
        self._last_load_dir = data.get("last_load_dir", "")
        calibrant = data.get("calibrant")
        if calibrant:
            self._calibrant_combo.setCurrentText(calibrant)

    def closeEvent(self, event) -> None:
        self._ch9_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)
