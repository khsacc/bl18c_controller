"""Scheduler and stage-window motion hand-off, without physical hardware."""
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import Future
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1,
    )

from PyQt6.QtWidgets import QApplication

from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from apps.stage_fpd_scope.fpd_scope_stg_controller_ui import Bl18cStageControlApp
from utils.stage.control_stage_sim import PM16CControllerSim

_app = QApplication.instance() or QApplication([])


class MotionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctrl = PM16CControllerSim()
        self.ctrl.connect()
        self.addCleanup(self.ctrl.disconnect)

    def make_runner(self):
        return SequenceRunner(
            Sequence(), DeviceContext(controller=self.ctrl), data_dir=self.tmp.name,
        )

    def test_completion_does_not_read_or_write_speed_registers(self):
        runner = self.make_runner()
        with patch.object(self.ctrl, "get_ch_speed_value") as read, \
             patch.object(self.ctrl, "set_ch_speed_value") as write:
            runner.run()
        read.assert_not_called()
        write.assert_not_called()
        self.assertFalse(runner._had_error)
        self.assertEqual(list(runner.log_dir.glob("original_ch_speeds_*.json")), [])
        with self.ctrl.motion_session("Interactive Camera", "Move"):
            pass

    def test_stop_waits_for_confirmation_before_other_apps_take_motion(self):
        runner = self.make_runner()
        confirmed = Future()
        real_request = self.ctrl.request_normal_stop

        def delayed_confirmation(*, source=None):
            real_future = real_request(source=source)

            def complete():
                real_future.result(timeout=2)
                confirmed.set_result(True)

            threading.Timer(0.15, complete).start()
            return confirmed

        with patch.object(self.ctrl, "request_normal_stop", side_effect=delayed_confirmation), \
             patch.object(runner, "_execute_actions", side_effect=lambda _steps: runner.request_stop()):
            runner.run()

        self.assertTrue(confirmed.done())
        self.assertFalse(runner._had_error)
        with self.ctrl.motion_session("Interactive Camera", "Move"):
            pass

    def test_failed_stop_confirmation_reports_error(self):
        runner = self.make_runner()
        failed_confirmation = Future()
        failed_confirmation.set_exception(RuntimeError("stop not confirmed"))
        real_request = self.ctrl.request_normal_stop

        def failed_request(*, source=None):
            real_request(source=source).result(timeout=2)
            return failed_confirmation

        with patch.object(self.ctrl, "request_normal_stop", side_effect=failed_request), \
             patch.object(runner, "_execute_actions", side_effect=lambda _steps: runner.request_stop()):
            runner.run()

        self.assertTrue(runner._had_error)
        self.assertIsNone(runner._motion_lease)
        self.assertTrue(any("stop not confirmed" in failure for failure in runner._cleanup_failures))

    def test_fpd_scope_close_releases_its_idle_motion_lease(self):
        window = Bl18cStageControlApp(controller=self.ctrl)
        lease = window._ensure_lease()
        self.assertTrue(self.ctrl.coordinator.is_valid(lease))

        self.assertTrue(window.close())
        self.assertFalse(self.ctrl.coordinator.is_valid(lease))
        with self.ctrl.motion_session("Experimental Scheduler", "Sequence run"):
            pass

    def test_fpd_scope_refuses_close_while_its_move_is_active(self):
        window = Bl18cStageControlApp(controller=self.ctrl)
        lease = window._ensure_lease()
        window._is_moving = True

        self.assertFalse(window.close())
        self.assertTrue(self.ctrl.coordinator.is_valid(lease))

        window._is_moving = False
        self.assertTrue(window.close())


if __name__ == "__main__":
    unittest.main()
