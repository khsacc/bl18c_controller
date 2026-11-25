"""Dark retries, averaging and shutter safety without detector hardware."""

import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtCore, QtWidgets

from apps.Rad_icon_2022 import radicon_backend, radicon_ui
from apps.Rad_icon_2022.radicon_backend import EarlyFrameError, RadiconError
from tests.test_beam_control import RecordingShutter
from utils.beam_control import BeamControl
from utils.pdindexer import PdiService


class DarkRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"],
        )

    def setUp(self):
        self.policy = BeamControl()
        self.shutter = RecordingShutter()
        self.policy.configure(self.shutter)
        for module in (radicon_backend, radicon_ui):
            patcher = mock.patch.object(module, "beam_control", self.policy)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.backend = radicon_backend.RadiconBackendSim(width=2, height=2)
        self.worker = radicon_ui._DarkWorker(self.backend, 1_000_000, 2, 6000)
        self.results = []
        self.errors = []
        self.progress = []
        self.retries = []
        self.worker.done.connect(self.results.append)
        self.worker.error.connect(self.errors.append)
        self.worker.progress.connect(lambda *args: self.progress.append(args))
        self.worker.retrying.connect(lambda *args: self.retries.append(args))

    def frame(self, value):
        return np.full((2, 2), value, dtype=np.uint16)

    def test_retries_each_frame_and_averages_only_accepted_frames(self):
        self.backend.snap = mock.Mock(side_effect=[
            EarlyFrameError(1000, 100), self.frame(10),
            EarlyFrameError(1000, 200), EarlyFrameError(1000, 300), self.frame(20),
        ])
        with mock.patch.object(
            self.backend, "capture_triggered", wraps=self.backend.capture_triggered,
        ) as capture:
            self.worker.run()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.progress, [(1, 2), (2, 2)])
        self.assertEqual(self.retries, [(1, 1, 100), (2, 1, 200), (2, 2, 300)])
        np.testing.assert_array_equal(self.results[0], self.frame(15))
        self.assertEqual(capture.call_args_list, [mock.call(1000, timeout_ms=6000)] * 5)

    def test_retry_exhaustion_discards_partial_average_and_releases_shutter(self):
        self.policy.set_automatic(True)
        self.shutter.events.clear()
        self.backend.snap = mock.Mock(side_effect=[
            self.frame(10), *[EarlyFrameError(1000, 100)] * 3,
        ])
        self.worker.run()
        self.assertEqual(self.results, [])
        self.assertEqual(self.progress, [(1, 2)])
        self.assertEqual(len(self.errors), 1)
        self.assertIn("dark frame 2/2 failed after 3 attempts", self.errors[0])
        self.assertEqual(self.backend.snap.call_count, 4)
        self.assertEqual(self.shutter.events, [True, True])
        self.assertFalse(self.policy.in_use)

    def test_auto_keeps_shutter_closed_through_nested_capture_and_retries(self):
        self.policy.set_automatic(True)
        self.shutter.events.clear()
        calls = []

        def snap(timeout_ms=None):
            self.assertTrue(self.shutter.get_status())
            calls.append(True)
            if len(calls) == 1:
                raise EarlyFrameError(1000, 100)
            return self.frame(10)

        self.backend.snap = snap
        self.worker.done.connect(lambda _: self.assertFalse(self.policy.in_use))
        self.worker.run()
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.results), 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.shutter.events, [True, True])
        self.assertTrue(self.shutter.get_status())

    def test_hardware_error_is_not_retried(self):
        self.backend.snap = mock.Mock(side_effect=RadiconError("DMA failed"))
        self.worker.run()
        self.assertEqual(self.results, [])
        self.assertEqual(self.errors, ["DMA failed"])
        self.assertEqual(self.retries, [])
        self.backend.snap.assert_called_once()

    def test_stop_before_capture_does_not_acquire(self):
        self.backend.snap = mock.Mock()
        self.worker.request_stop()
        self.worker.run()
        self.backend.snap.assert_not_called()
        self.assertEqual(self.results, [])

    def test_stop_on_retry_does_not_start_another_attempt(self):
        self.backend.snap = mock.Mock(side_effect=EarlyFrameError(1000, 100))
        self.worker.retrying.connect(lambda *_: self.worker.request_stop())
        self.worker.run()
        self.backend.snap.assert_called_once()
        self.assertEqual(self.results, [])
        self.assertEqual(self.errors, [])

    def test_stop_during_final_capture_discards_average(self):
        def snap(timeout_ms=None):
            if self.progress:
                self.worker.request_stop()
            return self.frame(10)

        self.backend.snap = snap
        self.worker.run()
        self.assertEqual(self.results, [])
        self.assertEqual(self.progress, [(1, 2)])
        self.assertEqual(self.errors, [])

    def test_retry_status_is_delivered_from_background_thread(self):
        self.backend.snap = mock.Mock(side_effect=[
            EarlyFrameError(1000, 100), self.frame(10), self.frame(20),
        ])
        service = PdiService()
        self.addCleanup(service.shutdown)
        with (mock.patch.object(radicon_ui.RadiconWindow, "_load_prefs", return_value={}),
              mock.patch.object(radicon_ui.RadiconWindow, "_save_prefs"),
              mock.patch.object(radicon_ui, "_DEFAULT_DEFECT_FILE", Path("missing.txt"))):
            window = radicon_ui.RadiconWindow(self.backend, pdi_service=service)
            try:
                window._exp_spin.setValue(1.0)
                window._dark_n_edit.setText("2")
                retry_status = []
                original_retry = window._on_dark_retry

                def on_retry(*args):
                    self.assertEqual(QtCore.QThread.currentThread(), self.app.thread())
                    original_retry(*args)
                    retry_status.append(window._dark_status_label.text())

                with (mock.patch.object(window, "_on_dark_retry", side_effect=on_retry),
                      mock.patch.object(window, "_on_dark_done") as done):
                    window._acquire_dark()
                    self.assertTrue(window._dark_worker.wait(3000))
                    self.app.processEvents()
                    done.assert_called_once()
                self.assertEqual(len(retry_status), 1)
                self.assertIn("Acquiring... (0 / 2)  Elapsed:", retry_status[0])
                self.assertTrue(retry_status[0].endswith(
                    "\nEarly frame (100 ms); retrying dark frame 1 (1/2).",
                ))
                window._dark_stop_requested = True
                window._dark_status_label.setText("Stopping...")
                window._on_dark_retry(1, 1, 100)
                window._on_dark_progress(1, 2)
                self.assertEqual(window._dark_status_label.text(), "Stopping...")
                with mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName") as save:
                    window._on_dark_done(self.frame(10))
                save.assert_not_called()
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
