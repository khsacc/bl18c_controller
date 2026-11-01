"""Operator-drawn, axis-aligned gasket outline and sample centre on an unmarked reference.

The sample centre is stored in reference-image pixels. Its offset from the
detected rim centre is what the scheduler moves onto the X-ray beam.
"""
from __future__ import annotations

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtCore import QLineF, QPointF
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QHBoxLayout, QLabel, QPushButton, QRadioButton,
                             QVBoxLayout, QWidget)

from apps.interactive_camera.gasket_tracking import GasketEllipse, detect_gasket_hole


class _EllipseCanvas(QWidget):
    ellipse_changed = pyqtSignal()
    sample_changed = pyqtSignal()

    def __init__(self, frame: np.ndarray, ellipse: GasketEllipse | None,
                 sample_point: dict | None = None, parent=None):
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
        self.sample_point = dict(sample_point) if sample_point else None
        self.placing_sample = False
        self._start = None
        self._active_handle = None
        self._move_start = None
        self._move_ellipse = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_ellipse(self, ellipse: GasketEllipse):
        if ellipse == self.ellipse:
            return
        self.ellipse = ellipse
        self.detected = None
        self.update()
        self.ellipse_changed.emit()

    def _handles(self):
        if self.ellipse is None:
            return {}
        e = self.ellipse
        s = self._scale
        return {"left": ((e.cx - e.rx) * s, e.cy * s),
                "right": ((e.cx + e.rx) * s, e.cy * s),
                "top": (e.cx * s, (e.cy - e.ry) * s),
                "bottom": (e.cx * s, (e.cy + e.ry) * s)}

    def _handle_at(self, pos):
        candidates = [((pos.x() - x) ** 2 + (pos.y() - y) ** 2, name)
                      for name, (x, y) in self._handles().items()
                      if abs(pos.x() - x) <= 9 and abs(pos.y() - y) <= 9]
        return min(candidates)[1] if candidates else None

    def _inside_ellipse(self, pos):
        e = self.ellipse
        if e is None:
            return False
        x, y = pos
        return ((x - e.cx) / e.rx) ** 2 + ((y - e.cy) / e.ry) ** 2 <= 1

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawImage(self.rect(), self._image)
        if self.ellipse is not None:
            painter.setPen(QPen(QColor("#00ffff"), 2))
            e = self.ellipse
            painter.drawEllipse(QRectF((e.cx - e.rx) * self._scale,
                                       (e.cy - e.ry) * self._scale,
                                       2 * e.rx * self._scale, 2 * e.ry * self._scale))
            painter.setBrush(QColor("#00ffff"))
            for x, y in self._handles().values():
                painter.drawRect(QRectF(x - 4, y - 4, 8, 8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.detected is not None:
            painter.setPen(QPen(QColor("#00ff55"), 2))
            e = self.detected
            painter.drawEllipse(QRectF((e.cx - e.rx) * self._scale,
                                       (e.cy - e.ry) * self._scale,
                                       2 * e.rx * self._scale, 2 * e.ry * self._scale))
        if self.sample_point is not None:
            painter.setPen(QPen(QColor("#ff00ff"), 2))
            x, y = self.sample_point["x"] * self._scale, self.sample_point["y"] * self._scale
            painter.drawLine(QLineF(x - 10, y, x + 10, y))
            painter.drawLine(QLineF(x, y - 10, x, y + 10))
            painter.drawEllipse(QPointF(x, y), 5, 5)
        painter.end()

    def _image_pos(self, event):
        p = event.position()
        return (min(max(p.x() / self._scale, 0), self._frame.shape[1] - 1),
                min(max(p.y() / self._scale, 0), self._frame.shape[0] - 1))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.placing_sample:
            x, y = self._image_pos(event)
            self.sample_point = {"x": float(x), "y": float(y)}
            self.update()
            self.sample_changed.emit()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._active_handle = self._handle_at(event.position())
            pos = self._image_pos(event)
            self._move_start = None
            self._move_ellipse = None
            self._start = None
            if self._active_handle is not None:
                return
            if self._inside_ellipse(pos):
                self._move_start = pos
                self._move_ellipse = self.ellipse
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self._start = pos

    def mouseMoveEvent(self, event):
        if self.placing_sample:
            return
        if self._active_handle is not None:
            self._resize(self._image_pos(event))
        elif self._move_start is not None:
            self._move(self._image_pos(event))
        elif self._start is not None:
            self._update_drag(self._image_pos(event))
        else:
            handle = self._handle_at(event.position())
            if handle in ("left", "right"):
                cursor = Qt.CursorShape.SizeHorCursor
            elif handle:
                cursor = Qt.CursorShape.SizeVerCursor
            elif self._inside_ellipse(self._image_pos(event)):
                cursor = Qt.CursorShape.SizeAllCursor
            else:
                cursor = Qt.CursorShape.CrossCursor
            self.setCursor(cursor)

    def mouseReleaseEvent(self, event):
        if self.placing_sample:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._active_handle is not None:
                self._resize(self._image_pos(event))
                self._active_handle = None
            elif self._move_start is not None:
                self._move(self._image_pos(event))
                self._move_start = None
                self._move_ellipse = None
            elif self._start is not None:
                self._update_drag(self._image_pos(event))
                self._start = None

    def _move(self, pos):
        e = self._move_ellipse
        x0, y0 = self._move_start
        if e is None or pos == (x0, y0):
            return
        width, height = self._frame.shape[1], self._frame.shape[0]
        cx = min(max(e.cx + pos[0] - x0, e.rx), width - 1 - e.rx)
        cy = min(max(e.cy + pos[1] - y0, e.ry), height - 1 - e.ry)
        self.set_ellipse(GasketEllipse(cx, cy, e.rx, e.ry))

    def _resize(self, pos):
        e = self.ellipse
        if e is None:
            return
        if self._active_handle in ("left", "right"):
            radius = min(abs(pos[0] - e.cx), e.cx, self._frame.shape[1] - 1 - e.cx)
            self.set_ellipse(GasketEllipse(e.cx, e.cy, max(20.0, radius), e.ry))
        else:
            radius = min(abs(pos[1] - e.cy), e.cy, self._frame.shape[0] - 1 - e.cy)
            self.set_ellipse(GasketEllipse(e.cx, e.cy, e.rx, max(20.0, radius)))

    def _update_drag(self, end):
        x0, y0 = self._start
        x1, y1 = end
        if abs(x1 - x0) >= 40 and abs(y1 - y0) >= 40:
            self.set_ellipse(GasketEllipse((x0 + x1) / 2, (y0 + y1) / 2,
                                           abs(x1 - x0) / 2, abs(y1 - y0) / 2))


class GasketMarkDialog(QDialog):
    def __init__(self, frame: np.ndarray, ellipse: GasketEllipse | None = None, parent=None,
                 sample_point: dict | None = None, sample_mode: str = "manual"):
        super().__init__(parent)
        self.sample_mode = sample_mode
        self.setWindowTitle("Mark gasket outline and sample (Beta)")
        layout = QVBoxLayout(self)
        guidance = QLabel(
            "Drag to draw the outer rim. Drag inside the ellipse to move it; "
            "drag cyan handles to resize, or drag outside to redraw.")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        self.canvas = _EllipseCanvas(frame, ellipse,
                                     sample_point if sample_mode == "manual" else None)
        layout.addWidget(self.canvas)
        self._manual_sample_point = (dict(sample_point) if sample_mode == "manual"
                                     and sample_point is not None else None)
        layout.addWidget(QLabel("Choose the sample position:"))
        self.centre_radio = QRadioButton("Use the detected gasket centre")
        self.manual_radio = QRadioButton(
            "Choose the sample position manually on the reference photo")
        layout.addWidget(self.centre_radio)
        layout.addWidget(self.manual_radio)
        (self.centre_radio if sample_mode == "gasket_centre"
         else self.manual_radio).setChecked(True)
        self.centre_radio.toggled.connect(self._on_centre_toggled)
        self.manual_radio.toggled.connect(self._on_manual_toggled)
        self.sample_button = QPushButton("Place sample centre")
        self.sample_button.setCheckable(True)
        self.sample_button.toggled.connect(self._on_sample_toggled)
        self.sample_button.setVisible(sample_mode == "manual")
        layout.addWidget(self.sample_button)
        self.sample_guidance = QLabel()
        self.sample_guidance.setWordWrap(True)
        layout.addWidget(self.sample_guidance)
        self._update_sample_guidance()
        radius_row = QHBoxLayout()
        self.horizontal_radius = QDoubleSpinBox()
        self.vertical_radius = QDoubleSpinBox()
        for label, spin in (("Horizontal radius (px):", self.horizontal_radius),
                            ("Vertical radius (px):", self.vertical_radius)):
            spin.setDecimals(1)
            spin.setMinimum(20.0)
            spin.setSingleStep(1.0)
            spin.wheelEvent = lambda event: event.ignore()
            radius_row.addWidget(QLabel(label))
            radius_row.addWidget(spin)
        layout.addLayout(radius_row)
        self.status = QLabel("Cyan: marked outline. Green: detected outline. Magenta: sample centre.")
        layout.addWidget(self.status)
        self.canvas.ellipse_changed.connect(self._on_ellipse_changed)
        self.canvas.sample_changed.connect(self._on_sample_changed)
        self._sync_radii()
        self.horizontal_radius.valueChanged.connect(lambda value: self._set_radius("x", value))
        self.vertical_radius.valueChanged.connect(lambda value: self._set_radius("y", value))
        test_button = QPushButton("Test detection on reference")
        test_button.clicked.connect(lambda: self._test(frame))
        layout.addWidget(test_button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_radii(self):
        e = self.canvas.ellipse
        for spin, value, extent, centre in (
            (self.horizontal_radius, e.rx if e else 20.0, self.canvas._frame.shape[1], e.cx if e else 0),
            (self.vertical_radius, e.ry if e else 20.0, self.canvas._frame.shape[0], e.cy if e else 0),
        ):
            previous = spin.blockSignals(True)
            spin.setEnabled(e is not None)
            spin.setMaximum(max(20.0, value, min(centre, extent - 1 - centre)))
            spin.setValue(value)
            spin.blockSignals(previous)

    def _on_sample_toggled(self, checked: bool):
        self.canvas.placing_sample = checked
        self.canvas.setCursor(Qt.CursorShape.PointingHandCursor if checked
                              else Qt.CursorShape.CrossCursor)

    def _on_centre_toggled(self, checked: bool):
        if not checked:
            return
        self._manual_sample_point = self.canvas.sample_point
        self.canvas.sample_point = None
        self.canvas.update()
        self.sample_button.setChecked(False)
        self.sample_button.hide()
        self.sample_mode = "gasket_centre"
        self._update_sample_guidance()

    def _on_manual_toggled(self, checked: bool):
        if not checked:
            return
        self.canvas.sample_point = self._manual_sample_point
        self.canvas.update()
        self.sample_button.show()
        self.sample_mode = "manual"
        self._update_sample_guidance()

    def _on_sample_changed(self):
        self._manual_sample_point = self.canvas.sample_point
        self.status.setText(
            "Sample centre at ({x:.0f}, {y:.0f}) px.".format(**self.canvas.sample_point))

    def _update_sample_guidance(self):
        self.sample_guidance.setText(
            "The detected gasket centre will be used as the sample position."
            if self.sample_mode == "gasket_centre" else
            "Press Place sample centre, then click the sample on the photo. "
            "Alignment moves this point onto the X-ray beam registered in Interactive Camera.")

    @property
    def sample_point(self) -> dict | None:
        return self.canvas.sample_point

    def _on_ellipse_changed(self):
        self._sync_radii()
        self.status.setText("Outline changed. Test detection again before saving.")

    def _set_radius(self, axis: str, value: float):
        e = self.canvas.ellipse
        if e is not None:
            self.canvas.set_ellipse(GasketEllipse(
                e.cx, e.cy, value if axis == "x" else e.rx,
                value if axis == "y" else e.ry,
            ))

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
        if self.sample_mode == "manual" and self.canvas.sample_point is None:
            self.status.setText("Place the sample centre on the reference photo first.")
            return
        self._test(self.canvas._frame)
        if self.canvas.detected is not None:
            if self.sample_mode == "gasket_centre":
                self.canvas.sample_point = {
                    "x": self.canvas.detected.cx, "y": self.canvas.detected.cy,
                }
            self.accept()
