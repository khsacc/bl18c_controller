"""GUI-thread capture bridge for the scheduler's Slack screenshot command."""
from __future__ import annotations

import logging
import math
from collections.abc import Callable

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

_log = logging.getLogger("bl18c.slack")
_MAX_PIXELS = 8_000_000
_PANEL_GAP = 12


class SchedulerScreenshotBridge(QObject):
    capture_requested = pyqtSignal(str, str)

    def __init__(self, window_provider: Callable[[], QWidget | None],
                 deliver: Callable[[str, tuple[QImage, ...] | None, str, str], None],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window_provider = window_provider
        self._deliver = deliver
        self.capture_requested.connect(self._capture, Qt.ConnectionType.QueuedConnection)

    def request(self, channel_id: str, target: str = "window") -> None:
        """Safe to call from the Socket Mode thread; never touch a widget here."""
        self.capture_requested.emit(channel_id, target)

    @pyqtSlot(str, str)
    def _capture(self, channel_id: str, target: str) -> None:
        images = None
        error = ""
        try:
            window = self._window_provider()
            if window is None or not window.isVisible() or window.isMinimized():
                error = "The Experimental Scheduler window is not open and visible."
            else:
                widgets = (window,) if target == "window" else window.slack_screenshot_widgets(target)
                widgets = tuple(widget for widget in widgets if widget.isVisible())
                if not widgets:
                    error = f"The {target} panel is not currently visible."
                else:
                    sizes = [
                        (math.ceil(widget.width() * widget.devicePixelRatioF()),
                         math.ceil(widget.height() * widget.devicePixelRatioF()))
                        for widget in widgets
                    ]
                    combined_pixels = max(width for width, _ in sizes) * (
                        sum(height for _, height in sizes) + _PANEL_GAP * (len(sizes) - 1)
                    )
                    if any(width <= 0 or height <= 0 for width, height in sizes) or combined_pixels > _MAX_PIXELS:
                        error = f"The {target} panel is too large to capture."
                    else:
                        captured = tuple(widget.grab().toImage() for widget in widgets)
                        actual_pixels = max(image.width() for image in captured) * (
                            sum(image.height() for image in captured)
                            + _PANEL_GAP * (len(captured) - 1)
                        )
                        if any(image.isNull() for image in captured) or actual_pixels > _MAX_PIXELS:
                            error = f"The {target} panel could not be captured."
                        else:
                            images = captured
        except Exception:
            _log.exception("Scheduler screenshot capture failed")
            error = "The Experimental Scheduler window could not be captured."
        try:
            self._deliver(channel_id, images, error, target)
        except Exception:
            # A screenshot must never propagate into the GUI event loop or run.
            _log.exception("Scheduler screenshot delivery failed")
