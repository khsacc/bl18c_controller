"""The detector restores a selected dark image without connecting to hardware."""

import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from utils.pdindexer import PdiService


class DarkPreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def setUp(self):
        self.root = Path("C:/radicon-test")
        self.prefs = self.root / "radicon_ui_prefs.json"
        self.saved_json = None

        def read_text(path, **kwargs):
            if path != self.prefs or self.saved_json is None:
                raise FileNotFoundError(path)
            return self.saved_json

        def write_text(path, contents, **kwargs):
            self.assertEqual(path, self.prefs)
            self.saved_json = contents
            return len(contents)

        self.patchers = [
            mock.patch("apps.Rad_icon_2022.radicon_ui._LOCALDATA", self.root),
            mock.patch("apps.Rad_icon_2022.radicon_ui._PREFS_FILE", self.prefs),
            mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE", self.root / "no-defects.txt"),
            mock.patch("apps.Rad_icon_2022.radicon_ui._read_tiff_metadata", return_value={}),
            mock.patch.object(Path, "read_text", read_text),
            mock.patch.object(Path, "write_text", write_text),
            mock.patch.object(Path, "mkdir", return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = PdiService()

    def make_window(self):
        window = RadiconWindow(RadiconBackendSim(width=8, height=6), pdi_service=self.service)
        self.addCleanup(window.close)
        return window

    def test_selected_dark_is_saved_and_applied_after_restart(self):
        dark_path = self.root / "dark.tif"
        with mock.patch("apps.Rad_icon_2022.radicon_ui.cv2.imread", return_value=np.full((6, 8), 7, dtype=np.uint16)):
            first = self.make_window()
            with mock.patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(dark_path), "")):
                first._load_dark()
            self.assertEqual(json.loads(self.saved_json)["dark_file_path"], str(dark_path))
            first.close()

            restored = self.make_window()
            self.assertEqual(restored._dark_path, dark_path)
            corrected, warning = restored._dark_correct(np.full((6, 8), 12, dtype=np.uint16))
            self.assertEqual(warning, "")
            np.testing.assert_array_equal(corrected, np.full((6, 8), 5, dtype=np.uint16))

    def test_missing_dark_keeps_path_and_does_not_block_startup(self):
        missing_path = self.root / "missing.tif"
        self.saved_json = json.dumps({"dark_file_path": str(missing_path)})

        with mock.patch("apps.Rad_icon_2022.radicon_ui.cv2.imread", return_value=None):
            window = self.make_window()
        self.assertIsNone(window._dark_img)
        self.assertIn("Load error", window._dark_status_label.text())
        window.close()
        self.assertEqual(json.loads(self.saved_json)["dark_file_path"], str(missing_path))


if __name__ == "__main__":
    unittest.main()
