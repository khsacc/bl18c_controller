from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import cv2
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QPushButton

from apps.exp_scheduler.actions import StartFollowingAction, TakeSpectrumAction
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.gasket_units import legacy_tolerance_um, load_stage_um_per_pixel
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

    def test_global_ruby_search_settings_round_trip(self):
        window = self._window()
        window._spectrum_search_chk.setChecked(True)
        window._spectrum_search_when_combo.setCurrentIndex(1)
        window._spectrum_search_axes[3][0].setChecked(True)
        window._spectrum_search_axes[4][0].setChecked(False)
        window._spectrum_search_axes[3][1].setValue(6.0)
        saved = window._spectrum_ui_to_dict()
        self.assertTrue(saved["search_enabled"])
        self.assertEqual(saved["search_when"], "on_low_quality")
        self.assertEqual(saved["search_channels"], [3, 5])
        self.assertEqual(saved["search_range_ch3_um"], 6.0)

        restored = self._window()
        restored._spectrum_dict_to_ui(saved)
        self.assertEqual(restored._spectrum_ui_to_dict()["search_channels"], [3, 5])
        self.assertEqual(restored._spectrum_ui_to_dict()["search_range_ch3_um"], 6.0)

    def test_position_controls_and_ruby_settings_are_separate_from_follow_settings(self):
        window = self._window()
        window._sequence.actions = [TakeSpectrumAction()]
        window._update_follow_panel_visibility()

        self.assertFalse(window._follow_panel.isHidden())
        self.assertFalse(window._ruby_fluorescence_group.isHidden())
        self.assertFalse(window._follow_panel.isAncestorOf(window._ruby_fluorescence_group))
        self.assertTrue(window._sample_positions_panel.isAncestorOf(window._btn_load_ref))
        self.assertFalse(any(
            button.text() == "Capture Now"
            for button in window._sample_positions_panel.findChildren(QPushButton)
        ))
        self.assertTrue(window._sample_positions_panel.isAncestorOf(window._follow_method_combo))
        self.assertTrue(window._sample_positions_panel.isAncestorOf(window._gasket_tolerance_spin))
        self.assertTrue(window._sample_positions_panel.isAncestorOf(window._gasket_average_frames_spin))
        self.assertTrue(window._sample_positions_panel.isAncestorOf(window._gasket_allow_scale_chk))
        self.assertFalse(window._follow_panel.isAncestorOf(window._follow_method_combo))

        window._sequence.actions = [StartFollowingAction()]
        window._update_follow_panel_visibility()
        self.assertFalse(window._follow_panel.isHidden())
        self.assertTrue(window._ruby_fluorescence_group.isHidden())

    def test_position_tracking_selection_invalidates_validation(self):
        window = self._window()
        window._validated = True
        window._certificate = object()
        window._btn_run.setEnabled(True)
        window._follow_method_combo.setCurrentIndex(1)
        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())
        window._validated = True
        window._certificate = object()
        window._btn_run.setEnabled(True)
        window._gasket_average_frames_spin.setValue(6)
        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())
        window._validated = True
        window._certificate = object()
        window._btn_run.setEnabled(True)
        window._gasket_allow_scale_chk.setChecked(True)
        self.assertIsNone(window._certificate)
        self.assertFalse(window._btn_run.isEnabled())

    def test_existing_tracking_setting_loads_in_sample_positions(self):
        window = self._window()
        window._follow_dict_to_ui({
            "tracking_method": "gasket_beta", "gasket_tolerance_px": 6,
            "gasket_average_frames": 10,
        })
        self.assertEqual(window._follow_method_combo.currentData(), "gasket_beta")
        expected_um = int(legacy_tolerance_um(6, load_stage_um_per_pixel()) * 10) / 10
        self.assertEqual(window._gasket_tolerance_spin.value(), expected_um)
        self.assertEqual(window._gasket_tolerance_spin.suffix(), " µm")
        self.assertEqual(window._gasket_average_frames_spin.value(), 10)
        self.assertEqual(window._gasket_average_frames_spin.maximum(), 10)
        self.assertEqual(window._follow_ui_to_dict()["tracking_method"], "gasket_beta")
        self.assertEqual(window._follow_ui_to_dict()["gasket_tolerance_um"], expected_um)
        self.assertNotIn("gasket_tolerance_px", window._follow_ui_to_dict())
        self.assertEqual(window._follow_ui_to_dict()["gasket_average_frames"], 10)
        self.assertFalse(window._gasket_allow_scale_chk.isChecked())
        window._gasket_allow_scale_chk.setChecked(True)
        self.assertTrue(window._build_global_follow().gasket_allow_uniform_scale)
        self.assertTrue(window._follow_ui_to_dict()["gasket_allow_uniform_scale"])
        window._follow_dict_to_ui({"tracking_method": "gasket_beta",
                                   "gasket_allow_uniform_scale": True})
        self.assertTrue(window._gasket_allow_scale_chk.isChecked())
        window._follow_dict_to_ui({"tracking_method": "gasket_beta"})
        self.assertEqual(window._gasket_average_frames_spin.value(), 5)
        self.assertFalse(window._gasket_allow_scale_chk.isChecked())

    def test_unconverted_legacy_tolerance_is_preserved_until_calibration_returns(self):
        window = self._window()
        with mock.patch.object(scheduler_window, "load_stage_um_per_pixel",
                               side_effect=OSError("calibration unavailable")):
            window._follow_dict_to_ui({"tracking_method": "gasket_beta",
                                       "gasket_tolerance_px": 6})
        self.assertIsNone(window._build_global_follow().gasket_tolerance_um)
        self.assertEqual(window._follow_ui_to_dict()["gasket_tolerance_px"], 6)
        self.assertEqual(window._gasket_tolerance_spin.text(), "Legacy px limit")
        window._gasket_tolerance_spin.setValue(7.0)
        self.assertEqual(window._follow_ui_to_dict()["gasket_tolerance_um"], 7.0)

    def test_replacing_reference_in_position_setup_clears_gasket_mark(self):
        window = self._window()
        window._gasket_ellipse = {"cx": 315, "cy": 230, "rx": 190, "ry": 175}
        window._gasket_reference_digest = "old"
        dialog = mock.Mock()
        dialog.xrd_position = (1, 2, 3)
        dialog.ruby_positions = {}
        dialog.reference_changed = True
        dialog.reference_path = Path("new_reference.png")
        dialog.reference_frame = np.zeros((40, 40, 3), np.uint8)
        dialog.save_directory = Path(".")
        dialog.gasket_changed = False
        with (mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.SamplePositionsDialog",
                         return_value=dialog),
              mock.patch.object(window, "_set_setting")):
            window._open_sample_positions()
        self.assertIsNone(window._gasket_ellipse)
        self.assertIsNone(window._gasket_reference_digest)
        self.assertEqual(window._build_global_follow().reference_path, "new_reference.png")
        self.assertEqual(window._sample_xrd_position, (1, 2, 3))

    def test_loading_reference_image_clears_positions(self):
        window = self._window()
        window._sample_xrd_position = (10, 20, 30)
        window._ruby_positions = {"Ruby A": {"ch3": 1, "ch4": 2, "ch5": 3}}
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        with (
            mock.patch.object(scheduler_window.QFileDialog, "getOpenFileName",
                              return_value=("saved_reference.png", "PNG Image (*.png)")),
            mock.patch("cv2.imread", return_value=frame),
            mock.patch.object(window, "_set_setting"),
            mock.patch.object(window, "_reset_validation"),
        ):
            window._on_load_ref_file()

        self.assertIsNone(window._sample_xrd_position)
        self.assertEqual(window._ruby_positions, {})
        self.assertEqual(window._build_global_follow().reference_path, "saved_reference.png")

    def test_reference_image_cannot_change_during_a_run(self):
        window = self._window()
        window._runner = SimpleNamespace(isRunning=lambda: True)
        with (
            mock.patch.object(scheduler_window.QMessageBox, "warning") as warning,
            mock.patch.object(scheduler_window.QFileDialog, "getOpenFileName") as chooser,
        ):
            window._on_load_ref_file()
        warning.assert_called_once()
        chooser.assert_not_called()

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

    def test_capture_reference_adds_timestamp_without_overwriting(self):
        class Controller:
            def get_is_moving(self):
                return False

            def get_ch_pos(self, ch):
                return {3: 100, 4: 200, 5: 300}[ch]

        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        original = Path("xrd_reference_2026-10-24_12-34-56.png")
        replacement = original.with_name(original.stem + "_2.png")
        dialog = SamplePositionsDialog(
            Controller(), None, None, None, {},
            borrowed_frame=lambda: frame, save_directory=Path.cwd(),
        )
        self.addCleanup(dialog.close)
        with (
            mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.datetime") as clock,
            mock.patch.object(scheduler_window.QFileDialog, "getSaveFileName",
                              return_value=(str(original), "PNG Image (*.png)")) as chooser,
            mock.patch.object(Path, "exists", autospec=True,
                              side_effect=lambda path: path.name == original.name),
            mock.patch("cv2.imwrite", return_value=True) as write,
        ):
            clock.now.return_value.strftime.return_value = "2026-10-24_12-34-56"
            dialog._capture_xrd()

        self.assertEqual(Path(chooser.call_args.args[2]).name, replacement.name)
        write.assert_called_once_with(str(replacement), mock.ANY)
        self.assertEqual(dialog.reference_path, replacement)
        self.assertIn(replacement.name, dialog.status.text())

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

    def test_gasket_method_confirms_xrd_position_for_ruby_offsets(self):
        from apps.interactive_camera.xray_beam import XrayBeamPosition

        positions = {3: 40000, 4: 20000, 5: 30000, 6: 1, 7: 99, 8: 3}

        class Controller:
            def get_is_moving(self):
                return False

            def get_ch_pos(self, ch):
                return positions[ch]

            def get_cached_states(self, channels, max_age):
                return {}

        reference = np.full((480, 640, 3), 105, np.uint8)
        live = reference.copy()
        cv2.ellipse(reference, (315, 230), (190, 175), 0, 0, 360, (130, 130, 130), -1)
        cv2.ellipse(live, (320, 226), (190, 175), 0, 0, 360, (90, 90, 90), -1)
        dlg = SamplePositionsDialog(
            Controller(), reference, "reference.png", None, {"Old": {"ch3": 0, "ch4": 1, "ch5": 2}},
            borrowed_frame=lambda: live, tracking_method="gasket_beta",
            gasket_ellipse={"cx": 315, "cy": 230, "rx": 190, "ry": 175},
            gasket_reference_digest="expected", gasket_tolerance_px=None,
            gasket_tolerance_um=8,
            # Sample 20 px right of and 20 px below the rim centre.
            gasket_sample_point={"x": 335, "y": 250}, ruby_offset_origin="xrd_position",
        )
        self.addCleanup(dlg.close)
        # The live rim at (320, 226) puts the sample on this beam.
        dlg._xray_beam = SimpleNamespace(
            get=lambda: XrayBeamPosition(340, 246, 640, 480, {6: 1, 7: 2, 8: 3}))
        dlg.frames.put(live)
        with (mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.reference_digest",
                         return_value="expected"),
              mock.patch("apps.exp_scheduler.ui.sample_positions_dialog._load_matrix_inv",
                         return_value=np.eye(2)),
              mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.QMessageBox.warning") as warning):
            dlg._confirm_xrd()
            warning.assert_not_called()
            self.assertEqual(dlg.xrd_position, (40000, 20000, 30000))

            positions.update({3: 40010, 4: 20100, 5: 29900})
            dlg.name.setText("Ruby A")
            dlg._record_ruby()
            warning.assert_not_called()
        # The gasket centre is on the beam at Ch4/Ch5 = 20000+20, 30000+20.
        self.assertEqual(dlg.ruby_offset_origin, "gasket_centre")
        self.assertEqual(dlg.ruby_positions, {"Ruby A": {"ch3": 10, "ch4": 80, "ch5": -120}})

        # Five horizontal pixels are 10 µm on Ch4 with this calibration.
        dlg._xray_beam = SimpleNamespace(
            get=lambda: XrayBeamPosition(345, 246, 640, 480, {6: 1, 7: 2, 8: 3}))
        with (mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.reference_digest",
                         return_value="expected"),
              mock.patch("apps.exp_scheduler.ui.sample_positions_dialog._load_matrix_inv",
                         return_value=np.eye(2)),
              mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.QMessageBox.warning") as warning):
            dlg._confirm_xrd()
        self.assertIn("10.0 µm", warning.call_args.args[2])
        self.assertIn("tolerance 8 µm", warning.call_args.args[2])

    def test_gasket_confirmation_rejects_a_moved_microscope(self):
        from apps.interactive_camera.xray_beam import XrayBeamPosition

        class Controller:
            def get_is_moving(self):
                return False

            def get_ch_pos(self, ch):
                return {3: 0, 4: 0, 5: 0, 6: 1, 7: 2, 8: 99}[ch]

            def get_cached_states(self, channels, max_age):
                return {}

        reference = np.full((480, 640, 3), 105, np.uint8)
        cv2.ellipse(reference, (315, 230), (190, 175), 0, 0, 360, (130, 130, 130), -1)
        dlg = SamplePositionsDialog(
            Controller(), reference, "reference.png", None, {},
            borrowed_frame=lambda: reference, tracking_method="gasket_beta",
            gasket_ellipse={"cx": 315, "cy": 230, "rx": 190, "ry": 175},
            gasket_reference_digest="expected", gasket_sample_point={"x": 315, "y": 230},
        )
        self.addCleanup(dlg.close)
        dlg._xray_beam = SimpleNamespace(
            get=lambda: XrayBeamPosition(315, 230, 640, 480, {6: 1, 7: 2, 8: 3}))
        dlg.frames.put(reference)
        with (mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.reference_digest",
                         return_value="expected"),
              mock.patch("apps.exp_scheduler.ui.sample_positions_dialog.QMessageBox.warning") as warning):
            dlg._confirm_xrd()
        self.assertIn("Ch8 3→99", warning.call_args.args[2])
        self.assertIsNone(dlg.xrd_position)

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

    def test_legacy_single_ruby_offset_remains_available_as_default(self):
        window = self._window()
        window._spectrum_dict_to_ui({
            "xrd_reference_ch4_pulse": 20000,
            "xrd_reference_ch5_pulse": 30000,
            "offset_ch4_pulse": 10,
            "offset_ch5_pulse": -20,
        })
        self.assertEqual(window._spectrum_position_names(), ("Default",))
        self.assertEqual(window._build_global_spectrum().offset_for("Default"),
                         {3: 0, 4: 10, 5: -20})

    def test_global_exposure_round_trip(self):
        window = self._window()
        self.assertEqual(window._build_global_spectrum().exposure_time_s, 0.1)
        self.assertEqual(window._build_global_spectrum().r_squared_min, 0.95)
        self.assertEqual(window._build_global_spectrum().em_gain_max, 10)
        self.assertEqual(window._build_global_spectrum().baseline_model, "linear")
        self.assertEqual(window._build_global_spectrum().speed, "M")
        window._spectrum_exposure_spin.setValue(3.25)
        window._spectrum_r2_spin.setValue(0.97)
        window._spectrum_em_gain_max_spin.setValue(8)
        window._spectrum_max_accum_spin.setValue(8)
        window._spectrum_fit_combo.setCurrentText("Pseudo Voigt")
        window._spectrum_baseline_combo.setCurrentIndex(
            window._spectrum_baseline_combo.findData("quadratic"))
        window._spectrum_speed_combo.setCurrentText("H")
        data = window._spectrum_ui_to_dict()
        restored = self._window()
        restored._spectrum_dict_to_ui(data)
        settings = restored._build_global_spectrum()
        self.assertEqual(settings.exposure_time_s, 3.25)
        self.assertEqual(settings.r_squared_min, 0.97)
        self.assertEqual(settings.em_gain_max, 8)
        self.assertEqual(settings.max_accumulations, 8)
        self.assertEqual(settings.fit_function, "Pseudo Voigt")
        self.assertEqual(settings.baseline_model, "quadratic")
        self.assertEqual(settings.speed, "H")

        restored._spectrum_dict_to_ui({"fit_function": "Moffat"})
        self.assertEqual(restored._build_global_spectrum().baseline_model, "linear")
        self.assertEqual(restored._build_global_spectrum().speed, "M")

    def test_legacy_snr_setting_is_ignored(self):
        window = self._window()
        window._spectrum_dict_to_ui({"snr_min": 20.0})
        self.assertEqual(window._build_global_spectrum().r_squared_min, 0.95)
        self.assertNotIn("snr_min", window._spectrum_ui_to_dict())

    def test_step_editor_builds_position_and_exposure(self):
        page = _page_take_spectrum(("Ruby A", "Ruby B"))
        self.addCleanup(page.widget.deleteLater)
        original = TakeSpectrumAction(
            position_name="Ruby B", exposure_time_s=2.5,
        )
        page.fill(original)
        self.assertEqual(page.build(), original)
        default_page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(default_page.widget.deleteLater)
        self.assertIsNone(default_page.build().exposure_time_s)

    def test_step_editor_preserves_ruby_pressure_stop(self):
        page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(page.widget.deleteLater)
        original = TakeSpectrumAction(
            position_name="Ruby A", stop_pressure_target_gpa=1.0,
            stop_pressure_tolerance_gpa=0.1,
        )
        page.fill(original)
        self.assertEqual(page.build(), original)

    def test_step_editor_selects_registered_position_or_all_positions(self):
        page = _page_take_spectrum(("Ruby A", "Ruby B"))
        self.addCleanup(page.widget.deleteLater)
        position = next(combo for combo in page.widget.findChildren(QComboBox)
                        if combo.findText("Ruby A") >= 0)
        all_positions = next(check for check in page.widget.findChildren(QCheckBox)
                             if check.text() == "Measure all registered ruby positions")
        self.assertFalse(position.isEditable())
        self.assertEqual([position.itemText(i) for i in range(position.count())],
                         ["Ruby A", "Ruby B"])
        position.setCurrentText("Ruby B")
        self.assertEqual(page.build().position_name, "Ruby B")
        all_positions.setChecked(True)
        self.assertFalse(position.isEnabled())
        self.assertTrue(page.build().all_positions)
        page.fill(TakeSpectrumAction(all_positions=True))
        self.assertTrue(page.build().all_positions)

    def test_step_editor_rejects_missing_position(self):
        page = _page_take_spectrum()
        self.addCleanup(page.widget.deleteLater)
        self.assertIsNone(page.build())
        page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(page.widget.deleteLater)
        page.fill(TakeSpectrumAction(position_name="Removed position"))
        self.assertIsNone(page.build())
        position = next(combo for combo in page.widget.findChildren(QComboBox)
                        if combo.findText("Ruby A") >= 0)
        self.assertEqual(position.count(), 1)

    def test_step_editor_preserves_nearby_channels_ranges_and_backend_steps(self):
        page = _page_take_spectrum(("Ruby A",))
        self.addCleanup(page.widget.deleteLater)
        action = TakeSpectrumAction(
            position_name="Ruby A", search_best_position_nearby=True,
            search_when="on_low_quality",
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
