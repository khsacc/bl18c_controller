"""Detection recovery using a moving synthetic image, without hardware."""
import contextlib
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from apps.interactive_camera import gasket_follow as follow
from apps.interactive_camera.gasket_tracking import GasketEllipse
from apps.interactive_camera.sample_tracking import clamp_xy_correction


def image(centre):
    yy, xx = np.indices((480, 640))
    hole = ((xx - centre[0]) / 190) ** 2 + ((yy - centre[1]) / 175) ** 2 <= 1
    noise = np.random.default_rng(1).normal(0, 5, hole.shape)
    return np.uint8(np.clip(np.where(hole, 130, 105) + noise, 0, 255))


class GasketRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.reference = np.array([315., 230.])
        self.target = np.array([50., 50.])  # rim largely outside the field
        self.drift = np.array([3., -2.])
        self.matrix = np.array([[2., 1.], [-1., 2.]])
        self.pos = np.zeros(2, dtype=int)
        self.cumulative = {4: 0, 5: 0}
        self.moves = []
        self.frames = []
        self.stopped = False
        self.blank = False
        self.cfg = {
            'tracking_method': 'gasket_beta',
            'reference_frame': image(self.reference),
            'gasket_ellipse': GasketEllipse(*self.reference, 190, 175).to_dict(),
            'gasket_reference_centre': tuple(self.reference),
            'gasket_sample_offset': (275., 186.),
            'gasket_average_frames': 5,
            'gasket_tolerance_um': 2.,
            'gasket_um_matrix': np.eye(2),
            'M_inv': self.matrix, 'lim4': 2000, 'lim5': 2000,
            'af_enabled': False, 'xy_max_retries': 0,
        }
        self.hooks = follow.FollowHooks(
            measure_average=self.measure, move_xy=self.move,
            autofocus=mock.Mock(), aborted=lambda: self.stopped,
            log=mock.Mock(), progress=mock.Mock(),
            refresh_target=self.refresh, read_xy=lambda: tuple(self.pos),
        )

    def refresh(self, cfg):
        cfg['gasket_target_centre'] = tuple(self.target)
        return tuple(self.target)

    def frame(self, purpose):
        self.frames.append((purpose, tuple(self.pos)))
        centre = self.target + self.drift + np.linalg.solve(self.matrix, self.pos)
        return np.zeros((480, 640), np.uint8) if self.blank else image(centre)

    def measure(self, cfg, purpose):
        return follow.measure_average(
            cfg, purpose, get_frame=self.frame,
            measure=lambda c, frame: follow.measure_frame(c, frame, c['gasket_target_centre'], lambda _: None),
            log=self.hooks.log, refresh_target=self.refresh,
        )

    def move(self, p4, p5, l4, l5):
        delta = np.array([np.clip(p4, -l4, l4), np.clip(p5, -l5, l5)], dtype=int)
        self.pos += delta
        self.moves.append(tuple(delta))
        return tuple(delta)

    def run_cycle(self, **kwargs):
        return follow.align_and_verify(
            self.cfg, self.cumulative, self.hooks,
            verify_purpose='final', retry_purpose='retry', **kwargs,
        )

    def test_clipped_rim_is_reacquired_and_real_drift_is_corrected(self):
        result = self.run_cycle(recover_detection=True)
        self.assertTrue(self.cfg['gasket_recovered'])
        self.assertEqual(len(self.moves), 2)
        self.assertGreater(self.moves[0][0], 0)
        self.assertLess(self.moves[1][0], 0)
        actual_offset = self.drift + np.linalg.solve(self.matrix, self.pos)
        self.assertLess(np.linalg.norm(actual_offset), 2.)
        np.testing.assert_allclose(result[:2], actual_offset, atol=1.5)
        np.testing.assert_array_equal([self.cumulative[4], self.cumulative[5]], self.pos)
        self.assertEqual(len(self.frames), 10)  # no verification back at clipped beam
        self.assertTrue(all('recovery' in purpose for purpose, _ in self.frames[5:]))

    def test_normal_detection_does_not_use_recovery_hooks(self):
        self.target = self.reference.copy()
        self.drift[:] = 0
        self.hooks.read_xy = mock.Mock(side_effect=AssertionError('unnecessary read'))
        self.hooks.refresh_target = mock.Mock(side_effect=AssertionError('unnecessary refresh'))
        self.run_cycle(recover_detection=True)
        self.assertFalse(self.cfg['gasket_recovered'])
        self.assertEqual(len(self.moves), 1)  # existing first XY correction

    def test_shared_alignment_requires_explicit_recovery_opt_in(self):
        with self.assertRaises(follow.GasketFollowError) as caught:
            self.run_cycle()
        self.assertEqual(caught.exception.code, follow.DETECTION_FAILED)
        self.assertEqual(self.moves, [])

    def test_reacquisition_failure_stops_at_excursion_without_return_or_retry(self):
        self.blank = True
        with self.assertRaises(follow.GasketFollowError) as caught:
            self.run_cycle(recover_detection=True)
        self.assertEqual(caught.exception.code, follow.DETECTION_FAILED)
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(len(self.frames), 10)
        self.assertFalse(self.cfg['gasket_recovered'])

    def test_detection_failure_at_final_verification_also_recovers_once(self):
        self.target = self.reference.copy()
        original = self.hooks.measure_average
        def measure(cfg, purpose):
            if purpose == 'final':
                raise follow.GasketFollowError(follow.DETECTION_FAILED, 'clipped')
            return original(cfg, purpose)
        self.hooks.measure_average = measure
        self.run_cycle(recover_detection=True)
        self.assertTrue(self.cfg['gasket_recovered'])
        self.assertEqual(len(self.moves), 3)

    def test_return_clamped_by_total_limit_is_not_success(self):
        # Correcting this drift needs negative Ch4 pulses, but the total
        # limit forbids going below the starting Ch4 position.
        self.drift = np.array([8., -2.])
        def limited(p4, p5, l4, l5):
            delta = (clamp_xy_correction(p4, l4, self.pos[0], 0, 2000),
                     clamp_xy_correction(p5, l5, self.pos[1], -2000, 2000))
            self.pos += delta
            self.moves.append(delta)
            return delta
        self.hooks.move_xy = limited
        with self.assertRaises(follow.GasketFollowError) as caught:
            self.run_cycle(recover_detection=True)
        self.assertEqual(caught.exception.code, follow.ALIGNMENT_FAILED)
        self.assertEqual(len(self.moves), 2)
        self.assertFalse(self.cfg['gasket_recovered'])

    def test_each_leg_keeps_the_per_attempt_limits(self):
        self.cfg.update(lim4=1, lim5=1)
        with self.assertRaises(follow.GasketFollowError):
            self.run_cycle(recover_detection=True)
        self.assertTrue(all(abs(p4) <= 1 and abs(p5) <= 1 for p4, p5 in self.moves))

    def test_readback_mismatch_never_passes_or_returns(self):
        def inaccurate(p4, p5, l4, l5):
            commanded = self.move(p4, p5, l4, l5)
            self.pos[0] -= 10
            return commanded
        self.hooks.move_xy = inaccurate
        with self.assertRaisesRegex(follow.GasketFollowError, 'readback'):
            self.run_cycle(recover_detection=True)
        self.assertEqual(len(self.moves), 1)

    def test_stop_after_outbound_prevents_measurement_and_return(self):
        def stop_after_move(*args):
            result = self.move(*args)
            self.stopped = True
            return result
        self.hooks.move_xy = stop_after_move
        self.assertIsNone(self.run_cycle(recover_detection=True))
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(len(self.frames), 5)
        np.testing.assert_array_equal([self.cumulative[4], self.cumulative[5]], self.pos)

    def test_stop_during_reacquisition_prevents_return(self):
        original = self.hooks.measure_average
        def stop_during_measure(cfg, purpose):
            result = original(cfg, purpose)
            if 'recovery' in purpose:
                self.stopped = True
            return result
        self.hooks.measure_average = stop_during_measure
        self.assertIsNone(self.run_cycle(recover_detection=True))
        self.assertEqual(len(self.moves), 1)

    def test_focus_is_before_reacquisition_and_never_after_return(self):
        self.cfg['af_enabled'] = True
        positions = []
        self.hooks.autofocus = lambda: positions.append(tuple(self.pos))
        self.run_cycle(recover_detection=True)
        self.assertEqual(positions, [self.moves[0]])

    def test_other_errors_never_start_recovery_motion(self):
        for exc in (RuntimeError('camera offline'),
                    follow.GasketFollowError(follow.BEAM_INVALID, 'microscope changed'),
                    follow.GasketFollowError(follow.ALIGNMENT_FAILED, 'out of tolerance')):
            with self.subTest(error=str(exc)):
                self.hooks.measure_average = mock.Mock(side_effect=exc)
                with self.assertRaises(type(exc)):
                    self.run_cycle(recover_detection=True)
                self.assertEqual(self.moves, [])

    def test_invalid_beam_before_recovery_prevents_any_motion(self):
        self.hooks.refresh_target = mock.Mock(side_effect=follow.GasketFollowError(follow.BEAM_INVALID, 'beam changed'))
        with self.assertRaisesRegex(follow.GasketFollowError, 'beam changed'):
            self.run_cycle(recover_detection=True)
        self.assertEqual(self.moves, [])

    def test_changed_beam_after_return_is_checked(self):
        original = self.hooks.refresh_target
        def refresh(cfg):
            if len(self.moves) == 2:
                raise follow.GasketFollowError(follow.BEAM_INVALID, 'microscope changed')
            return original(cfg)
        self.hooks.refresh_target = refresh
        with self.assertRaisesRegex(follow.GasketFollowError, 'microscope changed'):
            self.run_cycle(recover_detection=True)
        self.assertFalse(self.cfg['gasket_recovered'])

    def scheduler(self, blank=False):
        from apps.exp_scheduler.device_context import DeviceContext
        from apps.exp_scheduler.runner import SequenceRunner
        from apps.exp_scheduler.sequence import Sequence
        from tests.exp_scheduler_fakes import FakeStageController
        ctrl = FakeStageController()
        runner = SequenceRunner(Sequence(actions=[]), DeviceContext(controller=ctrl))
        runner._logger = mock.Mock()
        runner._resolve_follow_config = mock.Mock(return_value=self.cfg)
        runner._require_camera_index = mock.Mock()
        # Keep beam validation in the shared measurement path, with a known
        # registered beam rather than relying on files from the developer PC.
        def frame(*_args):
            pos = np.array([ctrl.positions[4], ctrl.positions[5]])
            centre = self.target + self.drift + np.linalg.solve(self.matrix, pos)
            return np.zeros((480, 640), np.uint8) if blank else image(centre)
        runner._get_camera_frame_after = frame
        return runner, ctrl

    def beam_patch(self):
        from apps.interactive_camera.xray_beam import XrayBeamPosition
        return mock.patch.object(follow, 'load_xray_beam', return_value=
            XrayBeamPosition(325, 236, 640, 480, {6: 0, 7: 0, 8: 0}))

    def test_scheduler_follow_loop_recovers_without_aborting(self):
        from apps.exp_scheduler.actions import StartFollowingAction
        runner, ctrl = self.scheduler()
        original = runner._verified_gasket_cycle
        def finish_after_cycle(*args, **kwargs):
            result = original(*args, **kwargs)
            runner._stop_event.set()  # end the test after this one cycle
            return result
        runner._verified_gasket_cycle = finish_after_cycle
        with self.beam_patch():
            runner._follow_loop(StartFollowingAction())
        self.assertTrue(self.cfg['gasket_recovered'])
        self.assertTrue(runner._follow_first_cycle_done.is_set())
        self.assertIsNone(runner._follow_exception)
        self.assertFalse(runner._had_error)
        self.assertEqual(ctrl.call_count('normal_stop'), 0)
        pos = [ctrl.positions[4], ctrl.positions[5]]
        self.assertLess(np.linalg.norm(self.drift + np.linalg.solve(self.matrix, pos)), 2.)

    def test_scheduler_reacquisition_failure_aborts_whole_sequence(self):
        from apps.exp_scheduler.actions import StartFollowingAction
        runner, ctrl = self.scheduler(blank=True)
        with self.beam_patch():
            runner._follow_loop(StartFollowingAction())
        self.assertEqual(runner._follow_exception.code, follow.DETECTION_FAILED)
        self.assertTrue(runner._stop_event.is_set())
        self.assertTrue(runner._had_error)
        self.assertEqual(ctrl.call_count('normal_stop'), 1)
        self.assertEqual(ctrl.call_count('move_ch_relative'), 2)  # out only

    def test_scheduler_final_stop_alignment_can_recover(self):
        runner, _ctrl = self.scheduler()
        with self.beam_patch():
            runner._align_gasket_to_reference(self.cfg, {4: 0, 5: 0}, 'stop_following')
        self.assertTrue(self.cfg['gasket_recovered'])

    def test_scheduler_one_shot_alignment_recovers(self):
        from apps.exp_scheduler.actions import AlignToReferenceAction
        runner, ctrl = self.scheduler()
        with self.beam_patch():
            runner._do_align_to_reference(AlignToReferenceAction())
        self.assertTrue(self.cfg['gasket_recovered'])
        self.assertEqual(ctrl.call_count('move_ch_relative'), 4)
        pos = [ctrl.positions[4], ctrl.positions[5]]
        self.assertLess(np.linalg.norm(self.drift + np.linalg.solve(self.matrix, pos)), 2.)
        self.assertIn('gasket inferred offset', runner._logger.log_ops.call_args.args[0])

    def test_scheduler_one_shot_reacquisition_failure_stops_at_reference(self):
        from apps.exp_scheduler.runner import RunnerError
        runner, ctrl = self.scheduler(blank=True)
        with self.beam_patch(), self.assertRaises(RunnerError) as caught:
            runner._align_gasket_to_reference(self.cfg, {4: 0, 5: 0}, 'align_to_reference')
        self.assertEqual(caught.exception.code, follow.DETECTION_FAILED)
        self.assertFalse(self.cfg['gasket_recovered'])
        self.assertEqual(ctrl.call_count('move_ch_relative'), 2)  # out only

    def test_return_readback_mismatch_is_not_success(self):
        def inaccurate_return(*args):
            commanded = self.move(*args)
            if len(self.moves) == 2:
                self.pos[0] -= 10
            return commanded
        self.hooks.move_xy = inaccurate_return
        with self.assertRaisesRegex(follow.GasketFollowError, 'readback'):
            self.run_cycle(recover_detection=True)
        self.assertFalse(self.cfg['gasket_recovered'])
        self.assertEqual(len(self.moves), 2)

    def test_stop_during_focus_prevents_reacquisition_and_return(self):
        self.cfg['af_enabled'] = True
        def stop():
            self.stopped = True
        self.hooks.autofocus = stop
        self.assertIsNone(self.run_cycle(recover_detection=True))
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(len(self.frames), 5)

    def test_scheduler_locks_out_competing_stage_moves_throughout_recovery(self):
        import threading
        runner, ctrl = self.scheduler()
        original = runner._get_camera_frame_after
        probes = []
        def probe_lock():
            acquired = runner._limited_stage_motion_lock.acquire(blocking=False)
            probes.append(acquired)
            if acquired:
                runner._limited_stage_motion_lock.release()
        def frame(*args):
            if ctrl.call_count('move_ch_relative'):
                competitor = threading.Thread(target=probe_lock)
                competitor.start()
                competitor.join(timeout=1)
                self.assertFalse(competitor.is_alive())
            return original(*args)
        runner._get_camera_frame_after = frame
        with self.beam_patch():
            runner._align_gasket_to_reference(self.cfg, {4: 0, 5: 0}, 'stop_following')
        self.assertEqual(probes, [False] * 5)
        competitor = threading.Thread(target=probe_lock)
        competitor.start()
        competitor.join(timeout=1)
        self.assertEqual(probes[-1], True)  # scope released after recovery

    def test_readback_failure_after_excursion_never_initiates_return(self):
        def read():
            if self.moves:
                raise RuntimeError('Ch4 read failed')
            return tuple(self.pos)
        self.hooks.read_xy = read
        with self.assertRaisesRegex(RuntimeError, 'Ch4 read failed'):
            self.run_cycle(recover_detection=True)
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(len(self.frames), 5)
        self.assertFalse(self.cfg['gasket_recovered'])

    def ruby_alignment_context(self, runner):
        runner._global_follow.reference_path = 'reference.png'
        runner._global_follow.tracking_method = 'gasket_beta'
        runner._global_follow.max_correction_ch4_um = 4000
        runner._global_follow.max_correction_ch5_um = 4000
        runner._start_camera_session_if_needed = mock.Mock()
        runner._resolve_gasket_reference = mock.Mock(return_value=self.cfg)
        calibration = SimpleNamespace(read_text=lambda **_kw: json.dumps(
            {'matrix_inv': self.matrix.tolist()}))
        stack = contextlib.ExitStack()
        stack.enter_context(self.beam_patch())
        stack.enter_context(mock.patch('apps.exp_scheduler.runner.cv2.imread',
                                       return_value=self.cfg['reference_frame']))
        stack.enter_context(mock.patch('apps.exp_scheduler.runner._CALIBRATION_PATH',
                                       calibration))
        return stack

    def test_ruby_alignment_recovers_and_preserves_gasket_centre_origin(self):
        from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
        runner, ctrl = self.scheduler()
        ctrl.positions[3] = 120
        with self.ruby_alignment_context(runner):
            shift = runner._align_to_xrd_reference(GlobalSpectrumSettings(xrd_reference_ch3_pulse=0))
            departure = {ch: ctrl.positions[ch] for ch in (3, 4, 5)}
            ruby = runner._ruby_target(departure, {3: 3, 4: 5, 5: -7}, shift)
        self.assertTrue(self.cfg['gasket_recovered'])
        self.assertEqual(ctrl.call_count('move_ch_relative'), 4)
        pos = [ctrl.positions[4], ctrl.positions[5]]
        residual = self.drift + np.linalg.solve(self.matrix, pos)
        self.assertLess(np.linalg.norm(residual), 2.)
        # The ruby origin is where the rim centre, rather than the sample,
        # reaches the beam. Include the measured residual after recovery.
        expected_shift = np.asarray(self.cfg['gasket_sample_offset']) - residual
        np.testing.assert_allclose(shift, expected_shift, atol=1.5)
        expected_origin = self.matrix @ (np.asarray(self.cfg['gasket_sample_offset']) - self.drift)
        np.testing.assert_allclose([ruby[4] - 5, ruby[5] + 7], expected_origin, atol=3)
        self.assertEqual(ctrl.positions[3], 120)  # preserve the current focus through recovery
        self.assertEqual(ruby[3], 123)
        self.assertFalse(self.cfg['af_enabled'])  # no additional autofocus for ruby alignment

    def test_ruby_reacquisition_failure_does_not_return_or_retry(self):
        from apps.exp_scheduler.runner import RunnerError
        from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
        runner, ctrl = self.scheduler(blank=True)
        with self.ruby_alignment_context(runner), self.assertRaises(RunnerError) as caught:
            runner._align_to_xrd_reference(GlobalSpectrumSettings())
        self.assertEqual(caught.exception.code, follow.DETECTION_FAILED)
        self.assertEqual(ctrl.call_count('move_ch_relative'), 2)
        self.assertFalse(self.cfg['gasket_recovered'])

    def test_ruby_normal_detection_never_enters_recovery(self):
        from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
        self.target = self.reference.copy()
        self.drift[:] = 0
        self.cfg['gasket_sample_offset'] = (10., 6.)
        runner, ctrl = self.scheduler()
        with self.ruby_alignment_context(runner), mock.patch.object(follow, 'recover_at_reference') as recover:
            shift = runner._align_to_xrd_reference(GlobalSpectrumSettings())
        recover.assert_not_called()
        self.assertEqual(ctrl.call_count('move_ch_relative'), 0)
        np.testing.assert_allclose(shift, (10, 6), atol=1)

    def test_ruby_other_errors_do_not_enter_recovery(self):
        from apps.exp_scheduler.runner import RunnerError
        from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
        runner, ctrl = self.scheduler()
        runner._measure_gasket_average = mock.Mock(side_effect=RunnerError(
            follow.BEAM_INVALID, 'microscope changed'))
        with self.ruby_alignment_context(runner), mock.patch.object(follow, 'recover_at_reference') as recover:
            with self.assertRaisesRegex(RunnerError, 'microscope changed'):
                runner._align_to_xrd_reference(GlobalSpectrumSettings())
        recover.assert_not_called()
        self.assertEqual(ctrl.call_count('move_ch_relative'), 0)

    def test_ruby_stop_after_excursion_prevents_return(self):
        from apps.exp_scheduler.runner import _StopRequested
        from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
        runner, ctrl = self.scheduler()
        original = runner._get_camera_frame_after
        def frame(*args):
            if ctrl.call_count('move_ch_relative'):
                runner._stop_event.set()
            return original(*args)
        runner._get_camera_frame_after = frame
        with self.ruby_alignment_context(runner), self.assertRaises(_StopRequested):
            runner._align_to_xrd_reference(GlobalSpectrumSettings())
        self.assertEqual(ctrl.call_count('move_ch_relative'), 2)
        self.assertFalse(self.cfg['gasket_recovered'])

    def test_ruby_recovers_when_normal_correction_clips_a_previously_visible_rim(self):
        from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
        runner, ctrl = self.scheduler()
        # Start with a fully visible rim at the reference centre. The normal
        # correction then moves the sample onto the beam and clips the rim.
        initial = np.rint(self.matrix @ (self.reference - self.target - self.drift)).astype(int)
        ctrl.positions[4], ctrl.positions[5] = map(int, initial)
        runner._do_wait = mock.Mock()
        with self.ruby_alignment_context(runner):
            shift = runner._align_to_xrd_reference(GlobalSpectrumSettings())
        self.assertTrue(self.cfg['gasket_recovered'])
        self.assertEqual(ctrl.call_count('move_ch_relative'), 6)  # normal, out, back
        pos = [ctrl.positions[4], ctrl.positions[5]]
        residual = self.drift + np.linalg.solve(self.matrix, pos)
        self.assertLess(np.linalg.norm(residual), 2.)
        np.testing.assert_allclose(shift, np.asarray(self.cfg['gasket_sample_offset']) - residual, atol=1.5)
