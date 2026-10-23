"""
Shared Rad-icon 2022 image display pieces.

Used by RadiconWindow (radicon_ui.py), the single-crystal app and the
Experimental Scheduler's run monitor, so every place that shows a detector
frame scales and windows it the same way (Min/Max levels → 8-bit grey).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6 import QtGui, QtWidgets
from PyQt6.QtCore import Qt

try:
    from settings.i18n import tr
except ImportError:
    import sys as _sys
    _pkg = str(Path(__file__).parent.parent.parent)
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.i18n import tr


def levels_to_qimage(img: np.ndarray, lo: int, hi: int) -> QtGui.QImage:
    """Window a 2-D frame to [lo, hi] and return an 8-bit greyscale QImage."""
    if hi <= lo:
        hi = lo + 1
    arr8 = ((img.astype(np.float32) - lo) / (hi - lo) * 255)
    arr8 = np.ascontiguousarray(arr8.clip(0, 255).astype(np.uint8))
    h, w = arr8.shape
    return QtGui.QImage(arr8.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8).copy()


class ImageLabel(QtWidgets.QLabel):
    """Grayscale image display that scales to fill available space while preserving aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QtGui.QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.setText(tr("(No image)"))
        self.setStyleSheet("color: #666; background: #111; font-size: 13px;")

    def setSourcePixmap(self, px: QtGui.QPixmap):
        self._src = px
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._src is None:
            return
        scaled = self._src.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        painter = QtGui.QPainter(scaled)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1))
        painter.drawRect(0, 0, scaled.width() - 1, scaled.height() - 1)
        painter.end()
        super().setPixmap(scaled)
