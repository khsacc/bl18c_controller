"""Hardware-free interaction checks for the gasket mark editor."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.ui.gasket_mark_dialog import GasketMarkDialog
from apps.interactive_camera.gasket_tracking import GasketEllipse


class GasketMarkDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        frame = np.full((480, 640, 3), 100, dtype=np.uint8)
        self.dialog = GasketMarkDialog(frame, GasketEllipse(320, 240, 150, 120))
        self.dialog.show()

    def tearDown(self):
        self.dialog.close()

    def test_handles_resize_one_axis_and_invalidate_detection(self):
        canvas = self.dialog.canvas
        canvas.detected = GasketEllipse(320, 240, 150, 120)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(470, 240))
        QTest.mouseMove(canvas, QPoint(500, 240))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(500, 240))
        self.assertEqual(canvas.ellipse, GasketEllipse(320, 240, 180, 120))
        self.assertIsNone(canvas.detected)
        self.assertEqual(self.dialog.horizontal_radius.value(), 180)

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(320, 120))
        QTest.mouseMove(canvas, QPoint(320, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(320, 80))
        self.assertEqual(canvas.ellipse, GasketEllipse(320, 240, 180, 160))
        self.assertEqual(self.dialog.vertical_radius.value(), 160)

    def test_numeric_radius_and_redraw(self):
        self.dialog.horizontal_radius.setValue(95)
        self.assertEqual(self.dialog.canvas.ellipse, GasketEllipse(320, 240, 95, 120))
        canvas = self.dialog.canvas
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        QTest.mouseMove(canvas, QPoint(300, 280))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(300, 280))
        self.assertEqual(canvas.ellipse, GasketEllipse(200, 190, 100, 90))
        self.assertEqual(self.dialog.horizontal_radius.value(), 100)
        self.assertEqual(self.dialog.vertical_radius.value(), 90)

    def test_save_checks_the_adjusted_outline(self):
        self.dialog.vertical_radius.setValue(135)
        adjusted = GasketEllipse(320, 240, 150, 135)
        result = SimpleNamespace(ellipse=adjusted, score=0.9, coverage=8,
                                 residual_px=1.0)
        with mock.patch("apps.exp_scheduler.ui.gasket_mark_dialog.detect_gasket_hole",
                        return_value=result) as detect:
            self.dialog._accept_if_valid()
        self.assertEqual(detect.call_args.args[2], adjusted)
        self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
