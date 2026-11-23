"""Shared, asynchronous manual sample moves for camera-based windows.

Only this worker owns the lease and performs hardware I/O. Widgets receive
queued Qt signals; rejected acquisitions never stop another owner's motion.
"""
from __future__ import annotations

import threading

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from apps.interactive_camera.xray_beam import check_xray_beam, read_microscope_pulses
from utils.stage.errors import MotionRevokedError


def pixel_move(matrix_inv, source, target, frame_shape) -> dict[int, int]:
    """Convert image displacement to whole pulses, as in Interactive Camera."""
    matrix = np.asarray(matrix_inv, dtype=float)
    if (matrix.shape != (2, 2) or not np.isfinite(matrix).all()
            or np.linalg.matrix_rank(matrix) != 2):
        raise ValueError("Camera calibration matrix is invalid.")
    height, width = frame_shape[:2]
    for point in (source, target):
        x, y = point
        if not (np.isfinite(x) and np.isfinite(y) and 0 <= x < width and 0 <= y < height):
            raise ValueError("The selected position is outside the camera image.")
    displacement = matrix @ (np.asarray(target) - np.asarray(source))
    if not np.isfinite(displacement).all():
        raise ValueError("The calibrated movement is not finite.")
    return {4: int(displacement[0]), 5: int(displacement[1])}


class ManualStageMoves(QObject):
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    _completed = pyqtSignal(str, str)

    def __init__(self, controller, owner, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.owner = owner
        self.thread = None
        self.busy = False
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._lease = None
        self._stop_future = None
        self._completed.connect(self._finish)

    def relative(self, ch, pulses, *, speed=None):
        if ch not in (3, 4, 5) or not np.isfinite(pulses) or int(pulses) != pulses:
            raise ValueError("Relative sample moves require Ch3/4/5 and whole pulses.")
        return self._start(lambda: {ch: int(pulses)}, speed, f"Move Ch{ch}")

    def to_pixel(self, source, target, matrix_inv, frame_shape, *, speed="M", beam=None):
        # Snapshot calibration and coordinates on the GUI thread. Microscope
        # readback and beam validation happen on the worker under its lease.
        moves = pixel_move(matrix_inv, source, target, frame_shape)

        def prepare():
            if beam is not None:
                check_xray_beam(beam, frame_shape, read_microscope_pulses(self.controller))
            return moves

        return self._start(prepare, speed, "Click-to-move")

    def _start(self, prepare, speed, operation):
        if self.busy:
            return False
        if self.controller is None:
            raise RuntimeError("Stage controller is not connected.")
        if speed is not None and speed not in ("H", "M", "L"):
            raise ValueError("Invalid stage speed.")
        self._cancel.clear()
        self._stop_future = None
        self.busy = True
        self.busy_changed.emit(True)
        self.status.emit(f"{operation} in progress…")
        self.thread = threading.Thread(
            target=self._run, args=(prepare, speed, operation), daemon=True)
        try:
            self.thread.start()
        except Exception:
            self.busy = False
            self.busy_changed.emit(False)
            raise
        return True

    def stop(self):
        """Cancel pending work; stop only a lease still held by this worker."""
        self._cancel.set()
        with self._lock:
            if (self._lease is not None and self._stop_future is None
                    and self.controller.coordinator.is_valid(self._lease)):
                self._stop_future = self.controller.request_normal_stop(source=self.owner)
        self.status.emit("Stopping stage…")

    def _run(self, prepare, speed, operation):
        failure = ""
        message = "Movement complete. Ready."
        try:
            with self.controller.motion_session(owner=self.owner, operation=operation) as motion:
                attempted = False
                try:
                    with self._lock:
                        self._lease = motion
                    if self._cancel.is_set():
                        return
                    if self.controller.get_is_moving():
                        raise RuntimeError("Wait for the stage to stop before moving the sample.")
                    moves = prepare()
                    for ch, pulses in moves.items():
                        if self._cancel.is_set():
                            break
                        if pulses:
                            if speed is not None:
                                self.controller.set_ch_speed(ch, speed, motion=motion)
                            # A communication failure may occur after the command
                            # reaches hardware, so even a failed attempt needs stop.
                            attempted = True
                            self.controller.move_ch_relative(ch, pulses, motion=motion)
                    if attempted:
                        self.controller.wait_until_stop(
                            stay_in_rem=True, should_stop=self._cancel.is_set)
                except Exception as exc:
                    if not (self._cancel.is_set() and isinstance(exc, MotionRevokedError)):
                        failure = str(exc)
                finally:
                    # Prevent a late Stop from revoking a subsequent owner's lease.
                    with self._lock:
                        self._lease = None
                        stop_future = self._stop_future
                    try:
                        if (failure and attempted and stop_future is None
                                and self.controller.coordinator.is_valid(motion)):
                            stop_future = self.controller.request_normal_stop(source=self.owner)
                        if stop_future is not None:
                            stop_future.result(timeout=40)
                        if self.controller.coordinator.is_valid(motion):
                            self.controller.switch_to_loc(motion=motion)
                        else:
                            message = "Movement stopped."
                    except Exception as exc:
                        failure = f"{failure + '; ' if failure else ''}Stop/LOCAL confirmation failed: {exc}"
        except Exception as exc:
            failure = str(exc)
        finally:
            if self._cancel.is_set():
                message = "Movement stopped."
            self._completed.emit(message, failure)

    @pyqtSlot(str, str)
    def _finish(self, message, failure):
        self.busy = False
        self.busy_changed.emit(False)
        self.status.emit(f"Error: {failure}" if failure else message)
        if failure:
            self.error.emit(failure)
