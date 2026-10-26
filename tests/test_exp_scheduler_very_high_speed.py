"""Temporary scheduler speed overrides, exercised without physical hardware."""
import json
import os
import sys
import tempfile
import types
import unittest
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
from apps.exp_scheduler.scheduler_settings import GlobalPositions
from apps.exp_scheduler.sequence import Sequence
from apps.speed_controller.speed_controller_app import SpeedControllerWindow
from utils.stage.control_stage_sim import PM16CControllerSim

_app = QApplication.instance() or QApplication([])


class VeryHighSpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctrl = PM16CControllerSim()
        self.ctrl.connect()
        self.addCleanup(self.ctrl.disconnect)
        self.original = {
            ch: {level: self.ctrl.get_ch_speed_value(ch, level)
                 for level in ("L", "M", "H")}
            for ch in range(1, 12)
        }

    def make_runner(self):
        return SequenceRunner(
            Sequence(), DeviceContext(controller=self.ctrl),
            global_positions=GlobalPositions(allow_very_high_speed=True),
            data_dir=self.tmp.name,
        )

    def assert_restored(self):
        self.assertEqual(
            {ch: {level: self.ctrl.get_ch_speed_value(ch, level)
                  for level in ("L", "M", "H")}
             for ch in range(1, 12)}, self.original,
        )

    def test_completion_saves_loadable_backup_and_restores(self):
        runner = self.make_runner()
        observed = []

        def during_sequence(_steps):
            observed.extend(
                (ch, level, self.ctrl.get_ch_speed_value(ch, level))
                for ch in (8, 9) for level in ("L", "M", "H")
            )

        with patch.object(runner, "_execute_actions", side_effect=during_sequence):
            runner.run()
        self.assertEqual(observed, [(8, level, 8500) for level in ("L", "M", "H")]
                         + [(9, level, 2000) for level in ("L", "M", "H")])
        backups = list(runner.log_dir.glob("original_ch_speeds_*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(SpeedControllerWindow._parse_payload(
            json.loads(backups[0].read_text(encoding="utf-8"))), self.original)
        self.assert_restored()

    def test_execution_error_restores(self):
        runner = self.make_runner()
        with patch.object(runner, "_execute_actions", side_effect=RuntimeError("test failure")):
            runner.run()
        self.assertTrue(runner._had_error)
        self.assert_restored()

    def test_user_stop_restores_after_lease_revocation(self):
        runner = self.make_runner()
        with patch.object(runner, "_execute_actions", side_effect=lambda _steps: runner.request_stop()):
            runner.run()
        self.assertFalse(runner._had_error)
        self.assertTrue(runner.user_stop_requested)
        self.assert_restored()

    def test_partial_override_failure_restores_without_running_sequence(self):
        runner = self.make_runner()
        original_set = self.ctrl.set_ch_speed_value
        failed = False

        def fail_once(ch, level, value, *, motion=None):
            nonlocal failed
            if not failed and ch == 9 and level == "L" and value == 2000:
                failed = True
                raise RuntimeError("injected write failure")
            return original_set(ch, level, value, motion=motion)

        with patch.object(self.ctrl, "set_ch_speed_value", side_effect=fail_once), \
             patch.object(runner, "_execute_actions") as execute:
            runner.run()
        execute.assert_not_called()
        self.assertTrue(runner._had_error)
        self.assert_restored()

    def test_backup_read_failure_prevents_override_and_sequence(self):
        runner = self.make_runner()
        original_get = self.ctrl.get_ch_speed_value

        def fail_read(ch, level):
            return None if (ch, level) == (4, "M") else original_get(ch, level)

        with patch.object(self.ctrl, "get_ch_speed_value", side_effect=fail_read), \
             patch.object(self.ctrl, "set_ch_speed_value") as write, \
             patch.object(runner, "_execute_actions") as execute:
            runner.run()
        write.assert_not_called()
        execute.assert_not_called()
        self.assertTrue(runner._had_error)
        self.assertEqual(list(runner.log_dir.glob("original_ch_speeds_*.json")), [])

    def test_restore_failure_reports_error_and_keeps_backup(self):
        runner = self.make_runner()
        original_set = self.ctrl.set_ch_speed_value

        def fail_restore(ch, level, value, *, motion=None):
            if (ch, level, value) == (8, "L", self.original[8]["L"]):
                raise RuntimeError("injected restore failure")
            return original_set(ch, level, value, motion=motion)

        with patch.object(self.ctrl, "set_ch_speed_value", side_effect=fail_restore):
            runner.run()
        self.assertTrue(runner._had_error)
        self.assertEqual(len(list(runner.log_dir.glob("original_ch_speeds_*.json"))), 1)

    def test_stop_during_restoration_reacquires_lease_and_finishes(self):
        runner = self.make_runner()
        original_set = self.ctrl.set_ch_speed_value
        stopped = False

        def stop_during_restore(ch, level, value, *, motion=None):
            nonlocal stopped
            if not stopped and (ch, level, value) == (1, "L", self.original[1]["L"]):
                stopped = True
                runner.request_stop()
            return original_set(ch, level, value, motion=motion)

        with patch.object(self.ctrl, "set_ch_speed_value", side_effect=stop_during_restore):
            runner.run()
        self.assertTrue(stopped)
        self.assertFalse(runner._had_error)
        self.assert_restored()


if __name__ == "__main__":
    unittest.main()
