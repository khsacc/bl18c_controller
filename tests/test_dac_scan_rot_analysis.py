"""Post-scan rotation analysis with synthetic profiles and no hardware."""
import json
import os
import tempfile
import threading
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
        self.window._reset_state(self.thetas, 300, 700)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.window._worker = self.window._move_worker = self.window._post_scan_worker = None
        self.window._correction_applied = True
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def populate(self, completed=None, model=(18, -12, 500)):
        for theta in self.thetas:
            centre = float(rot._rot_model(theta, *model))
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

    def analyse_exact_centres(self, centres):
        fits = [
            (centre, centre - 60, centre + 60,
             np.array([.5, centre - 60, centre + 60, 6, .1]), False)
            for theta, centre in zip(self.thetas, centres)
            if theta in self.window._completed_thetas
        ]
        with mock.patch.object(rot, "_fit_aperture", side_effect=fits):
            self.window._run_analysis()

    def test_aligned_threshold_includes_four_pulses_and_skips_sinusoidal_fit(self):
        self.populate()
        for spread, aligned in ((0, True), (3.9, True), (4, True), (4.0001, False)):
            with self.subTest(spread=spread):
                centres = np.linspace(498, 498 + spread, len(self.thetas))
                with mock.patch.object(rot, "_fit_rotation", wraps=rot._fit_rotation) as fit:
                    self.analyse_exact_centres(centres)
                self.assertEqual(self.window._rotation_centre_aligned, aligned)
                self.assertEqual(self.window._analysis_results["rotation_centre_aligned"], aligned)
                self.assertEqual(self.window._apply_btn.isEnabled(), not aligned)
                if aligned:
                    fit.assert_not_called()
                    self.assertEqual(self.window._move_ch3, 0)
                    self.assertEqual(self.window._move_ch4, 0)
                    self.assertEqual(self.window._ana_ch3_label.text(), "Ch3: unnecessary")
                    self.assertEqual(self.window._ana_ch4_label.text(), "Ch4: unnecessary")
                    self.assertAlmostEqual(self.window._get_center_at_zero(), 498 + spread / 2)
                    self.assertTrue(self.window._return_centre_btn.isEnabled())
                else:
                    fit.assert_called_once()

    def test_excluded_outlier_does_not_prevent_aligned_classification(self):
        self.populate()
        centres = [498, 500, 501, 502, 520]
        self.analyse_exact_centres(centres)
        self.assertFalse(self.window._rotation_centre_aligned)
        self.use(90.0, False)
        self.analyse_exact_centres(centres)
        self.assertTrue(self.window._rotation_centre_aligned)
        self.assertEqual(self.window._get_center_at_zero(), 500)
        self.assertEqual(self.window._target_ch10, 500)
        self.assertFalse(self.window._apply_btn.isEnabled())

    def test_failed_or_incomplete_included_profile_cannot_be_classified_aligned(self):
        self.populate()
        self.analyse_exact_centres([500, 500, np.nan, 500, 500])
        self.assertFalse(self.window._rotation_centre_aligned)
        self.window._completed_thetas.remove(0.0)
        self.analyse_exact_centres([500] * 5)
        self.assertFalse(self.window._rotation_centre_aligned)

    def test_aligned_state_clears_on_edits_and_new_scan(self):
        self.populate(model=(0, 0, 500))
        self.assertTrue(self.window._rotation_centre_aligned)
        self.range_spin(45.0, 2).setValue(360)
        self.assertFalse(self.window._rotation_centre_aligned)
        self.assertIsNone(self.window._analysis_results)
        self.assertFalse(self.window._apply_btn.isEnabled())
        self.window._run_analysis()
        self.assertTrue(self.window._rotation_centre_aligned)
        self.use(45.0, False)
        self.assertFalse(self.window._rotation_centre_aligned)
        self.window._run_analysis()
        self.assertTrue(self.window._rotation_centre_aligned)
        self.window._reset_state(self.thetas, 300, 700)
        self.assertFalse(self.window._rotation_centre_aligned)

    def test_aligned_return_moves_only_theta_and_ch10_without_zero_degree_profile(self):
        from utils.stage.control_stage_sim import PM16CControllerSim
        self.thetas = [-30.0, 10.0, 30.0]
        self.window._reset_state(self.thetas, 300, 700)
        self.populate(model=(0, 0, 500))
        self.assertTrue(self.window._rotation_centre_aligned)
        self.assertTrue(self.window._return_centre_btn.isEnabled())
        self.assertFalse(self.window._apply_btn.isEnabled())
        stage = PM16CControllerSim()
        origin = {3: 40000, 4: 20000, rot.CH_SCAN: 700, rot.CH_ROT: 7500}
        stage._positions.update(origin)
        stage._targets.update(origin)
        stage.connect()
        self.window._controller = stage
        try:
            with mock.patch.object(rot, "_CorrectionMoveWorker") as correction:
                self.window._on_apply_correction()
                correction.assert_not_called()
            with mock.patch.object(stage, "move_ch_relative", wraps=stage.move_ch_relative) as relative, \
                    mock.patch.object(stage, "move_ch_absolute", wraps=stage.move_ch_absolute) as absolute, \
                    mock.patch.object(rot.QMessageBox, "warning") as warning:
                self.window._return_centre_btn.click()
                self.assertFalse(self.window._return_centre_btn.isEnabled())
                self.assertTrue(self.window._post_scan_worker.wait(5000))
                APP.processEvents()
                warning.assert_not_called()
                relative.assert_not_called()
                self.assertEqual([call.args for call in absolute.call_args_list],
                                 [(rot.CH_ROT, 0), (rot.CH_SCAN, 500)])
                self.assertEqual(int(stage.get_ch_pos(3)), origin[3])
                self.assertEqual(int(stage.get_ch_pos(4)), origin[4])
                self.assertEqual(int(stage.get_ch_pos(rot.CH_SCAN)), 500)
                self.assertTrue(stage.is_motion_available())
                self.assertFalse(self.window._apply_btn.isEnabled())
        finally:
            if self.window._post_scan_worker is not None and self.window._post_scan_worker.isRunning():
                stage.normal_stop()
                self.window._post_scan_worker.wait(5000)
            self.window._controller = None
            stage.disconnect()

    def test_aligned_result_does_not_warn_about_unnecessary_unapplied_correction_on_close(self):
        self.populate(model=(0, 0, 500))
        with mock.patch.object(rot.QMessageBox, "warning") as warning:
            self.window.close()
        warning.assert_not_called()

    def apply_with_simulator(self, start_ch10, compensate=True):
        from utils.stage.control_stage_sim import PM16CControllerSim
        stage = PM16CControllerSim()
        origin = {rot.CH_CH3: 40000, rot.CH_CH4: 20000, rot.CH_SCAN: start_ch10}
        stage._positions.update(origin)
        stage._targets.update(origin)
        stage.connect()
        self.window._controller = stage
        self.window._also_move_ch10_chk.setChecked(compensate)
        self.window._correction_applied = False
        try:
            with mock.patch.object(rot.QMessageBox, "question", return_value=rot.QMessageBox.StandardButton.Yes) as question, \
                    mock.patch.object(rot.QMessageBox, "warning") as warning, \
                    mock.patch.object(stage, "move_ch_relative", wraps=stage.move_ch_relative) as relative, \
                    mock.patch.object(stage, "move_ch_absolute", wraps=stage.move_ch_absolute) as absolute:
                self.window._on_apply_correction()
                worker = self.window._move_worker
                self.assertIsNotNone(worker)
                self.assertTrue(worker.wait(5000), "Correction worker timed out")
                APP.processEvents()
                warning.assert_not_called()
                self.assertTrue(self.window._correction_applied)
                self.assertTrue(stage.is_motion_available())
                return (
                    {ch: int(stage.get_ch_pos(ch)) for ch in (rot.CH_CH3, rot.CH_CH4, rot.CH_SCAN)},
                    [call.args for call in relative.call_args_list],
                    [call.args for call in absolute.call_args_list],
                    question.call_args.args[2],
                )
        finally:
            if self.window._move_worker is not None and self.window._move_worker.isRunning():
                stage.normal_stop()
                self.window._move_worker.wait(5000)
            self.window._controller = None
            stage.disconnect()

    def test_apply_centres_ch10_for_both_signs_from_scan_end_or_zero_centre(self):
        for B in (-12, 12):
            for start in (700, 500 + B, 410):
                with self.subTest(B=B, ch10_start=start):
                    self.populate(model=(18, B, 500))
                    self.assertEqual(self.window._target_ch10, 500)
                    positions, relative, absolute, confirmation = self.apply_with_simulator(start)
                    self.assertEqual(relative, [(rot.CH_CH3, 18), (rot.CH_CH4, B)])
                    self.assertEqual(absolute, [(rot.CH_SCAN, 500)])
                    self.assertEqual(positions[rot.CH_SCAN], 500)
                    # Motor corrections cancel both fitted eccentricities.
                    centres_after = rot._rot_model(
                        np.array(self.thetas), 18 - (positions[rot.CH_CH3] - 40000),
                        B - (positions[rot.CH_CH4] - 20000), 500,
                    )
                    np.testing.assert_allclose(centres_after, positions[rot.CH_SCAN])
                    self.assertIn("Move Ch10 to 500 pulse", confirmation)
                    self.assertIn("absolute", confirmation)

    def test_apply_without_ch10_option_leaves_ch10_unchanged(self):
        self.populate()
        positions, relative, absolute, confirmation = self.apply_with_simulator(700, compensate=False)
        self.assertEqual(relative, [(rot.CH_CH3, 18), (rot.CH_CH4, -12)])
        self.assertEqual(absolute, [])
        self.assertEqual(positions[rot.CH_SCAN], 700)
        self.assertNotIn("Move Ch10", confirmation)

    def test_apply_allows_zero_absolute_ch10_target_with_zero_sample_corrections(self):
        self.window._fit_A = self.window._fit_B = self.window._fit_C = 0.0
        self.window._move_ch3 = self.window._move_ch4 = 0
        self.window._target_ch10 = 0
        positions, relative, absolute, confirmation = self.apply_with_simulator(700)
        self.assertEqual(relative, [])
        self.assertEqual(absolute, [(rot.CH_SCAN, 0)])
        self.assertEqual(positions[rot.CH_SCAN], 0)
        self.assertIn("Move Ch10 to 0 pulse", confirmation)

    def test_apply_uses_updated_C_after_reanalysis(self):
        self.populate()
        self.assertEqual(self.window._target_ch10, 500)
        self.populate(model=(18, -12, 520))
        positions, _, absolute, _ = self.apply_with_simulator(488)
        self.assertEqual(absolute, [(rot.CH_SCAN, 520)])
        self.assertEqual(positions[rot.CH_SCAN], 520)

    def test_failed_sample_move_does_not_continue_to_ch10(self):
        from utils.stage.control_stage_sim import PM16CControllerSim
        stage = PM16CControllerSim()
        errors = []
        worker = rot._CorrectionMoveWorker(stage, 18, -12, target_ch10=500)
        worker.move_failed.connect(errors.append)
        with mock.patch.object(stage, "move_ch_relative", side_effect=ValueError("Move refused")), \
                mock.patch.object(stage, "move_ch_absolute") as absolute:
            worker.run()
        absolute.assert_not_called()
        self.assertEqual(errors, ["Move refused"])
        self.assertTrue(stage.is_motion_available())

    def test_absolute_ch10_move_still_obeys_controller_soft_limits(self):
        from utils.stage import control_stage_sim
        stage = control_stage_sim.PM16CControllerSim()
        stage._positions[rot.CH_SCAN] = stage._targets[rot.CH_SCAN] = 350
        errors, completed = [], []
        worker = rot._CorrectionMoveWorker(stage, 0, 0, target_ch10=500)
        worker.move_failed.connect(errors.append)
        worker.move_completed.connect(lambda: completed.append(True))
        with mock.patch.dict(control_stage_sim.SOFT_LIMITS, {rot.CH_SCAN: (300, 400)}):
            worker.run()
        self.assertEqual(len(errors), 1)
        self.assertIn("soft limit", errors[0])
        self.assertEqual(completed, [])
        self.assertEqual(int(stage.get_ch_pos(rot.CH_SCAN)), 350)
        self.assertTrue(stage.is_motion_available())

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

    def test_analysis_controls_locked_during_scan_and_correction(self):
        self.populate()
        for attr in ("_worker", "_move_worker"):
            with self.subTest(worker=attr):
                worker = mock.Mock()
                worker.isRunning.return_value = True
                setattr(self.window, attr, worker)
                self.window._update_action_btns()
                self.assertFalse(self.window._angle_table.isEnabled())
                self.assertFalse(self.window._run_analysis_btn.isEnabled())
                with mock.patch.object(self.window, "_run_analysis") as analyse:
                    self.window._on_rerun_analysis()
                    analyse.assert_not_called()
                worker.isRunning.return_value = False
                self.window._update_action_btns()
                self.assertTrue(self.window._angle_table.isEnabled())
                setattr(self.window, attr, None)

    def test_reanalysis_during_return_preserves_target_and_blocks_new_moves(self):
        self.populate()
        controller = mock.MagicMock()
        self.window._controller = controller
        self.window._theta_edit.setText("-90, -30, 0, 45, 90")
        self.window._center_ch10_chk.setChecked(True)
        target = round(self.window._get_center_at_zero())
        entered, release = threading.Event(), threading.Event()

        def wait_for_return(**kwargs):
            entered.set()
            release.wait(5)

        controller.wait_until_stop.side_effect = wait_for_return
        self.window._start_post_scan_moves()
        post_worker = self.window._post_scan_worker
        try:
            self.assertTrue(entered.wait(2), "Return worker did not start")
            self.window._update_action_btns()
            self.assertTrue(self.window._angle_table.isEnabled())
            self.assertTrue(self.window._run_analysis_btn.isEnabled())
            self.assertFalse(self.window._apply_btn.isEnabled())
            self.assertFalse(self.window._return_centre_btn.isEnabled())

            self.range_spin(45.0, 2).setValue(360)
            self.use(0.0, False)
            self.assertIsNone(self.window._analysis_results)
            self.window._run_analysis_btn.click()
            self.assertIsNotNone(self.window._analysis_results)
            self.assertNotIn(0.0, self.window._analysis_results["included_theta_deg"])
            self.assertEqual(post_worker.ch10_center, target)
            self.assertTrue(post_worker.isRunning())
            self.assertFalse(self.window._apply_btn.isEnabled())
            self.assertFalse(self.window._return_centre_btn.isEnabled())
            with mock.patch.object(rot, "_CorrectionMoveWorker") as correction, \
                    mock.patch.object(rot, "_PostScanMoveWorker") as new_return:
                self.window._on_apply_correction()
                self.window._on_return_and_centre()
                correction.assert_not_called()
                new_return.assert_not_called()
        finally:
            release.set()
            self.assertTrue(post_worker.wait(5000))
            APP.processEvents()
        self.assertEqual([call.args for call in controller.move_ch_absolute.call_args_list],
                         [(rot.CH_ROT, 0), (rot.CH_SCAN, target)])
        self.assertTrue(self.window._apply_btn.isEnabled())
        self.assertTrue(self.window._angle_table.isEnabled())

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
        self.window._reset_state(self.thetas, 300, 700)
        self.populate()
        self.assertIsNone(self.window._analysis_results)
        self.assertIn("independent rotation angles", self.window._status_label.text())

    def test_new_scan_resets_per_angle_settings_and_completed_profiles(self):
        self.populate()
        self.use(0.0, False)
        self.range_spin(0.0, 2).setValue(360)
        self.window._reset_state(self.thetas, 100, 900)
        self.assertEqual(self.window._completed_thetas, set())
        self.assertEqual(self.window._aperture_centers, {})
        self.assertIsNone(self.window._analysis_results)
        self.assertTrue(self.window._angle_is_included(0.0))
        for theta in self.thetas:
            self.assertEqual(self.window._angle_fit_range(theta), (100, 900))

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
        for theta in self.window._completed_thetas:
            self.assertEqual(self.window._angle_fit_range(theta), (-200, 200))
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
            self.assertEqual(meta["analysis_results"]["suggested_ch10_target_pulse"], 500)
            with np.load(stem.with_suffix(".npz")) as arrays:
                np.testing.assert_array_equal(arrays["transmitted_map"][3], self.window._scan_data[45.0][1])
            self.assertGreater(stem.with_suffix(".png").stat().st_size, 0)
            for theta in self.thetas[:3]:
                self.use(theta, False)
            self.window._run_analysis_btn.click()
            meta = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertIsNone(meta["analysis_results"])
            for theta in self.thetas:
                self.use(theta, True)
            self.populate(model=(0, 0, 500))
            self.window._run_analysis_btn.click()
            meta = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertTrue(meta["analysis_results"]["rotation_centre_aligned"])
            self.assertEqual(meta["analysis_results"]["analysis_mode"], "common_centre")
            self.assertEqual(meta["analysis_results"]["suggested_ch3_pulse"], 0)
            self.assertEqual(meta["analysis_results"]["suggested_ch4_pulse"], 0)
            self.assertEqual(meta["analysis_results"]["suggested_ch10_target_pulse"], 500)


if __name__ == "__main__":
    unittest.main()
