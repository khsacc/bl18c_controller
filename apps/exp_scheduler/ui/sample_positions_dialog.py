"""Record manually positioned XRD and ruby locations with a live camera."""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np
from apps.interactive_camera.sample_tracking import measure_xy_shift_strict
from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)


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
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sample Positions")
        self.resize(760, 720)
        self.controller = controller
        self.reference_frame = reference_frame
        self.reference_path = reference_path
        self.reference_changed = False
        self.xrd_position = xrd_position
        self.ruby_positions = dict(ruby_positions)
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
        self.status = QLabel("Move the stage using the controller in LOCAL mode, then record its stopped position here.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.align_button = QPushButton("Confirm current position against saved XRD photo")
        self.align_button.clicked.connect(self._confirm_xrd)
        row.addWidget(self.align_button)
        self.capture_button = QPushButton("Use current position as XRD (capture photo)")
        self.capture_button.clicked.connect(self._capture_xrd)
        row.addWidget(self.capture_button)
        layout.addLayout(row)
        self.xrd_label = QLabel()
        layout.addWidget(self.xrd_label)
        self.current_label = QLabel("Current Ch3/4/5: waiting for stage monitor…")
        layout.addWidget(self.current_label)
        layout.addWidget(QLabel("2. Move the stage to a ruby fluorescence position with the controller."))
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
            self.xrd_position = position
            self._xrd_established = True
            self.ruby_positions.clear()
            self._refresh()
            self.status.setText("XRD reference photo and Ch3/4/5 position recorded. Previous ruby offsets cleared.")
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
            dx, dy, score = measure_xy_shift_strict(self.reference_frame, frame)
            if abs(dx) > 2 or abs(dy) > 2:
                raise RuntimeError(
                    f"The current image is {dx:+d}, {dy:+d} pixels from the XRD photo. "
                    "Adjust the stage with the controller and try again."
                )
            if self._read_position() != position:
                raise RuntimeError("The stage position changed during image comparison. Try again after it stops.")
            self.xrd_position = position
            self._xrd_established = True
            self._refresh()
            self.status.setText(
                f"XRD photo matched ({score:.2f}); Ch3/4/5 {position} recorded. "
                "Move to a ruby position with the controller."
            )
        except Exception as exc:
            QMessageBox.warning(self, "XRD Reference", str(exc))

    def _record_ruby(self) -> None:
        try:
            if self.xrd_position is None or not self._xrd_established:
                raise RuntimeError("First confirm the saved XRD photo or capture a new XRD reference.")
            name = self.name.text().strip()
            if not name:
                raise ValueError("Enter a ruby position name.")
            position = self._read_position()
            self.ruby_positions[name] = {f"ch{ch}": position[i] - self.xrd_position[i]
                                         for i, ch in enumerate((3, 4, 5))}
            self._refresh(select=name)
            self.status.setText(f"Ruby position {name!r} recorded.")
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
        self.positions.blockSignals(True)
        self.positions.clear()
        self.positions.addItems(sorted(self.ruby_positions))
        if select:
            self.positions.setCurrentText(select)
        self.positions.blockSignals(False)
        self._show_selected()

    def _show_selected(self) -> None:
        values = self.ruby_positions.get(self.positions.currentText())
        self.offset_label.setText(
            "Ruby offset (pulse): " +
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
