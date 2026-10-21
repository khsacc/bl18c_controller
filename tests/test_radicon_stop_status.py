"""Stop status follows simulated detector workers without physical hardware."""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from settings.i18n import tr
from utils.pdindexer import PdiService


class _BlockingBackend(RadiconBackendSim):
    def __init__(self):
        super().__init__(width=8, height=6)
        self.snap_started = threading.Event()
        self.release_snap = threading.Event()

    def snap(self, timeout_ms=None):
        self.snap_started.set()
        if not self.release_snap.wait(5):
            raise TimeoutError("Simulated frame was not released")
        return np.zeros((self.height, self.width), dtype=np.uint16)


class StopStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def setUp(self):
        self.backend = _BlockingBackend()
        self.service = PdiService()
        self.patchers = [
            mock.patch.object(RadiconWindow, "_load_prefs", return_value={}),
            mock.patch.object(RadiconWindow, "_save_prefs"),
            mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE", Path("nonexistent-defects.txt")),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = RadiconWindow(self.backend, pdi_service=self.service)

    def tearDown(self):
        self.backend.release_snap.set()
        self.window.close()
        self.app.processEvents()

    def _wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for the detector worker")

    def test_live_stop_remains_stopping_until_worker_finishes(self):
        self.window._start_live()
        self.assertTrue(self.backend.snap_started.wait(2))

        self.window._stop_acquisition()
        self.assertEqual(self.window._status_label.text(), tr("Stopping..."))
        self.assertTrue(self.window._live_worker.isRunning())

        self.backend.release_snap.set()
        self._wait_for(lambda: self.window._status_label.text() == tr("Stopped by user"))
        self.assertFalse(self.window._live_worker.isRunning())
        self.assertTrue(self.window._live_btn.isEnabled())

    def test_dark_stop_updates_main_status_after_worker_finishes(self):
        self.window._dark_n_edit.setText("2")
        self.window._acquire_dark()
        self.assertTrue(self.backend.snap_started.wait(2))

        self.window._stop_acquisition()
        self.assertEqual(self.window._status_label.text(), tr("Stopping..."))

        self.backend.release_snap.set()
        self._wait_for(lambda: self.window._status_label.text() == tr("Stopped by user"))
        self.assertFalse(self.window._dark_worker.isRunning())
        self.assertEqual(self.window._dark_status_label.text(), tr("Stopped by user"))

    def test_sequence_progress_does_not_replace_stopping_status(self):
        self.window._seq_n_edit.setText("2")
        self.window._seq_indiv_chk.setChecked(False)
        self.window._seq_avg_chk.setChecked(True)
        self.window._dir_edit.setText(".")
        with mock.patch("apps.Rad_icon_2022.radicon_ui._save_tiff", return_value=True):
            self.window._seq_start()
            self.assertTrue(self.backend.snap_started.wait(2))

            self.window._stop_acquisition()
            self.assertEqual(self.window._status_label.text(), tr("Stopping..."))
            self.window._on_seq_progress(1, 2)
            self.assertEqual(self.window._status_label.text(), tr("Stopping..."))

            self.backend.release_snap.set()
            self._wait_for(lambda: not self.window._seq_worker.isRunning())
            self._wait_for(lambda: self.window._status_label.text().startswith(tr("Stopped by user: ")))


if __name__ == "__main__":
    unittest.main()
