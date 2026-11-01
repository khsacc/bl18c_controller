"""Hardware-free XRD image viewer, masker and shared pyFAI 1D reducer."""
from __future__ import annotations

from pathlib import Path

if __package__ in (None, ""):
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import copy
import json

import numpy as np
import pyqtgraph as pg
import tifffile
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal

from apps.Rad_icon_2022.image_view import ImageLevelsControl, levels_to_qimage
from apps.Rad_icon_2022.instant1d import (
    find_spot_mask, reduce_frame, save_profile,
)
from apps.Rad_icon_2022.reduction_controls import (
    FORMAT_LABELS, UNIT_LABELS, angle_limit_spin, bin_width_spin,
)
from apps.xrd_1d_reduction.masks import load_mask, save_mask, shape_mask
from settings.pages.detector_calibration import (
    restore_remembered_poni, save_reduction_settings,
)
from settings.poni_state import PoniState
from utils.pdindexer import PdiProfile, PdiService, Transport, Trigger


_PREFS = Path(__file__).parent / "__localdata" / "reduction_prefs.json"


def _normalise_prefs(data: object) -> tuple[dict, str]:
    """Keep malformed local preferences from preventing an offline window opening."""
    if not isinstance(data, dict):
        return {}, "Invalid preferences were ignored."
    prefs = dict(data)
    warnings = []
    for key, default, low, high in (
        ("bin_width_deg", 0.01, 0.001, 1.0),
        ("min_2theta", -1.0, -1.0, 180.0),
        ("max_2theta", -1.0, -1.0, 180.0),
    ):
        try:
            value = float(prefs.get(key, default))
            if not np.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
            prefs[key] = value
        except (TypeError, ValueError):
            prefs[key] = default
            warnings.append(key)
    for key, allowed, default in (
        ("formats", {item[0] for item in FORMAT_LABELS}, ["csv"]),
        ("units", {item[0] for item in UNIT_LABELS}, ["2theta"]),
    ):
        value = prefs.get(key, default)
        if not isinstance(value, list) or any(
                not isinstance(item, str) or item not in allowed for item in value):
            prefs[key] = default
            warnings.append(key)
    for key in ("autosave", "autosend"):
        if not isinstance(prefs.get(key, False), bool):
            prefs[key] = False
            warnings.append(key)
    for kind in ("spot", "manual"):
        key = kind + "_policy"
        if prefs.get(key, "file") not in ("current", "file", "none"):
            prefs[key] = "file"
            warnings.append(key)
    for key in ("last_image_dir", "last_mask_dir", "spot_file", "manual_file"):
        if not isinstance(prefs.get(key, ""), str):
            prefs[key] = ""
            warnings.append(key)
    return prefs, ("Invalid preferences reset: " + ", ".join(warnings)
                   if warnings else "")


