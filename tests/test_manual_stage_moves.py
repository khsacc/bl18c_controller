"""Shared camera manual moves, with the real motion coordinator and simulator."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from concurrent.futures import Future
from unittest import mock

import numpy as np
from PyQt6.QtCore import QPointF, QThread
from PyQt6.QtWidgets import QApplication

from apps.interactive_camera.manual_stage_moves import ManualStageMoves, pixel_move
from apps.interactive_camera.interactive_camera import MainWindow
from apps.interactive_camera.sample_positions_dialog import SamplePositionsDialog
from apps.interactive_camera.xray_beam import XrayBeamPosition
from utils.stage.control_stage_sim import PM16CControllerSim

APP = QApplication.instance() or QApplication([])


class _StageTests(unittest.TestCase):
    def setUp(self):
        self.stage = PM16CControllerSim()
        self.origin = {3: 40000, 4: 20000, 5: 30000}
        self.stage._positions.update(self.origin)
        self.stage._targets.update(self.origin)
        self.stage.connect()
        self.addCleanup(self.stage.disconnect)
        self.moves = ManualStageMoves(self.stage, "Test camera")
        self.errors = []
        self.moves.error.connect(self.errors.append)

    def finish(self, moves=None):
        moves = moves or self.moves
        deadline = time.monotonic() + 4
        while moves.busy and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        self.assertFalse(moves.busy, "Worker did not finish")
        moves.thread.join(timeout=1)
        self.assertFalse(moves.thread.is_alive())


class ManualStageMovesTests(_StageTests):
    def test_pixel_conversion_preserves_sign_coupling_and_truncation(self):
        self.assertEqual(pixel_move([[2, .5], [-.5, 3]], (20, 30), (10.2, 40.2),
                                    (80, 100, 3)), {4: -14, 5: 35})
        for matrix in ([[1, 2]], [[1, 0], [0, float("nan")]], [[0, 0], [0, 0]]):
            with self.assertRaises(ValueError):
                pixel_move(matrix, (10, 10), (20, 20), (80, 100, 3))
        with self.assertRaises(ValueError):
            pixel_move(np.eye(2), (-1, 10), (20, 20), (80, 100, 3))

    def test_relative_axes_and_local_before_release_on_worker(self):
        calls = []
        original_loc = self.stage.switch_to_loc

        def loc(**kwargs):
            self.assertNotEqual(QThread.currentThread(), APP.thread())
            self.assertIsNotNone(self.stage.get_motion_holder())
            calls.append("loc")
            original_loc(**kwargs)

        with mock.patch.object(self.stage, "switch_to_loc", side_effect=loc):
            for ch, pulses in ((3, -13), (4, 17), (5, -21)):
                self.moves.relative(ch, pulses, speed="M")
                self.finish()
                self.assertEqual(int(self.stage.get_ch_pos(ch)) - self.origin[ch], pulses)
                self.assertTrue(self.stage.is_motion_available())
        self.assertEqual(calls, ["loc"] * 3)
        self.assertEqual(self.errors, [])

    def test_clicked_position_moves_both_axes(self):
        self.moves.to_pixel((10, 20), (50, 40), [[2, 0], [0, -3]], (80, 100, 3))
        self.finish()
        self.assertEqual((int(self.stage.get_ch_pos(4)) - self.origin[4], int(self.stage.get_ch_pos(5)) - self.origin[5]), (80, -60))

    def test_invalid_beam_refuses_movement_and_speed_changes(self):
        beam = XrayBeamPosition(40, 30, 100, 80, {6: 1, 7: 2, 8: 3})
        with mock.patch.object(self.stage, "move_ch_relative", wraps=self.stage.move_ch_relative) as move:
            self.moves.to_pixel((10, 10), beam.point, np.eye(2), (80, 100, 3), beam=beam)
            self.finish()
            move.assert_not_called()
        self.assertIn("microscope has moved", self.errors[0])
        self.assertTrue(self.stage.is_motion_available())

    def test_valid_beam_readback_occurs_on_worker_with_motion_owned(self):
        # Ch7 differs from the registered value and must not even be queried.
        beam = XrayBeamPosition(40, 30, 100, 80, {6: 12000, 7: 119999, 8: 0})
        original = self.stage.get_ch_pos

        def read(ch):
            self.assertNotEqual(ch, 7)
            self.assertNotEqual(QThread.currentThread(), APP.thread())
            self.assertEqual(self.stage.get_motion_holder()["owner"], "Test camera")
            return original(ch)

        with mock.patch.object(self.stage, "get_ch_pos", side_effect=read):
            self.moves.to_pixel((10, 10), beam.point, np.eye(2), (80, 100, 3), beam=beam)
            self.finish()
        self.assertEqual(self.errors, [])
        self.assertEqual(int(self.stage.get_ch_pos(4)) - self.origin[4], 30)

    def test_cancel_before_acquisition_never_moves_or_stops_other_owner(self):
        entered, release = threading.Event(), threading.Event()
        acquire = self.stage.acquire_motion

        def delayed_acquire(*args, **kwargs):
            entered.set()
            release.wait(2)
            return acquire(*args, **kwargs)

        with (mock.patch.object(self.stage, "acquire_motion", side_effect=delayed_acquire),
              mock.patch.object(self.stage, "move_ch_relative") as move,
              mock.patch.object(self.stage, "request_normal_stop") as stop):
            self.moves.relative(3, 10)
            self.assertTrue(entered.wait(1))
            self.moves.stop()
            release.set()
            self.finish()
            move.assert_not_called()
            stop.assert_not_called()
        self.assertTrue(self.stage.is_motion_available())

    def test_wait_failure_uses_confirmed_stop(self):
        with mock.patch.object(self.stage, "wait_until_stop", side_effect=RuntimeError("STQ? failed")):
            self.moves.relative(3, 1000)
            self.finish()
        self.assertIn("STQ? failed", self.errors[0])
        self.assertEqual(self.stage.get_stop_progress(), "confirmed")
        self.assertFalse(self.stage.get_is_moving())

    def test_competing_owner_is_never_stopped(self):
        lease = self.stage.acquire_motion("Scheduler", "Run")
        with mock.patch.object(self.stage, "request_normal_stop", wraps=self.stage.request_normal_stop) as stop:
            self.moves.relative(3, 20)
            self.finish()
            stop.assert_not_called()
        self.assertTrue(self.errors)
        self.assertTrue(self.stage.coordinator.is_valid(lease))
        self.stage.release_motion(lease)

    def test_second_axis_failure_confirms_stop_before_release(self):
        original = self.stage.move_ch_relative
        calls = []
        stop = self.stage.request_normal_stop

        def move(ch, pulses, **kwargs):
            if ch == 5:
                raise ValueError("Move blocked: test limit")
            original(ch, pulses, **kwargs)

        def request(**kwargs):
            calls.append("stop")
            return stop(**kwargs)

        with (mock.patch.object(self.stage, "move_ch_relative", side_effect=move),
              mock.patch.object(self.stage, "request_normal_stop", side_effect=request)):
            self.moves.to_pixel((0, 0), (90, 70), [[100, 0], [0, 100]], (80, 100, 3))
            self.finish()
        self.assertEqual(calls, ["stop"])
        self.assertEqual(self.stage.get_stop_progress(), "confirmed")
        self.assertFalse(self.stage.get_is_moving())
        self.assertTrue(self.stage.is_motion_available())
        self.assertIn("Move blocked: test limit", self.errors[0])

    def test_busy_stop_and_confirmation_are_asynchronous(self):
        entered = threading.Event()
        release = threading.Event()
        wait = self.stage.wait_until_stop

        def delayed_wait(**kwargs):
            entered.set()
            release.wait(2)
            wait(**kwargs)

        with mock.patch.object(self.stage, "wait_until_stop", side_effect=delayed_wait):
            self.moves.relative(3, 9999, speed="L")
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.moves.relative(4, 10))
            self.moves.stop()
            self.assertTrue(self.moves.busy)
            release.set()
            self.finish()
        self.assertFalse(self.stage.get_is_moving())
        self.assertLess(int(self.stage.get_ch_pos(3)) - self.origin[3], 9999)
        self.assertTrue(self.stage.is_motion_available())

    def test_error_delivery_is_on_gui_thread(self):
        threads = []
        self.moves.error.connect(lambda _: threads.append(QThread.currentThread()))
        with mock.patch.object(self.stage, "get_is_moving", side_effect=RuntimeError("query failed")):
            self.moves.relative(3, 10)
            self.finish()
        self.assertEqual(threads, [APP.thread()])
        self.assertIn("query failed", self.errors[0])

    def test_stop_during_speed_setup_is_cancellation_without_warning(self):
        entered, release = threading.Event(), threading.Event()
        original = self.stage.set_ch_speed

        def speed(*args, **kwargs):
            entered.set()
            release.wait(2)
            original(*args, **kwargs)

        with (mock.patch.object(self.stage, "set_ch_speed", side_effect=speed),
              mock.patch.object(self.stage, "move_ch_relative") as move):
            self.moves.relative(3, 100, speed="M")
            self.assertTrue(entered.wait(1))
            self.moves.stop()
            release.set()
            self.finish()
            move.assert_not_called()
        self.assertEqual(self.errors, [])
        self.assertTrue(self.stage.is_motion_available())

    def test_failed_stop_retains_controller_recovery_requirement(self):
        # The actual coordinator owns recovery gating even after the worker exits.
        def failed_stop(**kwargs):
            ticket = self.stage.coordinator.revoke_for_stop(source="test", emergency=False)
            self.stage.coordinator.note_stop_send_failed(ticket)
            future = Future()
            future.set_exception(RuntimeError("stop failed"))
            return future

        with (mock.patch.object(self.stage, "move_ch_relative", side_effect=RuntimeError("send failed")),
              mock.patch.object(self.stage, "request_normal_stop", side_effect=failed_stop)):
            self.moves.relative(3, 10)
            self.finish()
        self.assertFalse(self.stage.is_motion_available())
        self.assertIn("stop failed", self.errors[0])


class SamplePositionsMovementTests(_StageTests):
    def dialog(self):
        dialog = SamplePositionsDialog(self.stage, None, None, None, {}, borrowed_frame=lambda: None)
        self.addCleanup(dialog.close)
        dialog._moves.error.disconnect(dialog._move_error)
        dialog._camera_timer.stop()
        dialog._on_frame(np.zeros((80, 100, 3), np.uint8))
        dialog.image_label.resize(800, 500)
        return dialog

    @staticmethod
    def image_point(dialog, x, y):
        rect = dialog.image_label.contentsRect()
        size = dialog.image_label.pixmap().deviceIndependentSize()
        return QPointF(rect.x() + (rect.width() - size.width()) / 2 + x * size.width() / 100,
                       rect.y() + (rect.height() - size.height()) / 2 + y * size.height() / 80)

    def test_click_is_opt_in_and_ignores_letterbox(self):
        dialog = self.dialog()
        with mock.patch.object(dialog._moves, "to_pixel") as move:
            point = self.image_point(dialog, 10, 20)
            dialog._click_move(point)
            move.assert_not_called()
            dialog.click_move_checkbox.setChecked(True)
            dialog._click_move(QPointF(0, 0))
            move.assert_not_called()
            with mock.patch("apps.interactive_camera.sample_positions_dialog._load_matrix_inv", return_value=np.eye(2)):
                dialog._click_move(point)
            self.assertTrue(np.allclose(move.call_args.args[0], (10, 20)))
            self.assertEqual(move.call_args.args[1], (50, 40))

    def test_laser_target_uses_shared_worker_and_preserves_raw_frame(self):
        dialog = self.dialog()
        dialog.click_move_checkbox.setChecked(True)
        dialog.move_target.setCurrentText("Laser spot")
        with (mock.patch.object(dialog._marks, "get", return_value=([], (70, 60))),
              mock.patch("apps.interactive_camera.sample_positions_dialog._load_matrix_inv", return_value=np.eye(2))):
            dialog._click_move(self.image_point(dialog, 10, 20))
            self.finish(dialog._moves)
        self.assertEqual((int(self.stage.get_ch_pos(4)) - self.origin[4], int(self.stage.get_ch_pos(5)) - self.origin[5]), (60, 40))
        self.assertFalse(dialog.frames.get()[0].any())

    def test_missing_frame_cannot_issue_click_move(self):
        dialog = self.dialog()
        dialog.click_move_checkbox.setChecked(True)
        dialog._poll_borrowed()
        with mock.patch.object(dialog._moves, "to_pixel") as move:
            dialog._click_move(QPointF(400, 250))
            move.assert_not_called()
        self.assertIsNone(dialog.frames.get()[0])

    def test_xray_target_accepts_changed_ch7(self):
        dialog = self.dialog()
        dialog.click_move_checkbox.setChecked(True)
        dialog.move_target.setCurrentText("X-ray beam")
        beam = XrayBeamPosition(70, 60, 100, 80, {6: 12000, 7: 119999, 8: 0})
        with (mock.patch.object(dialog._xray_beam, "get", return_value=beam),
              mock.patch("apps.interactive_camera.sample_positions_dialog._load_matrix_inv", return_value=np.eye(2))):
            dialog._click_move(self.image_point(dialog, 10, 20))
            self.finish(dialog._moves)
        self.assertEqual(int(self.stage.get_ch_pos(4)) - self.origin[4], 60)
        self.assertEqual(int(self.stage.get_ch_pos(5)) - self.origin[5], 40)

    def test_moving_blocks_registration_and_close_until_stopped(self):
        dialog = self.dialog()
        entered, release = threading.Event(), threading.Event()
        original = self.stage.wait_until_stop

        def wait(**kwargs):
            entered.set()
            release.wait(2)
            original(**kwargs)

        with mock.patch.object(self.stage, "wait_until_stop", side_effect=wait):
            dialog.relative_steps[3].setValue(9999)
            dialog._relative_move(3, 1)
            self.assertTrue(entered.wait(1))
            self.assertFalse(dialog.capture_button.isEnabled())
            self.assertFalse(dialog.record_button.isEnabled())
            self.assertFalse(dialog.close_button.isEnabled())
            self.assertTrue(dialog.stop_move_button.isEnabled())
            with self.assertRaises(RuntimeError):
                dialog._read_position()
            self.assertFalse(dialog._prepare_close())
            self.assertTrue(dialog._position_timer.isActive())
            release.set()
            self.finish(dialog._moves)
        self.assertTrue(dialog.close_button.isEnabled())
        self.assertTrue(dialog._prepare_close())


class InteractiveCameraMovementTests(_StageTests):
    def window(self):
        frame = np.zeros((80, 100, 3), np.uint8)
        session = mock.Mock(frame_size=(100, 80), fps=30)
        session.frame_after.return_value = frame
        session.latest.return_value = (frame, time.monotonic())
        window = MainWindow(controller=self.stage, camera_session=session)
        window.timer.stop()
        window.current_frame = frame
        window.calibration_data = {"matrix_inv": [[1, 0], [0, 1]]}
        window._save_shapes = mock.Mock()
        self.addCleanup(window.close)
        return window

    def test_ic_relative_and_click_adapters_use_shared_worker(self):
        window = self.window()
        window._move_relative_ch(3, -15)
        self.assertTrue(window.is_moving)
        self.assertIs(window._move_thread, window._manual_moves.thread)
        self.finish(window._manual_moves)
        self.assertFalse(window.is_moving)
        window._move_to_centre(10, 20)
        self.finish(window._manual_moves)
        window.laser_spot_pos = (70, 60)
        window._move_to_laser_spot(50, 40)
        self.finish(window._manual_moves)
        self.assertEqual(int(self.stage.get_ch_pos(3)) - self.origin[3], -15)
        self.assertEqual(int(self.stage.get_ch_pos(4)) - self.origin[4], 60)
        self.assertEqual(int(self.stage.get_ch_pos(5)) - self.origin[5], 40)

    def test_ic_read_only_refuses_manual_moves(self):
        window = self.window()
        window._sequence_read_only = True
        with mock.patch.object(window._manual_moves, "relative") as move:
            window._move_relative_ch(3, 10)
            move.assert_not_called()
        window._sequence_read_only = False

    def test_ic_xray_target_accepts_changed_ch7(self):
        window = self.window()
        beam = XrayBeamPosition(70, 60, 100, 80, {6: 12000, 7: 119999, 8: 0})
        with mock.patch.object(window._xray_beam_cache, "get", return_value=beam):
            window._move_to_xray_beam_pos(10, 20)
            self.finish(window._manual_moves)
        self.assertEqual(int(self.stage.get_ch_pos(4)) - self.origin[4], 60)
        self.assertEqual(int(self.stage.get_ch_pos(5)) - self.origin[5], 40)

    def test_ic_limit_warning_preserves_reason_on_gui_thread(self):
        window = self.window()
        warnings = []

        def warning(parent, title, reason):
            self.assertEqual(QThread.currentThread(), APP.thread())
            warnings.append((title, reason))

        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning", side_effect=warning):
            window._move_relative_ch(3, -999999)
            self.finish(window._manual_moves)
        self.assertEqual(warnings[0][0], "Move Blocked")
        self.assertIn("soft limit", warnings[0][1])


if __name__ == "__main__":
    unittest.main()
