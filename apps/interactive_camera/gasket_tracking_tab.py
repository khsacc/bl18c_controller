"""Interactive Camera's "Sample Tracking (Gasket Detection)" tab.

Follows the sample by detecting the marked gasket outline and moving the
marked sample point onto the registered X-ray beam with Ch4/Ch5, with
optional Ch3 autofocus. The detection, averaging and correction cycle are
the Experimental Scheduler's (``gasket_follow.py``); this tab supplies the
camera, limited stage moves and Ch3 autofocus of Interactive Camera.

The setup (reference photo, gasket outline, sample point and ruby
positions) is made with the scheduler's Sample Positions dialog and saved
in ``__localdata/gasket_tracking.json`` (``gasket_setup.py``), independently
of any scheduler sequence; the scheduler can import it. Ruby positions are
only recorded here; no spectra are taken.
"""
from __future__ import annotations

import csv
from datetime import datetime
import math
import os
from pathlib import Path
import threading
import time

import cv2
import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from apps.interactive_camera import gasket_follow, gasket_overlay
from apps.interactive_camera.gasket_setup import load_state, save_state
from apps.interactive_camera.gasket_units import (
    RUBY_ORIGIN_GASKET_CENTRE, distance_um, load_stage_um_per_pixel,
)
from apps.interactive_camera.sample_tracking import clamp_xy_correction
from apps.interactive_camera.xray_beam import XrayBeamFileCache
from utils.stage.control_stage import PULSE_SCALE

_LIVE_INTERVAL_S = 0.5      # minimum gap between overlay detections
_RENDER_INTERVAL_MS = 100

