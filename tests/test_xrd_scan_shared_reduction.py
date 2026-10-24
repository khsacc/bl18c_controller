"""Hardware-free parity checks for XRD scan and Rad-icon 1D reduction."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from apps.xrd_scan.roi_dialog import RoiDialog
from apps.xrd_scan.xrd_scan_app import XrdScanWindow
from apps.xrd_scan.xrd_scan_backend import XrdScanWorker


class RecordingIntegrator:
    wavelength = 1e-10

    def __init__(self):
        self.calls = []

    def center_array(self, shape, *, unit, scale):
        return np.linspace(0, 1, np.prod(shape)).reshape(shape)

    def integrate1d(self, image, **kwargs):
        self.calls.append((image.copy(), kwargs))
        npt = kwargs["npt"]
        return SimpleNamespace(radial=np.linspace(0, 1, npt),
                               intensity=np.ones(npt))


class SharedReductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.raw = np.array([[10, 20], [30, 40]], dtype=np.uint16)
        self.dark = np.ones((2, 2), dtype=np.uint16)
        self.mask = np.array([[True, False], [False, False]])
        self.options = dict(flip_v=True, flip_h=False,
                            defect_mask=self.mask, defect_mode="median",
                            defect_kernel=3)
        self.expected = np.array([[29, 39], [29, 19]], dtype=np.uint16)

    def assert_reduction(self, ai):
        image, kwargs = ai.calls[-1]
        np.testing.assert_array_equal(image, self.expected.astype(np.float32))
        self.assertEqual(kwargs["method"], ("full", "histogram", "cython"))
        self.assertEqual(kwargs["unit"], "2th_deg")
        self.assertTrue(kwargs["correctSolidAngle"])
        self.assertEqual(kwargs["polarization_factor"], 0.95)

    def test_scan_reduces_and_saves_corrected_frame(self):
        ai = RecordingIntegrator()
        controller = MagicMock()
        controller.coordinator.is_valid.return_value = True
        backend = SimpleNamespace(capture_triggered=lambda _: self.raw)
        with patch("tifffile.imwrite") as write_tiff:
            worker = XrdScanWorker(
                controller=controller, backend=backend, ai=ai,
                x_pulses=[0], y_pulses=[0], center_x=0, center_y=0,
                n_bins=100, exposure_ms=1000, speed="H", dark=self.dark,
                processing_options=self.options, settle_ms=0, save_tiff=True,
                tiff_dir=Path("unused"),
            )
            completed = []
            status = []
            worker.scan_completed.connect(lambda: completed.append(True))
            worker.status_message.connect(status.append)
            worker.run()
            self.assertEqual(completed, [True], status)
            self.assert_reduction(ai)
            self.assertEqual(write_tiff.call_args.args[0], str(Path("unused") / "r000_c000.tif"))
            np.testing.assert_array_equal(write_tiff.call_args.args[1], self.expected)

    def test_roi_test_shot_uses_same_corrections_and_integrator(self):
        ai = RecordingIntegrator()
        service = MagicMock()
        service.clipboard_available.return_value = False
        service.watch_folder_configured.return_value = False
        dialog = RoiDialog(
            backend=SimpleNamespace(capture_triggered=lambda _: self.raw),
            params_getter=lambda: (0.01, 1000, ai, self.dark, self.options),
            pdi_service=service,
        )
        try:
            dialog._on_test_shot()
            self.assert_reduction(ai)
            np.testing.assert_array_equal(dialog._last_image, self.expected)
        finally:
            dialog.close()

    def test_scan_snapshots_live_detector_settings(self):
        detector_window = SimpleNamespace(processing_options=lambda: self.options)
        window = XrdScanWindow(
            controller=SimpleNamespace(get_ch_pos=lambda _: 0),
            backend=SimpleNamespace(width=2, height=2),
            get_radicon_window=lambda: detector_window,
        )
        try:
            self.assertIs(window._detector_processing_options(), self.options)
        finally:
            window.close()

    def test_bad_dark_size_aborts_after_restoring_local_control(self):
        ai = RecordingIntegrator()
        controller = MagicMock()
        controller.coordinator.is_valid.return_value = True
        worker = XrdScanWorker(
            controller=controller,
            backend=SimpleNamespace(capture_triggered=lambda _: self.raw),
            ai=ai, x_pulses=[0], y_pulses=[0], center_x=0, center_y=0,
            n_bins=100, exposure_ms=1000, speed="H",
            dark=np.zeros((3, 3), dtype=np.uint16), settle_ms=0,
        )
        aborted = []
        worker.scan_aborted.connect(lambda: aborted.append(True))
        worker.run()
        self.assertEqual(aborted, [True])
        self.assertEqual(ai.calls, [])
        controller.switch_to_loc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
