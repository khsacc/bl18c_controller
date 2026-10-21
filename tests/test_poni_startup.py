"""Saved detector geometry is available before Settings is opened."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from settings.pages import detector_calibration
from settings.poni_state import PoniState


_PONI = """poni_version: 2
Detector: Flat
Detector_config: {"pixel1": 0.0001, "pixel2": 0.0001}
Distance: 0.25
Poni1: 0.01
Poni2: 0.02
Rot1: 0
Rot2: 0
Rot3: 0
Wavelength: 1e-10
"""


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, value):
        self.text = value

    def setStyleSheet(self, value):
        pass

    def setVisible(self, value):
        pass


class PoniStartupTests(unittest.TestCase):
    def setUp(self):
        self.prefs = Path("detector_calibration.json")
        self.saved_path = Path("geometry.poni")
        self.prefs_text = ""
        self.poni_text = _PONI
        self.file_exists = True
        prefs_patch = patch.object(detector_calibration, "_PREFS_FILE", self.prefs)
        prefs_patch.start()
        self.addCleanup(prefs_patch.stop)
        read_patch = patch.object(Path, "read_text", lambda path, **kwargs: self._read_text(path, **kwargs))
        read_patch.start()
        self.addCleanup(read_patch.stop)
        exists_patch = patch.object(Path, "is_file", lambda path: self._is_file(path))
        exists_patch.start()
        self.addCleanup(exists_patch.stop)

    def _read_text(self, path, **_kwargs):
        return self.prefs_text if path == self.prefs else self.poni_text

    def _is_file(self, path):
        return self.file_exists and path == self.saved_path

    def _save_path(self, path):
        self.prefs_text = json.dumps({"last_poni_path": str(path)})

    def test_saved_poni_is_restored_before_detector_window_status(self):
        path = self.saved_path
        self._save_path(path)
        state = PoniState()

        detector_calibration.restore_remembered_poni(state)

        self.assertTrue(state.is_calibrated())
        self.assertEqual(state.poni_path, path)
        self.assertAlmostEqual(state.ai.dist, 0.25)
        panel = SimpleNamespace(
            _poni_state=state,
            _instant1d_status_label=_Label(),
            _instant1d_settings_btn=_Label(),
        )
        RadiconWindow._refresh_instant1d_status(panel)
        self.assertIn("geometry.poni", panel._instant1d_status_label.text)
        self.assertNotIn("No poni file registered", panel._instant1d_status_label.text)

    def test_missing_or_invalid_saved_file_leaves_state_uncalibrated(self):
        state = PoniState()
        self._save_path(self.saved_path)
        self.file_exists = False
        detector_calibration.restore_remembered_poni(state)
        self.assertFalse(state.is_calibrated())

        self.file_exists = True
        self.poni_text = "Distance: invalid\n"
        with self.assertLogs(detector_calibration._LOG, level="WARNING"):
            detector_calibration.restore_remembered_poni(state)
        self.assertFalse(state.is_calibrated())

    def test_existing_session_calibration_is_not_replaced(self):
        path = self.saved_path
        self._save_path(path)
        state = PoniState()
        current_ai = object()
        state.update(ai=current_ai)

        detector_calibration.restore_remembered_poni(state)

        self.assertIs(state.ai, current_ai)
        self.assertIsNone(state.poni_path)


if __name__ == "__main__":
    unittest.main()
