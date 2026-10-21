"""Shared Rad-icon capture, correction and save paths without detector hardware."""

import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.image_utils import (
    build_detector_metadata, process_frame,
)
from apps.Rad_icon_2022.radicon_backend import (
    RadiconBackend, RadiconBackendSim, RadiconError,
)
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from apps.exp_scheduler.actions import TakeXrdAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalXrdSettings
from apps.exp_scheduler.sequence import Sequence
from utils.pdindexer import PdiService


class SharedCaptureTests(unittest.TestCase):
    def _backend_without_hardware(self, snap):
        backend = object.__new__(RadiconBackend)
        backend._capture_lock = threading.RLock()
        backend._handle = object()
        backend._exposure_ms = None
        backend._snap_timeout_ms = 0
        backend._ser = None
        backend.close = lambda: None
        backend.snap = snap
        return backend

    def test_capture_sets_exposure_at_trigger_and_restores_idle(self):
        calls = []
        frame = np.full((2, 2), 7, dtype=np.uint16)

        def snap(timeout_ms):
            calls.append(("snap", timeout_ms))
            if timeout_ms == 300:
                raise RadiconError("timeout")
            return frame

        backend = self._backend_without_hardware(snap)
        backend._send_serial = lambda command: calls.append(("serial", command))

        result = backend.capture_triggered(200)

        self.assertIs(result, frame)
        self.assertEqual(calls, [
            ("serial", "set 200"), ("snap", 300), ("snap", 5200),
            ("serial", "set 100"),
        ])

    def test_capture_error_still_restores_idle(self):
        calls = []
        backend = self._backend_without_hardware(
            lambda timeout_ms: (_ for _ in ()).throw(RuntimeError("DMA failed")),
        )
        backend._send_serial = calls.append

        with self.assertRaisesRegex(RuntimeError, "DMA failed"):
            backend.capture_triggered(200)
        self.assertEqual(calls, ["set 200", "set 100"])


class SharedCorrectionTests(unittest.TestCase):
    def test_dark_and_defect_follow_same_flip_and_report_actual_application(self):
        raw = np.full((3, 3), 10, dtype=np.uint16)
        raw[0, 0] = 100
        mask = np.zeros_like(raw, dtype=bool)
        mask[0, 0] = True
        dark = np.ones_like(raw)

        result = process_frame(
            raw, flip_v=True, flip_h=False, dark=dark, dark_enabled=True,
            defect_mask=mask, defect_mode="median", defect_kernel=3,
        )

        np.testing.assert_array_equal(result.image, np.full((3, 3), 9, dtype=np.uint16))
        self.assertTrue(result.dark_applied)
        self.assertTrue(result.defect_applied)
        self.assertTrue(mask[0, 0])

    def test_shape_mismatch_skips_correction_and_metadata_can_record_it(self):
        raw = np.full((3, 3), 10, dtype=np.uint16)
        result = process_frame(
            raw, flip_v=False, flip_h=False,
            dark=np.zeros((2, 2)), dark_enabled=True,
            defect_mask=np.zeros((2, 2), dtype=bool), defect_mode="median",
        )
        self.assertEqual(result.dark_shape_mismatch, (2, 2))
        self.assertEqual(result.defect_shape_mismatch, (2, 2))
        self.assertFalse(result.dark_applied)
        self.assertFalse(result.defect_applied)
        metadata = build_detector_metadata(
            "xrd", 500, 1032, False, False,
            {"dark_corrected": result.dark_applied,
             "defect_corrected": result.defect_applied},
        )
        self.assertFalse(metadata["dark_corrected"])
        self.assertFalse(metadata["defect_corrected"])


class SchedulerSharedPipelineTests(unittest.TestCase):
    def test_saved_xrd_uses_shared_capture_and_flipped_defect_mask(self):
        raw = np.full((3, 3), 10, dtype=np.uint16)
        raw[0, 0] = 100
        mask = np.zeros_like(raw, dtype=bool)
        mask[0, 0] = True
        backend = mock.Mock(width=3, height=3, _h_blank=0)
        backend.capture_triggered.return_value = raw
        settings = GlobalXrdSettings(
            exposure_ms=200, save_dir="C:/test-xrd", flip_v=True, flip_h=False,
            dark_enabled=True, dark_file="C:/dark.tif",
            defect_enabled=True, defect_file="C:/defects.txt",
        )
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(radicon=backend), global_xrd=settings,
        )
        runner._logger = mock.Mock()
        action = TakeXrdAction(exposure_ms=200, save=True, prefix="scan")

        with (mock.patch.object(runner, "_load_xrd_dark", return_value=np.ones_like(raw)),
              mock.patch.object(runner, "_load_xrd_defect_mask", return_value=mask),
              mock.patch("apps.exp_scheduler.runner.capture_conditions", return_value={}),
              mock.patch("apps.exp_scheduler.runner.append_xrd_record", return_value=Path("C:/test-xrd/xrd_master.csv")),
              mock.patch("apps.Rad_icon_2022.image_utils.save_tiff", return_value=True) as save,
              mock.patch.object(Path, "mkdir"),
              mock.patch.object(Path, "exists", return_value=False)):
            runner._do_take_xrd(action, 0)

        backend.capture_triggered.assert_called_once_with(200, timeout_ms=5200)
        image = save.call_args.args[1]
        metadata = save.call_args.args[2]
        np.testing.assert_array_equal(image, np.full((3, 3), 9, dtype=np.uint16))
        self.assertTrue(metadata["dark_corrected"])
        self.assertTrue(metadata["defect_corrected"])
        self.assertEqual(metadata["exposure_ms"], 200)


class RadiconWindowSharedPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"],
        )

    def test_single_shot_uses_shared_correction_and_save_metadata(self):
        raw = np.full((3, 3), 10, dtype=np.uint16)
        raw[0, 0] = 100
        mask = np.zeros_like(raw, dtype=bool)
        mask[0, 0] = True
        service = PdiService()
        self.addCleanup(service.shutdown)
        with (mock.patch.object(RadiconWindow, "_load_prefs", return_value={}),
              mock.patch.object(RadiconWindow, "_save_prefs"),
              mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE", Path("nonexistent-defects.txt"))):
            window = RadiconWindow(RadiconBackendSim(width=3, height=3), pdi_service=service)
            try:
                window._dark_img = np.ones_like(raw)
                window._defect_mask = mask
                window._dark_correct_chk.setChecked(True)
                window._defect_median_radio.setChecked(True)
                with (mock.patch.object(window, "_build_save_path", return_value=Path("C:/scan.tif")),
                      mock.patch.object(window, "_display_image"),
                      mock.patch("apps.Rad_icon_2022.radicon_ui._save_tiff", return_value=True) as save,
                      mock.patch.object(Path, "mkdir")):
                    window._on_snap_done(raw, "")
            finally:
                window.close()

        image = save.call_args.args[1]
        metadata = save.call_args.args[2]
        np.testing.assert_array_equal(image, np.full((3, 3), 9, dtype=np.uint16))
        self.assertTrue(metadata["dark_corrected"])
        self.assertTrue(metadata["defect_corrected"])


if __name__ == "__main__":
    unittest.main()
