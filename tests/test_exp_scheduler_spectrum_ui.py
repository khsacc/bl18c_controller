from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.actions import TakeSpectrumAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.ui import scheduler_window
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow

_app = QApplication.instance() or QApplication([])


class SpectrumSettingsUiTests(unittest.TestCase):
    def _window(self, controller=None):
        missing_settings = Path("__missing_exp_scheduler_test_settings__.json")
        with mock.patch.object(scheduler_window, "_SETTINGS_PATH", missing_settings):
            window = ExperimentalSchedulerWindow(DeviceContext(controller=controller))
        self.addCleanup(window.deleteLater)
        return window

    def test_ruby_settings_are_nested_in_follow_settings(self):
        window = self._window()
        window._sequence.actions = [TakeSpectrumAction()]
        window._update_follow_panel_visibility()

        self.assertFalse(window._follow_panel.isHidden())
        self.assertFalse(window._ruby_fluorescence_group.isHidden())
        self.assertTrue(
            window._follow_panel.isAncestorOf(window._ruby_fluorescence_group)
        )

    def test_capture_now_records_xrd_reference_with_photo(self):
        class Controller:
            @staticmethod
            def get_cached_states(channels, max_age):
                return {
                    4: SimpleNamespace(position=1234),
                    5: SimpleNamespace(position=-5678),
                }

        window = self._window(Controller())
        window._sequence.actions = [TakeSpectrumAction()]
        frame = np.zeros((4, 4, 3), dtype=np.uint8)

        with (
            mock.patch.object(window, "_borrow_camera_frame", return_value=frame),
            mock.patch.object(
                scheduler_window.QFileDialog,
                "getSaveFileName",
                return_value=("reference.png", "PNG Image (*.png)"),
            ),
            mock.patch("cv2.imwrite", return_value=True),
            mock.patch.object(window, "_set_setting"),
            mock.patch.object(window, "_reset_validation"),
        ):
            window._on_capture_now()

        self.assertEqual(window._spectrum_xrd_reference, (1234, -5678))


if __name__ == "__main__":
    unittest.main()
