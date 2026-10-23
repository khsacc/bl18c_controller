"""Hardware-free checks for the experimental gasket follower."""
import unittest
from unittest import mock
from types import SimpleNamespace
import json

import cv2
import numpy as np

from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole, detect_gasket_hole_near,
)
from apps.interactive_camera.xray_beam import (
    XrayBeamFileCache, XrayBeamPosition, check_xray_beam, load_xray_beam, save_xray_beam,
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


def beam_at(x, y, pulses=None):
    return XrayBeamPosition(x, y, 640, 480, pulses or {6: 0, 7: 0, 8: 0})


def patch_beam(beam):
    return mock.patch("apps.exp_scheduler.runner.load_xray_beam", return_value=beam)


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
        runner = SequenceRunner(Sequence(actions=[]),
                                DeviceContext(controller=FakeStageController()))
        runner._logger = mock.Mock()
        runner._get_camera_frame_after = lambda *_args: np.full_like(reference, 110)
        moves = []
        runner._move_follow_xy = lambda *_args: moves.append(_args)
        cfg = {
            "tracking_method": "gasket_beta", "reference_frame": reference,
            "gasket_ellipse": GasketEllipse(315, 230, 190, 175).to_dict(),
            "gasket_reference_centre": (315, 230), "gasket_sample_offset": (0.0, 0.0),
            "gasket_tolerance_px": 8,
            "M_inv": np.eye(2), "lim4": 100, "lim5": 100,
            "similarity_threshold": 0.95, "xy_max_retries": 0,
            "af_enabled": False,
        }
        with self.assertRaises(RunnerError) as caught, patch_beam(beam_at(315, 230)):
            runner._follow_correction_cycle(cfg, {4: 0, 5: 0})
        self.assertEqual(caught.exception.code, "runtime.gasket_detection_failed")
        self.assertEqual(moves, [])

    def test_preview_state_is_limited_to_follow_and_paused_for_autofocus(self):
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext())
        self.assertFalse(runner.gasket_preview_enabled)
        runner._set_gasket_preview_enabled(True)
        first_epoch = runner.gasket_preview_state[1]
        states = []
        runner._do_follow_autofocus_impl = lambda *_args: states.append(
            runner.gasket_preview_enabled
        )
        runner._do_follow_autofocus(10, 3)
        self.assertEqual(states, [False])
        self.assertTrue(runner.gasket_preview_enabled)
        self.assertEqual(runner.gasket_preview_state[1], first_epoch + 2)
        runner._follow_stop_event.set()
        runner._do_follow_autofocus(10, 3)
        self.assertFalse(runner.gasket_preview_enabled)

        runner._follow_stop_event.clear()
        runner._resolve_follow_config = lambda _action: {
            "tracking_method": "gasket_beta", "similarity_threshold": 0.95,
            "gasket_tolerance_px": 8.0,
        }
        runner._require_camera_index = lambda _index: None
        def finish_cycle(_cfg, _cumulative, **_kwargs):
            self.assertTrue(runner.gasket_preview_enabled)
            runner._follow_stop_event.set()
        runner._follow_correction_cycle = finish_cycle
        runner._measure_gasket_average = lambda *_args: (0, 0, 0.9)
        runner._follow_loop(StartFollowingAction())
        self.assertFalse(runner.gasket_preview_enabled)

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
            "gasket_reference_centre": (315, 230), "gasket_sample_offset": (0.0, 0.0),
            "gasket_tolerance_px": 8,
            "M_inv": np.eye(2), "lim4": 10, "lim5": 10,
            "similarity_threshold": 0.95, "xy_max_retries": 0,
            "af_enabled": False,
        }
        cumulative = {4: 0, 5: 0}
        with patch_beam(beam_at(315, 230)):
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
            gasket_sample_point={"x": 315, "y": 230},
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
              mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration),
              patch_beam(beam_at(315, 230))):
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
            gasket_sample_point={"x": 345, "y": 220},
            gasket_reference_digest="expected", gasket_tolerance_px=6,
        )
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext(), global_follow=follow)
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {"matrix_inv": [[1, 0], [0, 1]]}
        ))
        with (mock.patch("apps.exp_scheduler.runner.cv2.imread", return_value=reference),
              mock.patch("apps.exp_scheduler.runner.reference_digest", return_value="expected"),
              mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration),
              patch_beam(beam_at(300, 200))):
            cfg = runner._resolve_follow_config(StartFollowingAction())
        self.assertEqual(cfg["gasket_reference_centre"], (315, 230))
        self.assertEqual(cfg["gasket_sample_offset"], (30, -10))
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


