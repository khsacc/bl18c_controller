"""Record manually positioned XRD and ruby locations with a live camera."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from apps.interactive_camera.sample_tracking import measure_xy_shift_strict
from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole, reference_digest, sample_offset,
)
from apps.interactive_camera.xray_beam import (
    XrayBeamFileCache, check_xray_beam, draw_xray_beam, read_microscope_pulses,
)
from apps.exp_scheduler.scheduler_settings import RUBY_ORIGIN_GASKET_CENTRE, ruby_origin_for
from apps.exp_scheduler.safety_rules import GASKET_BEAM_CHANNELS
from apps.exp_scheduler.ui.gasket_mark_dialog import GasketMarkDialog
from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
)


_CALIBRATION_PATH = Path(__file__).parents[2] / "interactive_camera" / "calibration.json"


def _unused_reference_path(path: Path) -> Path:
    """Add a number when a reference photo already exists at this path."""
    candidate = path
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        number += 1
    return candidate


def _load_matrix_inv() -> np.ndarray:
    try:
        matrix = np.asarray(json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))["matrix_inv"],
                            dtype=float)
    except Exception as exc:
        raise RuntimeError(f"Cannot load camera calibration: {exc}") from exc
    if matrix.shape != (2, 2) or not np.isfinite(matrix).all():
        raise RuntimeError("Camera calibration matrix is invalid")
    return matrix


class _CameraThread(QThread):
    frame_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, index: int = 0):
        super().__init__()
        self.index = index
        self._stop = threading.Event()

    def run(self) -> None:
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW) if hasattr(cv2, "CAP_DSHOW") else cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            self.failed.emit("Could not open the microscope camera.")
            return
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if ok:
                    self.frame_ready.emit(frame)
                else:
                    self.failed.emit("Could not read the microscope camera.")
                    return
                self.msleep(30)
        finally:
            cap.release()

    def stop(self) -> None:
        self._stop.set()


class _FrameStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame: np.ndarray | None = None
        self.serial = 0

    def put(self, frame: np.ndarray) -> None:
        with self.lock:
            self.frame = frame.copy()
            self.serial += 1

    def get(self) -> tuple[np.ndarray | None, int]:
        with self.lock:
            return (None if self.frame is None else self.frame.copy(), self.serial)


class SamplePositionsDialog(QDialog):
    """Expose updated reference data and ruby offsets when closed."""

    def __init__(self, controller, reference_frame, reference_path, xrd_position,
                 ruby_positions, *, borrowed_frame=None, save_directory=None,
                 tracking_method="similarity", gasket_ellipse=None,
                 gasket_reference_digest=None, gasket_allow_uniform_scale=False,
                 gasket_sample_point=None, gasket_sample_mode="manual",
                 gasket_reference_position=None, ruby_offset_origin=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sample Positions")
        self.resize(760, 820)
        self.controller = controller
        self.reference_frame = reference_frame
        self.reference_path = reference_path
        self.reference_changed = False
        self.tracking_method = tracking_method
        self.gasket_ellipse = gasket_ellipse
        self.gasket_reference_digest = gasket_reference_digest
        self.gasket_allow_uniform_scale = gasket_allow_uniform_scale
        self.gasket_sample_point = gasket_sample_point
        self.gasket_sample_mode = (gasket_sample_mode if gasket_sample_mode in
                                   ("manual", "gasket_centre") else "manual")
        # The stage position when the reference photo was captured. Older
        # manual setups only saved the later confirmation position and need a
        # new capture; older centre setups saved the capture position itself.
        self.gasket_reference_position = (
            tuple(gasket_reference_position) if gasket_reference_position is not None
            else tuple(xrd_position) if self.gasket_sample_mode == "gasket_centre"
            and xrd_position is not None else None)
        # True once the gasket outline was (re)marked in this dialog.
        self.gasket_changed = False
        self.xrd_position = (
            self.gasket_reference_position if tracking_method == "gasket_beta"
            and self.gasket_reference_position is not None else xrd_position)
        self.ruby_positions = dict(ruby_positions)
        self.ruby_offset_origin = ruby_offset_origin or ruby_origin_for(tracking_method)
        # Ch4/Ch5 at which the gasket centre would sit on the X-ray beam.
        self._gasket_centre_origin: tuple[float, float] | None = None
        self._xray_beam = XrayBeamFileCache()
        self.save_directory = Path(save_directory) if save_directory else Path.cwd()
        self._xrd_established = False
        self.frames = _FrameStore()
        self._borrowed_frame = borrowed_frame
        self._camera_thread = None

        layout = QVBoxLayout(self)
        self.image_label = QLabel("Waiting for microscope camera…")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(300)
        self.image_label.setStyleSheet("background: #222; color: white")
        layout.addWidget(self.image_label)
        reference_row = QHBoxLayout()
        self.reference_label = QLabel()
        self.reference_label.setWordWrap(True)
        reference_row.addWidget(self.reference_label, stretch=1)
        self.preview_button = QPushButton("Preview reference photo")
        self.preview_button.clicked.connect(self._preview_reference)
        reference_row.addWidget(self.preview_button)
        layout.addLayout(reference_row)
        method_name = "gasket outline (Beta)" if tracking_method == "gasket_beta" else "image similarity"
        message = (
            "Capture the reference photo with the stage stopped."
            if tracking_method == "gasket_beta" else
            "Move the stage using the controller in LOCAL mode, then record its stopped "
            "position here."
        ) + f" Position tracking: {method_name}."
        if tracking_method == "gasket_beta":
            message += (" Following moves the selected sample point onto the X-ray beam "
                        "(red); ruby offsets are stored relative to the gasket centre.")
        if self.ruby_positions and self.ruby_offset_origin != ruby_origin_for(tracking_method):
            message += (" The recorded ruby positions belong to the other tracking method "
                        "and will be replaced when a ruby position is recorded.")
        self.status = QLabel(message)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.current_label = QLabel("Current Ch3/4/5: waiting for stage monitor…")
        layout.addWidget(self.current_label)
        self.capture_button = QPushButton(
            "Capture reference photo at current position" if tracking_method == "gasket_beta"
            else "Use current position as XRD (capture photo)")
        self.capture_button.clicked.connect(self._capture_xrd)
        if tracking_method != "gasket_beta":
            self.align_button = QPushButton("Confirm current position against saved XRD photo")
            self.align_button.clicked.connect(self._confirm_xrd)
        self.mark_gasket_button = QPushButton("Mark gasket outline (Beta)…")
        self.mark_gasket_button.clicked.connect(self._mark_gasket)
        self.gasket_label = QLabel()
        if tracking_method == "gasket_beta":
            # Photo -> gasket outline -> choose sample point -> ruby positions.
            layout.addWidget(QLabel(
                "1. Capture the XRD reference photo (skip if the saved photo is still valid)."))
            row = QHBoxLayout()
            row.addWidget(self.capture_button)
            row.addStretch()
            layout.addLayout(row)
            layout.addWidget(QLabel(
                "2. Mark the gasket outline on the reference photo."))
            row = QHBoxLayout()
            row.addWidget(self.mark_gasket_button)
            row.addWidget(self.gasket_label)
            row.addStretch()
            layout.addLayout(row)
            layout.addWidget(QLabel("3. Choose the sample position."))
            self.centre_radio = QRadioButton("Use the detected gasket centre")
            self.manual_radio = QRadioButton(
                "Choose the sample position manually on the reference photo")
            layout.addWidget(self.centre_radio)
            layout.addWidget(self.manual_radio)
            (self.centre_radio if self.gasket_sample_mode == "gasket_centre"
             else self.manual_radio).setChecked(True)
            self.centre_radio.toggled.connect(self._on_sample_mode_changed)
            self.manual_radio.toggled.connect(self._on_sample_mode_changed)
            ruby_step = 4
        else:
            self.mark_gasket_button.hide()
            self.gasket_label.hide()
            layout.addWidget(QLabel(
                "1. Capture the XRD reference photo, or confirm the current position "
                "against the saved one."))
            row = QHBoxLayout()
            row.addWidget(self.align_button)
            row.addWidget(self.capture_button)
            layout.addLayout(row)
            ruby_step = 2
        self.xrd_label = QLabel()
        layout.addWidget(self.xrd_label)
        layout.addWidget(QLabel(
            f"{ruby_step}. Move the stage to a ruby fluorescence position with the controller."))
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Ruby position name")
        row.addWidget(self.name)
        self.record_button = QPushButton("Record current as ruby position")
        self.record_button.clicked.connect(self._record_ruby)
        row.addWidget(self.record_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.positions = QComboBox()
        self.positions.wheelEvent = lambda event: event.ignore()
        self.positions.currentTextChanged.connect(self._show_selected)
        row.addWidget(self.positions)
        delete_button = QPushButton("Delete selected")
        delete_button.clicked.connect(self._delete_selected)
        row.addWidget(delete_button)
        layout.addLayout(row)
        self.offset_label = QLabel()
        layout.addWidget(self.offset_label)
        close_button = QPushButton("Done")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)
        self._refresh()
        if tracking_method == "gasket_beta":
            self._establish_gasket_position(show_warning=False)
        self._position_timer = QTimer(self)
        self._position_timer.timeout.connect(self._update_current_position)
        self._position_timer.start(500)
        self._update_current_position()

        if borrowed_frame is not None:
            self._camera_timer = QTimer(self)
            self._camera_timer.timeout.connect(self._poll_borrowed)
            self._camera_timer.start(100)
            self._poll_borrowed()
        else:
            self._camera_thread = _CameraThread()
            self._camera_thread.frame_ready.connect(self._on_frame)
            self._camera_thread.failed.connect(self.status.setText)
            self._camera_thread.start()

    def _poll_borrowed(self) -> None:
        frame = self._borrowed_frame()
        if frame is not None:
            self._on_frame(frame)

    def _on_frame(self, frame) -> None:
        self.frames.put(frame)
        beam = self._xray_beam.get()
        if self.tracking_method == "gasket_beta" and beam is not None:
            frame = frame.copy()
            draw_xray_beam(frame, beam.point)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
        self.image_label.setPixmap(QPixmap.fromImage(image).scaled(
            710, 410, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _read_position(self):
        if self.controller is None:
            raise RuntimeError("Stage controller is not connected.")
        if self.controller.get_is_moving():
            raise RuntimeError("Wait for the stage to stop before recording a position.")
        values = tuple(self.controller.get_ch_pos(ch) for ch in (3, 4, 5))
        if any(value is None for value in values):
            raise RuntimeError("Ch3/4/5 positions could not be read from the stage.")
        if self.controller.get_is_moving():
            raise RuntimeError("The stage moved while its position was being read. Try again after it stops.")
        return tuple(int(value) for value in values)

    def _update_current_position(self) -> None:
        if self.controller is None:
            self.current_label.setText("Current Ch3/4/5: stage disconnected")
            return
        try:
            states = self.controller.get_cached_states([3, 4, 5], max_age=5.0)
            if all(ch in states for ch in (3, 4, 5)):
                self.current_label.setText(
                    "Current Ch3/4/5: " + str(tuple(int(states[ch].position) for ch in (3, 4, 5)))
                )
            else:
                self.current_label.setText("Current Ch3/4/5: waiting for stage monitor…")
        except Exception:
            self.current_label.setText("Current Ch3/4/5: unavailable")

    def _capture_xrd(self) -> None:
        try:
            position = self._read_position()
            frame, _ = self.frames.get()
            if frame is None:
                raise RuntimeError("No microscope image is available.")
            if self._read_position() != position:
                raise RuntimeError("The stage position changed during capture. Try again after it stops.")
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            suggested = _unused_reference_path(
                self.save_directory / f"xrd_reference_{timestamp}.png"
            )
            path, _ = QFileDialog.getSaveFileName(
                self, "Save XRD Reference Photo", str(suggested),
                "PNG Image (*.png)",
            )
            if not path:
                return
            if self._read_position() != position:
                raise RuntimeError("The stage position changed while choosing a photo path. Try again after it stops.")
            target = _unused_reference_path(Path(path))
            target.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(target), frame):
                raise RuntimeError(f"Could not save reference image: {target}")
            self.save_directory = target.parent
            self.reference_path = target
            self.reference_frame = frame
            self.reference_changed = True
            self.gasket_ellipse = None
            self.gasket_sample_point = None
            self.gasket_reference_digest = None
            self.gasket_changed = False
            self._gasket_centre_origin = None
            self.gasket_reference_position = position
            self.xrd_position = position
            self._xrd_established = self.tracking_method != "gasket_beta"
            self.ruby_positions.clear()
            self._refresh()
            message = (f"XRD reference photo saved as {target.name}; Ch3/4/5 capture position "
                       "recorded. Previous ruby offsets cleared.")
            if self.tracking_method == "gasket_beta":
                message += " Mark the gasket outline (step 2), then choose the sample position."
            self.status.setText(message)
        except Exception as exc:
            QMessageBox.warning(self, "XRD Reference", str(exc))

    def _preview_reference(self) -> None:
        if self.reference_frame is None:
            return
        frame = self.reference_frame
        if frame.dtype != np.uint8:
            frame = np.clip(frame.astype(np.float64), 0, 255).astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] >= 3:
            rgb = np.ascontiguousarray(cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2RGB))
            height, width, _ = rgb.shape
            image = QImage(rgb.tobytes(), width, height, rgb.strides[0],
                           QImage.Format.Format_RGB888)
        else:
            gray = np.ascontiguousarray(frame if frame.ndim == 2 else frame[:, :, 0])
            height, width = gray.shape
            image = QImage(gray.tobytes(), width, height, gray.strides[0],
                           QImage.Format.Format_Grayscale8)
        preview = QDialog(self)
        preview.setWindowTitle("Reference Image Preview")
        preview_layout = QVBoxLayout(preview)
        label = QLabel()
        label.setPixmap(QPixmap.fromImage(image).scaled(
            640, 480, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        preview_layout.addWidget(label)
        preview.exec()

    def _confirm_xrd(self) -> None:
        try:
            if self.tracking_method == "gasket_beta":
                raise RuntimeError("Gasket tracking uses the reference capture; confirmation is unavailable.")
            if self.reference_frame is None:
                raise RuntimeError("Capture an XRD reference photo first.")
            position = self._read_position()
            frame, _ = self.frames.get()
            if frame is None:
                raise RuntimeError("No microscope image is available.")
            dx, dy, score = measure_xy_shift_strict(self.reference_frame, frame)
            aligned = abs(dx) <= 2 and abs(dy) <= 2
            if not aligned:
                raise RuntimeError(
                    f"The current image is {dx:+.1f}, {dy:+.1f} pixels from the XRD photo "
                    "(tolerance 2 px). Adjust the stage with the controller and try again."
                )
            if self._read_position() != position:
                raise RuntimeError("The stage position changed during image comparison. Try again after it stops.")
            self.xrd_position = position
            self._xrd_established = True
            self._refresh()
            self.status.setText(
                f"XRD photo matched by {self.tracking_method} ({score:.2f}); "
                f"Ch3/4/5 {position} recorded. "
                "Move to a ruby position with the controller."
            )
        except Exception as exc:
            QMessageBox.warning(self, "XRD Reference", str(exc))

    def _mark_gasket(self) -> None:
        try:
            if self.reference_path is None:
                raise RuntimeError("Capture an XRD reference photo first.")
            frame = cv2.imread(str(self.reference_path), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError("Could not read the reference image.")
            digest = reference_digest(self.reference_path)
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self, "Gasket outline", str(exc))
            return
        prior = None
        prior_sample = None
        if self.gasket_reference_digest == digest and self.gasket_ellipse:
            try:
                prior = GasketEllipse.from_dict(self.gasket_ellipse)
            except (ValueError, KeyError, TypeError):
                pass
            prior_sample = self.gasket_sample_point
        dialog = GasketMarkDialog(frame, prior, self, sample_point=prior_sample,
                                  sample_mode=self.gasket_sample_mode)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.gasket_ellipse = dialog.canvas.ellipse.to_dict()
        self.gasket_sample_point = (dict(dialog.sample_point)
                                    if dialog.sample_point is not None else None)
        self.gasket_reference_digest = digest
        self.gasket_changed = True
        # The calculated ruby origin depends on this mark.
        self._gasket_centre_origin = None
        self._xrd_established = False
        self._refresh()
        self._establish_gasket_position(show_warning=True)

    def _on_sample_mode_changed(self, checked: bool) -> None:
        if not checked:
            return
        previous = self.gasket_sample_mode
        self.gasket_sample_mode = "gasket_centre" if self.centre_radio.isChecked() else "manual"
        self._gasket_centre_origin = None
        self._xrd_established = False
        if self.gasket_sample_mode == "manual":
            if previous == "gasket_centre":
                self.gasket_sample_point = None
                self.gasket_changed = True
            self.status.setText("Mark the sample position on the reference photo (step 2).")
        else:
            self.gasket_sample_point = None
            self.gasket_changed = True
            self._establish_gasket_position(show_warning=True)
        self._refresh()

    def _establish_gasket_position(self, *, show_warning: bool) -> bool:
        """Calculate the ruby origin from the reference capture without moving."""
        try:
            if self.gasket_reference_position is None:
                raise RuntimeError("Capture a new XRD reference photo to record its stage position.")
            if self.reference_frame is None or not self.gasket_ellipse or not self._gasket_marked():
                raise RuntimeError("Mark the gasket outline on the XRD reference image first (step 2).")
            if self.gasket_sample_mode == "manual" and self.gasket_sample_point is None:
                raise RuntimeError("Mark the sample position on the reference photo (step 2).")
            beam = check_xray_beam(
                self._xray_beam.get(), self.reference_frame.shape,
                read_microscope_pulses(self.controller, channels=GASKET_BEAM_CHANNELS),
                channels=GASKET_BEAM_CHANNELS)
            detected = detect_gasket_hole(
                self.reference_frame, self.reference_frame,
                GasketEllipse.from_dict(self.gasket_ellipse),
                allow_uniform_scale=self.gasket_allow_uniform_scale)
            if self.gasket_sample_mode == "manual":
                sample_offset(self.gasket_sample_point,
                              (detected.ellipse.cx, detected.ellipse.cy))
            matrix_inv = _load_matrix_inv()
            shift = matrix_inv @ np.array([
                beam.x - detected.ellipse.cx, beam.y - detected.ellipse.cy])
            if self.gasket_sample_mode == "gasket_centre":
                self.gasket_sample_point = {
                    "x": detected.ellipse.cx, "y": detected.ellipse.cy}
                self.gasket_changed = True
            self._gasket_centre_origin = (
                self.gasket_reference_position[1] + float(shift[0]),
                self.gasket_reference_position[2] + float(shift[1]))
            self._xrd_established = True
            self.status.setText(
                "Sample position saved. Following will move it onto the X-ray beam; "
                "move to a ruby position to record it.")
            return True
        except Exception as exc:
            self._gasket_centre_origin = None
            self._xrd_established = False
            self.status.setText(str(exc))
            if show_warning and self.gasket_ellipse is not None and (
                    self.gasket_sample_mode == "gasket_centre" or self.gasket_sample_point):
                QMessageBox.warning(self, "Sample position", str(exc))
            return False

    def _gasket_marked(self) -> bool:
        if not (self.gasket_ellipse and self.reference_path is not None):
            return False
        try:
            return reference_digest(self.reference_path) == self.gasket_reference_digest
        except OSError:
            return False

    def _record_ruby(self) -> None:
        try:
            if self.tracking_method == "gasket_beta":
                if not self._establish_gasket_position(show_warning=False):
                    raise RuntimeError(self.status.text())
            if self.xrd_position is None or not self._xrd_established:
                raise RuntimeError("Set up the XRD reference before recording ruby positions.")
            name = self.name.text().strip()
            if not name:
                raise ValueError("Enter a ruby position name.")
            position = self._read_position()
            origin = ruby_origin_for(self.tracking_method)
            offsets = {f"ch{ch}": position[i] - self.xrd_position[i]
                       for i, ch in enumerate((3, 4, 5))}
            if origin == RUBY_ORIGIN_GASKET_CENTRE:
                if self._gasket_centre_origin is None:
                    raise RuntimeError("The gasket centre position could not be calculated.")
                offsets["ch4"] = int(round(position[1] - self._gasket_centre_origin[0]))
                offsets["ch5"] = int(round(position[2] - self._gasket_centre_origin[1]))
            note = ""
            if origin != self.ruby_offset_origin:
                if self.ruby_positions:
                    note = " Ruby positions of the other tracking method were removed."
                self.ruby_positions.clear()
                self.ruby_offset_origin = origin
            self.ruby_positions[name] = offsets
            self._refresh(select=name)
            self.status.setText(f"Ruby position {name!r} recorded.{note}")
        except Exception as exc:
            QMessageBox.warning(self, "Ruby Position", str(exc))

    def _delete_selected(self) -> None:
        self.ruby_positions.pop(self.positions.currentText(), None)
        self._refresh()

    def _refresh(self, select: str | None = None) -> None:
        if self.reference_frame is not None and self.reference_path is not None:
            self.reference_label.setText(f"Reference photo: {Path(self.reference_path).name}")
            self.reference_label.setToolTip(str(self.reference_path))
        else:
            self.reference_label.setText("Reference photo: none available")
            self.reference_label.setToolTip("")
        self.preview_button.setEnabled(self.reference_frame is not None)
        label = ("Reference capture Ch3/4/5: " if self.tracking_method == "gasket_beta"
                 else "XRD Ch3/4/5: ")
        shown_position = (self.gasket_reference_position
                          if self.tracking_method == "gasket_beta" else self.xrd_position)
        self.xrd_label.setText(
            label + (
                str(shown_position) if shown_position
                else "not established (capture an XRD reference)"
            )
        )
        self.mark_gasket_button.setEnabled(self.reference_path is not None)
        self.gasket_label.setText(
            "Gasket outline: marked" if self._gasket_marked() else "Gasket outline: not marked")
        self.positions.blockSignals(True)
        self.positions.clear()
        self.positions.addItems(sorted(self.ruby_positions))
        if select:
            self.positions.setCurrentText(select)
        self.positions.blockSignals(False)
        self._show_selected()

    def _show_selected(self) -> None:
        values = self.ruby_positions.get(self.positions.currentText())
        origin = ("gasket centre" if self.ruby_offset_origin == RUBY_ORIGIN_GASKET_CENTRE
                  else "XRD position")
        self.offset_label.setText(
            f"Ruby offset from the {origin} (pulse): " +
            ("   ".join(f"Ch{ch} {values[f'ch{ch}']:+d}" for ch in (3, 4, 5))
             if values else "none")
        )

    def _prepare_close(self) -> bool:
        if hasattr(self, "_camera_timer"):
            self._camera_timer.stop()
        self._position_timer.stop()
        if self._camera_thread is not None:
            self._camera_thread.stop()
            if not self._camera_thread.wait(3000):
                QMessageBox.warning(self, "Camera", "Wait for the camera to stop before closing.")
                return False
            self._camera_thread = None
        return True

    def done(self, result: int) -> None:
        if self._prepare_close():
            super().done(result)

    def closeEvent(self, event) -> None:
        if not self._prepare_close():
            event.ignore()
            return
        super().closeEvent(event)
