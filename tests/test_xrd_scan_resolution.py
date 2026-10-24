"""Hardware-free checks for the XRD scan angular resolution control."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from apps.xrd_scan.roi_dialog import RoiDialog
from apps.xrd_scan.xrd_scan_app import XrdScanWindow
from apps.xrd_scan.xrd_scan_backend import bins_for_angular_resolution


class FakeIntegrator:
    wavelength = 1e-10

    def __init__(self, span=1.0):
        self.npt = None
        self.span = span

    def center_array(self, shape, *, unit, scale):
        assert unit == "2th_deg" and scale is True
        return np.linspace(0, self.span, np.prod(shape)).reshape(shape)

    def integrate1d(self, image, *, npt, **kwargs):
        self.npt = npt
        return SimpleNamespace(
            radial=np.linspace(0, 1, npt),
            intensity=np.ones(npt),
        )


class XrdScanResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_bin_count_from_requested_angular_step(self):
        ai = FakeIntegrator()
        self.assertEqual(bins_for_angular_resolution(ai, (2, 2), 0.01), 100)
        self.assertEqual(bins_for_angular_resolution(ai, (2, 2), 0.2), 10)
        with self.assertRaisesRegex(ValueError, "Cannot determine"):
            bins_for_angular_resolution(FakeIntegrator(span=0), (2, 2), 0.01)
        with self.assertRaisesRegex(ValueError, "10,000 bins"):
            bins_for_angular_resolution(FakeIntegrator(span=20), (2, 2), 0.001)

    def test_scan_uses_resolution_for_worker_and_spectrum_storage(self):
        ai = FakeIntegrator()
        backend = SimpleNamespace(height=2, width=2)
        controller = SimpleNamespace(get_ch_pos=lambda channel: 0)
        window = XrdScanWindow(controller=controller, backend=backend)
        try:
            window._ai = ai
            window._resolution_spin.setValue(0.01)
            self.assertEqual(window._get_roi_dialog_params()[0], 0.01)
            window._grid_n_ch4_spin.setValue(2)
            window._grid_n_ch5_spin.setValue(2)
            with patch("apps.xrd_scan.xrd_scan_app.XrdScanWorker.start"):
                window._on_start()
            self.assertEqual(window._n_bins, 100)
            self.assertEqual(window._spectra.shape, (2, 2, 100))
            self.assertEqual(window._scan_worker._n_bins, 100)
            self.assertEqual(window._angular_resolution_deg, 0.01)
        finally:
            window.close()

    def test_roi_test_shot_uses_captured_image_shape_and_resolution(self):
        ai = FakeIntegrator()
        backend = SimpleNamespace(capture_triggered=lambda _: np.ones((2, 2), dtype=np.uint16))
        service = MagicMock()
        service.clipboard_available.return_value = False
        service.watch_folder_configured.return_value = False
        dialog = RoiDialog(
            backend=backend,
            params_getter=lambda: (0.01, 1000, ai, None),
            pdi_service=service,
        )
        try:
            dialog._on_test_shot()
            self.assertEqual(ai.npt, 100)
            self.assertEqual(len(dialog._last_radial), 100)
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
