"""Record manually positioned XRD and ruby locations with a live camera."""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import cv2
import numpy as np
from apps.interactive_camera.sample_tracking import measure_xy_shift_strict
from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole, detect_gasket_hole_near, reference_digest,
    sample_offset,
)
from apps.interactive_camera.xray_beam import (
    XrayBeamFileCache, check_xray_beam, draw_xray_beam, read_microscope_pulses,
)
from apps.exp_scheduler.scheduler_settings import RUBY_ORIGIN_GASKET_CENTRE, ruby_origin_for
from apps.exp_scheduler.ui.gasket_mark_dialog import GasketMarkDialog
from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)


_CALIBRATION_PATH = Path(__file__).parents[2] / "interactive_camera" / "calibration.json"


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
                 gasket_reference_digest=None, gasket_tolerance_px=8.0,
                 gasket_sample_point=None, ruby_offset_origin=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sample Positions")
        self.resize(760, 720)
        self.controller = controller
        self.reference_frame = reference_frame
        self.reference_path = reference_path
        self.reference_changed = False
        self.tracking_method = tracking_method
        self.gasket_ellipse = gasket_ellipse
        self.gasket_reference_digest = gasket_reference_digest
        self.gasket_tolerance_px = gasket_tolerance_px
        self.gasket_sample_point = gasket_sample_point
        # True once the gasket outline was (re)marked in this dialog.
        self.gasket_changed = False
        self.xrd_position = xrd_position
        self.ruby_positions = dict(ruby_positions)
        self.ruby_offset_origin = ruby_offset_origin or ruby_origin_for(tracking_method)
        # Ch4/Ch5 at which the gasket centre would sit on the X-ray beam;
        # measured when the XRD position is confirmed in gasket beta mode.
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
        self.image_label.setMinimumHeight(350)
        self.image_label.setStyleSheet("background: #222; color: white")
        layout.addWidget(self.image_label)
        method_name = "gasket outline (Beta)" if tracking_method == "gasket_beta" else "image similarity"
        message = (
            "Move the stage using the controller in LOCAL mode, then record its stopped "
            f"position here. Position tracking: {method_name}."
        )
        if tracking_method == "gasket_beta":
            message += (" The XRD position puts the marked sample centre on the X-ray beam "
                        "(red); ruby offsets are stored relative to the gasket centre.")
        if self.ruby_positions and self.ruby_offset_origin != ruby_origin_for(tracking_method):
            message += (" The recorded ruby positions belong to the other tracking method "
                        "and will be replaced when a ruby position is recorded.")
        self.status = QLabel(message)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.current_label = QLabel("Current Ch3/4/5: waiting for stage monitor…")
        layout.addWidget(self.current_label)
        self.capture_button = QPushButton("Use current position as XRD (capture photo)")
        self.capture_button.clicked.connect(self._capture_xrd)
        self.align_button = QPushButton(
            "Confirm sample is on the X-ray beam" if tracking_method == "gasket_beta"
            else "Confirm current position against saved XRD photo")
        self.align_button.clicked.connect(self._confirm_xrd)
        self.mark_gasket_button = QPushButton("Mark gasket outline (Beta)…")
        self.mark_gasket_button.clicked.connect(self._mark_gasket)
        self.gasket_label = QLabel()
        if tracking_method == "gasket_beta":
            # Photo -> gasket outline -> beam confirmation -> ruby positions.
            layout.addWidget(QLabel(
                "1. Capture the XRD reference photo (skip if the saved photo is still valid)."))
            row = QHBoxLayout()
            row.addWidget(self.capture_button)
            row.addStretch()
            layout.addLayout(row)
            layout.addWidget(QLabel(
                "2. Mark the gasket outline and sample centre on the reference photo."))
            row = QHBoxLayout()
            row.addWidget(self.mark_gasket_button)
            row.addWidget(self.gasket_label)
            row.addStretch()
            layout.addLayout(row)
            layout.addWidget(QLabel(
                "3. Put the sample on the X-ray beam with the controller and confirm it."))
            row = QHBoxLayout()
            row.addWidget(self.align_button)
            row.addStretch()
            layout.addLayout(row)
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
            path, _ = QFileDialog.getSaveFileName(
                self, "Save XRD Reference Photo", str(self.save_directory / "xrd_reference.png"),
                "PNG Image (*.png)",
            )
            if not path:
                return
            if self._read_position() != position:
                raise RuntimeError("The stage position changed while choosing a photo path. Try again after it stops.")
            target = Path(path)
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
            self.xrd_position = position
            self._xrd_established = True
            self.ruby_positions.clear()
            self._refresh()
            message = "XRD reference photo and Ch3/4/5 position recorded. Previous ruby offsets cleared."
            if self.tracking_method == "gasket_beta":
                message += (" Mark the gasket outline and sample centre (step 2), then confirm "
                            "that the sample is on the X-ray beam before recording ruby positions.")
            self.status.setText(message)
        except Exception as exc:
            QMessageBox.warning(self, "XRD Reference", str(exc))

    def _confirm_xrd(self) -> None:
        try:
            if self.reference_frame is None:
                raise RuntimeError("Capture an XRD reference photo first.")
            position = self._read_position()
            frame, _ = self.frames.get()
            if frame is None:
                raise RuntimeError("No microscope image is available.")
            centre_origin = None
            if self.tracking_method == "gasket_beta":
                if self.reference_path is None or self.gasket_ellipse is None:
                    raise RuntimeError("Mark the gasket outline on the XRD reference image first (step 2).")
                if not self.gasket_sample_point:
                    raise RuntimeError("Mark the sample centre with Mark gasket outline first (step 2).")
                if reference_digest(self.reference_path) != self.gasket_reference_digest:
                    raise RuntimeError("The gasket outline belongs to a different reference image. Mark it again.")
                beam = check_xray_beam(self._xray_beam.get(), frame.shape,
                                       read_microscope_pulses(self.controller))
                marked = GasketEllipse.from_dict(self.gasket_ellipse)
                origin = detect_gasket_hole(self.reference_frame, self.reference_frame, marked)
                ox, oy = sample_offset(self.gasket_sample_point,
                                       (origin.ellipse.cx, origin.ellipse.cy))
                target = (beam.x - ox, beam.y - oy)
                detected = detect_gasket_hole_near(
                    self.reference_frame, frame, marked, (target, None))
                dx = detected.ellipse.cx - target[0]
                dy = detected.ellipse.cy - target[1]
                score = detected.score
                aligned = math.hypot(dx, dy) <= self.gasket_tolerance_px
                tolerance = f"{self.gasket_tolerance_px:g}"
                # Stage position that would put the gasket centre itself on the beam.
                shift = _load_matrix_inv() @ np.array([beam.x - detected.ellipse.cx,
                                                       beam.y - detected.ellipse.cy])
                centre_origin = (position[1] + float(shift[0]), position[2] + float(shift[1]))
            else:
                dx, dy, score = measure_xy_shift_strict(self.reference_frame, frame)
                aligned = abs(dx) <= 2 and abs(dy) <= 2
                tolerance = "2"
            if not aligned:
                where = ("the X-ray beam (sample offset)" if self.tracking_method == "gasket_beta"
                         else "the XRD photo")
                hint = (" In Interactive Camera, right-click the sample and choose "
                        "Move this position to: X-ray beam."
                        if self.tracking_method == "gasket_beta" else "")
                raise RuntimeError(
                    f"The current image is {dx:+.1f}, {dy:+.1f} pixels from {where} "
                    f"(tolerance {tolerance} px). "
                    "Adjust the stage with the controller and try again." + hint
                )
            if self._read_position() != position:
                raise RuntimeError("The stage position changed during image comparison. Try again after it stops.")
            self.xrd_position = position
            self._xrd_established = True
            self._gasket_centre_origin = centre_origin
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
        dialog = GasketMarkDialog(frame, prior, self, sample_point=prior_sample)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.gasket_ellipse = dialog.canvas.ellipse.to_dict()
        self.gasket_sample_point = dict(dialog.sample_point)
        self.gasket_reference_digest = digest
        self.gasket_changed = True
        # The beam confirmation depends on the marked sample centre.
        self._gasket_centre_origin = None
        self._refresh()
        self.status.setText(
            "Gasket outline and sample centre marked. Confirm that the sample is on "
            "the X-ray beam (step 3) before recording ruby positions."
        )

    def _gasket_marked(self) -> bool:
        if not (self.gasket_ellipse and self.gasket_sample_point
                and self.reference_path is not None):
            return False
        try:
            return reference_digest(self.reference_path) == self.gasket_reference_digest
        except OSError:
            return False

    def _record_ruby(self) -> None:
        try:
            if self.xrd_position is None or not self._xrd_established:
                raise RuntimeError("First confirm the saved XRD photo or capture a new XRD reference.")
            name = self.name.text().strip()
            if not name:
                raise ValueError("Enter a ruby position name.")
            position = self._read_position()
            origin = ruby_origin_for(self.tracking_method)
            offsets = {f"ch{ch}": position[i] - self.xrd_position[i]
                       for i, ch in enumerate((3, 4, 5))}
            if origin == RUBY_ORIGIN_GASKET_CENTRE:
                if self._gasket_centre_origin is None:
                    raise RuntimeError(
                        "Confirm that the sample is on the X-ray beam before recording ruby positions.")
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
        self.xrd_label.setText(
            "XRD Ch3/4/5: " + (
                str(self.xrd_position) if self.xrd_position
                else "not established (confirm or capture an XRD reference)"
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
