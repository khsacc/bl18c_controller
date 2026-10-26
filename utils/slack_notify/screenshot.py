"""GUI-thread capture bridge for the scheduler's Slack screenshot command."""
from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

_log = logging.getLogger("bl18c.slack")
_MAX_PIXELS = 8_000_000


class SchedulerScreenshotBridge(QObject):
    capture_requested = pyqtSignal(str)

    def __init__(self, window_provider: Callable[[], QWidget | None],
                 deliver: Callable[[str, QImage | None, str], None],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window_provider = window_provider
        self._deliver = deliver
        self.capture_requested.connect(self._capture, Qt.ConnectionType.QueuedConnection)

    def request(self, channel_id: str) -> None:
        """Safe to call from the Socket Mode thread; never touch a widget here."""
        self.capture_requested.emit(channel_id)

    @pyqtSlot(str)
    def _capture(self, channel_id: str) -> None:
        image = None
        error = ""
        try:
            window = self._window_provider()
            if window is None or not window.isVisible() or window.isMinimized():
                error = "The Experimental Scheduler window is not open and visible."
            elif window.width() * window.height() > _MAX_PIXELS:
                error = "The Experimental Scheduler window is too large to capture."
            else:
                image = window.grab().toImage()
                if image.isNull():
                    image = None
                    error = "The Experimental Scheduler window could not be captured."
        except Exception:
            _log.exception("Scheduler screenshot capture failed")
            error = "The Experimental Scheduler window could not be captured."
        try:
            self._deliver(channel_id, image, error)
        except Exception:
            # A screenshot must never propagate into the GUI event loop or run.
            _log.exception("Scheduler screenshot delivery failed")
