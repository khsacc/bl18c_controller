"""The combined LakeShore step keeps the primitive actions' contracts."""
import os
import sys
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

from apps.exp_scheduler.actions import (
    AllHeatersOffAction, SetAndWaitTemperatureAction, SetTemperatureAction,
    TakeXrdAction, WaitTemperatureAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.runner import SequenceRunner
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui.step_editor import StepEditorDialog
from apps.exp_scheduler.validator.models import Severity
from apps.exp_scheduler.validator.pre_validator import PreValidator
from tests.exp_scheduler_fakes import FakeLakeshore

_app = QApplication.instance() or QApplication([])


class CombinedTemperatureTests(unittest.TestCase):
    def test_json_and_dsl_round_trip_all_fields_and_loop_reference(self):
        action = SetAndWaitTemperatureAction("target", 2.5, 0.4, "A")
        self.assertEqual(Sequence.from_dict(Sequence([action]).to_dict()).actions, [action])
        result = DslCompiler().compile(
            'for target in [280.0, 290.0]:\n'
            '    set_and_wait_temperature(value=target, unit="K", ramp_rate=2.5, '
            'tol=0.4, channel="A")\n'
        )
        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(result.sequence.actions[0].body, [action])

    def test_visual_editor_preserves_all_fields(self):
        original = SetAndWaitTemperatureAction(285.0, 3.0, 0.5, "A")
        dialog = StepEditorDialog(action=original)
        self.addCleanup(dialog.close)
        dialog._on_ok()
        self.assertEqual(dialog.get_action(), original)

    def test_runner_calls_existing_set_then_wait_with_same_step_index(self):
        action = SetAndWaitTemperatureAction("target", 3.0, 0.5, "A")
        runner = SequenceRunner(Sequence([action]), DeviceContext())
        runner._current_step_idx = 7
        calls = []
        with patch.object(runner, "_do_set_temperature") as set_temp, patch.object(
            runner, "_do_wait_temperature"
        ) as wait_temp:
            set_temp.side_effect = lambda *_: calls.append("set")
            wait_temp.side_effect = lambda *_: calls.append("wait")
            runner._execute_one(action, {"target": 280.0})
        self.assertEqual(calls, ["set", "wait"])
        set_temp.assert_called_once_with(SetTemperatureAction("target", 3.0), {"target": 280.0})
        wait_temp.assert_called_once_with(WaitTemperatureAction(0.5, "A"), 7)

    def test_preflight_applies_both_parameter_checks_and_wait_state(self):
        action = SetAndWaitTemperatureAction(280.0, -1.0, 0.0, "C")
        result = PreValidator().validate(Sequence([action]), DeviceContext(lakeshore=FakeLakeshore()))
        codes = {d.code for d in result.diagnostics}
        self.assertIn("static.lakeshore.non_finite_ramp_rate", codes)
        self.assertIn("static.lakeshore.non_finite_tol_k", codes)
        self.assertIn("static.lakeshore.invalid_temperature_channel", codes)
        self.assertNotIn("preflight.lakeshore.wait_temperature_without_preceding_set", codes)

    def test_preflight_treats_arrival_as_complete_before_xrd_and_checks_heater(self):
        action = SetAndWaitTemperatureAction(280.0, 2.0, 0.5)
        sequence = Sequence([AllHeatersOffAction(), action, TakeXrdAction()])
        result = PreValidator().validate(sequence, DeviceContext(lakeshore=FakeLakeshore()))
        codes = {d.code for d in result.diagnostics}
        self.assertIn("preflight.lakeshore.set_temperature_after_all_heaters_off", codes)
        self.assertNotIn("preflight.lakeshore.xrd_without_wait_temperature", codes)


class CoolingToleranceValidationTests(unittest.TestCase):
    _CODE = "preflight.lakeshore.cooling_tb_tolerance_may_be_too_small"

    def _diagnostics(self, actions, *, temp_b=300.0, setpoint=300.0):
        data = [] if temp_b is None else [types.SimpleNamespace(temp_b_k=temp_b)]
        lakeshore = FakeLakeshore(setpoint=setpoint, data=data)
        result = PreValidator().validate(Sequence(actions), DeviceContext(lakeshore=lakeshore))
        return [d for d in result.diagnostics if d.code == self._CODE]

    def test_combined_and_standalone_b_wait_warn_when_cooling(self):
        combined = SetAndWaitTemperatureAction(280.0, 2.0, 0.39, "B")
        standalone = WaitTemperatureAction(0.39, "B")
        for warnings in (
            self._diagnostics([combined]),
            self._diagnostics([standalone], setpoint=280.0),
        ):
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0].severity, Severity.WARNING)

    def test_threshold_channel_and_direction(self):
        for action, temp_b in (
            (SetAndWaitTemperatureAction(280.0, 2.0, 0.4, "B"), 300.0),
            (SetAndWaitTemperatureAction(280.0, 2.0, 0.39, "A"), 300.0),
            (SetAndWaitTemperatureAction(295.0, 2.0, 0.39, "B"), 290.0),
            (SetAndWaitTemperatureAction(280.0, 2.0, 0.39, "B"), 280.2),
        ):
            with self.subTest(action=action, temp_b=temp_b):
                self.assertEqual(self._diagnostics([action], temp_b=temp_b), [])

    def test_missing_reading_does_not_guess_direction(self):
        action = SetAndWaitTemperatureAction(280.0, 2.0, 0.39, "B")
        self.assertEqual(self._diagnostics([action], temp_b=None), [])

    def test_later_wait_uses_prior_b_arrival_band(self):
        actions = [
            SetAndWaitTemperatureAction(290.0, 2.0, 1.0, "B"),
            SetAndWaitTemperatureAction(280.0, 2.0, 0.39, "B"),
        ]
        warnings = self._diagnostics(actions)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].action_path, "[1]")


if __name__ == "__main__":
    unittest.main()
