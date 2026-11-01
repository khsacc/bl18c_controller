"""Hardware-free checks for the offline XRD reducer and its mask workflow."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tifffile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QScrollArea
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from pyFAI.detectors import Detector
from pyFAI.integrator.azimuthal import AzimuthalIntegrator

from apps.Rad_icon_2022.instant1d import find_spot_mask, reduce_frame
from apps.xrd_1d_reduction.masks import load_mask, save_mask, shape_mask
from apps.xrd_1d_reduction.reduction_app import Xrd1DReductionWindow
from settings.poni_state import PoniState


class MaskTests(unittest.TestCase):
    def test_drag_selects_pixel_containing_endpoint(self):
        rectangle = shape_mask((8, 8), "rectangle", (2.9, 2.9), (4.1, 4.1))
        self.assertTrue(rectangle[2, 2])
        self.assertEqual(int(rectangle.sum()), 9)
        circle = shape_mask((8, 8), "circle", (2.9, 2.9), (4.1, 2.9))
        self.assertTrue(circle[2, 2])

    def test_ipanalyzer_known_mask_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "known.mas"
            path.write_bytes(bytes((8, 0, 0, 0, 0b10000101)))
            expected = np.array([[1, 0, 1, 0, 0, 0, 0, 1]], dtype=bool)
            np.testing.assert_array_equal(load_mask(path, (1, 8)), expected)
            save_mask(path, expected)
            self.assertEqual(path.read_bytes(), bytes((8, 0, 0, 0, 0b10000101)))

    def test_shapes_and_ipanalyzer_mask_roundtrip(self):
        rectangle = shape_mask((8, 8), "rectangle", (2, 1), (4, 3))
        self.assertEqual(int(rectangle.sum()), 9)
        circle = shape_mask((8, 8), "circle", (3, 3), (5, 3))
        self.assertEqual(int(circle.sum()), 13)
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".npz", ".mas"):
                path = Path(directory) / ("mask" + suffix)
                save_mask(path, rectangle)
                np.testing.assert_array_equal(load_mask(path, (8, 8)), rectangle)
                with self.assertRaises(ValueError):
                    load_mask(path, (4, 16)) if suffix == ".npz" else load_mask(path, (7, 8))

    def test_shared_reducer_passes_explicit_mask_without_changing_integrator_options(self):
        class Integrator:
            def center_array(self, shape, **_kwargs):
                return np.linspace(0, 1, np.prod(shape)).reshape(shape)

            def integrate1d(self, image, **kwargs):
                self.image, self.kwargs = image, kwargs
                return SimpleNamespace(radial=np.arange(kwargs["npt"]),
                                       intensity=np.ones(kwargs["npt"]))

        ai = Integrator()
        image = np.arange(16, dtype=np.uint16).reshape(4, 4)
        mask = np.zeros_like(image, dtype=bool)
        mask[1, 2] = True
        reduce_frame(ai, image, 0.01, mask=mask)
        np.testing.assert_array_equal(ai.kwargs["mask"], mask)
        self.assertEqual(ai.kwargs["method"], ("full", "histogram", "cython"))
        self.assertTrue(ai.kwargs["correctSolidAngle"])
        self.assertEqual(ai.kwargs["polarization_factor"], 0.95)

    def test_detector_mask_is_combined_with_manual_mask(self):
        class Integrator:
            detector = SimpleNamespace(mask=np.array([[True, False], [False, False]]))

            def center_array(self, shape, **_kwargs):
                return np.zeros(shape)

            def integrate1d(self, image, **kwargs):
                self.mask = kwargs["mask"]
                return None

        ai = Integrator()
        manual = np.array([[False, False], [False, True]])
        reduce_frame(ai, np.ones((2, 2)), 0.01, npt=10, mask=manual)
        np.testing.assert_array_equal(ai.mask, [[True, False], [False, True]])

    def test_spot_statistics_exclude_existing_manual_mask(self):
        class Integrator:
            dist = 0.2
            detector = SimpleNamespace(pixel2=1e-4, mask=None)

            def center_array(self, shape, **_kwargs):
                return np.zeros(shape)

        image = np.full((4, 4), 10.0)
        image[0, 0] = -1000.0
        image[3, 3] = 100.0
        manual = np.zeros_like(image, dtype=bool)
        manual[0, 0] = True
        without = find_spot_mask(Integrator(), image, 2.0)
        with_mask = find_spot_mask(Integrator(), image, 2.0, exclude_mask=manual)
        self.assertFalse(without[3, 3])
        self.assertTrue(with_mask[3, 3])
        self.assertFalse(with_mask[0, 0])


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_tiff_drop_loads_on_image_and_plot_and_rejects_other_urls(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            first = Path(directory) / "first.tif"
            second = Path(directory) / "second.TIFF"
            tifffile.imwrite(first, np.ones((8, 9), dtype=np.uint16))
            tifffile.imwrite(second, np.ones((10, 11), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=PoniState())

            def send_drag(target, urls):
                mime = QtCore.QMimeData()
                mime.setUrls(urls)
                enter = QtGui.QDragEnterEvent(
                    QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(target, enter)
                drop = QtGui.QDropEvent(
                    QtCore.QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(target, drop)
                return enter.isAccepted(), drop.isAccepted()

            try:
                window.show()
                self.app.processEvents()
                self.assertEqual(send_drag(window._canvas,
                                           [QtCore.QUrl.fromLocalFile(str(first))]),
                                 (True, True))
                self.assertEqual(window._image_path, first)
                self.assertEqual(window._image.shape, (8, 9))
                self.assertEqual(send_drag(window._plot.viewport(),
                                           [QtCore.QUrl.fromLocalFile(str(second))]),
                                 (True, True))
                self.assertEqual(window._image_path, second)
                self.assertEqual(window._image.shape, (10, 11))
                self.assertEqual(send_drag(window.findChild(QScrollArea).viewport(),
                                           [QtCore.QUrl.fromLocalFile(str(first))]),
                                 (True, True))
                self.assertEqual(window._image_path, first)
                self.assertEqual(send_drag(window,
                                           [QtCore.QUrl.fromLocalFile(str(second))]),
                                 (True, True))
                self.assertEqual(window._image_path, second)
                mime = QtCore.QMimeData()
                mime.setUrls([QtCore.QUrl.fromLocalFile(str(first))])
                actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
                move_enter = QtGui.QDragEnterEvent(
                    QPoint(5, 5), actions, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(window, move_enter)
                move_proposed = QtGui.QDropEvent(
                    QtCore.QPointF(5, 5), actions, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                move_proposed.setDropAction(Qt.DropAction.MoveAction)
                self.app.sendEvent(window, move_proposed)
                self.assertTrue(move_proposed.isAccepted())
                self.assertEqual(move_proposed.dropAction(), Qt.DropAction.CopyAction)
                self.assertTrue(first.exists())
                self.assertEqual(window._image_path, first)
                move_only = QtGui.QDragEnterEvent(
                    QPoint(5, 5), Qt.DropAction.MoveAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(window, move_only)
                self.assertFalse(move_only.isAccepted())
                for urls in (
                    [QtCore.QUrl.fromLocalFile(str(Path(directory) / "mask.mas"))],
                    [QtCore.QUrl("https://example.invalid/image.tif")],
                    [QtCore.QUrl.fromLocalFile(str(first)),
                     QtCore.QUrl.fromLocalFile(str(second))],
                ):
                    self.assertEqual(send_drag(window.centralWidget(), urls),
                                     (False, False))
                    self.assertEqual(window._image_path, first)
            finally:
                window.close()

    def test_image_mask_and_shared_reduction(self):
        state = PoniState()
        state.update(AzimuthalIntegrator(
            dist=0.2, poni1=0.003, poni2=0.003,
            detector=Detector(pixel1=1e-4, pixel2=1e-4), wavelength=1e-10))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.arange(64 * 65, dtype=np.uint16).reshape(64, 65))
            with patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
                window = Xrd1DReductionWindow(poni_state=state)
                try:
                    window.show()
                    window.load_image(path)
                    self._finish_work(window)
                    initial = window._profile
                    self.assertIsNotNone(initial)
                    expected = reduce_frame(state.integration_ai, window._image, 0.01)
                    np.testing.assert_allclose(initial.radial, expected.radial)
                    np.testing.assert_allclose(initial.intensity, expected.intensity)
                    window._save_profile()
                    self.assertTrue((Path(directory) / "image_2theta.csv").is_file())
                    window._on_drawn("rectangle", (2, 3), (10, 15), True)
                    self._finish_work(window)
                    self.assertEqual(int(window._manual_mask.sum()), 117)
                    self.assertIsNotNone(window._profile)
                    self.assertFalse(np.array_equal(initial.intensity,
                                                    window._profile.intensity))
                    window._on_drawn("rectangle", (2, 3), (10, 15), False)
                    self._finish_work(window)
                    self.assertFalse(window._combined_mask().any())
                    np.testing.assert_allclose(window._profile.intensity,
                                               initial.intensity)
                    canvas = window._canvas
                    rect = canvas._image_rect()
                    start = QPoint(int(rect.left() + rect.width() * 0.1),
                                   int(rect.top() + rect.height() * 0.1))
                    end = QPoint(int(rect.left() + rect.width() * 0.2),
                                 int(rect.top() + rect.height() * 0.2))
                    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
                    QTest.mouseMove(canvas, end)
                    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
                    self.assertTrue(window._manual_mask.any())
                    self._finish_work(window)
                    window._threshold.setValue(0)
                    self._finish_work(window)
                    window._start_job("spots")
                    self._finish_work(window)
                    self.assertTrue(window._spot_mask.any())
                finally:
                    window.close()

    def test_shared_settings_update_open_window(self):
        from settings.pages import detector_calibration

        state = PoniState()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"), \
             patch.object(detector_calibration, "_PREFS_FILE", Path(directory) / "detector.json"):
            window = Xrd1DReductionWindow(poni_state=state)
            page = detector_calibration.DetectorCalibrationPage(state)
            try:
                window._diamond_check.setChecked(True)
                window._diamond_spin.setValue(1.35)
                self.assertAlmostEqual(page._diamond_thickness_spin.value(), 1.35)
                page._spot_deviation_spin.setValue(4.25)
                self.assertAlmostEqual(window._threshold.value(), 4.25)
            finally:
                page.close()
                window.close()

    def test_autosave_error_is_visible_with_pdindexer_send(self):
        class Pdi:
            def clipboard_available(self):
                return True

            def watch_folder_configured(self):
                return False

            def send(self, *args, **kwargs):
                self.sent = True

        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"), \
             patch("apps.xrd_1d_reduction.reduction_app.save_profile",
                   return_value=([], ["disk full"])):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            pdi = Pdi()
            window = Xrd1DReductionWindow(poni_state=state, pdi_service=pdi)
            try:
                window._autosave.setChecked(True)
                window._autosend.setChecked(True)
                window.load_image(path)
                self._finish_work(window)
                self.assertIn("Save error: disk full", window._status.text())
                self.assertIn("PDIndexer send requested", window._status.text())
                self.assertTrue(pdi.sent)
            finally:
                window.close()

    def test_spot_request_survives_edit_while_running(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            try:
                window.load_image(path)
                self._finish_work(window)
                calls = []

                def delayed_spots(ai, image, threshold, exclude_mask=None):
                    calls.append(exclude_mask.copy())
                    time.sleep(0.05)
                    result = np.zeros(image.shape, dtype=bool)
                    result[20, 20] = True
                    return result

                with patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=delayed_spots):
                    window._start_job("spots")
                    window._on_drawn("rectangle", (1, 1), (3, 3), True)
                    self._finish_work(window)
                self.assertGreaterEqual(len(calls), 2)
                self.assertTrue(calls[-1][2, 2])
                self.assertTrue(window._spot_mask[20, 20])
                self.assertIsNotNone(window._profile)
            finally:
                window.close()

    def test_clear_cancels_running_spot_search(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            started, release = threading.Event(), threading.Event()
            calls = []

            def held_spots(ai, image, threshold, exclude_mask=None):
                calls.append(1)
                started.set()
                release.wait(5)
                result = np.zeros(image.shape, dtype=bool)
                result[20, 20] = True
                return result

            try:
                window.load_image(path)
                self._finish_work(window)
                with patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=held_spots):
                    window._start_job("spots")
                    self.assertTrue(started.wait(2))
                    window._clear_masks()
                    self.assertIn("Spot detection cancelled", window._status.text())
                    release.set()
                    self._finish_work(window)
                self.assertEqual(len(calls), 1)
                self.assertFalse(window._spot_mask.any())
                self.assertFalse(window._manual_mask.any())
                self.assertIn("Spot detection cancelled", window._status.text())
                window._request_reduction()
                self._finish_work(window)
                self.assertNotIn("Spot detection cancelled", window._status.text())
            finally:
                release.set()
                window.close()

    def test_left_drag_after_clear_does_not_restart_cancelled_spot_search(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            started, release = threading.Event(), threading.Event()
            calls = []

            def held_first_spot(ai, image, threshold, exclude_mask=None):
                calls.append(exclude_mask.copy())
                if len(calls) == 1:
                    started.set()
                    release.wait(5)
                result = np.zeros(image.shape, dtype=bool)
                result[20, 20] = True
                return result

            try:
                window.load_image(path)
                self._finish_work(window)
                with patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=held_first_spot):
                    window._start_job("spots")
                    self.assertTrue(started.wait(2))
                    window._clear_masks()
                    window._on_drawn("rectangle", (1, 1), (2, 2), True)
                    release.set()
                    self._finish_work(window)
                self.assertEqual(len(calls), 1)
                self.assertFalse(window._spot_mask.any())
                self.assertTrue(window._manual_mask[1, 1])
                with patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=held_first_spot):
                    window._start_job("spots")
                    self._finish_work(window)
                self.assertEqual(len(calls), 2)
                self.assertTrue(window._spot_mask[20, 20])
            finally:
                release.set()
                window.close()

    def test_range_or_threshold_edit_does_not_restart_cancelled_spot_search(self):
        for change in ("range", "threshold"):
            with self.subTest(change=change):
                state = self._calibrated_state()
                with tempfile.TemporaryDirectory() as directory, \
                     patch("apps.xrd_1d_reduction.reduction_app._PREFS",
                           Path(directory) / "prefs.json"):
                    path = Path(directory) / "image.tif"
                    tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
                    window = Xrd1DReductionWindow(poni_state=state)
                    started, release = threading.Event(), threading.Event()
                    calls = []

                    def held_spots(ai, image, threshold, exclude_mask=None):
                        calls.append(1)
                        started.set()
                        release.wait(5)
                        result = np.zeros(image.shape, dtype=bool)
                        result[20, 20] = True
                        return result

                    try:
                        window.load_image(path)
                        self._finish_work(window)
                        with patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                                   side_effect=held_spots):
                            window._start_job("spots")
                            self.assertTrue(started.wait(2))
                            window._clear_masks()
                            if change == "range":
                                window._max_angle.setValue(20.0)
                            else:
                                window._threshold.setValue(4.0)
                            release.set()
                            self._finish_work(window)
                        self.assertEqual(len(calls), 1)
                        self.assertFalse(window._spot_mask.any())
                    finally:
                        release.set()
                        window.close()

    def test_right_drag_cancels_running_spot_search(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            started, release = threading.Event(), threading.Event()

            def held_spots(ai, image, threshold, exclude_mask=None):
                started.set()
                release.wait(5)
                result = np.zeros(image.shape, dtype=bool)
                result[20, 20] = True
                return result

            try:
                window.load_image(path)
                self._finish_work(window)
                window._spot_mask[20, 20] = True
                with patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=held_spots):
                    window._start_job("spots")
                    self.assertTrue(started.wait(2))
                    window._on_drawn("rectangle", (19, 19), (21, 21), False)
                    release.set()
                    self._finish_work(window)
                self.assertFalse(window._spot_mask[20, 20])
                self.assertIn("Spot detection cancelled", window._status.text())
            finally:
                release.set()
                window.close()

    def test_clear_cancels_spot_request_waiting_behind_reduction(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            started, release = threading.Event(), threading.Event()
            calls = []

            def held_reduce(*args, **kwargs):
                started.set()
                release.wait(5)
                return reduce_frame(*args, **kwargs)

            def spots(*args, **kwargs):
                calls.append(1)
                return np.zeros((32, 32), dtype=bool)

            try:
                with patch("apps.xrd_1d_reduction.reduction_app.reduce_frame",
                           side_effect=held_reduce), \
                     patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=spots):
                    window.load_image(path)
                    self.assertTrue(started.wait(2))
                    window._start_job("spots")
                    window._clear_masks()
                    release.set()
                    self._finish_work(window)
                self.assertEqual(calls, [])
                self.assertFalse(window._combined_mask().any())
                self.assertIn("Spot detection cancelled", window._status.text())
            finally:
                release.set()
                window.close()

    def test_waiting_spot_request_survives_reduction_edit(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            try:
                window._autosave.setChecked(True)
                spot_calls = []

                def slow_reduce(*args, **kwargs):
                    time.sleep(0.05)
                    return reduce_frame(*args, **kwargs)

                def spots(ai, image, threshold, exclude_mask=None):
                    spot_calls.append(exclude_mask.copy())
                    result = np.zeros(image.shape, dtype=bool)
                    result[20, 20] = True
                    return result

                with patch("apps.xrd_1d_reduction.reduction_app.reduce_frame",
                           side_effect=slow_reduce), \
                     patch("apps.xrd_1d_reduction.reduction_app.find_spot_mask",
                           side_effect=spots):
                    window.load_image(path)
                    window._start_job("spots")
                    window._on_drawn("rectangle", (1, 1), (3, 3), True)
                    self._finish_work(window)
                self.assertEqual(len(spot_calls), 1)
                self.assertTrue(spot_calls[0][2, 2])
                self.assertTrue(window._spot_mask[20, 20])
                self.assertIn("Automatic save/send skipped", window._status.text())
            finally:
                window.close()

    def test_threshold_change_does_not_restart_reduction(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            try:
                window._autosave.setChecked(True)
                window.load_image(path)
                revision = window._revision
                state.set_spot_deviation_factor(4.0)
                self.assertEqual(window._revision, revision)
                self._finish_work(window)
                self.assertNotIn("Automatic save/send skipped", window._status.text())
                self.assertTrue((Path(directory) / "image_2theta.csv").is_file())
            finally:
                window.close()

    def test_early_range_edit_announces_skipped_automatic_save(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            try:
                window._autosave.setChecked(True)
                window.load_image(path)
                window._max_angle.setValue(20.0)
                self._finish_work(window)
                self.assertIsNotNone(window._profile)
                self.assertIn("Automatic save/send skipped", window._status.text())
                self.assertFalse((Path(directory) / "image_2theta.csv").exists())
            finally:
                window.close()

    def test_early_edit_with_automatic_actions_off_has_no_skip_notice(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            try:
                window.load_image(path)
                window._max_angle.setValue(20.0)
                self._finish_work(window)
                self.assertNotIn("skipped", window._status.text())
            finally:
                window.close()

    def test_launcher_refuses_close_while_reduction_worker_runs(self):
        from main import ModeSelectorLauncher

        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            launcher = ModeSelectorLauncher.__new__(ModeSelectorLauncher)
            QMainWindow.__init__(launcher)
            launcher.btn_xrd_1d_reduction = QPushButton()
            launcher._open_windows = {launcher.btn_xrd_1d_reduction: window}
            started, release = threading.Event(), threading.Event()

            def held_reduce(*args, **kwargs):
                started.set()
                release.wait(5)
                return reduce_frame(*args, **kwargs)

            try:
                launcher.show()
                with patch("apps.xrd_1d_reduction.reduction_app.reduce_frame",
                           side_effect=held_reduce):
                    window.load_image(path)
                    self.assertTrue(started.wait(2))
                    with patch("main.QMessageBox.warning") as warning:
                        closed = launcher.close()
                    warning.assert_called_once()
                    self.assertFalse(closed)
                    self.assertTrue(launcher.isVisible())
                    release.set()
                    self._finish_work(window)
            finally:
                release.set()
                window.close()
                launcher.hide()

    def test_previous_mask_file_is_announced(self):
        state = self._calibrated_state()
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            mask_path = Path(directory) / "old.npz"
            mask = np.zeros((32, 32), dtype=bool)
            mask[5, 5] = True
            save_mask(mask_path, mask)
            (Path(directory) / "prefs.json").write_text(
                '{"manual_file": "' + str(mask_path) + '"}')
            path = Path(directory) / "image.tif"
            tifffile.imwrite(path, np.ones((32, 32), dtype=np.uint16))
            window = Xrd1DReductionWindow(poni_state=state)
            try:
                window.load_image(path)
                self._finish_work(window)
                self.assertTrue(window._manual_mask[5, 5])
                self.assertIn(str(mask_path), window._status.text())
            finally:
                window.close()

    def test_malformed_prefs_do_not_prevent_opening(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory) / "prefs.json"):
            (Path(directory) / "prefs.json").write_text('{"bin_width_deg": "abc"}')
            window = Xrd1DReductionWindow(poni_state=PoniState())
            try:
                self.assertEqual(window._width.value(), 0.01)
                self.assertIn("bin_width_deg", window._status.text())
            finally:
                window.close()

    def test_preference_write_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("apps.xrd_1d_reduction.reduction_app._PREFS", Path(directory)):
            window = Xrd1DReductionWindow(poni_state=PoniState())
            try:
                window._save_prefs()
                self.assertIn("Preferences could not be saved", window._status.text())
            finally:
                window.close()

    @staticmethod
    def _calibrated_state():
        state = PoniState()
        state.update(AzimuthalIntegrator(
            dist=0.2, poni1=0.003, poni2=0.003,
            detector=Detector(pixel1=1e-4, pixel2=1e-4), wavelength=1e-10))
        return state

    @classmethod
    def _finish_work(cls, window):
        deadline = time.monotonic() + 20
        while (window._worker is not None or window._reduce_pending or window._spot_pending) \
                and time.monotonic() < deadline:
            cls.app.processEvents()
            time.sleep(0.01)
        cls.app.processEvents()
        if window._worker is not None:
            raise AssertionError("Reduction worker did not finish")


if __name__ == "__main__":
    unittest.main()
