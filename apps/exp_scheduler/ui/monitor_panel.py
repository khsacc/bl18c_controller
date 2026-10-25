"""
Run monitor panel — right-hand column of ExperimentalSchedulerWindow.

Shows, while a sequence runs (and while idle where a source exists):

1. Microscope live image, with the Interactive Camera's persisted marks
   (shapes.json / laser_spot.json) overlaid.
2. PACE5000 current pressure + pressure-vs-time plot (when connected).
3. LakeShore 335 current temperatures + temperature-vs-time plot (when
   connected). Shown side by side with the PACE5000 group.
4. Latest XRD image (Min/Max/Auto levels as in the Rad-icon window) and, when
   Instant 1D is on, its 1D profile side by side — shown from the start of a
   run whose sequence takes XRD images, and kept after it finishes
   (SequenceRunner.xrd_image_ready / xrd_profile_ready).
5. Live run log: every line the runner writes to <run>/log/ops.log
   (SequenceRunner.ops_logged), cleared at the start of each run.
6. Sample pressure plot below XRD when ruby pressure calculation is enabled.

Camera source selection (the USB camera can only be opened by one owner):

* Idle: mirror ``current_frame`` of the Interactive Camera window if it is
  open; otherwise show a placeholder. The panel never opens the camera while
  idle, so validation probes, the sample-positions dialog and
  the Interactive Camera itself keep exclusive access.
* Running, sequence uses the camera: show the runner's own camera-session
  frames (``SequenceRunner.latest_camera_frame()``).
* Running, sequence does not use the camera: the panel opens camera 0 itself
  once the runner thread has started, and releases it in ``run_finished()``
  — before the scheduler re-opens the sub-windows closed at run start.
"""
from __future__ import annotations

import threading
import time
import math
from datetime import datetime
from collections import deque
from typing import Callable

import cv2
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from apps.Rad_icon_2022.image_view import ImageLabel, levels_to_qimage, tint_excluded_angles
from apps.Rad_icon_2022.instant1d import excluded_angle_mask
from apps.interactive_camera.marks import MarksFileCache, draw_saved_marks
from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole, detect_gasket_hole_near, sample_offset,
)
from apps.interactive_camera.xray_beam import XrayBeamFileCache, draw_xray_beam
from apps.exp_scheduler.gasket_units import (
    displacement_um, distance_um, load_stage_um_per_pixel, nominal_um_per_pixel,
)

_CAMERA_INDEX = 0
_CAMERA_REFRESH_MS = 100
_DEVICE_REFRESH_MS = 1000
_PACE_HISTORY_S = 3600
_PLOT_HEIGHT_PX = 290  # ~60 % of pyqtgraph's 480 px default size hint
_DEFAULT_WINDOW_S = 300
_LOG_MAX_LINES = 5000  # oldest lines drop off; ops.log on disk keeps everything


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class _RectZoomViewBox(pg.ViewBox):
    """Time-plot ViewBox: the mouse wheel never zooms (it scrolls the panel
    instead), and a right-button drag zooms to the dragged rectangle. A plain
    right-click still opens the context menu ("View All" resets the zoom)."""

    zoomed = pyqtSignal()
    zoom_reset = pyqtSignal()

    def wheelEvent(self, ev, axis=None):
        ev.ignore()

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() != Qt.MouseButton.RightButton or axis is not None:
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        if ev.isFinish():
            self.rbScaleBox.hide()
            rect = self.childGroup.mapRectFromParent(
                QRectF(ev.buttonDownPos(ev.button()), ev.pos()).normalized())
            if rect.width() > 0 and rect.height() > 0:
                self.showAxRect(rect)
                self.axHistoryPointer += 1
                self.axHistory = self.axHistory[:self.axHistoryPointer] + [rect]
        else:
            self.updateScaleBox(ev.buttonDownPos(), ev.pos())

    def showAxRect(self, ax, **kwargs):
        super().showAxRect(ax, **kwargs)
        self.zoomed.emit()

    def autoRange(self, *args, **kwargs):
        super().autoRange(*args, **kwargs)
        self.zoom_reset.emit()


class _ClockAxisItem(pg.AxisItem):
    """Always show local clock time, including for a single ruby reading."""

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            try:
                labels.append(datetime.fromtimestamp(value).strftime("%H:%M:%S")
                              if math.isfinite(value) else "")
            except (OverflowError, OSError, ValueError):
                labels.append("")
        return labels


class _CameraCapture:
    """Background latest-frame reader for one VideoCapture. Opening happens on
    the worker thread too, so a slow DirectShow open never blocks the UI."""

    def __init__(self, index: int) -> None:
        self._index = index
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_at = 0.0
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="SchedulerMonitorCamera", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self._index)
        if not cap.isOpened():
            cap.release()
            self.error = f"Cannot open camera index {self._index}"
            return
        try:
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            while not self._stop.is_set():
                ret, frame = cap.read()
                if ret and frame is not None:
                    with self._lock:
                        self._frame = frame
                        self._frame_at = time.monotonic()
                    self.error = None
                else:
                    self.error = "Camera read failed"
                    time.sleep(0.05)
        finally:
            cap.release()

    def latest(self) -> tuple[np.ndarray, float] | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._frame, self._frame_at

    def stop(self, timeout: float = 2.0) -> None:
        """Stop and release; on return the camera is free for the next owner
        (unless read() hangs past `timeout`, which is printed)."""
        self._stop.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            print("[Scheduler monitor] camera thread did not stop within "
                  f"{timeout:.0f} s; the camera may still be held")