def _no_wheel(widget):
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class GasketTrackingTab(QtWidgets.QWidget):
    """Gasket-outline sample tracking inside Interactive Camera's window."""

    log_signal = QtCore.pyqtSignal(str)
    _stop_signal = QtCore.pyqtSignal(str)
    _detection_ready = QtCore.pyqtSignal(object)
    _attempt_finished = QtCore.pyqtSignal()
    _confirm_finished = QtCore.pyqtSignal()

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.controller = window.controller
        self._xray_beam = XrayBeamFileCache()
        self._state = load_state()
        self._reference_frame = None
        self._overlay = None
        self._um_matrix = None

        # Tracking session state (the worker thread only reads ``_cfg``).
        self.is_following = False
        self.is_following_running = False
        self.follow_thread: threading.Thread | None = None
        self._lease = None
        self._stop_pending = False   # a session awaits _finish_stop()
        # The lease is released only after the worker confirmed that the
        # stage has stopped; otherwise it is kept and the check can be retried.
        self._stop_confirmed = True
        self._release_blocked = False
        self._confirm_thread: threading.Thread | None = None
        self._interval_ms = 60_000
        self._cfg: dict | None = None
        self._limits: dict = {}
        self._origin: dict[int, int] = {}
        self.follow_cumulative = {3: 0, 4: 0, 5: 0}
        self._focusing = False
        self._csv_file = None
        self._csv_writer = None
        self._csv_path = None
        self._start_time = None
        self._images_dir = None
        self._image_counter = 0
        self._env_keys: tuple = ()
        self._save_images = False
        self._stop_reason = None

        # Live overlay detection.
        self._live = False
        self._live_busy = False
        self._live_next_at = 0.0
        self._live_token = 0
        self._last_result = None

        self._build_ui()
        self._load_reference()
        self._refresh_setup_summary()

        self.log_signal.connect(self._append_log)
        self._stop_signal.connect(self.stop_following)
        self._detection_ready.connect(self._on_detection)

        self._attempt_finished.connect(self._schedule_next_attempt)
        self._confirm_finished.connect(self._after_stop_confirmation)
        # Single shot, restarted when an attempt completes, so the interval
        # runs from the end of one attempt to the start of the next.
        self._follow_timer = QtCore.QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.timeout.connect(self._follow_iteration)
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.timeout.connect(self._render)
        self._render_timer.start(_RENDER_INTERVAL_MS)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        outer = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)

        self.video_label = QtWidgets.QLabel()
        self.video_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: black;")
        self.video_label.setMinimumHeight(300)
        self.video_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                       QtWidgets.QSizePolicy.Policy.Ignored)
        layout.addWidget(self.video_label, stretch=1)
        self.detection_label = QtWidgets.QLabel()
        self.detection_label.setWordWrap(True)
        layout.addWidget(self.detection_label)

        setup_row = QtWidgets.QHBoxLayout()
        self.btn_setup = QtWidgets.QPushButton("Set up sample positions…")
        self.btn_setup.setToolTip(
            "Capture the reference photo, mark the gasket outline and sample "
            "position, and record ruby positions.")
        self.btn_setup.clicked.connect(self.open_sample_positions)
        setup_row.addWidget(self.btn_setup)
        self.btn_live = QtWidgets.QPushButton("Activate gasket detection")
        self.btn_live.setToolTip(
            "Detect the marked gasket outline on the live image. Never moves the stage.")
        self.btn_live.clicked.connect(self.toggle_live_detection)
        setup_row.addWidget(self.btn_live)
        setup_row.addStretch()
        layout.addLayout(setup_row)
        self.setup_summary = QtWidgets.QLabel()
        self.setup_summary.setWordWrap(True)
        layout.addWidget(self.setup_summary)

        saved = self._state.get("settings", {})
        form = QtWidgets.QFormLayout()
        self.tolerance_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self.tolerance_spin.setRange(0.1, 100.0)
        self.tolerance_spin.setDecimals(1)
        self.tolerance_spin.setSuffix(" µm")
        self.tolerance_spin.setValue(float(saved.get("tolerance_um", 8.0)))
        self.tolerance_spin.setToolTip(
            "Maximum gasket-centre offset after correction. Each tracking attempt "
            "corrects its measured shift down to whole Ch4/Ch5 pulses.")
        form.addRow("Gasket tolerance:", self.tolerance_spin)
        self.frames_spin = _no_wheel(QtWidgets.QSpinBox())
        self.frames_spin.setRange(1, 10)
        self.frames_spin.setValue(int(saved.get("average_frames", 5)))
        self.frames_spin.setToolTip("Fresh camera frames averaged for each measurement")
        form.addRow("Average frames:", self.frames_spin)
        self.scale_chk = QtWidgets.QCheckBox("Allow uniform ellipse scaling")
        self.scale_chk.setChecked(bool(saved.get("allow_uniform_scale", False)))
        self.scale_chk.setToolTip(
            "Track aperture diameter changes while keeping the marked ellipse aspect "
            "ratio (up to 20% smaller or larger).")
        self.scale_chk.toggled.connect(self._on_settings_changed)
        form.addRow("", self.scale_chk)
        self.interval_spin = _no_wheel(QtWidgets.QDoubleSpinBox())
        self.interval_spin.setRange(0.1, 1440.0)
        self.interval_spin.setSingleStep(0.5)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" min")
        self.interval_spin.setValue(float(saved.get("interval_min", 1.0)))
        form.addRow("Interval:", self.interval_spin)
        self.retries_spin = _no_wheel(QtWidgets.QSpinBox())
        self.retries_spin.setRange(0, 10)
        self.retries_spin.setValue(int(saved.get("xy_max_retries", 3)))
        self.retries_spin.setToolTip("XY re-corrections allowed after the first one")
        form.addRow("XY retries:", self.retries_spin)
        self.af_chk = QtWidgets.QCheckBox(
            "Ch3 autofocus before and after XY correction "
            "(uses the Auto-Focus settings of the Interactive Camera tab)")
        self.af_chk.setChecked(bool(saved.get("autofocus", True)))
        form.addRow("", self.af_chk)
        for spin in (self.tolerance_spin, self.frames_spin, self.interval_spin,
                     self.retries_spin):
            spin.valueChanged.connect(self._on_settings_changed)
        self.af_chk.toggled.connect(self._on_settings_changed)
        layout.addLayout(form)

        attempt_group = QtWidgets.QGroupBox("Per-attempt movement limit (mm)")
        attempt_layout = QtWidgets.QVBoxLayout(attempt_group)
        self.limit_per_attempt = {}
        for ch in (4, 5):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(f"Ch{ch}:"))
            spin = _no_wheel(QtWidgets.QDoubleSpinBox())
            spin.setRange(0.0, 10.0)
            spin.setSingleStep(0.010)
            spin.setDecimals(3)
            spin.setValue(0.400)
            self.limit_per_attempt[ch] = spin
            row.addWidget(spin)
            row.addStretch()
            attempt_layout.addLayout(row)
        total_group = QtWidgets.QGroupBox("Total movement limits from start position (mm)")
        total_grid = QtWidgets.QGridLayout(total_group)
        total_grid.addWidget(QtWidgets.QLabel("Min (-)"), 0, 1)
        total_grid.addWidget(QtWidgets.QLabel("Max (+)"), 0, 2)
        self.limit_total = {}
        for row, (ch, default) in enumerate(((3, 0.5), (4, 5.0), (5, 50.0)), start=1):
            total_grid.addWidget(QtWidgets.QLabel(f"Ch{ch}:"), row, 0)
            for col, (sign, rng) in enumerate(((-1, (-100.0, 0.0)), (1, (0.0, 100.0))), start=1):
                spin = _no_wheel(QtWidgets.QDoubleSpinBox())
                spin.setRange(*rng)
                spin.setSingleStep(0.1)
                spin.setDecimals(3)
                spin.setValue(sign * default)
                self.limit_total[(ch, sign)] = spin
                total_grid.addWidget(spin, row, col)
        limits_row = QtWidgets.QHBoxLayout()
        limits_row.addWidget(attempt_group)
        limits_row.addWidget(total_group)
        limits_row.addStretch()
        layout.addLayout(limits_row)

        log_row = QtWidgets.QHBoxLayout()
        log_row.addWidget(QtWidgets.QLabel("Log directory:"))
        self.log_dir_edit = QtWidgets.QLineEdit(
            saved.get("log_dir") or self.window._cached_tracking_log_dir
            or os.path.expanduser("~"))
        self.log_dir_edit.setToolTip(
            "Directory where gasket_tracking_log_from_{timestamp}.csv will be saved.")
        log_row.addWidget(self.log_dir_edit)
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(self._browse_log_dir)
        log_row.addWidget(browse)
        layout.addLayout(log_row)
        self.save_images_chk = QtWidgets.QCheckBox("Save image after every tracking attempt")
        layout.addWidget(self.save_images_chk)

        ctrl_row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton(
            "Start Sample Position Tracking by moving Ch3,4,5")
        self.btn_stop = QtWidgets.QPushButton("Stop Tracking")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_following)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        large = QtGui.QFont()
        large.setPointSizeF(QtWidgets.QApplication.font().pointSizeF() * 1.5)
        self.btn_start.setFont(large)
        self.btn_stop.setFont(large)
        self.btn_start.setStyleSheet(
            "QPushButton:enabled { background-color: #6BB06C; color: white; font-weight: bold; }")
        self.btn_stop.setStyleSheet(
            "QPushButton:enabled { background-color: #FF3333; color: white; font-weight: bold;"
            " border-radius: 1px; }"
            " QPushButton:enabled:pressed { background-color: #CC0000; }")
        self.warning_label = QtWidgets.QLabel(
            "Sample position tracking in progress. Do not move the stages manually.")
        bold = QtGui.QFont(large)
        bold.setBold(True)
        self.warning_label.setFont(bold)
        self.warning_label.setStyleSheet("color: red;")
        self.warning_label.setVisible(False)
        ctrl_row.addWidget(self.btn_start)
        ctrl_row.addWidget(self.btn_stop)
        ctrl_row.addWidget(self.warning_label)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        outer.addWidget(left, stretch=4)
        outer.addWidget(self.log, stretch=1)

    # ------------------------------------------------------------------ state

    def _save(self):
        self._state["settings"] = {
            "tolerance_um": self.tolerance_spin.value(),
            "average_frames": self.frames_spin.value(),
            "allow_uniform_scale": self.scale_chk.isChecked(),
            "interval_min": self.interval_spin.value(),
            "xy_max_retries": self.retries_spin.value(),
            "autofocus": self.af_chk.isChecked(),
            "log_dir": self.log_dir_edit.text().strip(),
        }
        try:
            save_state(self._state)
        except OSError as exc:
            self._append_log(f"Warning: could not save gasket tracking settings: {exc}")

    def _on_settings_changed(self, *_args):
        self._save()
        self._load_overlay()

    def _load_reference(self):
        path = self._state.get("reference_path")
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR) if path else None
        self._reference_frame = frame
        self._load_overlay()

    def _load_overlay(self):
        self._stop_live()
        try:
            self._um_matrix = load_stage_um_per_pixel()
        except (OSError, ValueError, KeyError, TypeError):
            self._um_matrix = None
        self._overlay = (gasket_overlay.load_overlay_reference(
            self._state.get("reference_path"), self._state.get("gasket_ellipse"),
            self._state.get("gasket_sample_point"), self.scale_chk.isChecked())
            if self._reference_frame is not None else None)
        self._last_result = None

    def _refresh_setup_summary(self):
        path = self._state.get("reference_path")
        if self._reference_frame is None:
            reference = "Reference photo: none" if not path else (
                f"Reference photo: cannot read {Path(path).name}")
        else:
            reference = f"Reference photo: {Path(path).name}"
        outline = ("gasket outline marked" if self._overlay is not None
                   else "gasket outline not marked")
        sample = ("sample position marked" if self._state.get("gasket_sample_point")
                  else "sample position not marked")
        rubies = self._state.get("ruby_positions") or {}
        ruby_text = ("Ruby positions: " + ", ".join(sorted(rubies))
                     + " (offsets from the gasket centre)" if rubies
                     else "Ruby positions: none recorded")
        self.setup_summary.setText(f"{reference} | {outline} | {sample} | {ruby_text}")
        self.btn_start.setEnabled(not self.is_following and self._overlay is not None)

    def open_sample_positions(self):
        from apps.interactive_camera.sample_positions_dialog import SamplePositionsDialog
        if self.is_following:
            QtWidgets.QMessageBox.warning(
                self, "Sample Positions", "Stop tracking before setting sample positions.")
            return
        self._stop_live()
        s = self._state
        path = s.get("reference_path")
        save_dir = (str(Path(path).parent) if path else self.window.last_save_dir)

        def borrowed_frame():
            frame = self.window.current_frame
            return None if frame is None else frame.copy()

        dlg = SamplePositionsDialog(
            self.controller, self._reference_frame, path,
            tuple(s["xrd_position"]) if s.get("xrd_position") else None,
            s.get("ruby_positions") or {},
            borrowed_frame=borrowed_frame, save_directory=save_dir,
            tracking_method="gasket_beta",
            gasket_ellipse=s.get("gasket_ellipse"),
            gasket_reference_digest=s.get("gasket_reference_digest"),
            gasket_allow_uniform_scale=self.scale_chk.isChecked(),
            gasket_sample_point=s.get("gasket_sample_point"),
            gasket_sample_mode=s.get("gasket_sample_mode", "manual"),
            gasket_reference_position=s.get("gasket_reference_position"),
            ruby_offset_origin=s.get("ruby_offset_origin", RUBY_ORIGIN_GASKET_CENTRE),
            parent=self,
        )
        dlg.exec()
        if dlg.reference_path is not None:
            s["reference_path"] = str(dlg.reference_path)
        s.update(
            gasket_ellipse=dlg.gasket_ellipse,
            gasket_sample_point=dlg.gasket_sample_point,
            gasket_sample_mode=dlg.gasket_sample_mode,
            gasket_reference_digest=dlg.gasket_reference_digest,
            gasket_reference_position=(list(dlg.gasket_reference_position)
                                       if dlg.gasket_reference_position else None),
            xrd_position=list(dlg.xrd_position) if dlg.xrd_position else None,
            ruby_positions=dict(dlg.ruby_positions),
            ruby_offset_origin=dlg.ruby_offset_origin,
        )
        self._save()
        if dlg.reference_changed:
            self.window.last_save_dir = str(dlg.save_directory)
            self._reference_frame = dlg.reference_frame
            self._append_log(f"Reference photo: {Path(dlg.reference_path).name}")
        self._load_overlay()
        self._refresh_setup_summary()

    def _browse_log_dir(self):
        current = self.log_dir_edit.text().strip() or self.window.last_save_dir
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Log Directory", current)
        if directory:
            self.log_dir_edit.setText(directory)
            self._save()

    @QtCore.pyqtSlot(str)
    def _append_log(self, msg):
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _emit_log(self, msg):
        try:
            self.log_signal.emit(msg)
        except RuntimeError:
            pass  # the window was closed

    # ------------------------------------------------------------------ live overlay

    def toggle_live_detection(self):
        if self._live:
            self._stop_live()
        elif self._overlay is not None and self.window.current_frame is not None:
            self._live = True
            self._live_token += 1
            self._live_next_at = 0.0
            self.btn_live.setText("Terminate gasket detection")

    def _stop_live(self):
        self._live = False
        self._live_token += 1
        self._live_busy = False
        self._last_result = None
        if hasattr(self, "btn_live"):
            self.btn_live.setText("Activate gasket detection")

    def _detection_active(self) -> bool:
        return self._overlay is not None and (self._live or self.is_following)

    def _render(self):
        if not self.isVisible():
            return
        frame = self.window.current_frame
        self.btn_live.setEnabled(self.is_following or (
            self._overlay is not None and frame is not None))
        if frame is None:
            self.video_label.clear()
            return
        active = self._detection_active()
        if (active and not self._focusing and not self._live_busy
                and time.monotonic() >= self._live_next_at):
            self._live_busy = True
            self._live_next_at = time.monotonic() + _LIVE_INTERVAL_S
            overlay = self._overlay
            target = gasket_overlay.overlay_target(self._xray_beam.get(), overlay.sample_offset)
            threading.Thread(
                target=self._detect_once,
                args=(frame.copy(), overlay, target, self._live_token),
                daemon=True, name="ic-gasket-detection",
            ).start()
        # Show the analysed frame with its result; the live frame otherwise
        # (including during Ch3 autofocus, when detection pauses).
        analysed = active and not self._focusing and self._last_result is not None
        shown, detection = self._last_result if analysed else (frame, None)
        image = shown.copy()
        if self._overlay is not None:
            gasket_overlay.draw_overlay(
                image, self._overlay.marked, detection, self._overlay.sample_offset,
                self._xray_beam.get(), test_label=analysed)
        self.window._render_to_label(self.window._add_information_band(image),
                                     self.video_label)

    def _detect_once(self, frame, overlay, target, token):
        detection, error = gasket_overlay.detect(
            frame, overlay.frame, overlay.marked, target, overlay.allow_uniform_scale)
        try:
            self._detection_ready.emit((token, frame, detection, error, target))
        except RuntimeError:
            pass

    def _on_detection(self, result):
        token, frame, detection, error, target = result
        if token != self._live_token:
            return
        self._live_busy = False
        if not self._detection_active() or self._focusing:
            return
        self._last_result = (frame, detection)
        stamp = time.strftime('%H:%M:%S')
        if detection is None:
            self.detection_label.setText(f"Gasket detection {stamp}: not detected ({error})")
        else:
            self.detection_label.setText(
                f"Gasket detection {stamp} (cyan reference, green detected, magenta sample): "
                + gasket_overlay.describe_offset(
                    detection, target, self._overlay.marked, self._um_matrix))

    # ------------------------------------------------------------------ tracking

    def start_following(self):
        window = self.window
        if self.is_following or window.is_following:
            return
        if not window.calibration_data or "matrix_inv" not in window.calibration_data:
            QtWidgets.QMessageBox.warning(
                self, "Gasket tracking", "Calibrate the camera before tracking.")
            return
        s = self._state
        try:
            if self._reference_frame is None:
                raise RuntimeError("Capture a reference photo in Set up sample positions.")
            matrix_inv = np.asarray(window.calibration_data["matrix_inv"], dtype=float)
            cfg = gasket_follow.resolve_reference(
                reference_path=s["reference_path"], reference_frame=self._reference_frame,
                ellipse=s.get("gasket_ellipse"), digest=s.get("gasket_reference_digest"),
                sample_point=s.get("gasket_sample_point"),
                tolerance_um=self.tolerance_spin.value(),
                average_frames=self.frames_spin.value(),
                allow_uniform_scale=self.scale_chk.isChecked(),
                matrix_inv=matrix_inv,
            )
            positions = {ch: self.controller.get_ch_pos(ch) for ch in (3, 4, 5)}
            if None in positions.values():
                raise RuntimeError("Could not read motor positions.")
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Gasket tracking", str(exc))
            return
        cfg.update(
            M_inv=matrix_inv,
            lim4=max(0, int(self.limit_per_attempt[4].value() * 1000 / PULSE_SCALE[4])),
            lim5=max(0, int(self.limit_per_attempt[5].value() * 1000 / PULSE_SCALE[5])),
            xy_max_retries=self.retries_spin.value(),
            af_enabled=self.af_chk.isChecked() and window.autofocus.focus_range > 0,
        )
        try:
            self._lease = self.controller.acquire_motion(
                owner="Interactive Camera", operation="Gasket tracking")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error", f"Could not start tracking: {exc}")
            return
        self._cfg = cfg
        self._limits = {
            key: int(spin.value() * 1000 / PULSE_SCALE[key[0]])
            for key, spin in self.limit_total.items()
        }
        self._origin = {ch: int(value) for ch, value in positions.items()}
        self.follow_cumulative = {3: 0, 4: 0, 5: 0}
        self._env_keys = window._main_sample_environment_keys()
        self._save_images = self.save_images_chk.isChecked()
        self._stop_reason = None
        self._open_logs()
        self.is_following = True
        self._stop_pending = True
        self._live_token += 1
        self._live_busy = False
        self._last_result = None
        self._set_running_ui(True)
        self._append_log(
            "Gasket tracking started. Origin "
            + ", ".join(f"Ch{ch}={self._origin[ch]}" for ch in (3, 4, 5))
            + f"; tolerance {self.tolerance_spin.value():.1f} µm")
        # Correct at once; the interval applies between completed attempts.
        self._interval_ms = int(self.interval_spin.value() * 60 * 1000)
        self._follow_iteration()

    def _set_running_ui(self, running: bool):
        self.btn_stop.setEnabled(running)
        self.warning_label.setVisible(running)
        for widget in (self.btn_setup, self.tolerance_spin, self.frames_spin, self.scale_chk,
                       self.interval_spin, self.retries_spin, self.af_chk,
                       self.save_images_chk, self.log_dir_edit,
                       *self.limit_per_attempt.values(), *self.limit_total.values()):
            widget.setEnabled(not running)
        self.window.tab_widget.setTabEnabled(0, not running)
        self._refresh_setup_summary()

    def _open_logs(self):
        self._start_time = datetime.now()
        ts = self._start_time.strftime('%Y%m%d-%H%M%S')
        log_dir = self.log_dir_edit.text().strip() or os.path.expanduser("~")
        self._images_dir = None
        try:
            self._csv_path = os.path.join(log_dir, f"gasket_tracking_log_from_{ts}.csv")
            self._csv_file = open(self._csv_path, 'w', newline='', encoding='utf-8')
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow([
                'timestamp', 'elapsed_s', 'ch3_pulse', 'ch4_pulse', 'ch5_pulse',
                'delta_ch3', 'delta_ch4', 'delta_ch5',
                'gasket_offset_x_px', 'gasket_offset_y_px', 'gasket_offset_um', 'gasket_score',
            ] + self.window._sample_environment_log_headers(self._env_keys))
            self._csv_file.flush()
            self._append_log(f"CSV log: {self._csv_path}")
            if self._save_images:
                self._images_dir = os.path.join(log_dir, f"images_from_{ts}")
                os.makedirs(self._images_dir, exist_ok=True)
                self._image_counter = 0
                self._append_log(f"Images dir: {self._images_dir}")
        except Exception as exc:
            self._append_log(f"Warning: could not open CSV or images dir: {exc}")
            self._close_csv()
            self._csv_path = None
            self._images_dir = None

    def _close_csv(self):
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
        self._csv_file = None
        self._csv_writer = None

    @QtCore.pyqtSlot()
    @QtCore.pyqtSlot(str)
    def stop_following(self, reason: str | None = None):
        if not self.is_following:
            return
        self._follow_timer.stop()
        self.is_following = False
        if reason:
            self._stop_reason = reason
        if not (self.follow_thread is not None and self.follow_thread.is_alive()):
            self._finish_stop()
        else:
            # The worker sees is_following=False between moves and exits; the
            # lease is released once it has.
            self._append_log("Stopping after the current stage move…")
            QtCore.QTimer.singleShot(200, self._finish_stop_when_idle)

    def _finish_stop_when_idle(self):
        if self.follow_thread is not None and self.follow_thread.is_alive():
            QtCore.QTimer.singleShot(200, self._finish_stop_when_idle)
            return
        self._finish_stop()

    @QtCore.pyqtSlot()
    def _schedule_next_attempt(self):
        if self.is_following:
            self._follow_timer.start(self._interval_ms)

    def _on_stop_clicked(self):
        if self._release_blocked:
            self._retry_stop_confirmation()
        else:
            self.stop_following()

    def _confirm_stopped(self) -> bool:
        """Confirm that every motor has stopped (worker threads only).

        If the confirmation fails, send a normal stop and confirm again."""
        try:
            self.controller.wait_until_stop(stay_in_rem=True, motion=self._lease)
            return True
        except Exception as exc:
            self._emit_log(f"Could not confirm that the stage stopped: {exc}")
        try:
            self.controller.normal_stop(source="Interactive Camera gasket tracking")
            # The stop revokes the lease; this check needs none.
            self.controller.wait_until_stop(stay_in_rem=True)
            self._emit_log("Normal stop sent; the stage has stopped.")
            return True
        except Exception as exc:
            self._emit_log(f"Stop could not be confirmed after a normal stop: {exc}")
            return False

    def _retry_stop_confirmation(self):
        if self._confirm_thread is not None and self._confirm_thread.is_alive():
            return
        self.btn_stop.setEnabled(False)
        self._append_log("Checking that the stage has stopped…")

        def run():
            self._stop_confirmed = self._confirm_stopped()
            try:
                self._confirm_finished.emit()
            except RuntimeError:
                pass

        self._confirm_thread = threading.Thread(target=run, daemon=True,
                                                name="ic-gasket-stop-check")
        self._confirm_thread.start()

    @QtCore.pyqtSlot()
    def _after_stop_confirmation(self):
        self.btn_stop.setEnabled(True)
        self._finish_stop()

    def holds_motion(self) -> bool:
        """True while the session's lease is kept (e.g. stop unconfirmed)."""
        return self._lease is not None

    def _finish_stop(self):
        """Return the controller to LOC, release the lease and close logs.

        Runs once per session, only after the worker thread has exited, and
        only once the stage is confirmed stopped; otherwise the lease is kept
        and the Stop button retries the confirmation."""
        if not self._stop_pending:
            return
        if self._lease is not None and not self._stop_confirmed:
            self._release_blocked = True
            self.btn_stop.setText("Confirm stage stopped")
            self.btn_stop.setEnabled(True)
            message = ("The stage could not be confirmed as stopped, so Interactive Camera "
                       "keeps motion control. Check the PM16C, then press Confirm stage "
                       "stopped.")
            self._append_log(message)
            QtWidgets.QMessageBox.warning(self, "Gasket tracking", message)
            return
        self._release_blocked = False
        self.btn_stop.setText("Stop Tracking")
        self._stop_pending = False
        if self._lease is not None:
            # Attempts wait with stay_in_rem=True; hand the front panel back.
            try:
                if self.controller.coordinator.is_valid(self._lease):
                    self.controller.switch_to_loc(motion=self._lease)
            except Exception as exc:
                self._append_log(f"Warning: could not switch the controller to LOC: {exc}")
            self.controller.release_motion(self._lease)
            self._lease = None
        self._live_token += 1
        self._last_result = None
        self._focusing = False
        self._set_running_ui(False)
        saved_csv = self._csv_path
        self._close_csv()
        self._csv_path = None
        self._images_dir = None
        o, c = self._origin, self.follow_cumulative
        prefix = (f"Tracking automatically stopped ({self._stop_reason})."
                  if self._stop_reason else "Tracking stopped.")
        self._stop_reason = None
        self._append_log(
            f"{prefix} Start: Ch3={o.get(3, 0)}, Ch4={o.get(4, 0)}, Ch5={o.get(5, 0)} [pulse] | "
            f"Total movement: ΔCh3={c[3]:+d}, ΔCh4={c[4]:+d}, ΔCh5={c[5]:+d} [pulse]")
        if saved_csv and os.path.exists(saved_csv):
            self._append_log("Saving plots...")
            threading.Thread(target=self.window._save_tracking_plots,
                             args=(saved_csv, self._emit_log), daemon=True).start()

    def shutdown(self):
        """Ask a running session to stop (window closing).

        Cleanup is completed by ``finish_close()`` once the window has
        confirmed that the worker thread has exited."""
        self.stop_following("window closed")

    def finish_close(self) -> bool:
        """Release the session after the worker has exited (window closing).

        Returns False while the lease must be kept (stop not confirmed)."""
        if not (self.follow_thread is not None and self.follow_thread.is_alive()):
            self._finish_stop()
        if self.holds_motion():
            return False
        self._render_timer.stop()
        return True

    def busy(self) -> bool:
        return (self.is_following or self.is_following_running or self.holds_motion()
                or (self.follow_thread is not None and self.follow_thread.is_alive()))

    def _follow_iteration(self):
        if self.is_following_running or not self.is_following:
            return
        self.is_following_running = True
        self.follow_thread = threading.Thread(target=self._follow_task, daemon=True,
                                              name="ic-gasket-follow")
        self.follow_thread.start()

    # Worker thread ----------------------------------------------------------

    def _hooks(self) -> gasket_follow.FollowHooks:
        def measure_average(c, purpose):
            return gasket_follow.live_measure_average(
                c, purpose, controller=self.controller,
                get_frame=lambda _purpose: self.window._fresh_camera_frame(),
                log=lambda _msg: None)

        return gasket_follow.FollowHooks(
            measure_average=measure_average,
            move_xy=self._move_xy,
            autofocus=self._autofocus,
            aborted=lambda: not self.is_following,
            log=self._emit_log,
            progress=lambda _msg: None,
        )

    def _move_xy(self, proposed4, proposed5, lim4, lim5):
        moves = {}
        for ch, proposed, limit in ((4, proposed4, lim4), (5, proposed5, lim5)):
            moves[ch] = clamp_xy_correction(
                proposed, limit, self.follow_cumulative[ch],
                self._limits[(ch, -1)], self._limits[(ch, 1)])
        if not self.is_following:
            return 0, 0
        commanded = False
        try:
            for ch, delta in moves.items():
                if delta:
                    commanded = True
                    self.controller.move_ch_relative(ch, delta, motion=self._lease)
            if commanded:
                self.controller.wait_until_stop(stay_in_rem=True, motion=self._lease)
        except Exception:
            # An axis may already be moving (e.g. Ch4 moved, Ch5 rejected):
            # wait for it before the error releases the lease, and take the
            # totals from the stage, since the cycle does not count them.
            if commanded:
                try:
                    self.controller.wait_until_stop(stay_in_rem=True, motion=self._lease)
                finally:
                    self._sync_xy_from_stage()
            raise
        return moves[4], moves[5]

    def _sync_xy_from_stage(self):
        for ch in (4, 5):
            try:
                position = self.controller.get_ch_pos(ch)
            except Exception:
                position = None
            if position is not None:
                self.follow_cumulative[ch] = int(position) - self._origin[ch]

    def _autofocus(self):
        self._focusing = True
        self._last_result = None
        try:
            self.window._z_sharpness_scan(
                self.follow_cumulative[3], self._limits[(3, -1)], self._limits[(3, 1)],
                self._lease, origin3=self._origin[3],
                should_stop=lambda: not self.is_following)
        finally:
            self._focusing = False
        position = self.controller.get_ch_pos(3)
        if position is not None:
            self.follow_cumulative[3] = int(position) - self._origin[3]

    def _follow_task(self):
        cfg = self._cfg
        before = dict(self.follow_cumulative)
        self._stop_confirmed = False
        try:
            if not self.is_following:
                return
            self.controller.switch_to_rem(motion=self._lease)
            result = gasket_follow.align_and_verify(
                cfg, self.follow_cumulative, self._hooks(), pre_focus=True,
                verify_purpose="follow final verification",
                retry_purpose="follow post-focus verification",
            )
            if result is None:
                return
            dx, dy, score = result
            deltas = {ch: self.follow_cumulative[ch] - before[ch] for ch in (3, 4, 5)}
            self._emit_log(
                f"Verified gasket offset: {gasket_follow.offset_text(cfg, dx, dy)}; "
                f"score={score:.3f}")
            self._emit_log(
                f"ΔCh3={deltas[3]:+d}, ΔCh4={deltas[4]:+d}, ΔCh5={deltas[5]:+d} [pulse] | "
                f"Total: Ch3={self.follow_cumulative[3]:+d}, Ch4={self.follow_cumulative[4]:+d}, "
                f"Ch5={self.follow_cumulative[5]:+d}")
            self._write_csv(deltas, dx, dy, score)
            self._save_image()
            hits = self._limit_hits()
            if hits:
                self._stop_signal.emit("Total limit reached: " + ", ".join(hits))
        except Exception as exc:
            self._emit_log(f"Error: {exc}")
            try:
                self._stop_signal.emit(str(exc))
            except RuntimeError:
                pass
        finally:
            # LOC and the lease release wait for this confirmation.
            self._stop_confirmed = self._confirm_stopped()
            self.is_following_running = False
            try:
                self._attempt_finished.emit()
            except RuntimeError:
                pass  # the window was closed

    def _limit_hits(self) -> list[str]:
        hits = []
        for ch in (3, 4, 5):
            if ch == 3 and not self._cfg["af_enabled"]:
                continue
            value = self.follow_cumulative[ch]
            low, high = self._limits[(ch, -1)], self._limits[(ch, 1)]
            if value <= low:
                hits.append(f"Ch{ch} min ({low * PULSE_SCALE[ch] / 1000:.3f} mm)")
            elif value >= high:
                hits.append(f"Ch{ch} max ({high * PULSE_SCALE[ch] / 1000:.3f} mm)")
        return hits

    def _write_csv(self, deltas, dx, dy, score):
        if not self._csv_writer or not self._start_time:
            return
        now = datetime.now()
        try:
            offset_um = distance_um(gasket_follow.um_matrix(self._cfg), dx, dy)
            self._csv_writer.writerow([
                now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                f"{(now - self._start_time).total_seconds():.3f}",
                *(self._origin[ch] + self.follow_cumulative[ch] for ch in (3, 4, 5)),
                deltas[3], deltas[4], deltas[5],
                f"{dx:.2f}", f"{dy:.2f}",
                f"{offset_um:.2f}" if math.isfinite(offset_um) else "",
                f"{score:.4f}",
            ] + self.window._sample_environment_log_values(self._env_keys))
            self._csv_file.flush()
        except Exception as exc:
            self._emit_log(f"Warning: could not write CSV: {exc}")

    def _save_image(self):
        if not (self._save_images and self._images_dir):
            return
        self._image_counter += 1
        name = (f"frame_{self._image_counter:04d}_"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
        try:
            frame = self.window._fresh_camera_frame()
            image = self.window._add_information_band(frame.copy())
            if not cv2.imwrite(os.path.join(self._images_dir, name), image):
                raise OSError(f"Could not write {name}")
            self._emit_log(f"Image saved: {name}")
        except Exception as exc:
            self._emit_log(f"Warning: could not save image: {exc}")
