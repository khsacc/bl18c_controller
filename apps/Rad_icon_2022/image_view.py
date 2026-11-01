"""
Shared Rad-icon 2022 image display pieces.

Used by RadiconWindow (radicon_ui.py), the single-crystal app and the
Experimental Scheduler's run monitor, so every place that shows a detector
frame scales and windows it the same way (Min/Max levels → 8-bit grey).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
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


def tint_excluded_angles(image: QtGui.QImage, mask: np.ndarray | None) -> QtGui.QImage:
    """Overlay translucent purple on detector pixels excluded from 1D reduction."""
    if mask is None or not np.any(mask):
        return image
    if mask.shape != (image.height(), image.width()):
        raise ValueError("2θ mask does not match detector image")
    pixels = image.constBits()
    pixels.setsize(image.sizeInBytes())
    grey = np.frombuffer(pixels, dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()]
    rgb = np.repeat(grey[:, :, None], 3, axis=2).copy()
    purple = np.array([153, 102, 204], dtype=np.float32)
    rgb[mask] = (0.6 * rgb[mask] + 0.4 * purple).astype(np.uint8)
    rgb = np.ascontiguousarray(rgb)
    return QtGui.QImage(rgb.data, image.width(), image.height(), 3 * image.width(),
                        QtGui.QImage.Format.Format_RGB888).copy()


class ImageLevelsControl(QtWidgets.QWidget):
    """Shared 16-bit Min/Max slider, spin-box and Auto controls."""

    levels_changed = QtCore.pyqtSignal()
    auto_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #1e1e1e; color: #ccc; font-size: 11px;")
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setSpacing(4)
        for row, (name, initial) in enumerate((("min", 0), ("max", 65535))):
            grid.addWidget(QtWidgets.QLabel(name.title()), row, 0)
            slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
            slider.wheelEvent = lambda event: event.ignore()
            slider.setRange(0, 65535)
            slider.setValue(initial)
            spin = QtWidgets.QSpinBox()
            spin.wheelEvent = lambda event: event.ignore()
            spin.setRange(0, 65535)
            spin.setValue(initial)
            spin.setFixedWidth(68)
            spin.setKeyboardTracking(False)
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            slider.valueChanged.connect(self.levels_changed)
            setattr(self, name + "_slider", slider)
            setattr(self, name + "_spin", spin)
            grid.addWidget(slider, row, 1)
            grid.addWidget(spin, row, 2)
        button = QtWidgets.QPushButton("Auto")
        button.clicked.connect(self.auto_requested)
        grid.addWidget(button, 0, 3, 2, 1)


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
