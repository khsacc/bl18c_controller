"""One USB microscope capture, shared by the launcher and its sub-applications.

Only the capture thread touches VideoCapture. Consumers receive copies of raw
frames and may wait for a frame whose read began after a stage movement.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time

import cv2
import numpy as np


_log = logging.getLogger(__name__)


class CameraSession:
    def __init__(self, index: int = 0) -> None:
        self.index = int(index)
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame: np.ndarray | None = None
        self._frame_at = 0.0
        self._error: str | None = None
        self._size = (0, 0)
        self._fps = 30.0

    def start(self) -> None:
        """Start capture asynchronously; missing cameras are retried in place."""
        with self._condition:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._capture, name="MicroscopeCamera", daemon=True,
            )
            self._thread.start()

    @property
    def frame_size(self) -> tuple[int, int]:
        with self._condition:
            return self._size

    @property
    def fps(self) -> float:
        with self._condition:
            return self._fps

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    def latest(self, max_age_s: float = 2.0) -> tuple[np.ndarray, float] | None:
        with self._condition:
            if self._frame is None or time.monotonic() - self._frame_at > max_age_s:
                return None
            return self._frame.copy(), self._frame_at

    def frame_after(self, after: float, timeout_s: float = 5.0,
                    should_stop=None, require_nonblack: bool = False) -> np.ndarray:
        """Wait for an unmarked frame read after ``after`` (monotonic time)."""
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                if should_stop is not None and should_stop():
                    raise InterruptedError("Camera frame wait stopped")
                if self._frame is not None and self._frame_at > after:
                    frame = self._frame.copy()
                    if frame.size and (not require_nonblack or int(frame.max()) > 0):
                        return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(self._error or "No fresh camera frame available")
                self._condition.wait(min(remaining, 0.1))

    def stop(self, timeout_s: float = 2.0) -> None:
        """Wait for the capture thread to release the camera itself.

        A blocked driver read must never be raced by release() on this thread.
        """
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout_s)
        if thread.is_alive():
            raise RuntimeError("Camera capture thread did not stop; camera may still be held")
        with self._condition:
            self._thread = None
            self._frame = None
            self._frame_at = 0.0

    def _open(self):
        if sys.platform == "win32":
            cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            if cap.isOpened():
                return cap
            cap.release()
        cap = cv2.VideoCapture(self.index)
        if cap.isOpened():
            return cap
        cap.release()
        return None

    def _capture(self) -> None:
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._open()
                if cap is None:
                    with self._condition:
                        self._error = f"Cannot open camera index {self.index}"
                        self._condition.notify_all()
                    self._stop.wait(2.0)
                    continue
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                fps = cap.get(cv2.CAP_PROP_FPS)
                width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
                height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                with self._condition:
                    self._fps = fps if math.isfinite(fps) and fps > 0 else 30.0
                    self._size = (int(width) if math.isfinite(width) and width > 0 else 0,
                                  int(height) if math.isfinite(height) and height > 0 else 0)
                failures = 0
                while not self._stop.is_set():
                    read_started_at = time.monotonic()
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        failures = 0
                        with self._condition:
                            self._frame = frame.copy()
                            self._frame_at = read_started_at
                            self._size = (frame.shape[1], frame.shape[0])
                            self._error = None
                            self._condition.notify_all()
                    else:
                        failures += 1
                        with self._condition:
                            self._error = "Camera read failed"
                            self._condition.notify_all()
                        if failures >= 20:
                            break
                        self._stop.wait(0.05)
            except Exception as exc:
                _log.exception("Microscope camera capture failed")
                with self._condition:
                    self._error = str(exc)
                    self._condition.notify_all()
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        _log.exception("Could not release microscope camera")
                with self._condition:
                    self._frame = None
                    self._frame_at = 0.0
                    self._condition.notify_all()
            if not self._stop.is_set():
                self._stop.wait(1.0)
