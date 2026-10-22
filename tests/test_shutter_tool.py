from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from tools.shutter_tool import ShutterToolWindow
from utils.shutter import ShutterControllerSim


class ShutterToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_for_idle(self, window: ShutterToolWindow) -> None:
        deadline = time.monotonic() + 2
        while window.is_busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(window.is_busy)

    def test_open_and_close_use_confirmed_position_without_confirmation(self) -> None:
        controller = ShutterControllerSim()
        window = ShutterToolWindow(controller)
        self.addCleanup(window.close)
        self._wait_for_idle(window)
        self.assertTrue(window._toggle_button.isEnabled())
        self.assertTrue(controller.get_status())

        window._toggle_button.click()
        self._wait_for_idle(window)
        self.assertFalse(controller.get_status())

        window._toggle_button.click()
        self._wait_for_idle(window)
        self.assertTrue(controller.get_status())
        window.close()
        self.assertFalse(controller._listeners)

    def test_unknown_status_disables_toggle(self) -> None:
        window = ShutterToolWindow(ShutterControllerSim())
        self.addCleanup(window.close)
        self._wait_for_idle(window)
        window._on_status_received(None)
        self.assertFalse(window._toggle_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
