from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from tools.ring_current_tool import RingCurrentToolWindow
from utils.shutter import ShutterControllerSim


class RingCurrentToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_for_read(self, window: RingCurrentToolWindow) -> None:
        deadline = time.monotonic() + 2
        while window.is_busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(window.is_busy)

    def test_simulated_read_shows_current_and_status(self) -> None:
        window = RingCurrentToolWindow(ShutterControllerSim())
        self.addCleanup(window.close)
        self._wait_for_read(window)
        self.assertEqual(window._value.text(), "450 mA")
        self.assertEqual(window._detail.text(), "Simulated ring current.")
        self.assertEqual(window._status.text(), "Ring status: 5 — User (Top-UP)")
        self.assertEqual(window._status_detail.text(), "Simulated Ring.Status.")

    def test_non_user_status_is_shown_with_its_mode(self) -> None:
        controller = ShutterControllerSim()
        controller.ring_status = 2
        window = RingCurrentToolWindow(controller)
        self.addCleanup(window.close)
        self._wait_for_read(window)
        self.assertEqual(window._status.text(), "Ring status: 2 — Injection")
        controller.ring_status = 5
        window.read_current()
        self._wait_for_read(window)
        self.assertEqual(window._status.text(), "Ring status: 5 — User (Top-UP)")

    def test_status_failure_preserves_successful_current_read(self) -> None:
        class Reader:
            def read_ring_current(self):
                return "449.8"

            def read_ring_status(self):
                raise RuntimeError("Ring.Status unavailable")

        window = RingCurrentToolWindow(Reader())
        self.addCleanup(window.close)
        self._wait_for_read(window)
        self.assertEqual(window._value.text(), "449.8 mA")
        self.assertEqual(window._status.text(), "Ring status: Reading unavailable")
        self.assertEqual(window._status_detail.text(), "Ring.Status unavailable")

    def test_current_failure_preserves_successful_status_read_off_ui_thread(self) -> None:
        calls = []

        class Reader:
            def read_ring_current(self):
                calls.append(threading.get_ident())
                raise RuntimeError("Ring.DCCT unavailable")

            def read_ring_status(self):
                calls.append(threading.get_ident())
                return 4

        window = RingCurrentToolWindow(Reader())
        self.addCleanup(window.close)
        self._wait_for_read(window)
        self.assertEqual(window._value.text(), "Reading unavailable")
        self.assertEqual(window._detail.text(), "Ring.DCCT unavailable")
        self.assertEqual(window._status.text(), "Ring status: 4 — User")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call != threading.get_ident() for call in calls))


if __name__ == "__main__":
    unittest.main()
