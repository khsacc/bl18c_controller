"""Shutter confirmation before detector exposures, without beamline hardware."""

import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow, _DarkWorker, _read_shutter_status
from utils.pdindexer import PdiService


class ShutterConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def setUp(self):
        self.service = PdiService()
        self.patches = [
            mock.patch.object(RadiconWindow, "_load_prefs", return_value={}),
            mock.patch.object(RadiconWindow, "_save_prefs"),
            mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE", Path("missing-defects.txt")),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.window = RadiconWindow(
            RadiconBackendSim(width=8, height=6), pdi_service=self.service,
            shutter_status=lambda: True,
        )

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def _wait_for_check(self):
        deadline = time.monotonic() + 2
        while self.window._shutter_check_pending and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window._shutter_check_pending)
        self.window._shutter_check_worker.wait()

    def test_in_requires_confirmation_for_each_non_dark_mode(self):
        for entry, capture in (
            ("_snap", "_start_snap_capture"),
            ("_seq_start", "_start_sequence_capture"),
            ("_start_live", "_start_live_capture"),
        ):
            with self.subTest(entry=entry):
                with mock.patch.object(self.window, capture) as start, mock.patch.object(
                    QtWidgets.QMessageBox, "question",
                    return_value=QtWidgets.QMessageBox.StandardButton.No,
                ) as question:
                    getattr(self.window, entry)()
                    self._wait_for_check()
                    question.assert_called_once()
                    start.assert_not_called()

                with mock.patch.object(self.window, capture) as start, mock.patch.object(
                    QtWidgets.QMessageBox, "question",
                    return_value=QtWidgets.QMessageBox.StandardButton.Yes,
                ) as question:
                    getattr(self.window, entry)()
                    self._wait_for_check()
                    question.assert_called_once()
                    start.assert_called_once()

    def test_out_and_unavailable_stars_do_not_prompt(self):
        for read_status in (lambda: False, lambda: None,
                            lambda: (_ for _ in ()).throw(RuntimeError("offline"))):
            with self.subTest(read_status=read_status):
                self.window._read_shutter_status = read_status
                with mock.patch.object(self.window, "_start_snap_capture") as start, mock.patch.object(
                    QtWidgets.QMessageBox, "question"
                ) as question:
                    self.window._snap()
                    self._wait_for_check()
                    question.assert_not_called()
                    start.assert_called_once()

    def test_dark_acquisition_skips_shutter_check(self):
        with mock.patch.object(_DarkWorker, "start") as start, mock.patch.object(
            self.window, "_read_shutter_status"
        ) as read_status:
            self.window._acquire_dark()
            read_status.assert_not_called()
            start.assert_called_once()

    def test_standalone_status_reader_closes_connection_on_error(self):
        with mock.patch("apps.Rad_icon_2022.radicon_ui.ShutterController") as controller_type:
            controller = controller_type.return_value
            controller.get_status.side_effect = RuntimeError("offline")
            with self.assertRaisesRegex(RuntimeError, "offline"):
                _read_shutter_status()
            controller.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
