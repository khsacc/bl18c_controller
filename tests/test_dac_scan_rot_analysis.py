"""Post-scan rotation analysis with synthetic profiles and no hardware."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from apps.dac_scan import dac_scan_rot_app as rot

APP = QApplication.instance() or QApplication([])


class RotationAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.window = rot.DacScanRotWindow(debug=True)
        self.thetas = [-90.0, -30.0, 0.0, 45.0, 90.0]
        self.pulses = np.arange(300, 701, 4, dtype=float)
        self.window._fit_ch10_min_spin.setValue(300)
        self.window._fit_ch10_max_spin.setValue(700)
        self.window._reset_state(self.thetas)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.window._worker = self.window._move_worker = self.window._post_scan_worker = None
        self.window._correction_applied = True
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def populate(self, completed=None):
        for theta in self.thetas:
            centre = float(rot._rot_model(theta, 18, -12, 500))
            values = rot._aperture_model(self.pulses, .5, centre - 60, centre + 60, 6, .1)
            # Initialise the plot items through the same path as scan signals.
            self.window._on_point_measured(theta, int(self.pulses[0]), float(values[0]))
            self.window._scan_data[theta] = (self.pulses.copy(), values)
            self.window._data_curves[theta].setData(self.pulses, values)
            if completed is None or theta in completed:
                self.window._on_theta_completed(theta)
        self.window._run_analysis()

    def use(self, theta, included):
        row = self.window._angle_rows[theta]
        self.window._angle_table.item(row, 0).setCheckState(
            Qt.CheckState.Checked if included else Qt.CheckState.Unchecked
        )

    def range_spin(self, theta, column):
        return self.window._angle_table.cellWidget(self.window._angle_rows[theta], column)

    def test_exclusion_refits_only_selected_centres_and_preserves_raw_data(self):
        self.populate()
        original = self.window._scan_data[45.0][1].copy()
        self.use(45.0, False)
        self.assertIsNone(self.window._fit_A)
        self.assertIsNone(self.window._analysis_results)
        self.assertFalse(self.window._apply_btn.isEnabled())
        self.assertEqual(self.window._move_ch3, 0)
        self.window._run_analysis()
        np.testing.assert_allclose([self.window._fit_A, self.window._fit_B, self.window._fit_C],
                                   [18, -12, 500], atol=.01)
        self.assertNotIn(45.0, self.window._analysis_results["included_theta_deg"])
        self.assertIn(45.0, self.window._aperture_centers)
        np.testing.assert_array_equal(self.window._excluded_scatter.getData()[0], [45.0])
        np.testing.assert_array_equal(self.window._scan_data[45.0][1], original)
        self.use(45.0, True)
        self.window._run_analysis()
        self.assertEqual(len(self.window._analysis_results["included_theta_deg"]), 5)

    def test_per_angle_range_removes_contaminated_tail(self):
        self.populate()
        x, y = self.window._scan_data[45.0]
        y[x >= 650] += 4
        self.range_spin(45.0, 2).setValue(360)
        self.range_spin(45.0, 3).setValue(640)
        self.assertNotIn(45.0, self.window._aperture_centers)
        self.assertNotIn(45.0, self.window._center_vlines)
        self.assertIsNone(self.window._fit_A)
        with mock.patch.object(rot, "_fit_aperture", wraps=rot._fit_aperture) as fit:
            self.window._run_analysis()
        fitted_x = [call.args[0] for call in fit.call_args_list]
        self.assertEqual([len(x) for x in fitted_x], [101, 101, 101, 71, 101])
        self.assertEqual(self.window._angle_fit_range(0.0), (300, 700))
        np.testing.assert_allclose([self.window._fit_A, self.window._fit_B], [18, -12], atol=.01)

    def test_invalid_range_or_too_few_points_discards_old_fit_and_can_recover(self):
        self.populate()
        self.range_spin(0.0, 2).setValue(700)
        self.window._run_analysis()
        row = self.window._angle_rows[0.0]
        self.assertIn("Invalid range", self.window._angle_table.item(row, 5).text())
        self.assertNotIn(0.0, self.window._aperture_centers)
        self.assertIsNone(self.window._get_center_at_zero())
        self.range_spin(0.0, 2).setValue(696)
        self.window._run_analysis()
        self.assertIn("5 finite points", self.window._angle_table.item(row, 5).text())
        self.assertTrue(self.window._run_analysis_btn.isEnabled())
        self.range_spin(0.0, 2).setValue(300)
        self.window._run_analysis()
        self.assertIsNotNone(self.window._get_center_at_zero())

    def test_fewer_than_three_selected_angles_clears_correction(self):
        self.populate()
        for theta in self.thetas[:3]:
            self.use(theta, False)
        self.window._run_analysis()
        self.assertIsNone(self.window._fit_A)
        self.assertIsNone(self.window._analysis_results)
        self.assertFalse(self.window._apply_btn.isEnabled())
        self.assertEqual(self.window._move_ch3, 0)
        self.assertIn("at least 3", self.window._status_label.text())

    def test_fit_failure_clears_all_stale_curves_and_allows_retry(self):
        self.populate()
        with mock.patch.object(rot, "_fit_aperture", side_effect=RuntimeError("No convergence")):
            self.window._run_analysis()
        self.assertEqual(self.window._aperture_centers, {})
        self.assertEqual(self.window._center_vlines, {})
        self.assertIsNone(self.window._analysis_results)
        self.assertTrue(self.window._run_analysis_btn.isEnabled())
        for row in self.window._angle_rows.values():
            self.assertIn("No convergence", self.window._angle_table.item(row, 5).text())
        self.window._run_analysis()
        self.assertAlmostEqual(self.window._fit_A, 18, places=2)

    def test_rotation_fit_failure_does_not_retain_previous_correction(self):
        self.populate()
        with mock.patch.object(rot, "_fit_rotation", side_effect=ValueError("Singular fit")):
            self.window._run_analysis()
        self.assertIsNone(self.window._analysis_results)
        self.assertFalse(self.window._apply_btn.isEnabled())
        self.assertIn("Singular fit", self.window._status_label.text())

    def test_aborted_partial_angle_is_not_used(self):
        self.populate(completed=set(self.thetas[:3]))
        with mock.patch.object(rot.log_prefs, "should_save", return_value=False):
            self.window._on_scan_aborted()
        self.assertEqual(self.window._scan_outcome, "aborted")
        self.assertEqual(self.window._analysis_results["included_theta_deg"], self.thetas[:3])
        self.assertNotIn(45.0, self.window._aperture_centers)

    def test_excluded_zero_angle_cannot_supply_centre_move(self):
        self.populate()
        self.use(0.0, False)
        self.assertIsNone(self.window._get_center_at_zero())
        self.assertFalse(self.window._return_centre_btn.isEnabled())

    def test_bulk_range_applies_to_all_angles(self):
        self.populate()
        self.window._fit_ch10_min_spin.setValue(350)
        self.window._fit_ch10_max_spin.setValue(650)
        self.window._apply_range_btn.click()
        for theta in self.thetas:
            self.assertEqual(self.window._angle_fit_range(theta), (350, 650))
        self.assertIsNone(self.window._fit_A)
        self.window._run_analysis()
        self.assertAlmostEqual(self.window._fit_A, 18, places=2)

    def test_analysis_controls_locked_during_each_worker(self):
        self.populate()
        for attr in ("_worker", "_move_worker", "_post_scan_worker"):
            with self.subTest(worker=attr):
                worker = mock.Mock()
                worker.isRunning.return_value = True
                setattr(self.window, attr, worker)
                self.window._update_action_btns()
                self.assertFalse(self.window._angle_table.isEnabled())
                self.assertFalse(self.window._run_analysis_btn.isEnabled())
                self.assertFalse(self.window._apply_range_btn.isEnabled())
                with mock.patch.object(self.window, "_run_analysis") as analyse:
                    self.window._on_rerun_analysis()
                    analyse.assert_not_called()
                worker.isRunning.return_value = False
                self.window._update_action_btns()
                self.assertTrue(self.window._angle_table.isEnabled())
                setattr(self.window, attr, None)

    def test_rerun_after_correction_cannot_apply_same_correction_twice(self):
        self.populate()
        self.window._correction_applied = True
        self.window._run_analysis()
        self.assertFalse(self.window._apply_btn.isEnabled())

    def test_save_failure_keeps_valid_analysis_and_reports_error(self):
        self.populate()
        self.window._scan_outcome = "completed"
        with mock.patch.object(rot.log_prefs, "should_save", return_value=True), \
                mock.patch.object(self.window, "_save_details", side_effect=OSError("Disk full")):
            self.window._on_rerun_analysis()
        self.assertIsNotNone(self.window._analysis_results)
        self.assertIn("Disk full", self.window._status_label.text())

    def test_non_finite_aperture_fit_is_rejected(self):
        self.populate()
        with mock.patch.object(rot, "_fit_aperture", return_value=(np.nan, 0, 0, np.zeros(5), False)):
            self.window._run_analysis()
        self.assertEqual(self.window._aperture_centers, {})
        self.assertIsNone(self.window._analysis_results)

    def test_equivalent_angles_cannot_produce_a_rotation_correction(self):
        self.thetas = [0.0, 360.0, 720.0]
        self.window._reset_state(self.thetas)
        self.populate()
        self.assertIsNone(self.window._analysis_results)
        self.assertIn("independent rotation angles", self.window._status_label.text())

    def test_new_scan_resets_per_angle_settings_and_completed_profiles(self):
        self.populate()
        self.use(0.0, False)
        self.range_spin(0.0, 2).setValue(360)
        self.window._reset_state(self.thetas)
        self.assertEqual(self.window._completed_thetas, set())
        self.assertEqual(self.window._aperture_centers, {})
        self.assertIsNone(self.window._analysis_results)
        self.assertTrue(self.window._angle_is_included(0.0))
        self.assertEqual(self.window._angle_fit_range(0.0), (300, 700))

    def test_simulated_scan_delivers_results_then_unlocks_table(self):
        from utils.stage.control_stage_sim import PM16CControllerSim
        stage = PM16CControllerSim()
        stage.connect()
        self.addCleanup(stage.disconnect)
        self.window._controller = stage
        self.window._gpib_reader = rot.GpibReaderRotSim(
            true_A=18, true_B=-12, true_C=0, aperture_half_width=60,
            edge_width=10, noise_level=0,
        )
        self.window._theta_edit.setText("-20, 0, 20, 40")
        self.window._half_range_spin.setValue(400)
        self.window._step_spin.setValue(20)
        self.window._settle_spin.setValue(0)
        self.window._accum_spin.setValue(1)
        self.window._return_zero_chk.setChecked(False)
        for button in self.window._speed_grp.buttons():
            button.setChecked(button.property("speed_val") == "H")
        with mock.patch.object(rot.QMessageBox, "question", return_value=rot.QMessageBox.StandardButton.Yes), \
                mock.patch.object(rot.log_prefs, "should_save", return_value=False), \
                mock.patch.object(rot, "play_current_sound"):
            self.window._on_start()
            self.assertFalse(self.window._angle_table.isEnabled())
            try:
                deadline = time.monotonic() + 50
                while self.window._worker.isRunning() and time.monotonic() < deadline:
                    APP.processEvents()
                    time.sleep(.005)
                self.assertFalse(self.window._worker.isRunning(), "Simulated scan timed out")
                APP.processEvents()
            finally:
                if self.window._worker.isRunning():
                    self.window._worker.abort()
                    stage.normal_stop()
                    self.window._worker.wait(5000)
                    APP.processEvents()
        self.assertEqual(self.window._scan_outcome, "completed")
        self.assertEqual(self.window._completed_thetas, {-20, 0, 20, 40})
        self.assertTrue(self.window._angle_table.isEnabled())
        self.assertTrue(self.window._run_analysis_btn.isEnabled())
        self.assertTrue(stage.is_motion_available())
        np.testing.assert_allclose([self.window._fit_A, self.window._fit_B, self.window._fit_C],
                                   [18, -12, 0], atol=.1)
        with mock.patch.object(stage, "move_ch_absolute") as move:
            self.use(40.0, False)
            self.window._on_rerun_analysis()
            move.assert_not_called()

    def test_rerun_saves_settings_and_preserves_arrays_even_when_rotation_fails(self):
        self.populate()
        self.window._scan_start_time = datetime(2026, 11, 25, 12, 0, 0)
        self.window._scan_theta_list = self.thetas
        self.window._ch10_pulses = self.pulses.astype(int).tolist()
        self.window._scan_outcome = "completed"
        self.use(45.0, False)
        self.range_spin(0.0, 2).setValue(360)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(rot.log_prefs, "get_app_dir", return_value=Path(directory)), \
                mock.patch.object(rot.log_prefs, "should_save", return_value=True):
            self.window._run_analysis_btn.click()
            stem = Path(directory) / "20261125_120000_rot"
            meta = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(meta["outcome"], "completed")
            self.assertFalse(meta["angle_analysis"][3]["included"])
            self.assertEqual(meta["angle_analysis"][2]["fit_min_pulse"], 360)
            self.assertNotIn(45.0, meta["analysis_results"]["included_theta_deg"])
            with np.load(stem.with_suffix(".npz")) as arrays:
                np.testing.assert_array_equal(arrays["transmitted_map"][3], self.window._scan_data[45.0][1])
            self.assertGreater(stem.with_suffix(".png").stat().st_size, 0)
            for theta in self.thetas[:3]:
                self.use(theta, False)
            self.window._run_analysis_btn.click()
            meta = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertIsNone(meta["analysis_results"])


if __name__ == "__main__":
    unittest.main()