def _no_wheel(widget):
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class MaskCanvas(QtWidgets.QWidget):
    """Aspect-preserving image view with drag-to-mask in source coordinates."""

    drawn = pyqtSignal(str, object, object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setMouseTracking(True)
        self._image: QtGui.QImage | None = None
        self._shape: tuple[int, int] | None = None
        self.tool = "rectangle"
        self._start: tuple[float, float] | None = None
        self._end: tuple[float, float] | None = None
        self._add = True

    def set_frame(self, image: np.ndarray, mask: np.ndarray, lo: int, hi: int) -> None:
        base = levels_to_qimage(image, lo, hi)
        if np.any(mask):
            grey = np.frombuffer(base.constBits().asstring(base.sizeInBytes()),
                                 dtype=np.uint8).reshape(
                                     image.shape[0], base.bytesPerLine())[:, :image.shape[1]]
            rgb = np.repeat(grey[:, :, None], 3, axis=2).copy()
            rgb[mask] = (rgb[mask].astype(np.float32) * 0.55
                         + np.array([110, 15, 15])).clip(0, 255).astype(np.uint8)
            rgb = np.ascontiguousarray(rgb)
            h, w = image.shape
            self._image = QtGui.QImage(rgb.data, w, h, 3 * w,
                                       QtGui.QImage.Format.Format_RGB888).copy()
        else:
            self._image = base
        self._shape = image.shape
        self.update()

    def _image_rect(self) -> QtCore.QRectF:
        if self._image is None:
            return QtCore.QRectF()
        scale = min(self.width() / self._image.width(),
                    self.height() / self._image.height())
        w = self._image.width() * scale
        h = self._image.height() * scale
        return QtCore.QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _point(self, pos: QtCore.QPointF) -> tuple[float, float] | None:
        rect = self._image_rect()
        if rect.isEmpty() or not rect.contains(pos):
            return None
        return ((pos.x() - rect.left()) / rect.width() * self._image.width(),
                (pos.y() - rect.top()) / rect.height() * self._image.height())

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#111111"))
        if self._image is None:
            painter.setPen(QtGui.QColor("#cccccc"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Open or drop a 2D TIFF image")
            return
        rect = self._image_rect()
        painter.drawImage(rect, self._image)
        if self._start is not None and self._end is not None:
            sx, sy = self._start
            ex, ey = self._end
            def xy(x, y):
                return QtCore.QPointF(rect.left() + x / self._image.width() * rect.width(),
                                      rect.top() + y / self._image.height() * rect.height())
            painter.setPen(QtGui.QPen(QtGui.QColor("#ffcc44" if self._add else "#55ddff"), 2))
            if self.tool == "rectangle":
                painter.drawRect(QtCore.QRectF(xy(sx, sy), xy(ex, ey)).normalized())
            else:
                centre = xy(sx, sy)
                radius = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
                rx = radius / self._image.width() * rect.width()
                ry = radius / self._image.height() * rect.height()
                painter.drawEllipse(centre, rx, ry)

    def mousePressEvent(self, event):
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        point = self._point(event.position())
        if point is None:
            return
        self._start = self._end = point
        self._add = event.button() == Qt.MouseButton.LeftButton
        self.update()

    def mouseMoveEvent(self, event):
        if self._start is None or self._shape is None:
            return
        rect = self._image_rect()
        pos = event.position()
        x = min(max(pos.x(), rect.left()), rect.right())
        y = min(max(pos.y(), rect.top()), rect.bottom())
        self._end = ((x - rect.left()) / rect.width() * self._shape[1],
                     (y - rect.top()) / rect.height() * self._shape[0])
        self.update()

    def mouseReleaseEvent(self, event):
        if self._start is None or event.button() not in (
                Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        self.mouseMoveEvent(event)
        start, end = self._start, self._end
        self._start = self._end = None
        self.update()
        self.drawn.emit(self.tool, start, end, self._add)


class ReductionWorker(QtCore.QThread):
    result_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, operation, ai, image, mask, width, threshold, minimum, maximum):
        super().__init__()
        self.operation = operation
        self.ai = ai
        self.image = image.copy()
        self.mask = mask.copy()
        self.width = width
        self.threshold = threshold
        self.minimum = minimum
        self.maximum = maximum

    def run(self):
        try:
            if self.operation == "spots":
                result = find_spot_mask(self.ai, self.image, self.threshold,
                                        exclude_mask=self.mask)
            else:
                result = reduce_frame(self.ai, self.image, self.width,
                                      min_2theta=self.minimum, max_2theta=self.maximum,
                                      mask=self.mask if np.any(self.mask) else None)
            self.result_ready.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class Xrd1DReductionWindow(QtWidgets.QMainWindow):
    def __init__(self, *, poni_state=None, open_settings=None,
                 open_calibration=None, pdi_service=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("XRD 1D reduction")
        self.resize(1400, 950)
        self.setAcceptDrops(True)
        self._drop_targets: set[QtWidgets.QWidget] = set()
        self._owns_state = poni_state is None
        self._state = poni_state or PoniState(self)
        if self._owns_state:
            restore_remembered_poni(self._state)
        self._open_settings_callback = open_settings
        self._open_calibration_callback = open_calibration
        self._settings_window = None
        self._calibration_window = None
        self._pdi = pdi_service or PdiService()
        self._image: np.ndarray | None = None
        self._image_path: Path | None = None
        self._spot_mask: np.ndarray | None = None
        self._manual_mask: np.ndarray | None = None
        self._spot_file: Path | None = None
        self._manual_file: Path | None = None
        self._profile = None
        self._worker: ReductionWorker | None = None
        self._job_result = None
        self._job_error = None
        self._job_operation = ""
        self._job_image = None
        self._revision = 0
        self._job_revision = 0
        self._reduce_pending = False
        self._spot_pending = False
        self._closing = False
        self._auto_trigger_revision: int | None = None
        self._auto_skip_notice = ""
        self._mask_notice = ""
        self._prefs_error_notice = ""
        self._spot_error_notice = ""
        self._spot_cancel_notice = ""
        self._last_ai = self._state.integration_ai
        self._last_threshold = self._state.spot_deviation_factor
        read_warning = ""
        try:
            raw_prefs = json.loads(_PREFS.read_text(encoding="utf-8"))
        except OSError:
            raw_prefs = {}
        except ValueError:
            raw_prefs = {}
            read_warning = "Invalid preferences file was ignored."
        self._prefs, prefs_warning = _normalise_prefs(raw_prefs)
        prefs_warning = " ".join(part for part in (read_warning, prefs_warning) if part)
        for kind in ("spot", "manual"):
            saved = self._prefs.get(kind + "_file")
            if isinstance(saved, str) and saved:
                setattr(self, "_" + kind + "_file", Path(saved))
        self._build_ui()
        self._state.poni_changed.connect(self._on_state_changed)
        self._on_state_changed()
        if prefs_warning:
            self._status.setText(prefs_warning)

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        self._add_drop_target(root)
        layout = QtWidgets.QVBoxLayout(root)
        top = QtWidgets.QHBoxLayout()
        open_btn = QtWidgets.QPushButton("Open 2D image…")
        open_btn.setToolTip("Open a TIFF image or drop one into this window.")
        open_btn.clicked.connect(self._open_image)
        top.addWidget(open_btn)
        self._file_label = QtWidgets.QLabel("No image loaded")
        top.addWidget(self._file_label, 1)
        self._reduce_btn = QtWidgets.QPushButton("Reduce 1D")
        self._reduce_btn.clicked.connect(self._request_reduction)
        top.addWidget(self._reduce_btn)
        layout.addLayout(top)

        vertical = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        horizontal = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        vertical.addWidget(horizontal)
        layout.addWidget(vertical, 1)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        self._canvas = MaskCanvas()
        self._add_drop_target(self._canvas)
        self._canvas.drawn.connect(self._on_drawn)
        left_layout.addWidget(self._canvas, 1)
        levels = ImageLevelsControl()
        self._min_spin, self._max_spin = levels.min_spin, levels.max_spin
        levels.levels_changed.connect(self._render_image)
        levels.auto_requested.connect(self._auto_levels)
        left_layout.addWidget(levels)
        self._image_info = QtWidgets.QLabel("—")
        self._image_info.setWordWrap(True)
        left_layout.addWidget(self._image_info)
        horizontal.addWidget(left)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel = QtWidgets.QWidget()
        controls = QtWidgets.QVBoxLayout(panel)
        scroll.setWidget(panel)
        self._add_drop_target(scroll.viewport())
        horizontal.addWidget(scroll)
        horizontal.setSizes([700, 550])

        links = QtWidgets.QHBoxLayout()
        settings_btn = QtWidgets.QPushButton("Detector settings")
        settings_btn.clicked.connect(self._open_settings)
        links.addWidget(settings_btn)
        calibration_btn = QtWidgets.QPushButton("Calibrate detector geometry")
        calibration_btn.clicked.connect(self._open_calibration)
        links.addWidget(calibration_btn)
        controls.addLayout(links)
        self._calibration_status = QtWidgets.QLabel()
        controls.addWidget(self._calibration_status)

        diamond = QtWidgets.QGroupBox("Diamond thickness correction")
        dl = QtWidgets.QHBoxLayout(diamond)
        self._diamond_check = QtWidgets.QCheckBox("Correct")
        self._diamond_check.setChecked(self._state.correct_diamond_thickness)
        self._diamond_check.toggled.connect(self._change_diamond)
        dl.addWidget(self._diamond_check)
        self._diamond_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._diamond_spin.setRange(0, 1000)
        self._diamond_spin.setDecimals(3)
        self._diamond_spin.setSuffix(" mm")
        self._diamond_spin.setKeyboardTracking(False)
        self._diamond_spin.setValue(self._state.diamond_thickness_mm)
        self._diamond_spin.valueChanged.connect(self._change_diamond)
        dl.addWidget(self._diamond_spin)
        controls.addWidget(diamond)

        spots = QtWidgets.QGroupBox("Spots")
        sl = QtWidgets.QVBoxLayout(spots)
        spot_row = QtWidgets.QHBoxLayout()
        spot_btn = QtWidgets.QPushButton("Mask Spots")
        spot_btn.clicked.connect(lambda: self._start_job("spots"))
        spot_row.addWidget(spot_btn)
        spot_row.addWidget(QtWidgets.QLabel("Threshold:"))
        self._threshold = _no_wheel(QtWidgets.QDoubleSpinBox())
        self._threshold.setRange(0, 10)
        self._threshold.setDecimals(2)
        self._threshold.setKeyboardTracking(False)
        self._threshold.setValue(self._state.spot_deviation_factor)
        self._threshold.valueChanged.connect(self._change_threshold)
        spot_row.addWidget(self._threshold)
        sl.addLayout(spot_row)
        sl.addLayout(self._mask_file_row("spot"))
        self._spot_policy = self._policy_group(sl, "spot")
        controls.addWidget(spots)

        manual = QtWidgets.QGroupBox("Mask")
        ml = QtWidgets.QVBoxLayout(manual)
        tool_row = QtWidgets.QHBoxLayout()
        tool_row.addWidget(QtWidgets.QLabel("Drag shape:"))
        shape = _no_wheel(QtWidgets.QComboBox())
        shape.addItems(["Rectangle", "Circle"])
        shape.currentTextChanged.connect(lambda value: setattr(self._canvas, "tool", value.lower()))
        tool_row.addWidget(shape)
        tool_row.addWidget(QtWidgets.QLabel("Left: mask   Right: unmask"))
        ml.addLayout(tool_row)
        ml.addWidget(QtWidgets.QLabel("Drag on the image to select a region."))
        clear = QtWidgets.QPushButton("Clear mask")
        clear.clicked.connect(self._clear_masks)
        ml.addWidget(clear)
        ml.addLayout(self._mask_file_row("manual"))
        self._manual_policy = self._policy_group(ml, "manual")
        controls.addWidget(manual)

        options = QtWidgets.QGroupBox("1D reduction")
        ol = QtWidgets.QVBoxLayout(options)
        angle_grid = QtWidgets.QGridLayout()
        angle_grid.addWidget(QtWidgets.QLabel("Angular resolution (deg/bin):"), 0, 0)
        self._width = bin_width_spin(float(self._prefs.get("bin_width_deg", 0.01)))
        self._width.valueChanged.connect(self._options_changed)
        angle_grid.addWidget(self._width, 0, 1)
        for row, (name, label) in enumerate((("min", "Min 2θ (°):"),
                                            ("max", "Max 2θ (°):")), start=1):
            angle_grid.addWidget(QtWidgets.QLabel(label), row, 0)
            saved = self._prefs.get(name + "_2theta", -1)
            spin = angle_limit_spin(None if saved is None or saved == -1 else float(saved))
            spin.valueChanged.connect(self._options_changed)
            setattr(self, "_" + name + "_angle", spin)
            angle_grid.addWidget(spin, row, 1)
        angle_grid.setColumnStretch(2, 1)
        ol.addLayout(angle_grid)
        ol.addWidget(QtWidgets.QLabel("Save formats:"))
        format_grid = QtWidgets.QGridLayout()
        self._formats = {}
        for index, (key, label) in enumerate(FORMAT_LABELS):
            box = QtWidgets.QCheckBox(label)
            box.setChecked(key in self._prefs.get("formats", ["csv"]))
            box.toggled.connect(self._save_prefs)
            self._formats[key] = box
            format_grid.addWidget(box, index // 2, index % 2)
        ol.addLayout(format_grid)
        ol.addWidget(QtWidgets.QLabel("X-axis units to save:"))
        unit_grid = QtWidgets.QGridLayout()
        self._units = {}
        for index, (key, label) in enumerate(UNIT_LABELS):
            box = QtWidgets.QCheckBox(label)
            box.setChecked(key in self._prefs.get("units", ["2theta"]))
            box.toggled.connect(self._save_prefs)
            self._units[key] = box
            unit_grid.addWidget(box, index // 2, index % 2)
        ol.addLayout(unit_grid)
        self._autosave = QtWidgets.QCheckBox("Auto-save 1D data when an image is opened")
        self._autosave.setChecked(bool(self._prefs.get("autosave", False)))
        self._autosave.toggled.connect(self._save_prefs)
        ol.addWidget(self._autosave)
        self._autosend = QtWidgets.QCheckBox("Send to PDIndexer when an image is opened")
        self._autosend.setChecked(bool(self._prefs.get("autosend", False)))
        self._autosend.toggled.connect(self._save_prefs)
        ol.addWidget(self._autosend)
        save_row = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save 1D data")
        save_btn.clicked.connect(self._save_profile)
        save_row.addWidget(save_btn)
        self._send_btn = QtWidgets.QPushButton("Send now to PDIndexer")
        self._send_btn.clicked.connect(self._send_pdi)
        save_row.addWidget(self._send_btn)
        self._transport = _no_wheel(QtWidgets.QComboBox())
        if self._pdi.clipboard_available():
            self._transport.addItem("Clipboard", Transport.CLIPBOARD)
        if self._pdi.watch_folder_configured():
            self._transport.addItem(".pdi folder", Transport.WATCH_FOLDER)
        self._send_btn.setEnabled(self._transport.count() > 0)
        self._autosend.setEnabled(self._transport.count() > 0)
        save_row.addWidget(self._transport)
        ol.addLayout(save_row)
        controls.addWidget(options)
        controls.addStretch()

        bottom = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(bottom)
        self._plot = pg.PlotWidget(background="w")
        self._add_drop_target(self._plot)
        self._add_drop_target(self._plot.viewport())
        self._plot.setLabel("bottom", "2θ (deg)")
        self._plot.setLabel("left", "Intensity (a.u.)")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._curve = self._plot.plot(pen=pg.mkPen((40, 80, 160), width=1))
        bl.addWidget(self._plot)
        self._status = QtWidgets.QLabel("Open an image to begin.")
        bl.addWidget(self._status)
        vertical.addWidget(bottom)
        vertical.setSizes([650, 250])

    def _mask_file_row(self, kind):
        row = QtWidgets.QHBoxLayout()
        load = QtWidgets.QPushButton("Load mask…")
        load.clicked.connect(lambda: self._load_mask(kind))
        row.addWidget(load)
        save = QtWidgets.QPushButton("Save mask…")
        save.clicked.connect(lambda: self._save_mask(kind))
        row.addWidget(save)
        return row

    def _policy_group(self, layout, kind):
        group = QtWidgets.QButtonGroup(self)
        labels = (("current", "Keep masked spots" if kind == "spot" else "Keep current mask"),
                  ("file", "Keep mask file"), ("none", "Do not keep mask"))
        for key, label in labels:
            button = QtWidgets.QRadioButton(label)
            button.setProperty("policy", key)
            group.addButton(button)
            layout.addWidget(button)
            button.toggled.connect(self._save_prefs)
            if key == self._prefs.get(kind + "_policy", "file"):
                button.setChecked(True)
        if group.checkedButton() is None:
            group.buttons()[1].setChecked(True)
        return group

    @staticmethod
    def _policy(group):
        return group.checkedButton().property("policy")

    def _save_prefs(self, *_args):
        if not hasattr(self, "_manual_policy"):
            return
        data = dict(self._prefs)
        data.update(bin_width_deg=self._width.value(),
                    min_2theta=self._min_angle.value(), max_2theta=self._max_angle.value(),
                    formats=[k for k, w in self._formats.items() if w.isChecked()],
                    units=[k for k, w in self._units.items() if w.isChecked()],
                    autosave=self._autosave.isChecked(), autosend=self._autosend.isChecked(),
                    spot_policy=self._policy(self._spot_policy),
                    manual_policy=self._policy(self._manual_policy))
        self._prefs = data
        try:
            _PREFS.parent.mkdir(parents=True, exist_ok=True)
            _PREFS.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            self._prefs_error_notice = f"Preferences could not be saved: {exc}"
            if hasattr(self, "_status"):
                self._status.setText(self._prefs_error_notice)
        else:
            self._prefs_error_notice = ""

    def _options_changed(self, *_args):
        self._save_prefs()
        self._request_reduction()

    def _change_diamond(self, *_args):
        self._state.set_diamond_correction(self._diamond_check.isChecked(),
                                           self._diamond_spin.value())
        save_reduction_settings(self._state)

    def _change_threshold(self, value):
        self._state.set_spot_deviation_factor(value)
        save_reduction_settings(self._state)

    def _on_state_changed(self):
        ai = self._state.integration_ai
        geometry_changed = ai is not self._last_ai
        threshold_changed = self._state.spot_deviation_factor != self._last_threshold
        self._last_ai = ai
        self._last_threshold = self._state.spot_deviation_factor
        for widget, value in ((self._diamond_check, self._state.correct_diamond_thickness),
                              (self._diamond_spin, self._state.diamond_thickness_mm),
                              (self._threshold, self._state.spot_deviation_factor)):
            blocked = widget.blockSignals(True)
            (widget.setChecked if isinstance(widget, QtWidgets.QCheckBox) else widget.setValue)(value)
            widget.blockSignals(blocked)
        self._diamond_spin.setEnabled(self._diamond_check.isChecked())
        if self._state.is_calibrated():
            path = self._state.poni_path
            self._calibration_status.setText(
                f"Calibration: {path.name if path else 'in-session geometry'}")
        else:
            self._calibration_status.setText("Calibration: no poni file registered")
        if geometry_changed:
            self._request_reduction()
        elif threshold_changed and (self._spot_pending or
                                    (self._worker is not None and
                                     self._job_operation == "spots")):
            self._revision += 1
            self._spot_pending = True
            self._spot_cancel_notice = ""
            self._status.setText("Spot detection will use the updated threshold.")
            self._dispatch_pending()

    def _open_settings(self):
        if self._open_settings_callback is not None:
            self._open_settings_callback()
            return
        from settings.settings_window import SettingsWindow
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self._state, parent=self)
            self._settings_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._settings_window.destroyed.connect(
                lambda: setattr(self, "_settings_window", None))
        self._settings_window.show()
        self._settings_window.raise_()

    def _open_calibration(self):
        if self._open_calibration_callback is not None:
            self._open_calibration_callback()
            return
        from apps.calibrate_instruments.calibrate_instruments_app import CalibrateInstrumentsWindow
        if self._calibration_window is None:
            self._calibration_window = CalibrateInstrumentsWindow(
                poni_state=self._state, parent=self)
            self._calibration_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._calibration_window.destroyed.connect(
                lambda: setattr(self, "_calibration_window", None))
        self._calibration_window.show()
        self._calibration_window.raise_()

    def _open_image(self):
        start = self._prefs.get("last_image_dir", "")
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open XRD image", start, "TIFF images (*.tif *.tiff)")
        if filename:
            self.load_image(Path(filename))

    def _add_drop_target(self, widget: QtWidgets.QWidget):
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)
        self._drop_targets.add(widget)

    @staticmethod
    def _drop_image_path(event) -> Path | None:
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = Path(urls[0].toLocalFile())
        return path if path.suffix.lower() in (".tif", ".tiff") else None

    def _handle_image_drop_event(self, event) -> None:
        path = self._drop_image_path(event)
        if path is None or not (event.possibleActions() & Qt.DropAction.CopyAction):
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if event.type() == QtCore.QEvent.Type.Drop:
            self.load_image(path)

    def eventFilter(self, watched, event):
        if watched in self._drop_targets and event.type() in (
                QtCore.QEvent.Type.DragEnter,
                QtCore.QEvent.Type.DragMove,
                QtCore.QEvent.Type.Drop):
            self._handle_image_drop_event(event)
            return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        self._handle_image_drop_event(event)

    def dragMoveEvent(self, event):
        self._handle_image_drop_event(event)

    def dropEvent(self, event):
        self._handle_image_drop_event(event)

    def load_image(self, path: Path):
        try:
            image = tifffile.imread(path)
            if image.ndim != 2 or not np.issubdtype(image.dtype, np.number):
                raise ValueError("Select a two-dimensional greyscale TIFF image")
            if not np.all(np.isfinite(image)):
                raise ValueError("Image contains non-finite pixel values")
            if image.size == 0:
                raise ValueError("Image is empty")
            image = np.asarray(image)
            previous_shape = self._image.shape if self._image is not None else None
            self._mask_notice = ""
            spot = self._carry_mask("spot", image.shape, previous_shape)
            manual = self._carry_mask("manual", image.shape, previous_shape)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Image load error", str(exc))
            return
        if self._spot_pending or (self._worker is not None and
                                  self._job_operation == "spots"):
            self._mask_notice += "Spot detection for the previous image was cancelled. "
        self._spot_pending = False
        self._auto_skip_notice = ""
        self._spot_error_notice = ""
        self._spot_cancel_notice = ""
        self._image = image
        self._image_path = path
        self._spot_mask = spot
        self._manual_mask = manual
        self._profile = None
        self._curve.setData([], [])
        self._file_label.setText(str(path))
        self._image_info.setText(f"{image.shape[1]}×{image.shape[0]}  "
                                 f"min={float(image.min()):.0f}  "
                                 f"max={float(image.max()):.0f}  "
                                 f"mean={float(image.mean()):.0f}"
                                 + ("\n" + self._mask_notice if self._mask_notice else ""))
        self._prefs["last_image_dir"] = str(path.parent)
        self._save_prefs()
        self._auto_levels()
        self._render_image()
        self._request_reduction(image_load=True)
        self._auto_trigger_revision = self._revision

    def _carry_mask(self, kind, shape, previous_shape):
        policy = self._policy(self._spot_policy if kind == "spot" else self._manual_policy)
        current = self._spot_mask if kind == "spot" else self._manual_mask
        path = self._spot_file if kind == "spot" else self._manual_file
        if policy == "current" and previous_shape == shape and current is not None:
            return current.copy()
        if policy == "current" and current is not None and previous_shape != shape:
            self._mask_notice += f"{kind.title()} mask reset because image dimensions changed. "
        if policy == "file" and path is not None:
            try:
                mask = load_mask(path, shape)
                self._mask_notice += f"{kind.title()} mask loaded from {path}. "
                return mask
            except Exception as exc:
                self._mask_notice += f"{kind.title()} mask reset: {exc}. "
        return np.zeros(shape, dtype=bool)

    def _combined_mask(self):
        if self._image is None:
            return None
        return self._spot_mask | self._manual_mask

    def _render_image(self, *_args):
        if self._image is not None:
            self._canvas.set_frame(self._image, self._combined_mask(),
                                   self._min_spin.value(), self._max_spin.value())

    def _auto_levels(self):
        if self._image is None:
            return
        lo = max(0, min(65535, int(np.min(self._image))))
        hi = max(0, min(65535, int(np.max(self._image))))
        self._min_spin.setValue(lo)
        self._max_spin.setValue(hi)

    def _on_drawn(self, kind, start, end, add):
        if self._image is None:
            return
        area = shape_mask(self._image.shape, kind, start, end)
        if add:
            self._manual_mask[area] = True
        else:
            self._manual_mask[area] = False
            self._spot_mask[area] = False
            self._cancel_spot_detection()
        self._render_image()
        self._request_reduction(rerun_spots=add)

    def _cancel_spot_detection(self):
        """Cancel a pending or running search without blocking the GUI thread."""
        running = (self._worker is not None and self._job_operation == "spots"
                   and self._job_image is self._image)
        if not (self._spot_pending or running):
            return
        self._spot_pending = False
        if running:
            # The worker may still run, but its result and automatic retries
            # no longer belong to an active spot request.
            self._job_operation = "spots_cancelled"
        self._spot_cancel_notice = "Spot detection cancelled."
        self._status.setText(self._spot_cancel_notice)

    def _clear_masks(self):
        if self._image is None:
            return
        self._manual_mask[:] = False
        self._spot_mask[:] = False
        self._cancel_spot_detection()
        self._render_image()
        self._request_reduction(rerun_spots=False)

    def _load_mask(self, kind):
        if self._image is None:
            self._status.setText("Open an image before loading a mask.")
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load mask", self._prefs.get("last_mask_dir", ""),
            "Mask files (*.npz *.mas)")
        if not path:
            return
        try:
            mask = load_mask(Path(path), self._image.shape)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Mask load error", str(exc))
            return
        setattr(self, "_" + kind + "_mask", mask)
        setattr(self, "_" + kind + "_file", Path(path))
        self._prefs[kind + "_file"] = path
        self._prefs["last_mask_dir"] = str(Path(path).parent)
        self._save_prefs()
        self._render_image()
        self._request_reduction()

    def _save_mask(self, kind):
        if self._image is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save mask", self._prefs.get("last_mask_dir", ""),
            "Native mask (*.npz);;IPAnalyzer mask (*.mas)")
        if not path:
            return
        mask = self._spot_mask if kind == "spot" else self._combined_mask()
        try:
            save_mask(Path(path), mask)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Mask save error", str(exc))
            return
        setattr(self, "_" + kind + "_file", Path(path))
        self._prefs[kind + "_file"] = path
        self._prefs["last_mask_dir"] = str(Path(path).parent)
        self._save_prefs()

    def _request_reduction(self, *_args, image_load=False, rerun_spots=True):
        if self._image is None:
            return
        self._revision += 1
        if self._auto_trigger_revision is not None and not image_load:
            if self._autosave.isChecked() or self._autosend.isChecked():
                self._auto_skip_notice = (
                    "Automatic save/send skipped because the image or reduction settings "
                    "changed before the first result. Use Save 1D data or Send now."
                )
        self._auto_trigger_revision = None
        self._profile = None
        if not self._state.is_calibrated():
            self._reduce_pending = False
            self._spot_pending = False
            self._curve.setData([], [])
            self._status.setText(" | ".join(filter(None, (
                "Load a poni calibration in Detector settings.",
                self._mask_notice, self._prefs_error_notice))))
            return
        if (self._worker is not None and self._job_operation == "spots"
                and self._job_image is self._image and rerun_spots):
            self._spot_pending = True
            self._spot_cancel_notice = ""
        self._reduce_pending = True
        self._dispatch_pending()

    def _start_job(self, operation):
        if self._image is None:
            return
        if not self._state.is_calibrated():
            self._status.setText("Load a poni calibration before masking spots.")
            return
        if operation == "spots":
            self._revision += 1
            if self._auto_trigger_revision is not None:
                if self._autosave.isChecked() or self._autosend.isChecked():
                    self._auto_skip_notice = (
                        "Automatic save/send skipped because spot masking was requested "
                        "before the first result. Use Save 1D data or Send now."
                    )
                self._auto_trigger_revision = None
            self._spot_error_notice = ""
            self._spot_cancel_notice = ""
            self._spot_pending = True
        elif operation == "reduce":
            self._reduce_pending = True
        else:
            raise ValueError(f"Unknown reduction operation: {operation}")
        self._dispatch_pending()

    def _dispatch_pending(self):
        if self._worker is not None:
            return
        if self._image is None or not self._state.is_calibrated():
            return
        if self._spot_pending:
            operation = "spots"
            self._spot_pending = False
        elif self._reduce_pending:
            operation = "reduce"
            self._reduce_pending = False
        else:
            return
        self._job_operation = operation
        self._job_revision = self._revision
        self._job_image = self._image
        self._job_result = self._job_error = None
        minimum = None if self._min_angle.value() == -1 else self._min_angle.value()
        maximum = None if self._max_angle.value() == -1 else self._max_angle.value()
        try:
            ai = copy.deepcopy(self._state.integration_ai)
        except Exception as exc:
            self._status.setText(f"Could not prepare reduction geometry: {exc}")
            self._dispatch_pending()
            return
        self._worker = ReductionWorker(operation, ai,
                                       self._image, self._combined_mask(),
                                       self._width.value(), self._threshold.value(),
                                       minimum, maximum)
        self._worker.result_ready.connect(self._on_job_result)
        self._worker.failed.connect(self._on_job_error)
        self._worker.finished.connect(self._job_finished)
        self._status.setText("Finding spots…" if operation == "spots" else "Reducing image…")
        self._worker.start()

    @QtCore.pyqtSlot(object)
    def _on_job_result(self, result):
        self._job_result = result

    @QtCore.pyqtSlot(str)
    def _on_job_error(self, error):
        self._job_error = error

    @QtCore.pyqtSlot()
    def _job_finished(self):
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        current = (self._job_revision == self._revision and
                   self._job_image is self._image)
        if not self._closing and current:
            if self._job_error:
                if self._job_operation == "spots":
                    self._spot_error_notice = f"Spot detection failed: {self._job_error}"
                else:
                    self._curve.setData([], [])
                self._status.setText(" | ".join(filter(None, (
                    self._spot_error_notice if self._job_operation == "spots"
                    else f"Reduction failed: {self._job_error}",
                    self._auto_skip_notice, self._mask_notice,
                    self._prefs_error_notice, self._spot_cancel_notice))))
                self._spot_cancel_notice = ""
            elif self._job_operation == "spots":
                self._spot_error_notice = ""
                self._spot_mask |= self._job_result
                self._render_image()
                self._request_reduction()
            elif self._job_result is not None:
                self._profile = self._job_result
                self._curve.setData(self._profile.radial, self._profile.intensity)
                messages = [f"Reduced {len(self._profile.radial)} bins."]
                messages.extend(note for note in (
                    self._mask_notice, self._auto_skip_notice,
                    self._prefs_error_notice, self._spot_error_notice,
                    self._spot_cancel_notice,
                ) if note)
                if self._auto_trigger_revision == self._job_revision:
                    self._auto_trigger_revision = None
                    if self._autosave.isChecked():
                        messages.append(self._save_profile(report=False))
                    if self._autosend.isChecked():
                        messages.append(self._send_pdi(report=False))
                self._status.setText(" | ".join(messages))
                self._spot_cancel_notice = ""
        elif (not self._closing and self._job_operation == "spots"
              and not self._spot_pending and self._job_image is not self._image):
            self._status.setText("Spot detection for the previous image was cancelled.")
        if self._closing:
            self.close()
        else:
            self._dispatch_pending()

    def _save_profile(self, *_args, report=True):
        def done(message):
            if report:
                self._status.setText(message)
            return message
        if self._profile is None or self._image_path is None:
            return done("Reduce an image before saving.")
        formats = tuple(key for key, box in self._formats.items() if box.isChecked())
        units = tuple(key for key, box in self._units.items() if box.isChecked())
        if not formats:
            return done("Select at least one save format.")
        if ("csv" in formats or "tsv" in formats) and not units:
            return done("Select at least one X-axis unit.")
        try:
            ai = self._state.integration_ai
            paths, errors = save_profile(
                self._profile.radial, self._profile.intensity, ai.wavelength,
                ai.dist, self._image_path, formats=formats, units=units,
                exposure_ms=None, binning=None, source="XRD 1D reduction")
        except Exception as exc:
            return done(f"Save error: {exc}")
        parts = []
        if paths:
            parts.append("Saved: " + ", ".join(p.name for p in paths))
        if errors:
            parts.append("Save error: " + "; ".join(errors))
        return done("; ".join(parts) if parts else "No data was saved.")

    def _send_pdi(self, *_args, report=True):
        def done(message):
            if report:
                self._status.setText(message)
            return message
        if self._profile is None:
            return done("Reduce an image before sending.")
        transport = self._transport.currentData()
        if transport is None:
            return done("No PDIndexer transport is available.")
        try:
            profile = PdiProfile.from_pyfai(
                self._profile, wavelength_m=self._state.integration_ai.wavelength,
                name=self._image_path.stem)
            self._pdi.send([profile], trigger=Trigger.MANUAL, transport=transport)
            return done("PDIndexer send requested.")
        except Exception as exc:
            return done(f"PDIndexer error: {exc}")

    def closeEvent(self, event):
        if self._worker is not None:
            self._closing = True
            event.ignore()
            self._status.setText("Closing after the current reduction finishes…")
            return
        super().closeEvent(event)


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    window = Xrd1DReductionWindow()
    window.show()
    sys.exit(app.exec())
