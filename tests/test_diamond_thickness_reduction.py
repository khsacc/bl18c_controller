"""Hardware-free checks for the reduction-only diamond distance correction."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from apps.Rad_icon_2022.instant1d import reduce_frame
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from apps.xrd_scan.xrd_scan_app import XrdScanWindow
from settings.pages import detector_calibration
from settings.poni_state import PoniState
from utils.poni_io import build_ai, parse_poni
from utils.pdindexer import Trigger


PONI_TEXT = """poni_version: 2
Detector: Flat
Detector_config: {"pixel1": 0.0001, "pixel2": 0.0001}
Distance: 0.25
Poni1: 0.0008
Poni2: 0.0008
Rot1: 0
Rot2: 0
Rot3: 0
Wavelength: 1e-10
"""


class DiamondThicknessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(__file__).parent
        stem = f"_diamond_test_{uuid4().hex}"
        self.poni_path = self.root / f"{stem}.poni"
        self.poni_path.write_text(PONI_TEXT, encoding="utf-8")
        self.addCleanup(lambda: self.poni_path.unlink(missing_ok=True))
        self.prefs = self.root / f"{stem}.json"
        self.addCleanup(lambda: self.prefs.unlink(missing_ok=True))
        for name, value in (("_LOCALDATA", self.root), ("_PREFS_FILE", self.prefs)):
            p = patch.object(detector_calibration, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_corrected_reduction_keeps_standard_geometry_and_file(self):
        state = PoniState()
        state.update(build_ai(parse_poni(self.poni_path)), poni_path=self.poni_path)
        image = np.ones((16, 16), dtype=np.uint16)
        base = reduce_frame(state.integration_ai, image, 0.01, npt=32)

        state.set_diamond_correction(True, 0.8)
        corrected_ai = state.integration_ai
        corrected = reduce_frame(corrected_ai, image, 0.01, npt=32)

        self.assertIs(corrected_ai, state.integration_ai)
        self.assertAlmostEqual(state.ai.dist, 0.25)
        self.assertAlmostEqual(corrected_ai.dist, 0.2508)
        self.assertFalse(np.allclose(base.radial, corrected.radial))
        self.assertEqual(self.poni_path.read_text(encoding="utf-8"), PONI_TEXT)

        state.set_diamond_correction(False, 0.8)
        self.assertIs(state.integration_ai, state.ai)
        state.set_diamond_correction(True, 1.2)
        self.assertAlmostEqual(state.integration_ai.dist, 0.2512)
        replacement = build_ai(parse_poni(self.poni_path))
        replacement.dist = 0.3
        state.update(replacement)
        self.assertAlmostEqual(state.integration_ai.dist, 0.3012)

    def test_settings_persist_independently_of_poni_path(self):
        state = PoniState()
        page = detector_calibration.DetectorCalibrationPage(state)
        try:
            self.assertFalse(page._diamond_check.isChecked())
            self.assertFalse(page._diamond_thickness_spin.isEnabled())
            page._diamond_check.setChecked(True)
            page._diamond_thickness_spin.setValue(0.8)
            self.assertTrue(page._diamond_thickness_spin.isEnabled())
            detector_calibration.remember_poni_path(self.poni_path)
        finally:
            page.close()

        saved = json.loads(self.prefs.read_text(encoding="utf-8"))
        self.assertTrue(saved["correct_diamond_thickness"])
        self.assertEqual(saved["diamond_thickness_mm"], 0.8)
        self.assertEqual(saved["last_poni_path"], str(self.poni_path))

        restored = PoniState()
        detector_calibration.restore_remembered_poni(restored)
        self.assertAlmostEqual(restored.ai.dist, 0.25)
        self.assertAlmostEqual(restored.integration_ai.dist, 0.2508)

    def test_scan_and_scheduler_receive_reduction_geometry(self):
        state = PoniState()
        state.update(build_ai(parse_poni(self.poni_path)))
        state.set_diamond_correction(True, 0.8)

        scan = XrdScanWindow(poni_state=state)
        try:
            self.assertIs(scan._ai, state.integration_ai)
            state.set_diamond_correction(True, 1.2)
            self.assertIs(scan._ai, state.integration_ai)
            self.assertAlmostEqual(scan._ai.dist, 0.2512)
        finally:
            scan.close()

        runner = SequenceRunner(Sequence(actions=[]), DeviceContext(poni_state=state))
        self.assertIs(runner._poni_ai, state.integration_ai)

    def test_radicon_instant_1d_uses_reduction_geometry(self):
        state = PoniState()
        state.update(build_ai(parse_poni(self.poni_path)))
        state.set_diamond_correction(True, 0.8)
        panel = SimpleNamespace(
            _poni_state=state,
            _instant1d_chk=SimpleNamespace(isChecked=lambda: True),
            _instant1d_find_spots_chk=SimpleNamespace(isChecked=lambda: False),
            _instant1d_angle_limits=lambda: (None, None),
            _instant1d_binwidth_spin=SimpleNamespace(value=lambda: 0.01),
            _get_cached_npt=lambda *_: 32,
            _instant1d_curve=MagicMock(),
            _update_pdi_profile=MagicMock(),
        )
        result = SimpleNamespace(radial=np.array([1.0]), intensity=np.array([2.0]))
        with patch("apps.Rad_icon_2022.radicon_ui.reduce_frame", return_value=result) as reduce:
            RadiconWindow._maybe_run_instant_1d(
                panel, np.ones((2, 2), dtype=np.uint16), None, trigger=Trigger.RECOMPUTE
            )
        self.assertIs(reduce.call_args.args[0], state.integration_ai)


if __name__ == "__main__":
    unittest.main()
