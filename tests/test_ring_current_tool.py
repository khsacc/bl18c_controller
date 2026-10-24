from __future__ import annotations

import os
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

    def test_simulated_read_completes_off_the_ui_thread(self) -> None:
        window = RingCurrentToolWindow(ShutterControllerSim())
        self.addCleanup(window.close)
        deadline = time.monotonic() + 2
        while window.is_busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(window.is_busy)
        self.assertEqual(window._value.text(), "450 (simulated)")


if __name__ == "__main__":
    unittest.main()
