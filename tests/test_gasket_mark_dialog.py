"""Hardware-free interaction checks for the gasket mark editor."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.ui.gasket_mark_dialog import GasketMarkDialog
from apps.exp_scheduler.ui.sample_positions_dialog import SamplePositionsDialog
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

    def test_drag_inside_moves_outline_without_moving_sample_centre(self):
        canvas = self.dialog.canvas
        canvas.sample_point = {"x": 330.0, "y": 250.0}
        canvas.detected = GasketEllipse(320, 240, 150, 120)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(350, 260))
        QTest.mouseMove(canvas, QPoint(390, 290))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(390, 290))
        self.assertEqual(canvas.ellipse, GasketEllipse(360, 270, 150, 120))
        self.assertEqual(canvas.sample_point, {"x": 330.0, "y": 250.0})
        self.assertIsNone(canvas.detected)
        self.assertEqual(self.dialog.horizontal_radius.value(), 150)
        self.assertEqual(self.dialog.vertical_radius.value(), 120)

    def test_drag_inside_stops_at_image_edge(self):
        canvas = self.dialog.canvas
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(320, 240))
        QTest.mouseMove(canvas, QPoint(639, 479))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(639, 479))
        self.assertEqual(canvas.ellipse, GasketEllipse(489, 359, 150, 120))

    def test_drag_inside_uses_image_pixels_when_preview_is_scaled(self):
        frame = np.full((960, 1280, 3), 100, dtype=np.uint8)
        dialog = GasketMarkDialog(frame, GasketEllipse(640, 480, 200, 150))
        try:
            canvas = dialog.canvas
            self.assertEqual(canvas._scale, 0.625)
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 300))
            QTest.mouseMove(canvas, QPoint(440, 320))
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(440, 320))
            self.assertEqual(canvas.ellipse, GasketEllipse(704, 512, 200, 150))
        finally:
            dialog.close()

    def test_sample_centre_is_placed_without_changing_the_outline(self):
        canvas = self.dialog.canvas
        self.dialog.sample_button.setChecked(True)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(350, 260))
        self.assertEqual(self.dialog.sample_point, {"x": 350.0, "y": 260.0})
        self.assertEqual(canvas.ellipse, GasketEllipse(320, 240, 150, 120))
        self.dialog.sample_button.setChecked(False)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        self.assertEqual(self.dialog.sample_point, {"x": 350.0, "y": 260.0})

    def test_sample_choice_controls_centre_button_and_restores_manual_point(self):
        self.assertTrue(self.dialog.manual_radio.isChecked())
        self.assertTrue(self.dialog.sample_button.isVisible())
        self.dialog.sample_button.setChecked(True)
        QTest.mouseClick(self.dialog.canvas, Qt.MouseButton.LeftButton,
                         pos=QPoint(350, 260))

        self.dialog.centre_radio.setChecked(True)
        self.assertEqual(self.dialog.sample_mode, "gasket_centre")
        self.assertFalse(self.dialog.sample_button.isVisible())
        self.assertFalse(self.dialog.canvas.placing_sample)
        self.assertIsNone(self.dialog.sample_point)

        self.dialog.manual_radio.setChecked(True)
        self.assertEqual(self.dialog.sample_mode, "manual")
        self.assertTrue(self.dialog.sample_button.isVisible())
        self.assertEqual(self.dialog.sample_point, {"x": 350.0, "y": 260.0})

    def test_save_requires_a_sample_centre(self):
        with mock.patch("apps.exp_scheduler.ui.gasket_mark_dialog.detect_gasket_hole") as detect:
            self.dialog._accept_if_valid()
        detect.assert_not_called()
        self.assertNotEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)

    def test_centre_choice_saves_detected_centre_without_manual_click(self):
        self.dialog.centre_radio.setChecked(True)
        result = SimpleNamespace(ellipse=GasketEllipse(322, 241, 150, 120),
                                 score=0.9, coverage=8, residual_px=1.0)
        with mock.patch("apps.exp_scheduler.ui.gasket_mark_dialog.detect_gasket_hole",
                        return_value=result):
            self.dialog._accept_if_valid()
        self.assertEqual(self.dialog.sample_point, {"x": 322, "y": 241})
        self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)

    def test_save_checks_the_adjusted_outline(self):
        self.dialog.canvas.sample_point = {"x": 320.0, "y": 240.0}
        self.dialog.vertical_radius.setValue(135)
        adjusted = GasketEllipse(320, 240, 150, 135)
        result = SimpleNamespace(ellipse=adjusted, score=0.9, coverage=8,
                                 residual_px=1.0)
        with mock.patch("apps.exp_scheduler.ui.gasket_mark_dialog.detect_gasket_hole",
                        return_value=result) as detect:
            self.dialog._accept_if_valid()
        self.assertEqual(detect.call_args.args[2], adjusted)
        self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)


class SamplePositionsMarkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mark_dialog_returns_selected_mode_and_point(self):
        frame = np.full((480, 640, 3), 100, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.png"
            self.assertTrue(cv2.imwrite(str(reference), frame))
            controller = SimpleNamespace(get_cached_states=lambda *args, **kwargs: {})
            dialog = SamplePositionsDialog(
                controller, frame, reference, (1, 2, 3), {},
                tracking_method="gasket_beta", gasket_reference_position=(1, 2, 3),
                borrowed_frame=lambda: None)
            try:
                mark = mock.Mock()
                mark.exec.return_value = GasketMarkDialog.DialogCode.Accepted
                mark.canvas.ellipse = GasketEllipse(320, 240, 150, 120)
                mark.sample_mode = "gasket_centre"
                mark.sample_point = {"x": 321.0, "y": 241.0}
                with mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.GasketMarkDialog",
                                return_value=mark), mock.patch.object(
                                    dialog, "_establish_gasket_position", return_value=True):
                    dialog._mark_gasket()
                self.assertEqual(dialog.gasket_sample_mode, "gasket_centre")
                self.assertEqual(dialog.gasket_sample_point, {"x": 321.0, "y": 241.0})
                self.assertTrue(dialog.gasket_changed)
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
