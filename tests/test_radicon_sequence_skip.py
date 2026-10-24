"""Sequential frame skipping with a simulated detector (no hardware)."""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_backend import RadiconBackendSim
from apps.Rad_icon_2022.radicon_ui import RadiconWindow, _SeqWorker
from settings.i18n import tr
from utils.pdindexer import PdiService


class _CountingBackend(RadiconBackendSim):
    def __init__(self):
        super().__init__(width=4, height=3)
        self.calls = 0

    def snap(self, timeout_ms=None):
        self.calls += 1
        return np.full((self.height, self.width), self.calls, dtype=np.uint16)


class _BlockingSkipBackend(_CountingBackend):
    def __init__(self):
        super().__init__()
        self.skip_started = threading.Event()
        self.release_skip = threading.Event()

    def snap(self, timeout_ms=None):
        if self.calls == 1:
            self.skip_started.set()
            if not self.release_skip.wait(3):
                raise TimeoutError("Simulated skipped frame timed out")
        return super().snap(timeout_ms)


class SequenceSkipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["test", "-platform", "offscreen"]
        )

    def _wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for the sequence worker")

    def test_saved_frames_follow_skip_count(self):
        for skipped, expected in ((0, [1, 2, 3]),
                                  (1, [1, 3, 5]),
                                  (2, [1, 4, 7])):
            with self.subTest(skipped=skipped):
                backend = _CountingBackend()
                worker = _SeqWorker(backend, 100_000, 3, skipped, 1000)
                results = []
                errors = []
                worker.done.connect(results.append)
                worker.error.connect(errors.append)
                worker.start()
                self._wait_for(lambda: bool(results or errors))
                worker.wait()
                self.assertEqual(errors, [])
                self.assertEqual([int(frame[0, 0]) for frame in results[0]], expected)
                self.assertEqual(backend.calls, 3 + 2 * skipped)

    def test_stop_during_skipped_frame_keeps_first_saved_frame(self):
        backend = _BlockingSkipBackend()
        worker = _SeqWorker(backend, 100_000, 3, 2, 1000)
        results = []
        worker.done.connect(results.append)
        worker.start()
        try:
            self.assertTrue(backend.skip_started.wait(2))
            worker.request_stop()
        finally:
            backend.release_skip.set()
        self._wait_for(lambda: bool(results))
        worker.wait()
        self.assertEqual([int(frame[0, 0]) for frame in results[0]], [1])
        self.assertEqual(backend.calls, 2)

    def test_ui_estimate_preferences_and_metadata_use_skip_frames(self):
        with (mock.patch.object(RadiconWindow, "_load_prefs", return_value={
                  "seq_interval_ms": "2500", "seq_skip_frames": "1",
              }),
              mock.patch.object(RadiconWindow, "_save_prefs"),
              mock.patch("apps.Rad_icon_2022.radicon_ui._DEFAULT_DEFECT_FILE",
                         Path("nonexistent-defects.txt"))):
            window = RadiconWindow(_CountingBackend(), pdi_service=PdiService(),
                                   shutter_status=lambda: False)
            try:
                self.assertEqual(window._seq_skip_edit.text(), "1")
                window._seq_n_edit.setText("3")
                window._exp_spin.setValue(1.0)
                self.assertEqual(window._seq_time_label.text(),
                                 tr("Estimated: {sec:.1f} s", sec=5.0))

                window._seq_ts = "test"
                with (mock.patch.object(window, "_build_metadata",
                                        side_effect=lambda _kind, extra: extra),
                      mock.patch.object(window, "_build_save_path",
                                        return_value=Path("test.tif")),
                      mock.patch("apps.Rad_icon_2022.radicon_ui._save_tiff") as save_tiff,
                      mock.patch.object(window, "_display_image")):
                    window._on_seq_frame_ready(0, np.ones((3, 4), dtype=np.uint16))
                    self.assertEqual(save_tiff.call_args.args[2]["skip_frames"], 1)
                    self.assertNotIn("interval_ms", save_tiff.call_args.args[2])

                window._seq_skip_edit.setText("-1")
                self.assertEqual(window._seq_time_label.text(), tr("Estimated: --"))
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
