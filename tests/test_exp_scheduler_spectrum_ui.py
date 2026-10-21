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
from apps.exp_scheduler.ui.step_editor import _page_take_spectrum
from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
from apps.exp_scheduler.ui.sample_positions_dialog import SamplePositionsDialog

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

    def test_dialog_records_multiple_named_ch3_ch4_ch5_offsets(self):
        class Controller:
            position = [40000, 20000, 30000]

            def get_is_moving(self):
                return False

            def get_cached_states(self, channels, max_age):
                return {ch: SimpleNamespace(position=self.position[ch - 3]) for ch in channels}

            def get_ch_pos(self, ch):
                return self.position[ch - 3]

        controller = Controller()
        frame = np.random.default_rng(1).integers(0, 255, (40, 40, 3), dtype=np.uint8)
        dlg = SamplePositionsDialog(
            controller, None, None, None, {}, borrowed_frame=lambda: frame,
        )
        self.addCleanup(dlg.close)
        dlg._on_frame(frame)
        with (
            mock.patch.object(scheduler_window.QFileDialog, "getSaveFileName",
                              return_value=("reference.png", "PNG Image (*.png)")),
            mock.patch("cv2.imwrite", return_value=True),
        ):
            dlg._capture_xrd()
        controller.position = [40005, 20010, 29980]
        dlg.name.setText("Ruby A")
        dlg._record_ruby()
        controller.position = [39990, 20100, 30020]
        dlg.name.setText("Ruby B")
        dlg._record_ruby()
        self.assertEqual(dlg.ruby_positions, {
            "Ruby A": {"ch3": 5, "ch4": 10, "ch5": -20},
            "Ruby B": {"ch3": -10, "ch4": 100, "ch5": 20},
        })

    def test_saved_xrd_photo_confirms_manual_position_without_stage_commands(self):
        class Controller:
            position = {3: 40010, 4: 20000, 5: 20000}
            reads = []

            def get_is_moving(self):
                return False

            def get_ch_pos(self, ch):
                self.reads.append(ch)
                return self.position[ch]

            def get_cached_states(self, channels, max_age):
                return {ch: SimpleNamespace(position=-999) for ch in channels}

        controller = Controller()
        reference = np.random.default_rng(5).integers(0, 255, (100, 100, 3), dtype=np.uint8)
        existing = {"Ruby A": {"ch3": 1, "ch4": 2, "ch5": 3}}
        dlg = SamplePositionsDialog(
            controller, reference, None, (40000, 20000, 20000), existing,
            borrowed_frame=lambda: reference,
        )
        self.addCleanup(dlg.close)
        dlg._confirm_xrd()
        self.assertEqual(dlg.xrd_position, (40010, 20000, 20000))
        self.assertEqual(dlg.ruby_positions, existing)
        self.assertEqual(controller.reads, [3, 4, 5, 3, 4, 5])

        shifted = np.zeros_like(reference)
        shifted[:, 5:] = reference[:, :-5]
        dlg.frames.put(shifted)
        controller.position[3] = 40020
        with mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.QMessageBox.warning") as warning:
            dlg._confirm_xrd()
        self.assertIn("Adjust the stage", warning.call_args.args[2])
        self.assertEqual(dlg.xrd_position, (40010, 20000, 20000))

    def test_record_rejects_a_stage_that_is_still_moving(self):
        class Controller:
            def get_is_moving(self):
                return True

            def get_cached_states(self, channels, max_age):
                return {}

            def get_ch_pos(self, ch):
                raise AssertionError("No position query while moving")

        dlg = SamplePositionsDialog(
            Controller(), None, None, (1, 2, 3), {}, borrowed_frame=lambda: None,
        )
        self.addCleanup(dlg.close)
        with self.assertRaisesRegex(RuntimeError, "Wait for the stage to stop"):
            dlg._read_position()

    def test_named_positions_round_trip_through_scheduler_settings(self):
        window = self._window()
        window._sample_xrd_position = (40000, 20000, 30000)
        window._spectrum_xrd_reference = (20000, 30000)
        window._ruby_positions = {
            "Ruby A": {"ch3": 5, "ch4": 10, "ch5": -20},
            "Ruby B": {"ch3": -10, "ch4": 100, "ch5": 20},
        }
        data = window._spectrum_ui_to_dict()
        restored = self._window()
        restored._spectrum_dict_to_ui(data)
        self.assertEqual(restored._ruby_positions, window._ruby_positions)
        self.assertEqual(restored._sample_xrd_position, (40000, 20000, 30000))
        self.assertEqual(restored._spectrum_position_names(), ("Ruby A", "Ruby B"))

    def test_global_exposure_and_save_directory_round_trip(self):
        window = self._window()
        window._spectrum_exposure_spin.setValue(3.25)
        window._spectrum_save_dir_edit.setText("C:/data/global-ruby")
        data = window._spectrum_ui_to_dict()
        restored = self._window()
        restored._spectrum_dict_to_ui(data)
        settings = restored._build_global_spectrum()
        self.assertEqual(settings.exposure_time_s, 3.25)
        self.assertEqual(settings.save_dir, "C:/data/global-ruby")

    def test_step_editor_builds_position_exposure_and_directory(self):
        page = _page_take_spectrum(("Ruby A", "Ruby B"))
        self.addCleanup(page.widget.deleteLater)
        original = TakeSpectrumAction(
            position_name="Ruby B", exposure_time_s=2.5,
            save_dir="C:/data/step-ruby",
        )
        page.fill(original)
        self.assertEqual(page.build(), original)
        default_page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(default_page.widget.deleteLater)
        self.assertIsNone(default_page.build().exposure_time_s)
        self.assertIsNone(default_page.build().save_dir)

    def test_step_editor_preserves_nearby_channels_ranges_and_backend_steps(self):
        page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(page.widget.deleteLater)
        action = TakeSpectrumAction(
            position_name="Ruby A", search_best_position_nearby=True,
            search_channels=(3, 5), search_range_ch3_um=8.0,
            search_range_ch5_um=6.0, search_step_ch5_um=1.0,
        )
        page.fill(action)
        self.assertEqual(page.build(), action)

    def test_step_editor_rejects_nearby_search_without_channels(self):
        page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(page.widget.deleteLater)
        validity = []
        page.widget.validity_changed.connect(validity.append)
        page.fill(TakeSpectrumAction(
            position_name="Ruby A", search_best_position_nearby=True,
            search_channels=(),
        ))
        self.assertFalse(validity[-1])
        self.assertIsNone(page.build())


if __name__ == "__main__":
    unittest.main()
