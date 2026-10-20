"""Live-camera setup of an XRD reference and named ruby offsets."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from apps.interactive_camera.sample_tracking import measure_xy_shift_strict
from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

_CALIBRATION_PATH = Path(__file__).parents[2] / "interactive_camera" / "calibration.json"


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


class _StageTask(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, controller, frames: _FrameStore, *, reference=None,
                 jog: tuple[int, int] | None = None, max_pulses: tuple[int, int] = (200, 3600),
                 xrd_ch3: int | None = None):
        super().__init__()
        self.controller = controller
        self.frames = frames
        self.reference = reference
        self.jog = jog
        self.max_pulses = max_pulses
        self.xrd_ch3 = xrd_ch3
        self.cancelled = threading.Event()
        self.lease_acquired = threading.Event()

    def _move(self, ch: int, target: int, lease) -> None:
        if self.cancelled.is_set():
            raise RuntimeError("Setup move was stopped.")
        current = int(self.controller.get_ch_pos(ch))
        if current == target:
            return
        ok, reason = self.controller.check_move_constraints(ch, target)
        if not ok:
            raise ValueError(reason)
        ok, reason = self.controller.check_soft_limits(ch, target)
        if not ok:
            raise ValueError(reason)
        ok, reason = self.controller.check_max_move(ch, target - current)
        if not ok:
            raise ValueError(reason)
        self.controller.move_ch_absolute(ch, target, motion=lease)
        self.controller.wait_until_stop(motion=lease, stay_in_rem=True)
        if self.cancelled.is_set():
            raise RuntimeError("Setup move was stopped.")

    def run(self) -> None:
        lease = None
        moved = False
        try:
            ctrl = self.controller
            if ctrl.get_is_moving():
                raise RuntimeError("Wait for the stage to stop before starting a setup move.")
            lease = ctrl.acquire_motion("Sample position setup", "XRD alignment or manual jog")
            self.lease_acquired.set()
            if self.cancelled.is_set():
                raise RuntimeError("Setup move was stopped.")
            if self.jog is not None:
                ch, delta = self.jog
                moved = True
                self._move(ch, int(ctrl.get_ch_pos(ch)) + delta, lease)
            else:
                if self.reference is None:
                    raise ValueError("Capture an XRD reference image first.")
                if self.xrd_ch3 is not None and int(ctrl.get_ch_pos(3)) != self.xrd_ch3:
                    moved = True
                    self._move(3, self.xrd_ch3, lease)
                data = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
                matrix = np.asarray(data["matrix_inv"], dtype=float)
                if matrix.shape != (2, 2) or not np.isfinite(matrix).all():
                    raise ValueError("Camera calibration matrix is invalid.")
                last_serial = -1
                for attempt in range(6):
                    if self.cancelled.is_set():
                        raise RuntimeError("Setup move was stopped.")
                    deadline = time.monotonic() + 5
                    frame = None
                    while time.monotonic() < deadline:
                        if self.cancelled.is_set():
                            raise RuntimeError("Setup move was stopped.")
                        frame, serial = self.frames.get()
                        if frame is not None and serial != last_serial:
                            last_serial = serial
                            break
                        self.msleep(50)
                    else:
                        raise RuntimeError("No fresh microscope frame is available.")
                    dx, dy, score = measure_xy_shift_strict(self.reference, frame)
                    shift = np.array([dx, dy])
                    self.progress.emit(f"Image match {score:.2f}; pixel shift {shift[0]:+d}, {shift[1]:+d}")
                    if np.max(np.abs(shift)) <= 2:
                        break
                    correction = np.rint(-(matrix @ shift)).astype(int)
                    if any(abs(int(correction[i])) > self.max_pulses[i] for i in (0, 1)):
                        raise RuntimeError("Required correction exceeds the configured per-step follow limit.")
                    if not correction.any():
                        raise RuntimeError("Image shift cannot be corrected at the current calibration scale.")
                    for ch, delta in ((4, int(correction[0])), (5, int(correction[1]))):
                        if delta:
                            moved = True
                            self._move(ch, int(ctrl.get_ch_pos(ch)) + delta, lease)
                    # Allow the image to settle and require a newly captured frame.
                    last_serial = self.frames.get()[1]
                    self.msleep(150)
                else:
                    raise RuntimeError("XRD alignment did not converge after six corrections.")
            self.succeeded.emit(tuple(int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)))
        except Exception as exc:
            message = str(exc)
            if moved:
                message += " Verify the stage position before recording a ruby offset."
            self.failed.emit(message)
        finally:
            if lease is not None:
                self.controller.release_motion(lease)


class SamplePositionsDialog(QDialog):
    """Returns updated reference data through ``state`` when closed."""

    def __init__(self, controller, reference_frame, reference_path, xrd_position,
                 ruby_positions, *, borrowed_frame=None, save_directory=None,
                 max_pulses=(200, 3600), parent=None):
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
        self.max_pulses = max_pulses
        self._xrd_established = False
        self.frames = _FrameStore()
        self._borrowed_frame = borrowed_frame
        self._camera_thread = None
        self._stage_task = None

        layout = QVBoxLayout(self)
        self.image_label = QLabel("Waiting for microscope camera…")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(350)
        self.image_label.setStyleSheet("background: #222; color: white")
        layout.addWidget(self.image_label)
        self.status = QLabel("Stage moves use Ch3/4/5 pulses. Manual jog is available below.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.align_button = QPushButton("Move to XRD position by image recognition")
        self.align_button.clicked.connect(self._align)
        row.addWidget(self.align_button)
        self.capture_button = QPushButton("Use current position as XRD (capture photo)")
        self.capture_button.clicked.connect(self._capture_xrd)
        row.addWidget(self.capture_button)
        layout.addLayout(row)
        self.stop_button = QPushButton("Stop setup move")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_move)
        layout.addWidget(self.stop_button)
        self.xrd_label = QLabel()
        layout.addWidget(self.xrd_label)
        self.current_label = QLabel("Current Ch3/4/5: waiting for stage monitor…")
        layout.addWidget(self.current_label)
        layout.addWidget(QLabel("2. Move the stage to a ruby fluorescence position."))
        grid = QGridLayout()
        self.jog_step = QSpinBox()
        self.jog_step.setRange(1, 500)
        self.jog_step.setValue(10)
        self.jog_step.wheelEvent = lambda event: event.ignore()
        grid.addWidget(QLabel("Jog step (pulse):"), 0, 0)
        grid.addWidget(self.jog_step, 0, 1)
        for col, ch in enumerate((3, 4, 5)):
            for row_idx, sign in enumerate((-1, 1), 1):
                button = QPushButton(f"Ch{ch} {'−' if sign < 0 else '+'}")
                button.clicked.connect(lambda _, c=ch, s=sign: self._start_stage(jog=(c, s * self.jog_step.value())))
                grid.addWidget(button, row_idx, col + 1)
        layout.addLayout(grid)
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
        states = self.controller.get_cached_states([3, 4, 5], max_age=5.0)
        if any(ch not in states for ch in (3, 4, 5)):
            raise RuntimeError("Fresh Ch3/4/5 positions are not available.")
        return tuple(int(states[ch].position) for ch in (3, 4, 5))

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
        except Exception:
            self.current_label.setText("Current Ch3/4/5: unavailable")

    def _capture_xrd(self) -> None:
        try:
            position = self._read_position()
            frame, _ = self.frames.get()
            if frame is None:
                raise RuntimeError("No microscope image is available.")
            path, _ = QFileDialog.getSaveFileName(
                self, "Save XRD Reference Photo", str(self.save_directory / "xrd_reference.png"),
                "PNG Image (*.png)",
            )
            if not path:
                return
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

    def _align(self) -> None:
        if self.reference_frame is None:
            QMessageBox.warning(self, "XRD Alignment", "Capture an XRD reference photo first.")
            return
        self._xrd_established = False
        if self.xrd_position is None:
            self.status.setText("No saved Ch3 reference; align XY with the current Ch3 position.")
        self._start_stage(reference=self.reference_frame)

    def _start_stage(self, *, reference=None, jog=None) -> None:
        if self.controller is None:
            QMessageBox.warning(self, "Stage", "Stage controller is not connected.")
            return
        if self._stage_task is not None and self._stage_task.isRunning():
            return
        self._stage_task = _StageTask(self.controller, self.frames, reference=reference,
                                      jog=jog, max_pulses=self.max_pulses,
                                      xrd_ch3=(self.xrd_position[0] if reference is not None
                                               and self.xrd_position is not None else None))
        self._stage_task.progress.connect(self.status.setText)
        self._stage_task.failed.connect(lambda message: QMessageBox.warning(self, "Stage Move", message))
        self._stage_task.succeeded.connect(self._stage_done)
        self._stage_task.finished.connect(self._stage_finished)
        self.align_button.setEnabled(False)
        self.capture_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._stage_task.start()

    def _stop_move(self) -> None:
        if self._stage_task is not None and self._stage_task.isRunning():
            self._stage_task.cancelled.set()
            if self._stage_task.lease_acquired.is_set():
                try:
                    self.controller.request_normal_stop(source="sample_position_setup")
                except Exception as exc:
                    QMessageBox.warning(self, "Stage Stop", str(exc))
            self.status.setText("Stopping setup move…")
            self.stop_button.setEnabled(False)

    def _stage_done(self, position) -> None:
        if self._stage_task.jog is None:
            self.xrd_position = position
            self._xrd_established = True
            self._refresh()
            self.status.setText("XRD position reached. Move to a ruby position and record it.")
        else:
            self.status.setText(f"Stage stopped at Ch3/4/5 {position}.")

    def _stage_finished(self) -> None:
        self.align_button.setEnabled(True)
        self.capture_button.setEnabled(True)
        self.record_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _record_ruby(self) -> None:
        try:
            if self.xrd_position is None or not self._xrd_established:
                raise RuntimeError("First establish the XRD position by alignment or capture.")
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
                else "not established (image alignment uses current Ch3)"
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
        if self._stage_task is not None and self._stage_task.isRunning():
            QMessageBox.warning(self, "Stage Move", "Wait for the stage move to finish before closing.")
            return False
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