class MonitorPanel(QWidget):
    # Shutter status from the STARS reader / query threads (bool | None).
    _shutter_status_received = pyqtSignal(object, int)
    _shutter_query_failed = pyqtSignal(str, int)
    _gasket_result_received = pyqtSignal(object)

    def __init__(self, ctx, interactive_camera_window: Callable[[], object] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._icw_provider = interactive_camera_window or (lambda: None)
        self._marks = MarksFileCache()

        self._runner = None
        self._runner_uses_camera = False
        self._own_capture: _CameraCapture | None = None
        self._last_frame_key = None
        self._gasket_reference: np.ndarray | None = None
        self._gasket_marked: GasketEllipse | None = None
        self._gasket_sample_offset: tuple[float, float] | None = None
        self._gasket_um_matrix: np.ndarray | None = None
        self._gasket_allow_uniform_scale = False
        self._xray_beam = XrayBeamFileCache()
        self._gasket_generation = 0
        self._gasket_worker_busy = False
        self._gasket_next_scan_at = 0.0
        self._gasket_preview_was_enabled = False
        self._gasket_shutdown = False

        self._pace_backend = None
        self._pace_user_zoomed = False
        self._pace_times: deque = deque()
        self._pace_pressures: deque = deque()
        self._lakeshore_cleared_at = 0.0
        self._ruby_pressure_enabled = False
        self._sample_pressures: list[tuple[float, float, float | None]] = []
        self._sample_user_zoomed = False
        self._sample_axis_mode = "time"

        # Shutter monitoring is per run: _shutter_run_id tags every
        # thread-originated update so late ones from a finished run are dropped.
        self._shutter_run_id = 0
        self._shutter_listener = None
        self._shutter_owns_connection = False
        self._shutter_query_thread: threading.Thread | None = None
        self._shutter_status_received.connect(self._on_shutter_status)
        self._shutter_query_failed.connect(self._on_shutter_query_failed)
        self._gasket_result_received.connect(self._on_gasket_result)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._make_camera_group())
        layout.addWidget(self._make_shutter_row())
        # PACE5000 and LakeShore side by side; if only one is connected it
        # takes the full width.
        device_row = QHBoxLayout()
        device_row.setSpacing(6)
        self._pace_group = self._make_pace_group()
        device_row.addWidget(self._pace_group, 1)
        self._lakeshore_group = self._make_lakeshore_group()
        device_row.addWidget(self._lakeshore_group, 1)
        layout.addLayout(device_row)
        # Latest XRD image and its 1D profile, side by side like the row above.
        self._xrd_image: np.ndarray | None = None
        self._xrd_levels_set = False
        self._xrd_mask: np.ndarray | None = None
        self._xrd_mask_key = None
        self._xrd_mask_ai = None
        self._xrd_mask_limits = (None, None)
        xrd_row = QHBoxLayout()
        xrd_row.setSpacing(6)
        self._xrd_image_group = self._make_xrd_image_group()
        xrd_row.addWidget(self._xrd_image_group, 1)
        self._xrd_profile_group = self._make_xrd_profile_group()
        xrd_row.addWidget(self._xrd_profile_group, 1)
        layout.addLayout(xrd_row)
        self._sample_group = self._make_sample_plot_group()
        layout.addWidget(self._sample_group)
        layout.addWidget(self._make_log_group(), stretch=1)

        self._camera_timer = QTimer(self)
        self._camera_timer.setInterval(_CAMERA_REFRESH_MS)
        self._camera_timer.timeout.connect(self._refresh_camera)
        self._camera_timer.start()

        self._device_timer = QTimer(self)
        self._device_timer.setInterval(_DEVICE_REFRESH_MS)
        self._device_timer.timeout.connect(self._refresh_devices)
        self._device_timer.start()
        self._refresh_devices()
        self._refresh_camera()

    # ── construction ─────────────────────────────────────────────────────

    def _make_camera_group(self) -> QGroupBox:
        group = QGroupBox("Microscope")
        vbox = QVBoxLayout(group)
        self._camera_enable_chk = QCheckBox("Show live image")
        self._camera_enable_chk.setChecked(True)
        self._camera_enable_chk.setToolTip(
            "While a sequence without camera steps runs, the monitor opens "
            f"camera {_CAMERA_INDEX} itself. Untick to leave the camera alone."
        )
        self._camera_enable_chk.toggled.connect(self._on_camera_enable_toggled)
        self._marks_chk = QCheckBox("Show Interactive Camera marks")
        self._marks_chk.setChecked(True)
        self._marks_chk.toggled.connect(self._on_marks_toggled)
        row = QHBoxLayout()
        row.addWidget(self._camera_enable_chk)
        row.addWidget(self._marks_chk)
        row.addStretch()
        vbox.addLayout(row)

        self._camera_label = QLabel()
        self._camera_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._camera_label.setWordWrap(True)
        self._camera_label.setMinimumSize(320, 408)
        self._camera_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._camera_label.setStyleSheet("background: #202020; color: #bbbbbb;")
        vbox.addWidget(self._camera_label, stretch=1)

        self._camera_source_label = QLabel()
        self._camera_source_label.setStyleSheet("color: #555; font-size: 9pt;")
        self._camera_source_label.setWordWrap(True)
        vbox.addWidget(self._camera_source_label)
        return group

    def _make_shutter_row(self) -> QFrame:
        """One-line X-ray shutter indicator (states as in tools/shutter_tool.py)."""
        frame = QFrame()
        frame.setObjectName("shutterRow")
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(8)
        title = QLabel("X-ray shutter:")
        title.setStyleSheet("font-weight: bold; border: none; background: transparent;")
        row.addWidget(title)
        self._shutter_label = QLabel()
        row.addWidget(self._shutter_label, stretch=1)
        self._shutter_frame = frame
        self._render_shutter_idle()
        return frame

    def _set_shutter_display(self, text: str, colour: str, background: str, border: str,
                             *, italic: bool = False, tooltip: str = "") -> None:
        self._shutter_frame.setStyleSheet(
            f"QFrame#shutterRow {{ background: {background}; border: 1px {border}; "
            "border-radius: 1px; }")
        self._shutter_label.setStyleSheet(
            f"color: {colour}; font-weight: bold; border: none; background: transparent;"
            + (" font-style: italic; font-weight: normal;" if italic else ""))
        self._shutter_label.setText(text)
        self._shutter_label.setToolTip(tooltip)

    def _render_shutter_idle(self) -> None:
        # Deliberately not a position: greyed out, dashed, no colour, so it
        # can never be mistaken for the current shutter state.
        self._set_shutter_display("—  not monitored (no sequence running)", "#9e9e9e",
                                  "#f2f2f2", "dashed #bdbdbd", italic=True)

    def _render_shutter(self, inserted: bool | None, detail: str = "") -> None:
        if inserted is True:
            self._set_shutter_display("IN — X-rays blocked", "#a65e64", "#f6efef", "solid #a65e64",
                                      tooltip=detail)
        elif inserted is False:
            self._set_shutter_display("OUT — X-rays at sample", "#5e83a6", "#eff3f6", "solid #5e83a6",
                                      tooltip=detail)
        else:
            self._set_shutter_display(detail or "Unknown shutter position", "#ad6a00", "#fff8e1",
                                      "solid #ad6a00", tooltip=detail)

    @staticmethod
    def _make_plot(left_label: str, title: str) -> pg.PlotWidget:
        plot = pg.PlotWidget(viewBox=_RectZoomViewBox(), axisItems={"bottom": pg.DateAxisItem()})
        plot.setTitle(title)
        plot.setLabel("left", left_label)
        plot.setLabel("bottom", "Time")
        plot.getPlotItem().getAxis("left").enableAutoSIPrefix(False)
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.plotItem.vb.setMouseMode(pg.ViewBox.RectMode)
        plot.setFixedHeight(_PLOT_HEIGHT_PX)
        return plot

    @staticmethod
    def _window_row(spin: QSpinBox, clear_btn: QPushButton) -> QHBoxLayout:
        spin.setRange(10, 7200)
        spin.setValue(_DEFAULT_WINDOW_S)
        spin.setMaximumWidth(135)
        spin.setToolTip("Display window: show only the latest N seconds")
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(QLabel("Window:"))
        row.addWidget(spin)
        row.addWidget(QLabel("sec"))
        row.addSpacing(16)
        row.addWidget(clear_btn)
        return row

    def _make_pace_group(self) -> QGroupBox:
        group = QGroupBox("PACE5000 — Live Pressure")
        vbox = QVBoxLayout(group)
        self._pace_label = QLabel("Current Pressure:  ---  MPa")
        self._pace_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pace_label.setStyleSheet("font-size: 22px; color: black;")
        vbox.addWidget(self._pace_label)
        self._pace_setpoint_label = QLabel("Target Pressure:  ---    Slew Rate:  ---")
        self._pace_setpoint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pace_setpoint_label.setStyleSheet("font-size: 10pt; color: #555;")
        vbox.addWidget(self._pace_setpoint_label)
        self._pace_plot = self._make_plot("Pressure (MPa)", "Pressure vs Time")
        self._pace_curve = self._pace_plot.plot(pen=pg.mkPen("#1a6fc4", width=2))
        pace_vb = self._pace_plot.plotItem.vb
        pace_vb.zoomed.connect(lambda: self._set_pace_zoomed(True))
        pace_vb.zoom_reset.connect(lambda: self._set_pace_zoomed(False))
        vbox.addWidget(self._pace_plot)
        self._pace_window_spin = _no_wheel(QSpinBox())
        btn_clear = QPushButton("Clear Graph")
        btn_clear.clicked.connect(self._clear_pace)
        vbox.addLayout(self._window_row(self._pace_window_spin, btn_clear))
        self._pace_window_spin.valueChanged.connect(lambda _v: self._redraw_pace())
        group.hide()
        return group

    def _make_lakeshore_group(self) -> QGroupBox:
        group = QGroupBox("LakeShore 335 — Temperature Monitor")
        vbox = QVBoxLayout(group)
        self._ls_ab_label = QLabel("Ch A:  ---  K    Ch B:  ---  K")
        self._ls_ab_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ls_ab_label.setStyleSheet("font-size: 22px; color: black;")
        vbox.addWidget(self._ls_ab_label)
        self._ls_sp_label = QLabel("Setpoint:  ---  K    Heater:  ---    Output:  ---")
        self._ls_sp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ls_sp_label.setStyleSheet("font-size: 10pt; color: #555;")
        vbox.addWidget(self._ls_sp_label)
        self._ls_plot = self._make_plot("Temperature (K)", "Temperature vs Time")
        # Curves above the grid (see LakeShore335Window._build_plot_frame).
        self._ls_plot.plotItem.vb.setZValue(10)
        self._ls_plot.addLegend(offset=(-10, 10))
        self._ls_line_a = self._ls_plot.plot(pen=pg.mkPen("#1a6fc4", width=2), name="Ch A")
        self._ls_line_b = self._ls_plot.plot(pen=pg.mkPen("#2e8b57", width=2), name="Ch B")
        self._ls_line_sp = self._ls_plot.plot(
            pen=pg.mkPen("#b0413e", width=2, style=Qt.PenStyle.DashLine), name="Setpoint (ramp)")
        # A zoom turns pyqtgraph's auto-range off; "View All" turns it back
        # on so the plot follows new readings again.
        ls_vb = self._ls_plot.plotItem.vb
        ls_vb.zoom_reset.connect(lambda: ls_vb.enableAutoRange())
        vbox.addWidget(self._ls_plot)
        self._ls_window_spin = _no_wheel(QSpinBox())
        btn_clear = QPushButton("Clear Graph")
        btn_clear.clicked.connect(self._clear_lakeshore)
        vbox.addLayout(self._window_row(self._ls_window_spin, btn_clear))
        self._ls_window_spin.valueChanged.connect(lambda _v: self._refresh_lakeshore())
        group.hide()
        return group

    def _make_xrd_image_group(self) -> QGroupBox:
        """Latest XRD frame; level controls copy RadiconWindow's image panel."""
        group = QGroupBox("XRD — Latest Image")
        vbox = QVBoxLayout(group)
        dark = QWidget()
        dark.setStyleSheet("background: #111;")
        dark_vbox = QVBoxLayout(dark)
        dark_vbox.setContentsMargins(4, 4, 4, 4)
        dark_vbox.setSpacing(2)

        self._xrd_img_label = ImageLabel()
        self._xrd_img_label.setMinimumSize(200, 200)
        self._xrd_img_label.setFixedHeight(_PLOT_HEIGHT_PX)
        dark_vbox.addWidget(self._xrd_img_label)

        slider_widget = QWidget()
        slider_widget.setStyleSheet("background: #1e1e1e; color: #ccc; font-size: 11px;")
        sg = QGridLayout(slider_widget)
        sg.setContentsMargins(6, 4, 6, 4)
        sg.setSpacing(4)
        sg.addWidget(QLabel("Min"), 0, 0)
        self._xrd_min_slider = _no_wheel(QSlider(Qt.Orientation.Horizontal))
        self._xrd_min_slider.setRange(0, 65535)
        self._xrd_min_slider.setValue(0)
        sg.addWidget(self._xrd_min_slider, 0, 1)
        self._xrd_min_spin = _no_wheel(QSpinBox())
        self._xrd_min_spin.setRange(0, 65535)
        self._xrd_min_spin.setValue(0)
        self._xrd_min_spin.setFixedWidth(68)
        sg.addWidget(self._xrd_min_spin, 0, 2)
        sg.addWidget(QLabel("Max"), 1, 0)
        self._xrd_max_slider = _no_wheel(QSlider(Qt.Orientation.Horizontal))
        self._xrd_max_slider.setRange(0, 65535)
        self._xrd_max_slider.setValue(65535)
        sg.addWidget(self._xrd_max_slider, 1, 1)
        self._xrd_max_spin = _no_wheel(QSpinBox())
        self._xrd_max_spin.setRange(0, 65535)
        self._xrd_max_spin.setValue(65535)
        self._xrd_max_spin.setFixedWidth(68)
        sg.addWidget(self._xrd_max_spin, 1, 2)
        auto_btn = QPushButton("Auto")
        auto_btn.clicked.connect(self._xrd_auto_levels)
        sg.addWidget(auto_btn, 0, 3, 2, 1)
        self._xrd_min_slider.valueChanged.connect(self._xrd_min_spin.setValue)
        self._xrd_min_spin.valueChanged.connect(self._xrd_min_slider.setValue)
        self._xrd_max_slider.valueChanged.connect(self._xrd_max_spin.setValue)
        self._xrd_max_spin.valueChanged.connect(self._xrd_max_slider.setValue)
        self._xrd_min_slider.valueChanged.connect(self._render_xrd_image)
        self._xrd_max_slider.valueChanged.connect(self._render_xrd_image)
        dark_vbox.addWidget(slider_widget)

        self._xrd_info_label = QLabel("—")
        self._xrd_info_label.setStyleSheet(
            "color: #aaa; font-size: 11px; padding: 2px 4px; background: #1e1e1e;")
        self._xrd_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._xrd_info_label.setWordWrap(True)
        dark_vbox.addWidget(self._xrd_info_label)
        vbox.addWidget(dark)
        group.hide()
        return group

    def _make_xrd_profile_group(self) -> QGroupBox:
        group = QGroupBox("XRD — Latest 1D Profile")
        vbox = QVBoxLayout(group)
        self._xrd_plot = pg.PlotWidget(viewBox=_RectZoomViewBox())
        self._xrd_plot.setLabel("left", "Intensity")
        self._xrd_plot.setLabel("bottom", "2θ (deg)")
        self._xrd_plot.getPlotItem().getAxis("left").enableAutoSIPrefix(False)
        self._xrd_plot.showGrid(x=True, y=True, alpha=0.2)
        self._xrd_plot.plotItem.vb.setMouseMode(pg.ViewBox.RectMode)
        self._xrd_plot.setFixedHeight(_PLOT_HEIGHT_PX)
        self._xrd_curve = self._xrd_plot.plot(pen=pg.mkPen("#1a6fc4", width=1.5))
        vbox.addWidget(self._xrd_plot)
        self._xrd_profile_label = QLabel("—")
        self._xrd_profile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._xrd_profile_label.setStyleSheet("color: #555; font-size: 9pt;")
        self._xrd_profile_label.setWordWrap(True)
        vbox.addWidget(self._xrd_profile_label)
        vbox.addStretch()
        group.hide()
        return group

    def _on_xrd_image(self, image, name: str) -> None:
        self._xrd_image = image
        key = (id(self._xrd_mask_ai), image.shape, self._xrd_mask_limits)
        if self._xrd_mask_key != key:
            self._xrd_mask = None
            if self._xrd_mask_ai is not None:
                try:
                    self._xrd_mask = excluded_angle_mask(
                        self._xrd_mask_ai, image.shape, *self._xrd_mask_limits)
                except ValueError:
                    pass  # The worker reports invalid reduction settings.
            self._xrd_mask_key = key
        h, w = image.shape
        self._xrd_info_label.setText("  ".join([
            name, f"{w}×{h}", f"min={int(image.min())}", f"max={int(image.max())}",
            f"mean={float(image.mean()):.0f}"]))
        if not self._xrd_levels_set:
            # The first frame starts auto-levelled; afterwards the user's
            # Min/Max stay put so successive frames compare directly.
            self._xrd_levels_set = True
            self._xrd_auto_levels()
        self._render_xrd_image()

    def _render_xrd_image(self) -> None:
        if self._xrd_image is None:
            return
        qimg = levels_to_qimage(self._xrd_image, self._xrd_min_slider.value(),
                                self._xrd_max_slider.value())
        if self._xrd_mask is not None:
            qimg = tint_excluded_angles(qimg, self._xrd_mask)
        self._xrd_img_label.setSourcePixmap(QPixmap.fromImage(qimg))

    def _xrd_auto_levels(self) -> None:
        if self._xrd_image is None:
            return
        self._xrd_min_slider.setValue(int(self._xrd_image.min()))
        self._xrd_max_slider.setValue(int(self._xrd_image.max()))

    def _on_xrd_profile(self, radial, intensity, name: str) -> None:
        self._xrd_curve.setData(radial, intensity)
        self._xrd_plot.plotItem.enableAutoRange()
        self._xrd_profile_label.setText(name)

    def _make_sample_plot_group(self) -> QGroupBox:
        group = QGroupBox("Sample pressure — Live View")
        vbox = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Plot:"))
        self._sample_plot_combo = _no_wheel(QComboBox())
        self._sample_plot_combo.addItem("Time vs Sample P", "time")
        self._sample_plot_combo.currentIndexChanged.connect(self._on_sample_plot_changed)
        row.addWidget(self._sample_plot_combo)
        row.addStretch()
        vbox.addLayout(row)
        self._sample_plot = self._make_plot("Sample P (GPa)", "Sample pressure")
        self._sample_plot.setAxisItems({"bottom": _ClockAxisItem("bottom")})
        self._sample_curve = self._sample_plot.plot(
            pen=pg.mkPen("#8e44ad", width=2), symbol="o", symbolSize=7,
            symbolBrush="#8e44ad")
        view = self._sample_plot.plotItem.vb
        view.zoomed.connect(lambda: self._set_sample_zoomed(True))
        view.zoom_reset.connect(lambda: self._set_sample_zoomed(False))
        zoom_out = view.getMenu(None).addAction("Zoom Out")
        zoom_out.triggered.connect(self._zoom_out_sample_plot)
        vbox.addWidget(self._sample_plot)
        self._sample_plot_status = QLabel("No valid ruby pressure measurements yet.")
        self._sample_plot_status.setStyleSheet("color: #555; font-size: 9pt;")
        vbox.addWidget(self._sample_plot_status)
        group.hide()
        return group

    def set_ruby_pressure_enabled(self, enabled: bool) -> None:
        self._ruby_pressure_enabled = enabled
        self._refresh_sample_plot_options()

    def _refresh_sample_plot_options(self) -> None:
        lakeshore = self._ctx.lakeshore
        ls_on = lakeshore is not None and bool(getattr(lakeshore, "is_connected", False))
        combo = self._sample_plot_combo
        index = combo.findData("temperature")
        if ls_on and index < 0:
            combo.addItem("Sample P vs Sample T", "temperature")
        elif not ls_on and index >= 0:
            combo.removeItem(index)
        self._sample_group.setVisible(self._ruby_pressure_enabled)

    def _on_sample_pressure(self, timestamp: float, pressure: float,
                            temperature: float | None) -> None:
        if not self._ruby_pressure_enabled or not math.isfinite(pressure):
            return
        self._sample_pressures.append((timestamp, pressure, temperature))
        self._redraw_sample_plot()

    def _on_sample_plot_changed(self, _index: int) -> None:
        self._sample_user_zoomed = False
        self._redraw_sample_plot()

    def _redraw_sample_plot(self, _index: int = 0) -> None:
        by_temperature = self._sample_plot_combo.currentData() == "temperature"
        plot = self._sample_plot
        mode = "temperature" if by_temperature else "time"
        if mode != self._sample_axis_mode:
            plot.setAxisItems({"bottom": pg.AxisItem("bottom") if by_temperature
                               else _ClockAxisItem("bottom")})
            self._sample_axis_mode = mode
        plot.setLabel("bottom", "Sample T (K)" if by_temperature else "Time")
        points = [(temperature, pressure) for _, pressure, temperature in self._sample_pressures
                  if temperature is not None and math.isfinite(temperature)] if by_temperature else [
                      (timestamp, pressure) for timestamp, pressure, _ in self._sample_pressures]
        self._sample_curve.setData([x for x, _ in points], [y for _, y in points])
        self._sample_plot_status.setText(
            f"{len(points)} valid measurement{'s' if len(points) != 1 else ''}"
            if points else ("No pressure measurements with a recent Sample T reading."
                            if by_temperature and self._sample_pressures else
                            "No valid ruby pressure measurements yet.")
        )
        if not self._sample_user_zoomed:
            plot.plotItem.enableAutoRange()

    def _set_sample_zoomed(self, zoomed: bool) -> None:
        self._sample_user_zoomed = zoomed
        if not zoomed:
            self._redraw_sample_plot()

    def _zoom_out_sample_plot(self) -> None:
        self._sample_user_zoomed = True
        self._sample_plot.plotItem.vb.scaleBy((2, 2))

    def _make_log_group(self) -> QGroupBox:
        group = QGroupBox("Run log (ops.log)")
        vbox = QVBoxLayout(group)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log_view.setMaximumBlockCount(_LOG_MAX_LINES)
        self._log_view.setFont(QFont("Consolas", 9))
        self._log_view.setMinimumHeight(200)
        self._log_view.setPlaceholderText("ops.log lines appear here while a sequence runs.")
        vbox.addWidget(self._log_view, stretch=1)
        row = QHBoxLayout()
        row.addStretch()
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._log_view.clear)
        row.addWidget(btn_clear)
        vbox.addLayout(row)
        return group

    def _append_log_line(self, line: str) -> None:
        # Follow the tail only if the user has not scrolled up to read.
        bar = self._log_view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        self._log_view.appendPlainText(line)
        if at_bottom:
            bar.setValue(bar.maximum())

    # ── run lifecycle (called by ExperimentalSchedulerWindow) ────────────

    def run_started(self, runner, uses_camera: bool, shutter_managed: bool = False,
                    uses_xrd: bool = False, xrd_1d: bool = False, xrd_ai=None,
                    xrd_min_2theta: float | None = None,
                    xrd_max_2theta: float | None = None) -> None:
        """Call before runner.start(). `shutter_managed` is True when the
        runner itself drives the shutter (GlobalShutterSettings.only_open_during_xrd).
        `uses_xrd` shows the latest-XRD row; `xrd_1d` (Instant 1D on) adds
        the 1D profile next to the image."""
        # Each run starts a fresh view, mirroring its own ops.log file; the
        # previous run's lines stay visible after it finishes.
        self._log_view.clear()
        self._sample_pressures.clear()
        self._sample_user_zoomed = False
        self._redraw_sample_plot()
        runner.ops_logged.connect(self._append_log_line)
        runner.sample_pressure_ready.connect(self._on_sample_pressure)
        # The XRD row keeps the last run's result until a new frame arrives.
        self._xrd_image_group.setVisible(uses_xrd)
        self._xrd_profile_group.setVisible(uses_xrd and xrd_1d)
        self._xrd_mask = None
        self._xrd_mask_key = None
        self._xrd_mask_ai = xrd_ai if xrd_1d else None
        self._xrd_mask_limits = (xrd_min_2theta, xrd_max_2theta)
        if uses_xrd:
            runner.xrd_image_ready.connect(self._on_xrd_image)
            if xrd_1d:
                runner.xrd_profile_ready.connect(self._on_xrd_profile)
        self._runner = runner
        self._runner_uses_camera = uses_camera
        self._gasket_generation += 1
        self._gasket_preview_was_enabled = False
        self._last_frame_key = None
        if self._gasket_reference is not None:
            try:
                self._gasket_um_matrix = load_stage_um_per_pixel()
            except (OSError, ValueError, KeyError, TypeError):
                self._gasket_um_matrix = None
        if not uses_camera:
            # Open only once the runner thread really runs.
            runner.started.connect(self._open_own_capture_if_wanted)
        self._start_shutter_monitoring(shutter_managed)

    def run_finished(self) -> None:
        """Release the monitor's own camera synchronously, so sub-windows
        restored right after (Interactive Camera) can open it."""
        self._runner = None
        self._runner_uses_camera = False
        self._gasket_generation += 1
        self._gasket_preview_was_enabled = False
        self._last_frame_key = None
        self._close_own_capture()
        self._stop_shutter_monitoring()

    def shutdown(self) -> None:
        self._gasket_shutdown = True
        self._gasket_generation += 1
        self._camera_timer.stop()
        self._device_timer.stop()
        self._close_own_capture()
        self._stop_shutter_monitoring()
        self._set_pace_backend(None)

    # ── X-ray shutter ────────────────────────────────────────────────────

    def _start_shutter_monitoring(self, shutter_managed: bool) -> None:
        self._stop_shutter_monitoring()
        shutter = self._ctx.shutter
        if shutter is None:
            self._set_shutter_display("Not configured", "#757575", "#f2f2f2", "solid #bdbdbd")
            return
        self._shutter_run_id += 1
        run_id = self._shutter_run_id
        self._shutter_listener = lambda inserted: self._shutter_status_received.emit(inserted, run_id)
        shutter.add_status_listener(self._shutter_listener)
        self._set_shutter_display("Checking…", "#555555", "#f5f5f5", "solid #9e9e9e")
        if shutter_managed:
            # The runner queries/drives the shutter right away and every
            # result is published to the listener; a parallel first query
            # from here could race ShutterController.connect().
            return

        # Nothing else talks to STARS during this run: query once (which
        # also subscribes to stopper.Status change events) and close the
        # connection again in _stop_shutter_monitoring().
        self._shutter_owns_connection = True

        def query() -> None:
            try:
                shutter.get_status()  # published to the listener
            except Exception as exc:
                self._shutter_query_failed.emit(str(exc), run_id)

        self._shutter_query_thread = threading.Thread(
            target=query, name="scheduler-monitor-shutter", daemon=True)
        self._shutter_query_thread.start()

    def _stop_shutter_monitoring(self) -> None:
        shutter = self._ctx.shutter
        self._shutter_run_id += 1  # drop queued updates from this run
        if shutter is not None and self._shutter_listener is not None:
            shutter.remove_status_listener(self._shutter_listener)
        self._shutter_listener = None
        if shutter is not None and self._shutter_owns_connection:
            thread = self._shutter_query_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
            try:
                shutter.close()
            except Exception:
                pass
        self._shutter_owns_connection = False
        self._shutter_query_thread = None
        self._render_shutter_idle()

    def _on_shutter_status(self, inserted, run_id: int) -> None:
        if run_id == self._shutter_run_id:
            self._render_shutter(inserted)

    def _on_shutter_query_failed(self, error: str, run_id: int) -> None:
        if run_id == self._shutter_run_id:
            self._render_shutter(None, f"Unknown — {error}")

    # ── camera ───────────────────────────────────────────────────────────

    def configure_gasket_overlay(self, reference_path: str | None, ellipse: dict | None,
                                 sample_point: dict | None = None,
                                 allow_uniform_scale: bool = False) -> None:
        """Configure a diagnostic overlay; this never issues stage commands."""
        self._gasket_generation += 1
        self._gasket_reference = None
        self._gasket_marked = None
        self._gasket_sample_offset = None
        self._gasket_um_matrix = None
        self._gasket_allow_uniform_scale = allow_uniform_scale
        if reference_path is not None and ellipse is not None:
            try:
                self._gasket_um_matrix = load_stage_um_per_pixel()
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                frame = cv2.imread(reference_path, cv2.IMREAD_COLOR)
                marked = GasketEllipse.from_dict(ellipse)
                if frame is not None:
                    self._gasket_reference = frame
                    self._gasket_marked = marked
                    if sample_point:
                        found = detect_gasket_hole(
                            frame, frame, marked,
                            allow_uniform_scale=allow_uniform_scale)
                        self._gasket_sample_offset = sample_offset(
                            sample_point, (found.ellipse.cx, found.ellipse.cy))
            except (ValueError, KeyError, TypeError, cv2.error):
                pass
        self._gasket_next_scan_at = 0.0
        self._last_frame_key = None

    def _gasket_preview_active(self) -> bool:
        """Only the beta follow session, outside autofocus, needs live fits."""
        return bool(self._gasket_preview_state()[0]
                    and self._gasket_reference is not None
                    and self._gasket_marked is not None)

    def _gasket_preview_state(self) -> tuple[bool, int]:
        if self._runner is None:
            return False, 0
        state = getattr(self._runner, "gasket_preview_state", None)
        if state is not None:
            return state
        return bool(getattr(self._runner, "gasket_preview_enabled", False)), 0

    def _gasket_preview_token(self) -> tuple[int, int]:
        return self._gasket_generation, self._gasket_preview_state()[1]

    def _scan_gasket(self, frame: np.ndarray, reference: np.ndarray,
                     marked: GasketEllipse, generation: int, shapes: list, laser,
                     source: str, target: tuple[float, float] | None = None,
                     allow_uniform_scale: bool = False) -> None:
        if generation != self._gasket_preview_token() or not self._gasket_preview_active():
            detection = None
            error = "Preview paused"
        else:
            try:
                detection = detect_gasket_hole_near(
                    reference, frame, marked, (target, None),
                    allow_uniform_scale=allow_uniform_scale)
                error = None
            except Exception as exc:
                detection = None
                error = str(exc)
        if not self._gasket_shutdown:
            try:
                self._gasket_result_received.emit((generation, frame, detection, error,
                                                    shapes, laser, source))
            except RuntimeError:
                pass  # The monitor was closed while this frame was analysed.

    def _gasket_target(self) -> tuple[float, float] | None:
        """Rim centre that puts the marked sample on the shared X-ray beam."""
        beam = self._xray_beam.get()
        if beam is None or self._gasket_sample_offset is None:
            return None
        return beam.x - self._gasket_sample_offset[0], beam.y - self._gasket_sample_offset[1]

    def _on_gasket_result(self, result) -> None:
        self._gasket_worker_busy = False
        generation, frame, detection, error, shapes, laser, source = result
        if (generation != self._gasket_preview_token()
                or not self._camera_enable_chk.isChecked()
                or not self._gasket_preview_active()):
            return
        if detection is not None:
            target = self._gasket_target()
            if target is None:
                target = (self._gasket_marked.cx, self._gasket_marked.cy)
                what = "offset"
            else:
                what = "sample to X-ray beam (red)"
            offset_x = detection.ellipse.cx - target[0]
            offset_y = detection.ellipse.cy - target[1]
            offset_physical = ""
            residual_physical = ""
            if self._gasket_um_matrix is not None:
                ux, uy = displacement_um(self._gasket_um_matrix, offset_x, offset_y)
                residual_um = detection.residual_px * nominal_um_per_pixel(self._gasket_um_matrix)
                offset_physical = (f" / ({ux:+.1f}, {uy:+.1f}) µm "
                                   f"(|offset|={distance_um(self._gasket_um_matrix, offset_x, offset_y):.1f} µm)")
                residual_physical = f" / ~{residual_um:.1f} µm"
            self._camera_source_label.setText(
                f"Source: {source} | Gasket Beta preview {time.strftime('%H:%M:%S')} "
                "(cyan reference, green detected, magenta sample): "
                f"{what} ({offset_x:+.1f}, {offset_y:+.1f}) px{offset_physical}, "
                f"score {detection.score:.2f}, coverage {detection.coverage}/8, "
                f"residual {detection.residual_px:.1f} px{residual_physical}"
            )
        else:
            self._camera_source_label.setText(
                f"Source: {source} | Gasket Beta preview {time.strftime('%H:%M:%S')} "
                "(cyan reference): "
                f"no detection ({error})"
            )
        self._render(frame, shapes if self._marks_chk.isChecked() else [],
                     laser if self._marks_chk.isChecked() else None, detection)

    def _open_own_capture_if_wanted(self) -> None:
        if (self._runner is None or self._runner_uses_camera or self._own_capture is not None
                or not self._camera_enable_chk.isChecked()):
            return
        self._own_capture = _CameraCapture(_CAMERA_INDEX)

    def _close_own_capture(self) -> None:
        if self._own_capture is not None:
            self._own_capture.stop()
            self._own_capture = None

    def _on_camera_enable_toggled(self, checked: bool) -> None:
        if checked:
            self._open_own_capture_if_wanted()
        else:
            self._close_own_capture()
        self._last_frame_key = None
        self._refresh_camera()

    def _on_marks_toggled(self, _checked: bool) -> None:
        self._last_frame_key = None
        self._refresh_camera()

    def _current_frame(self):
        """(frame or None, change-key, source description, shapes, laser spot)."""
        shapes, laser = self._marks.get()
        if self._runner is not None:
            if self._runner_uses_camera:
                latest = getattr(self._runner, "latest_camera_frame", lambda: None)()
                if isinstance(latest, tuple):
                    return latest[0], ("runner", latest[1]), "Sequence camera session", shapes, laser
                return None, None, "Waiting for the sequence camera session…", shapes, laser
            if self._own_capture is not None:
                latest = self._own_capture.latest()
                if latest is not None:
                    return (latest[0], ("own", latest[1]), f"Camera {_CAMERA_INDEX} (monitor)",
                            shapes, laser)
                return None, None, self._own_capture.error or "Opening camera…", shapes, laser
            return None, None, "Waiting for the sequence to start…", shapes, laser

        window = self._icw_provider()
        if window is not None:
            frame = getattr(window, "current_frame", None)
            if isinstance(frame, np.ndarray):
                # Mirror the window's live (possibly unsaved) marks.
                laser = getattr(window, "laser_spot_pos", laser)
                shapes = (getattr(window, "shapes", shapes)
                          if getattr(window, "show_all_marks", True) else [])
                return frame, ("icw", id(frame)), "Interactive Camera", shapes, laser
        return (None, None,
                "No camera source — the image appears while a sequence runs "
                "or while Interactive Camera is open", shapes, laser)

    def _show_message(self, text: str) -> None:
        if self._camera_label.pixmap() is not None and not self._camera_label.pixmap().isNull():
            self._camera_label.clear()
        if self._camera_label.text() != text:
            self._camera_label.setText(text)

    def _refresh_camera(self) -> None:
        preview_active = self._gasket_preview_active()
        if preview_active != self._gasket_preview_was_enabled:
            self._gasket_preview_was_enabled = preview_active
            self._gasket_generation += 1
            self._gasket_next_scan_at = 0.0
            self._last_frame_key = None
        if not self._camera_enable_chk.isChecked():
            self._show_message("Live image off")
            self._camera_source_label.setText("")
            self._last_frame_key = None
            return
        frame, key, source, shapes, laser = self._current_frame()
        if not preview_active:
            self._camera_source_label.setText(f"Source: {source}")
        if frame is None:
            self._show_message(source)
            self._last_frame_key = None
            return
        if preview_active:
            if (self.isVisible() and not self._gasket_worker_busy
                    and time.monotonic() >= self._gasket_next_scan_at):
                self._gasket_worker_busy = True
                self._gasket_next_scan_at = time.monotonic() + 0.5
                threading.Thread(
                    target=self._scan_gasket,
                    args=(frame.copy(), self._gasket_reference, self._gasket_marked,
                          self._gasket_preview_token(), list(shapes), laser, source,
                          self._gasket_target(), self._gasket_allow_uniform_scale),
                    daemon=True, name="scheduler-gasket-preview",
                ).start()
            return
        if key == self._last_frame_key or not self.isVisible():
            return
        self._last_frame_key = key
        if self._marks_chk.isChecked():
            self._render(frame, shapes, laser)
        else:
            self._render(frame, [], None)

    def _render(self, frame: np.ndarray, shapes: list, laser, gasket=None) -> None:
        image = frame.copy()
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        draw_saved_marks(image, shapes, laser)
        if self._gasket_preview_active():
            e = self._gasket_marked
            cv2.ellipse(image, (round(e.cx), round(e.cy)),
                        (round(e.rx), round(e.ry)), 0, 0, 360, (255, 255, 0), 1)
        if gasket is not None and self._gasket_preview_active():
            e = gasket.ellipse
            cv2.ellipse(image, (round(e.cx), round(e.cy)),
                        (round(e.rx), round(e.ry)), 0, 0, 360, (60, 255, 60), 2)
            cv2.drawMarker(image, (round(e.cx), round(e.cy)), (60, 255, 60),
                           cv2.MARKER_CROSS, 16, 2)
            if self._gasket_sample_offset is not None:
                ox, oy = self._gasket_sample_offset
                cv2.drawMarker(image, (round(e.cx + ox), round(e.cy + oy)), (255, 0, 255),
                               cv2.MARKER_CROSS, 20, 2)
        if self._gasket_preview_active():
            beam = self._xray_beam.get()
            if beam is not None:
                draw_xray_beam(image, beam.point)
        image = np.ascontiguousarray(image)
        h, w = image.shape[:2]
        qimage = QImage(image.data, w, h, image.strides[0], QImage.Format.Format_BGR888).copy()
        self._camera_label.setPixmap(QPixmap.fromImage(qimage).scaled(
            self._camera_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # ── devices ──────────────────────────────────────────────────────────

    def _refresh_devices(self) -> None:
        pace = self._ctx.pace5000
        pace_on = pace is not None and bool(getattr(pace, "connected", False))
        self._set_pace_backend(pace if pace_on else None)
        self._pace_group.setVisible(pace_on)

        lakeshore = self._ctx.lakeshore
        ls_on = lakeshore is not None and bool(getattr(lakeshore, "is_connected", False))
        self._lakeshore_group.setVisible(ls_on)
        if ls_on:
            self._refresh_lakeshore()
        self._refresh_sample_plot_options()

    # PACE5000 — history is kept here (the backend only emits readings).

    def _pace_connections(self) -> list:
        pairs = []
        for name, slot in (("pressure_updated", self._on_pace_pressure),
                           ("setpoint_updated", self._on_pace_setpoint)):
            signal = getattr(self._pace_backend, name, None)
            if signal is not None and hasattr(signal, "connect"):
                pairs.append((signal, slot))
        return pairs

    def _set_pace_backend(self, backend) -> None:
        if backend is self._pace_backend:
            return
        for signal, slot in self._pace_connections():
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._pace_backend = backend
        for signal, slot in self._pace_connections():
            signal.connect(slot)

    def _pace_factor_to_mpa(self) -> float:
        # pressure_updated/setpoint_updated are in the device's active unit.
        unit = getattr(self._pace_backend, "_active_pressure_unit", "MPa")
        return 0.1 if unit == "Bar" else 1.0

    def _on_pace_pressure(self, pressure: float) -> None:
        mpa = float(pressure) * self._pace_factor_to_mpa()
        t = time.time()
        self._pace_times.append(t)
        self._pace_pressures.append(mpa)
        while len(self._pace_times) > 1 and self._pace_times[0] < t - _PACE_HISTORY_S:
            self._pace_times.popleft()
            self._pace_pressures.popleft()
        self._pace_label.setText(f"Current Pressure:  {mpa:.4f}  MPa")
        self._redraw_pace()

    def _on_pace_setpoint(self, target: float, slew_per_sec: float) -> None:
        factor = self._pace_factor_to_mpa()
        self._pace_setpoint_label.setText(
            f"Target Pressure:  {target * factor:.4f} MPa    "
            f"Slew Rate:  {slew_per_sec * factor * 60.0:.4f} MPa/min"
        )

    def _redraw_pace(self) -> None:
        if not self._pace_times:
            self._pace_curve.setData([], [])
            return
        x_data = list(self._pace_times)
        y_data = list(self._pace_pressures)
        x_last = x_data[-1]
        x_min = x_last - self._pace_window_spin.value()
        self._pace_curve.setData(x_data, y_data)
        if self._pace_user_zoomed:
            return  # keep the user's right-drag zoom until View All / Clear Graph
        self._pace_plot.setXRange(x_min, x_last, padding=0.05)
        visible_y = [y for x, y in zip(x_data, y_data) if x >= x_min]
        if visible_y:
            y_min, y_max = min(visible_y), max(visible_y)
            y_pad = (y_max - y_min) * 0.1 if y_max != y_min else abs(y_max) * 0.05 or 0.01
            self._pace_plot.setYRange(y_min - y_pad, y_max + y_pad, padding=0)

    def _clear_pace(self) -> None:
        self._pace_times.clear()
        self._pace_pressures.clear()
        self._pace_curve.setData([], [])
        self._pace_user_zoomed = False
        self._pace_plot.plotItem.enableAutoRange()

    def _set_pace_zoomed(self, zoomed: bool) -> None:
        self._pace_user_zoomed = zoomed
        if not zoomed:
            self._redraw_pace()

    # LakeShore 335 — the backend already buffers 2 h of data points.

    def _refresh_lakeshore(self) -> None:
        backend = self._ctx.lakeshore
        try:
            data = backend.get_data()
        except Exception:
            return
        if not data:
            return
        latest = data[-1]
        try:
            self._ls_ab_label.setText(
                f"Ch A:  {latest.temp_a_k:.3f}  K    Ch B:  {latest.temp_b_k:.3f}  K")
            ranges = getattr(backend, "HEATER_RANGES", ("OFF", "LOW", "MEDIUM", "HIGH"))
            self._ls_sp_label.setText(
                f"Setpoint:  {latest.eff_setpoint_k:.3f} K    "
                f"Heater:  {ranges[latest.heater_range_idx]}    "
                f"Output:  {latest.heater_output_pct:.1f} %")
            cutoff = max(latest.timestamp - self._ls_window_spin.value(), self._lakeshore_cleared_at)
            visible = [d for d in data if d.timestamp >= cutoff]
            times = [d.timestamp for d in visible]
            self._ls_line_a.setData(times, [d.temp_a_k for d in visible])
            self._ls_line_b.setData(times, [d.temp_b_k for d in visible])
            self._ls_line_sp.setData(times, [d.eff_setpoint_k for d in visible])
        except (AttributeError, IndexError, TypeError, ValueError):
            pass

    def _clear_lakeshore(self) -> None:
        # Local cut-off only: the backend buffer is shared with the
        # LakeShore window and the runner's temperature checks.
        self._lakeshore_cleared_at = time.time()
        for line in (self._ls_line_a, self._ls_line_b, self._ls_line_sp):
            line.setData([], [])
        self._ls_plot.plotItem.enableAutoRange()
