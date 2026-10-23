"""Operator-drawn, axis-aligned gasket outline on an unmarked reference."""
from __future__ import annotations

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget

from apps.interactive_camera.gasket_tracking import GasketEllipse, detect_gasket_hole


class _EllipseCanvas(QWidget):
    def __init__(self, frame: np.ndarray, ellipse: GasketEllipse | None, parent=None):
        super().__init__(parent)
        self._frame = frame
        rgb = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self._image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                             QImage.Format.Format_RGB888).copy()
        scale = min(1.0, 800 / frame.shape[1], 600 / frame.shape[0])
        self._scale = scale
        self.setFixedSize(round(frame.shape[1] * scale), round(frame.shape[0] * scale))
        self.ellipse = ellipse
        self.detected: GasketEllipse | None = None
        self._start = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawImage(self.rect(), self._image)
        if self.ellipse is not None:
            painter.setPen(QPen(QColor("#00ffff"), 2))
            e = self.ellipse
            painter.drawEllipse(QRectF((e.cx - e.rx) * self._scale,
                                       (e.cy - e.ry) * self._scale,
                                       2 * e.rx * self._scale, 2 * e.ry * self._scale))
        if self.detected is not None:
            painter.setPen(QPen(QColor("#00ff55"), 2))
            e = self.detected
            painter.drawEllipse(QRectF((e.cx - e.rx) * self._scale,
                                       (e.cy - e.ry) * self._scale,
                                       2 * e.rx * self._scale, 2 * e.ry * self._scale))
        painter.end()

    def _image_pos(self, event):
        p = event.position()
        return (min(max(p.x() / self._scale, 0), self._frame.shape[1] - 1),
                min(max(p.y() / self._scale, 0), self._frame.shape[0] - 1))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = self._image_pos(event)
            self.detected = None

    def mouseMoveEvent(self, event):
        if self._start is not None:
            self._update_drag(self._image_pos(event))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._start is not None:
            self._update_drag(self._image_pos(event))
            self._start = None

    def _update_drag(self, end):
        x0, y0 = self._start
        x1, y1 = end
        if abs(x1 - x0) >= 40 and abs(y1 - y0) >= 40:
            self.ellipse = GasketEllipse((x0 + x1) / 2, (y0 + y1) / 2,
                                         abs(x1 - x0) / 2, abs(y1 - y0) / 2)
            self.update()


class GasketMarkDialog(QDialog):
    def __init__(self, frame: np.ndarray, ellipse: GasketEllipse | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Mark gasket outline (Beta)")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Drag a box around the outer rim of the gasket hole."))
        self.canvas = _EllipseCanvas(frame, ellipse)
        layout.addWidget(self.canvas)
        self.status = QLabel("Cyan: marked outline. Green: detected outline.")
        layout.addWidget(self.status)
        test_button = QPushButton("Test detection on reference")
        test_button.clicked.connect(lambda: self._test(frame))
        layout.addWidget(test_button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _test(self, frame):
        if self.canvas.ellipse is None:
            self.status.setText("Draw the gasket outline first.")
            return
        try:
            result = detect_gasket_hole(frame, frame, self.canvas.ellipse)
        except ValueError as exc:
            self.canvas.detected = None
            self.status.setText(f"Detection failed: {exc}")
        else:
            self.canvas.detected = result.ellipse
            self.status.setText(
                f"Detected: score {result.score:.2f}, "
                f"rim coverage {result.coverage}/8, residual {result.residual_px:.1f} px"
            )
        self.canvas.update()

    def _accept_if_valid(self):
        if self.canvas.ellipse is None:
            self.status.setText("Draw the gasket outline first.")
            return
        self._test(self.canvas._frame)
        if self.canvas.detected is not None:
            self.accept()