class GasketXrayBeamTests(unittest.TestCase):
    image = staticmethod(GasketTrackingTests.image)

    def test_expected_centre_moves_the_search_window(self):
        reference = self.image(315, 230, 130, 105)
        current = self.image(315 + 90, 230 - 60, 130, 105, seed=2)
        marked = GasketEllipse(315, 230, 190, 175)
        with self.assertRaises(ValueError):
            detect_gasket_hole(reference, current, marked)
        result = detect_gasket_hole(reference, current, marked, expected_centre=(400, 175))
        self.assertLessEqual(abs(result.ellipse.cx - 405), 3)
        self.assertLessEqual(abs(result.ellipse.cy - 170), 3)
        # The reference location remains a fallback for an unmoved stage.
        result = detect_gasket_hole_near(reference, reference, marked, ((400, 175), None))
        self.assertEqual((result.ellipse.cx, result.ellipse.cy), (315, 230))

    def test_beam_file_round_trip_and_geometry_checks(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xray_beam.json"
            cache = XrayBeamFileCache(path)
            self.assertIsNone(cache.get())
            save_xray_beam(beam_at(320.5, 240, {6: 1, 7: 2, 8: 3}), path)
            beam = cache.get()
            self.assertEqual(beam, load_xray_beam(path))
            self.assertEqual(beam.point, (320.5, 240))
            check_xray_beam(beam, (480, 640, 3), {6: 1, 7: 2, 8: 3})
            with self.assertRaisesRegex(ValueError, "640×480"):
                check_xray_beam(beam, (600, 800, 3))
            with self.assertRaisesRegex(ValueError, "Ch8 3→4"):
                check_xray_beam(beam, (480, 640, 3), {6: 1, 7: 2, 8: 4})
            with self.assertRaisesRegex(ValueError, "not registered"):
                check_xray_beam(None, (480, 640, 3))

    def _runner(self, positions=None):
        reference = cv2.cvtColor(self.image(315, 230, 130, 105), cv2.COLOR_GRAY2BGR)
        follow = GlobalFollowSettings(
            reference_path="reference.png", tracking_method="gasket_beta",
            gasket_ellipse=GasketEllipse(315, 230, 190, 175).to_dict(),
            # Sample 20 px right of and 10 px below the rim centre.
            gasket_sample_point={"x": 335, "y": 240},
            gasket_reference_digest="expected", gasket_tolerance_px=4,
        )
        runner = SequenceRunner(
            Sequence(actions=[]),
            DeviceContext(controller=FakeStageController(positions)),
            global_follow=follow,
        )
        runner._logger = mock.Mock()
        runner._start_camera_session_if_needed = mock.Mock()
        runner._require_camera_index = mock.Mock()
        runner._do_wait = mock.Mock()
        return runner, reference

    def _patches(self, reference, beam):
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {"matrix_inv": [[1, 0], [0, 1]]}
        ))
        return (mock.patch("apps.exp_scheduler.runner.cv2.imread", return_value=reference),
                mock.patch("apps.exp_scheduler.runner.reference_digest", return_value="expected"),
                mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration),
                patch_beam(beam))

    def test_alignment_moves_the_marked_sample_onto_the_beam(self):
        runner, reference = self._runner()
        # Beam at (395, 180): rim must end at (375, 170), i.e. +60, -60 px.
        on_beam = cv2.cvtColor(self.image(375, 170, 130, 105, seed=3), cv2.COLOR_GRAY2BGR)
        runner._get_camera_frame_after = mock.Mock(side_effect=[reference] * 5 + [on_beam] * 6)
        moves = []
        runner._do_stage = lambda action, _values: moves.append((action.ch, action.value))
        a, b, c, d = self._patches(reference, beam_at(395, 180))
        with a, b, c, d:
            centre_shift = runner._align_to_xrd_reference(GlobalSpectrumSettings())
        self.assertEqual([ch for ch, _ in moves], [4, 5])
        self.assertLessEqual(abs(moves[0][1] - 60), 3)
        self.assertLessEqual(abs(moves[1][1] + 60), 3)
        # From there the gasket centre itself reaches the beam after (+20, +10) px.
        self.assertLessEqual(abs(centre_shift[0] - 20), 3)
        self.assertLessEqual(abs(centre_shift[1] - 10), 3)

    def test_moved_microscope_stops_alignment_before_stage_motion(self):
        runner, reference = self._runner({8: 5})
        runner._get_camera_frame_after = mock.Mock(return_value=reference)
        runner._do_stage = mock.Mock()
        a, b, c, d = self._patches(reference, beam_at(395, 180))
        with a, b, c, d, self.assertRaises(RunnerError) as caught:
            runner._align_to_xrd_reference(GlobalSpectrumSettings())
        self.assertEqual(caught.exception.code, "runtime.xray_beam_invalid")
        self.assertIn("Ch8", str(caught.exception))
        runner._do_stage.assert_not_called()

    def test_ruby_target_starts_from_gasket_centre_on_beam(self):
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {"matrix_inv": [[2, 0], [0, -1]]}
        ))
        departure = {3: 100, 4: 1000, 5: 2000}
        offsets = {3: 5, 4: 30, 5: -40}
        with mock.patch("apps.exp_scheduler.runner._CALIBRATION_PATH", calibration):
            self.assertEqual(SequenceRunner._ruby_target(departure, offsets, (20, 10)),
                             {3: 105, 4: 1070, 5: 1950})
        self.assertEqual(SequenceRunner._ruby_target(departure, offsets, None),
                         {3: 105, 4: 1030, 5: 1960})

    def test_ruby_offsets_of_the_other_method_are_rejected(self):
        runner, _reference = self._runner()
        runner._align_to_xrd_reference = mock.Mock()
        with self.assertRaises(RunnerError) as caught:
            runner._acquire_spectrum_at_ruby(
                TakeSpectrumAction(), 0, GlobalSpectrumSettings(), {3: 0, 4: 1, 5: 0},
                0.1, False, None, (), {}, None, {3: 0, 4: 0, 5: 0},
            )
        self.assertEqual(caught.exception.code, "runtime.spectrum_ruby_origin_mismatch")
        runner._align_to_xrd_reference.assert_not_called()


if __name__ == "__main__":
    unittest.main()
