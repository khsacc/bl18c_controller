"""Four-digit Rad-icon TIFF filenames use only matching image numbers."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from utils.pdindexer import PdiService


class IndexFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def setUp(self):
        temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_dir.cleanup)
        self.save_dir = Path(temporary_dir.name)
        self.prefs = {}
        self.service = PdiService()
        self.addCleanup(self.service.shutdown)
        patchers = (
            mock.patch.object(RadiconWindow, "_load_prefs", side_effect=lambda: self.prefs),
            mock.patch.object(RadiconWindow, "_save_prefs"),
            mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE",
                       self.save_dir / "no-defects.txt"),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_window(self):
        window = RadiconWindow(RadiconBackendSim(width=4, height=3),
                               pdi_service=self.service, shutter_status=lambda: False)
        self.addCleanup(window.close)
        window._dir_edit.setText(str(self.save_dir))
        window._filename_edit.setText("sample")
        return window

    def test_only_matching_tiffs_advance_index(self):
        window = self.make_window()
        self.assertEqual(window._build_save_path(self.save_dir).name, "sample_0001.tif")

        for name in (
            "sample_0001.tif", "sample_0001_2theta.csv",
            "sample_ave_1.tif", "sample_0004.gsa", "sample_0005_2theta.csv",
            "sample_006.tif", "sample_2026-10-24-12-00-00.tif",
            "sample2_0020.tif", "sample_0003.TIFF",
        ):
            (self.save_dir / name).touch()

        self.assertEqual(window._build_save_path(self.save_dir).name, "sample_0004.tif")
        (self.save_dir / "sample_0010.tif").touch()
        self.assertEqual(window._build_save_path(self.save_dir).name, "sample_0011.tif")

    def test_prefix_is_literal_and_number_expands_after_9999(self):
        window = self.make_window()
        window._filename_edit.setText("sample[1]")
        (self.save_dir / "sample1_10000.tif").touch()
        (self.save_dir / "sample[1]_9999.tif").touch()
        self.assertEqual(window._build_save_path(self.save_dir).name, "sample[1]_10000.tif")

    def test_legacy_index_preferences_select_four_digits(self):
        for mode in ("index", "index3", "index4", "unknown"):
            with self.subTest(mode=mode):
                self.prefs = {"filename_suffix_mode": mode}
                window = self.make_window()
                self.assertTrue(window._suffix_index4_radio.isChecked())
                self.assertEqual(window._current_suffix_mode(), "index4")
                self.assertEqual(window._build_save_path(self.save_dir).name, "sample_0001.tif")
                window.close()


if __name__ == "__main__":
    unittest.main()
