"""Hardware-free checks for the experimental gasket follower."""
import unittest
from unittest import mock
from types import SimpleNamespace
import json

import cv2
import numpy as np

from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.actions import StartFollowingAction, TakeSpectrumAction
from apps.exp_scheduler.runner import RunnerError, SequenceRunner
from apps.exp_scheduler.scheduler_settings import GlobalFollowSettings, GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.checks.camera_follow import check_camera
from apps.exp_scheduler.validator.execution_trace import ExecutionTrace
from apps.exp_scheduler.validator.pre_validator import PreCheckResult
from tests.exp_scheduler_fakes import FakeStageController


class GasketTrackingTests(unittest.TestCase):
    @staticmethod
    def image(cx, cy, inside, outside, seed=1):
        rng = np.random.default_rng(seed)
        yy, xx = np.indices((480, 640))
        hole = ((xx - cx) / 190) ** 2 + ((yy - cy) / 175) ** 2 <= 1
        signal = np.where(hole, inside, outside)
        return np.uint8(np.clip(signal + rng.normal(0, 5, signal.shape), 0, 255))

    def test_tracks_shift_when_contrast_reverses_and_bubble_appears(self):
        reference = self.image(315, 230, 130, 105)
        current = self.image(330, 218, 105, 130, seed=2)
        cv2.circle(current, (250, 160), 65, 40, -1)
        result = detect_gasket_hole(
            reference, current, GasketEllipse(315, 230, 190, 175)
        )
        self.assertLessEqual(abs(result.ellipse.cx - 330), 3)
        self.assertLessEqual(abs(result.ellipse.cy - 218), 3)
        self.assertGreaterEqual(result.coverage, 5)

    def test_rejects_blank_frame_instead_of_returning_a_centre(self):
        reference = self.image(315, 230, 130, 105)
        with self.assertRaises(ValueError):
            detect_gasket_hole(
                reference, np.full_like(reference, 110),
                GasketEllipse(315, 230, 190, 175),
            )

    def test_rejects_different_camera_resolution(self):
        reference = self.image(315, 230, 130, 105)
        with self.assertRaises(ValueError):
            detect_gasket_hole(
                reference, reference[:400], GasketEllipse(315, 230, 190, 175)
            )

    def test_rejects_unrelated_texture(self):
        reference = self.image(315, 230, 130, 105)
        unrelated = np.random.default_rng(42).integers(
            0, 256, reference.shape, dtype=np.uint8
        )
        with self.assertRaises(ValueError):
            detect_gasket_hole(
                reference, unrelated, GasketEllipse(315, 230, 190, 175)
            )

    def test_scheduler_does_not_move_when_live_rim_is_missing(self):
        reference = self.image(315, 230, 130, 105)
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        runner._get_camera_frame_after = lambda *_args: np.full_like(reference, 110)
        moves = []
        runner._move_follow_xy = lambda *_args: moves.append(_args)
        cfg = {
            "tracking_method": "gasket_beta", "reference_frame": reference,
            "gasket_ellipse": GasketEllipse(315, 230, 190, 175).to_dict(),
            "gasket_reference_centre": (315, 230), "gasket_tolerance_px": 8,
            "M_inv": np.eye(2), "lim4": 100, "lim5": 100,
            "similarity_threshold": 0.95, "xy_max_retries": 0,
            "af_enabled": False,
        }
        with self.assertRaises(RunnerError) as caught:
            runner._follow_correction_cycle(cfg, {4: 0, 5: 0})
        self.assertEqual(caught.exception.code, "runtime.gasket_detection_failed")
        self.assertEqual(moves, [])

    def test_scheduler_correction_uses_existing_per_step_limits(self):
        reference = self.image(315, 230, 130, 105)
        shifted = self.image(330, 218, 130, 105, seed=2)
        stage = FakeStageController()
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext(controller=stage))
        runner._logger = mock.Mock()
        runner._get_camera_frame_after = lambda *_args: shifted
        cfg = {
            "tracking_method": "gasket_beta", "reference_frame": reference,
            "gasket_ellipse": GasketEllipse(315, 230, 190, 175).to_dict(),
            "gasket_reference_centre": (315, 230), "gasket_tolerance_px": 8,
            "M_inv": np.eye(2), "lim4": 10, "lim5": 10,
            "similarity_threshold": 0.95, "xy_max_retries": 0,
            "af_enabled": False,
        }
        cumulative = {4: 0, 5: 0}
        runner._follow_correction_cycle(cfg, cumulative)
        self.assertEqual(cumulative, {4: -10, 5: 10})
        self.assertEqual(stage.positions[4], -10)
        self.assertEqual(stage.positions[5], 10)

    def test_ruby_alignment_uses_the_selected_gasket_detector(self):
        reference_gray = self.image(315, 230, 130, 105)
        shifted_gray = self.image(330, 218, 130, 105, seed=2)
        reference = cv2.cvtColor(reference_gray, cv2.COLOR_GRAY2BGR)
        shifted = cv2.cvtColor(shifted_gray, cv2.COLOR_GRAY2BGR)
        follow = GlobalFollowSettings(
            reference_path="reference.png", tracking_method="gasket_beta",
            gasket_ellipse=GasketEllipse(315, 230, 190, 175).to_dict(),
            gasket_reference_digest="expected", gasket_tolerance_px=8,
        )
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=FakeStageController()),
            global_follow=follow,
        )
        runner._logger = mock.Mock()
        runner._start_camera_session_if_needed = mock.Mock()
        runner._require_camera_index = mock.Mock()
        runner._get_camera_frame_after = mock.Mock(
            side_effect=[shifted] * 5 + [reference] * 6
        )
        runner._do_wait = mock.Mock()
        moves = []
        runner._do_stage = lambda action, _values: moves.append((action.ch, action.value))
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {"matrix_inv": [[1, 0], [0, 1]]}
        ))
        with (mock.patch("apps.exp_scheduler.runner.cv2.imread", return_value=reference),
              mock.patch("apps.exp_scheduler.runner.reference_digest", return_value="expected"),
              mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)):
            runner._align_to_xrd_reference(GlobalSpectrumSettings())
        self.assertEqual([ch for ch, _ in moves], [4, 5])
        self.assertLessEqual(abs(moves[0][1] + 15), 3)
        self.assertLessEqual(abs(moves[1][1] - 12), 3)

    def test_five_frame_average_ignores_two_transient_bad_centres(self):
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        runner._logger = mock.Mock()
        runner._get_camera_frame_after = mock.Mock(return_value=np.ones((2, 2), dtype=np.uint8))
        runner._measure_follow_frame = mock.Mock(side_effect=[
            (5, -2, 0.9), (6, -2, 0.9), (60, 50, 0.8),
            (4, -3, 0.9), (-40, -40, 0.7),
        ])
        dx, dy, score = runner._measure_gasket_average(
            {"gasket_tolerance_px": 20.0}, "test"
        )
        self.assertEqual((dx, dy), (5, -2))
        self.assertAlmostEqual(score, 0.9)
        self.assertEqual(runner._get_camera_frame_after.call_count, 5)

    def test_five_frame_average_requires_three_coherent_detections(self):
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        runner._logger = mock.Mock()
        runner._get_camera_frame_after = mock.Mock(return_value=np.ones((2, 2), dtype=np.uint8))
        runner._measure_follow_frame = mock.Mock(side_effect=[
            (0, 0, 0.9), (30, 0, 0.9), (60, 0, 0.9),
            (90, 0, 0.9), (120, 0, 0.9),
        ])
        with self.assertRaises(RunnerError) as caught:
            runner._measure_gasket_average({"gasket_tolerance_px": 20.0}, "test")
        self.assertEqual(caught.exception.code, "runtime.gasket_detection_failed")

    def test_average_accepts_boundary_jitter_but_not_persistent_drift(self):
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        runner._logger = mock.Mock()
        runner._get_camera_frame_after = mock.Mock(return_value=np.ones((2, 2), dtype=np.uint8))
        cfg = {"tracking_method": "gasket_beta", "gasket_tolerance_px": 20.0}
        runner._measure_follow_frame = mock.Mock(side_effect=[
            (18, 0, 0.9), (19, 0, 0.9), (21, 0, 0.9),
            (20, 0, 0.9), (22, 0, 0.9),
        ])
        dx, dy, score = runner._measure_gasket_average(cfg, "test")
        self.assertTrue(runner._follow_frame_accepted(cfg, dx, dy, score))
        runner._measure_follow_frame = mock.Mock(return_value=(22, 0, 0.9))
        dx, dy, score = runner._measure_gasket_average(cfg, "test")
        self.assertFalse(runner._follow_frame_accepted(cfg, dx, dy, score))

    def test_configured_frame_count_controls_majority_and_one_frame_mode(self):
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        runner._logger = mock.Mock()
        runner._get_camera_frame_after = mock.Mock(return_value=np.ones((2, 2), dtype=np.uint8))
        runner._measure_follow_frame = mock.Mock(return_value=(2, 0, 0.9))
        self.assertEqual(
            runner._measure_gasket_average(
                {"gasket_tolerance_px": 20.0, "gasket_average_frames": 1}, "test"
            )[:2], (2, 0)
        )
        self.assertEqual(runner._get_camera_frame_after.call_count, 1)
        runner._get_camera_frame_after.reset_mock()
        runner._measure_follow_frame = mock.Mock(side_effect=[
            (2, 0, 0.9), (2, 0, 0.9), (2, 0, 0.9),
            (2, 0, 0.9), (2, 0, 0.9), (2, 0, 0.9),
            (40, 0, 0.9), (40, 0, 0.9), (40, 0, 0.9), (40, 0, 0.9),
        ])
        self.assertEqual(
            runner._measure_gasket_average(
                {"gasket_tolerance_px": 20.0, "gasket_average_frames": 10}, "test"
            )[:2], (2, 0)
        )
        self.assertEqual(runner._get_camera_frame_after.call_count, 10)

    def test_follow_and_ruby_share_the_annotated_reference_validation(self):
        reference_gray = self.image(315, 230, 130, 105)
        reference = cv2.cvtColor(reference_gray, cv2.COLOR_GRAY2BGR)
        follow = GlobalFollowSettings(
            reference_path="reference.png", tracking_method="gasket_beta",
            gasket_ellipse=GasketEllipse(315, 230, 190, 175).to_dict(),
            gasket_reference_digest="expected", gasket_tolerance_px=6,
        )
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext(), global_follow=follow)
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {"matrix_inv": [[1, 0], [0, 1]]}
        ))
        with (mock.patch("apps.exp_scheduler.runner.cv2.imread", return_value=reference),
              mock.patch("apps.exp_scheduler.runner.reference_digest", return_value="expected"),
              mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)):
            cfg = runner._resolve_follow_config(StartFollowingAction())
        self.assertEqual(cfg["gasket_reference_centre"], (315, 230))
        self.assertEqual(cfg["gasket_tolerance_px"], 6)

    def test_ruby_alignment_rejects_missing_mark_before_stage_motion(self):
        reference_gray = self.image(315, 230, 130, 105)
        reference = cv2.cvtColor(reference_gray, cv2.COLOR_GRAY2BGR)
        follow = GlobalFollowSettings(
            reference_path="reference.png", tracking_method="gasket_beta",
        )
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=FakeStageController()),
            global_follow=follow,
        )
        runner._do_stage = mock.Mock()
        runner._start_camera_session_if_needed = mock.Mock()
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {"matrix_inv": [[1, 0], [0, 1]]}
        ))
        with (mock.patch("apps.exp_scheduler.runner.cv2.imread", return_value=reference),
              mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration)):
            with self.assertRaises(RunnerError):
                runner._align_to_xrd_reference(
                    GlobalSpectrumSettings(xrd_reference_ch3_pulse=10000)
                )
        runner._start_camera_session_if_needed.assert_not_called()
        runner._do_stage.assert_not_called()

    def test_ruby_preflight_requires_a_mark_when_beta_is_selected(self):
        camera = mock.Mock()
        camera.isOpened.return_value = True
        result = PreCheckResult()
        with (mock.patch("apps.exp_scheduler.validator.checks.camera_follow.cv2.VideoCapture",
                         return_value=camera),
              mock.patch("apps.exp_scheduler.validator.checks.camera_follow.Path.exists",
                         return_value=True),
              mock.patch("apps.exp_scheduler.validator.checks.camera_follow._check_calibration")):
            check_camera(
                ExecutionTrace.build([TakeSpectrumAction()]), result,
                GlobalFollowSettings(reference_path="reference.png", tracking_method="gasket_beta"),
                DeviceContext(controller=FakeStageController()),
            )
        self.assertIn(
            "preflight.camera.gasket_outline_invalid",
            {item.code for item in result.diagnostics},
        )


if __name__ == "__main__":
    unittest.main()
