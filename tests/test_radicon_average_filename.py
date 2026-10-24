"""Average sequence images use their own numbered filenames."""

import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from utils.pdindexer import PdiService


class AverageFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def test_sequence_average_uses_next_average_index_even_with_no_suffix(self):
        save_dir = Path("C:/scan")
        existing = [save_dir / name for name in (
            "sample_1.tif", "sample_2.tif", "sample_ave_1.tif",
            "sample_ave_3.TIF", "sample_ave_notes.tif",
        )]
        service = PdiService()
        with (mock.patch.object(RadiconWindow, "_load_prefs", return_value={}),
              mock.patch.object(RadiconWindow, "_save_prefs"),
              mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE",
                         Path("nonexistent-defects.txt"))):
            window = RadiconWindow(RadiconBackendSim(width=4, height=3),
                                   pdi_service=service, shutter_status=lambda: False)
            try:
                window._dir_edit.setText(str(save_dir))
                window._filename_edit.setText("sample")
                window._suffix_none_radio.setChecked(True)
                window._seq_indiv_chk.setChecked(True)
                window._seq_avg_chk.setChecked(True)
                window._seq_ts = "test"
                frames = [np.ones((3, 4), dtype=np.uint16)]
                with (mock.patch.object(Path, "mkdir"),
                      mock.patch.object(Path, "iterdir", return_value=iter(existing)),
                      mock.patch.object(Path, "is_file", return_value=True),
                      mock.patch("apps.Rad_icon_2022.radicon_ui._save_tiff") as save,
                      mock.patch.object(window, "_display_image"),
                      mock.patch.object(window, "_flush_pdi_sequence"),
                      mock.patch.object(window, "_emit_done_sound")):
                    window._on_seq_frame_ready(0, frames[0])
                    window._on_seq_done(frames)

                self.assertEqual(save.call_args_list[0].args[0],
                                 save_dir / "sample.tif")
                self.assertEqual(save.call_args_list[1].args[0],
                                 save_dir / "sample_ave_4.tif")
                self.assertEqual(save.call_args_list[1].args[2]["image_type"],
                                 "sequence_average")
            finally:
                window.close()
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
