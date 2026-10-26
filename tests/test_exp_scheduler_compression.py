"""Hardware-free checks for ruby-guided PACE5000 compression."""
from __future__ import annotations

import ast
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox, QRadioButton

from apps.exp_scheduler.actions import (
    CompressToTargetPressureUsingRubyAction, ForLoopAction, SetControlModeAction,
    SetPressureAction, TakeSpectrumAction, TakeXrdAction, action_from_dict,
    action_loop_var_ref, rename_loop_var_refs,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.dsl.parser import SequenceBuilder
from apps.exp_scheduler.dsl.compiler import DslCompiler
from apps.exp_scheduler.dsl import _factories
from apps.exp_scheduler.dsl.validator import ASTValidator
from apps.exp_scheduler.nearby_search import build_search_axes, effective_search_action
from apps.exp_scheduler.runner import RunnerError, SequenceRunner, _StopRequested
from apps.exp_scheduler.run_plan import build_run_plan
from apps.exp_scheduler.scheduler_settings import GlobalSpectrumSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.ui.step_editor import StepEditorDialog, _page_take_spectrum
from apps.exp_scheduler.validator.execution_trace import ExecutionTrace
from apps.exp_scheduler.validator.pre_validator import PreCheckResult, _check_spectrum
from apps.exp_scheduler.validator.checks.pace5000 import (
    check_compression_sequence, check_pace5000_source_pressure,
)
from apps.exp_scheduler.validator.snapshots import (
    PaceSnapshot, ValidationSnapshot, collect_pace_snapshot, determine_requirements,
)
from utils.stage.control_stage_sim import PM16CControllerSim

_app = QApplication.instance() or QApplication([])


class FakePace:
    connected = True

    def __init__(self):
        self.target = 1.0
        self.events = []

    def get_output_state(self):
        return "ON"

    def query(self, command):
        assert command == ":UNIT:PRES?"
        return "MPA"

    def get_target_pressure(self):
        return str(self.target)

    def get_pressure(self):
        return self.target

    def get_positive_source_pressure(self):
        return 10.0

    def get_slew_rate(self):
        return "0.01"  # MPa/s = 0.6 MPa/min

    def set_pressure_with_ramp(self, target, rate, **callbacks):
        self.events.append(("set", target, rate))
        self.target = target

    def wait_for_pressure(self, tol, **kwargs):
        self.events.append(("arrival", self.target, tol))
        return self.target


class CompressionTests(unittest.TestCase):
    def test_loop_target_round_trips_through_json_and_dsl(self):
        action = CompressToTargetPressureUsingRubyAction(target_sample_pressure_gpa="p")
        loop = ForLoopAction(var="p", values=[1.0, 2.0], body=[action])
        encoded = action.to_dict()
        self.assertEqual(encoded["target_sample_pressure_gpa_var"], "p")
        self.assertNotIn("target_sample_pressure_gpa", encoded)
        self.assertEqual(action_from_dict(encoded), action)
        self.assertEqual(action_from_dict(loop.to_dict()), loop)
        self.assertEqual(action_loop_var_ref(action), "p")
        self.assertIn("target_sample_pressure_gpa=p", loop.to_dsl())
        self.assertTrue(DslCompiler().compile(loop.to_dsl()).ok)
        rebuilt = SequenceBuilder().build(ast.parse(loop.to_dsl())).actions[0]
        self.assertEqual(rebuilt, loop)
        rename_loop_var_refs(loop.body, "p", "target")
        self.assertEqual(action.target_sample_pressure_gpa, "target")

    def test_loop_target_is_checked_for_each_iteration(self):
        action = CompressToTargetPressureUsingRubyAction(target_sample_pressure_gpa="p")
        loop = ForLoopAction(var="p", values=[1.0, 0.0], body=[action])
        result = PreCheckResult()
        _check_spectrum(
            ExecutionTrace.build([loop]), DeviceContext(),
            GlobalSpectrumSettings(pressure_enabled=True), result,
        )
        errors = [d for d in result.diagnostics
                  if d.code == "static.compression.parameters_invalid"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Target sample pressure", errors[0].message)
        self.assertIn("p=0", errors[0].loop_context)

    def test_loop_target_keeps_pace_preflight_checks(self):
        action = CompressToTargetPressureUsingRubyAction(
            target_sample_pressure_gpa="p", max_allowed_gas_pressure=1.5,
        )
        loop = ForLoopAction(var="p", values=[1.0, 2.0], body=[action])
        self.assertIn(
            "preflight.pace5000.compression_control_mode_required",
            self._preflight([loop], mode="OFF"),
        )

    def test_action_json_and_dsl_round_trip(self):
        original = CompressToTargetPressureUsingRubyAction(
            position_name="Ruby B", target_sample_pressure_gpa=2.0,
            gas_pressure_increment=0.2, gas_pressure_unit="Bar",
            stabilisation_duration_s=5.0, max_allowed_gas_pressure=20.0,
            gas_pressure_rate=0.5, gas_pressure_rate_unit="Bar/min",
            exposure_time_s=0.2, search_best_position_nearby=True,
            use_global_search=True,
        )
        self.assertEqual(action_from_dict(original.to_dict()), original)
        dsl = original.to_dsl()
        self.assertEqual(ASTValidator().validate(dsl), [])
        rebuilt = SequenceBuilder().build(ast.parse(dsl)).actions[0]
        self.assertEqual(rebuilt, original)

    def test_boolean_pressure_fields_are_rejected_before_numeric_conversion(self):
        action = CompressToTargetPressureUsingRubyAction(
            gas_pressure_rate=0.5, gas_pressure_tolerance=0.02,
        )
        numeric_fields = (
            "target_sample_pressure_gpa", "gas_pressure_increment",
            "stabilisation_duration_s", "max_allowed_gas_pressure",
            "gas_pressure_rate", "gas_pressure_tolerance",
        )
        for field in numeric_fields:
            with self.subTest(field=field):
                encoded = action.to_dict()
                encoded[field] = True
                with self.assertRaisesRegex(ValueError, field):
                    action_from_dict(encoded)
                with self.assertRaisesRegex(ValueError, field):
                    _factories.compress_to_target_pressure_using_ruby(encoded)
                expression = action.to_dsl().replace(
                    f"{field}={getattr(action, field)!r}", f"{field}=True", 1,
                )
                self.assertFalse(DslCompiler().compile(expression).ok)

    def test_preflight_requests_pace_source_control_state_and_current_setpoint(self):
        action = CompressToTargetPressureUsingRubyAction(
            max_allowed_gas_pressure=20.0, gas_pressure_unit="Bar",
        )
        requirements = determine_requirements(ExecutionTrace.build([action]), None)
        self.assertTrue(requirements.stage_moving)
        self.assertTrue(requirements.pace_used)
        self.assertTrue(requirements.pace_output_state)
        self.assertTrue(requirements.pace_target)
        self.assertEqual(requirements.pace_max_set_pressure_mpa, 2.0)
        self.assertTrue(requirements.pace_slew)
        self.assertTrue(requirements.pace_measured)

    def _preflight(self, actions, *, target=1.0, measured=1.0, source=10.0,
                   mode="ON", slew=0.6):
        trace = ExecutionTrace.build(actions)
        requirements = determine_requirements(trace, None)
        snapshot = ValidationSnapshot(pace=PaceSnapshot(
            connected=True, output_state=mode, unit="MPa",
            target_pressure_mpa=target,
            positive_source_pressure_mpa=source,
            slew_rate_mpa_per_min=slew,
            measured_pressure_mpa=measured,
        ))
        result = PreCheckResult()
        check_pace5000_source_pressure(requirements, snapshot, result)
        check_compression_sequence(trace, snapshot, result)
        return {d.code for d in result.diagnostics}

    def test_source_pressure_must_cover_allowed_maximum(self):
        action = CompressToTargetPressureUsingRubyAction(
            max_allowed_gas_pressure=2.0,
        )
        self.assertIn(
            "preflight.pace5000.source_pressure_insufficient",
            self._preflight([action], source=1.99),
        )
        self.assertNotIn(
            "preflight.pace5000.source_pressure_insufficient",
            self._preflight([action], source=2.0),
        )
        self.assertIn(
            "preflight.pace5000.compression_source_headroom_missing",
            self._preflight([action], source=2.0),
        )
        self.assertNotIn(
            "preflight.pace5000.compression_source_headroom_missing",
            self._preflight([action], source=2.01),
        )

    def test_preceding_setpoint_and_mode_are_checked_at_compression_step(self):
        compression = CompressToTargetPressureUsingRubyAction(
            max_allowed_gas_pressure=1.5,
        )
        prior_set = SetPressureAction(
            pressure=2.0, unit="MPa", rate=0.2, rate_unit="MPa/min",
        )
        codes = self._preflight([prior_set, compression])
        self.assertIn("preflight.pace5000.compression_above_maximum", codes)
        codes = self._preflight([SetControlModeAction(enabled=False), compression])
        self.assertIn("preflight.pace5000.compression_control_mode_required", codes)

    def test_unsettled_initial_pressure_warns_and_missing_slew_fails(self):
        action = CompressToTargetPressureUsingRubyAction(
            max_allowed_gas_pressure=2.0,
        )
        self.assertIn(
            "preflight.pace5000.compression_not_at_setpoint",
            self._preflight([action], measured=0.8),
        )
        self.assertIn(
            "preflight.pace5000.compression_slew_unreadable",
            self._preflight([action], slew=None),
        )
        prior_set = SetPressureAction(
            pressure=1.2, unit="MPa", rate=0.2, rate_unit="MPa/min",
        )
        self.assertIn(
            "preflight.pace5000.compression_waits_for_prior_set",
            self._preflight([prior_set, action], target=1.0),
        )
        wide_band = CompressToTargetPressureUsingRubyAction(
            max_allowed_gas_pressure=1.0, gas_pressure_increment=0.5,
            gas_pressure_tolerance=0.2,
        )
        self.assertIn(
            "preflight.pace5000.compression_measured_above_maximum",
            self._preflight([wide_band], target=0.9, measured=1.05),
        )

    def test_many_increments_warn_without_blocking(self):
        action = CompressToTargetPressureUsingRubyAction(
            gas_pressure_increment=0.001, gas_pressure_tolerance=0.0001,
            max_allowed_gas_pressure=2.5,
        )
        self.assertIn(
            "preflight.pace5000.compression_many_increments",
            self._preflight([action], target=1.0),
        )

    def test_bar_snapshot_converts_target_source_slew_and_measured_pressure(self):
        pace = FakePace()
        pace.query = lambda command: "BAR"
        pace.target = 10.0
        pace.get_positive_source_pressure = lambda: 100.0
        pace.get_slew_rate = lambda: "0.1"
        action = CompressToTargetPressureUsingRubyAction()
        requirements = determine_requirements(ExecutionTrace.build([action]), None)
        result = PreCheckResult()
        snapshot = collect_pace_snapshot(DeviceContext(pace5000=pace), result, requirements)
        self.assertEqual(snapshot.target_pressure_mpa, 1.0)
        self.assertEqual(snapshot.measured_pressure_mpa, 1.0)
        self.assertEqual(snapshot.positive_source_pressure_mpa, 10.0)
        self.assertAlmostEqual(snapshot.slew_rate_mpa_per_min, 0.6)
        self.assertEqual(result.errors, [])

    def test_global_search_resolves_for_both_ruby_actions(self):
        settings = GlobalSpectrumSettings(
            search_enabled=True, search_when="on_low_quality",
            search_channels=(3,), search_range_ch3_um=6.0,
            search_step_ch3_um=4.0,
        )
        for action in (TakeSpectrumAction(use_global_search=True),
                       CompressToTargetPressureUsingRubyAction(use_global_search=True)):
            resolved = effective_search_action(action, settings)
            self.assertTrue(resolved.search_best_position_nearby)
            self.assertEqual(resolved.search_when, "on_low_quality")
            self.assertEqual(resolved.search_channels, (3,))
            self.assertEqual(resolved.search_step_ch3_um, 4.0)
            self.assertEqual(len(build_search_axes(resolved)), 1)
            self.assertFalse(action.search_best_position_nearby)

    def test_visual_page_reuses_ruby_controls_and_selects_one_position(self):
        page = _page_take_spectrum(("Ruby A", "Ruby B"), compression=True)
        self.addCleanup(page.widget.deleteLater)
        position = next(c for c in page.widget.findChildren(QComboBox)
                        if c.findText("Ruby A") >= 0)
        position.setCurrentText("Ruby B")
        action = page.build()
        self.assertIsInstance(action, CompressToTargetPressureUsingRubyAction)
        self.assertEqual(action.position_name, "Ruby B")
        self.assertFalse(action.all_positions)
        self.assertIsNone(action.exposure_time_s)
        self.assertEqual(action.search_channels, (4, 5))
        page.fill(action)
        self.assertEqual(page.build(), action)

    def test_visual_loop_target_can_select_the_for_variable(self):
        dialog = StepEditorDialog(
            available_loop_vars=("p",), spectrum_positions=("Ruby A",),
        )
        self.addCleanup(dialog.deleteLater)
        dialog._dev_combo.setCurrentText("PACE5000")
        dialog._op_combo.setCurrentText("compress_to_target_pressure_using_ruby")
        page = dialog._pages["compress_to_target_pressure_using_ruby"]
        variable_option = next(
            radio for radio in page.widget.findChildren(QRadioButton)
            if radio.text() == "Loop variable"
        )
        variable_option.setChecked(True)
        self.assertTrue(dialog._ok_btn.isEnabled())
        dialog._on_ok()
        action = dialog.get_action()
        self.assertEqual(action.target_sample_pressure_gpa, "p")

        edit = StepEditorDialog(
            action=action, available_loop_vars=("p",),
            spectrum_positions=("Ruby A",),
        )
        self.addCleanup(edit.deleteLater)
        self.assertEqual(
            edit._pages["compress_to_target_pressure_using_ruby"].build(), action,
        )

    def test_runtime_resolves_loop_target_before_pace_access(self):
        pace = FakePace()
        runner = self._runner(pace)
        action = CompressToTargetPressureUsingRubyAction(
            target_sample_pressure_gpa="p", max_allowed_gas_pressure=1.5,
        )
        with patch.object(pace, "get_output_state") as read_output_state:
            with self.assertRaisesRegex(RunnerError, "not defined"):
                runner._do_compress_to_target_pressure_using_ruby(action, 0, {})
            with self.assertRaises(RunnerError):
                runner._do_compress_to_target_pressure_using_ruby(action, 0, {"p": 0.0})
            read_output_state.assert_not_called()
        self.assertEqual(pace.events, [])

        pressures = iter((1.5, 2.0))

        def measure(*args, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            return SimpleNamespace(status="valid", pressure_gpa=next(pressures))

        with (patch.object(runner, "_do_take_spectrum", side_effect=measure),
              patch.object(runner, "_return_to_xrd_position")):
            runner._execute_actions(build_run_plan([
                ForLoopAction(var="p", values=[1.5, 2.0], body=[action]),
            ]))
        reached = [kwargs["note"] for args, kwargs in runner.science_events
                   if args[0] == "compression_reached"]
        self.assertEqual(len(reached), 2)
        self.assertIn("target_sample_pressure_gpa=1.500000", reached[0])
        self.assertIn("target_sample_pressure_gpa=2.000000", reached[1])
        self.assertEqual(action.target_sample_pressure_gpa, "p")

    def test_add_step_dialog_places_compression_under_pace5000(self):
        dialog = StepEditorDialog(spectrum_positions=("Ruby A",))
        self.addCleanup(dialog.deleteLater)
        dialog._dev_combo.setCurrentText("PACE5000")
        dialog._op_combo.setCurrentText("compress_to_target_pressure_using_ruby")
        self.assertTrue(dialog._ok_btn.isEnabled())
        dialog._on_ok()
        self.assertIsInstance(
            dialog.get_action(), CompressToTargetPressureUsingRubyAction,
        )
        self.assertEqual(dialog.get_action().position_name, "Ruby A")

    def _runner(self, pace):
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(pace5000=pace),
            global_spectrum=GlobalSpectrumSettings(pressure_enabled=True),
        )
        runner.science_events = []
        runner._logger = SimpleNamespace(
            log_ops=lambda *args: None,
            log_science=lambda *args, **kwargs: runner.science_events.append((args, kwargs)),
        )
        return runner

    def test_arrival_then_stabilisation_then_ruby_and_final_increment_cap(self):
        pace = FakePace()
        runner = self._runner(pace)
        pressures = iter((0.5, 1.0, 2.2))

        def measure(action, step, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            self.assertIsInstance(action, TakeSpectrumAction)
            self.assertNotIsInstance(action, CompressToTargetPressureUsingRubyAction)
            self.assertTrue(kwargs["leave_at_ruby"])
            pace.events.append(("ruby", pace.target))
            return SimpleNamespace(status="valid", pressure_gpa=next(pressures))

        def dwell(seconds):
            pace.events.append(("dwell", seconds))

        def return_to_xrd(*args):
            pace.events.append(("return", pace.target))

        action = CompressToTargetPressureUsingRubyAction(
            position_name="Ruby A", target_sample_pressure_gpa=2.0,
            gas_pressure_increment=0.3, stabilisation_duration_s=7.0,
            max_allowed_gas_pressure=1.5,
        )
        with (patch.object(runner, "_do_take_spectrum", side_effect=measure),
              patch.object(runner, "_do_wait", side_effect=dwell),
              patch.object(runner, "_return_to_xrd_position", side_effect=return_to_xrd)):
            runner._do_compress_to_target_pressure_using_ruby(action, 0)
        self.assertEqual(pace.events, [
            ("arrival", 1.0, 0.001), ("ruby", 1.0),
            ("set", 1.3, 0.6), ("arrival", 1.3, 0.001),
            ("dwell", 7.0), ("return", 1.3), ("ruby", 1.3),
            ("set", 1.5, 0.6), ("arrival", 1.5, 0.001),
            ("dwell", 7.0), ("return", 1.5), ("ruby", 1.5),
            ("return", 1.5),
        ])
        reached = [event for event in runner.science_events
                   if event[0][0] == "compression_reached"]
        self.assertEqual(reached, [(
            ("compression_reached",),
            {"step_index": 0,
             "note": "gas_setpoint_mpa=1.500000; sample_pressure_gpa=2.200000; "
                     "target_sample_pressure_gpa=2.000000"},
        )])

    def test_maximum_without_target_ends_after_final_ruby(self):
        pace = FakePace()
        runner = self._runner(pace)
        def measure(*args, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            return SimpleNamespace(status="valid", pressure_gpa=1.0)
        action = CompressToTargetPressureUsingRubyAction(
            target_sample_pressure_gpa=2.0, gas_pressure_increment=0.5,
            max_allowed_gas_pressure=1.5, stabilisation_duration_s=0,
        )
        with (patch.object(runner, "_do_take_spectrum", side_effect=measure) as measurement,
              patch.object(runner, "_do_wait"),
              patch.object(runner, "_return_to_xrd_position") as returning):
            with self.assertRaises(RunnerError) as error:
                runner._do_compress_to_target_pressure_using_ruby(action, 0)
        self.assertEqual(error.exception.code, "runtime.compression_target_not_reached")
        self.assertEqual(measurement.call_count, 2)
        self.assertEqual(returning.call_count, 2)
        self.assertEqual(pace.target, 1.5)
        self.assertFalse(any(args[0] == "compression_reached"
                             for args, _ in runner.science_events))

    def test_current_gas_setpoint_above_maximum_sends_no_command_or_stage_move(self):
        pace = FakePace()
        pace.target = 2.0
        runner = self._runner(pace)
        action = CompressToTargetPressureUsingRubyAction(
            max_allowed_gas_pressure=1.5,
        )
        with patch.object(runner, "_do_take_spectrum") as measure:
            with self.assertRaises(RunnerError) as error:
                runner._do_compress_to_target_pressure_using_ruby(action, 0)
        self.assertEqual(error.exception.code, "runtime.compression_above_maximum")
        measure.assert_not_called()
        self.assertEqual(pace.events, [])

    def test_initial_measurement_detects_changed_pace_state_and_returns_to_xrd(self):
        pace = FakePace()
        runner = self._runner(pace)

        def measure(*args, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            pace.target = 1.1
            return SimpleNamespace(status="valid", pressure_gpa=2.0)

        action = CompressToTargetPressureUsingRubyAction(
            target_sample_pressure_gpa=2.0, max_allowed_gas_pressure=1.5,
        )
        with (patch.object(runner, "_do_take_spectrum", side_effect=measure),
              patch.object(runner, "_return_to_xrd_position") as returning):
            with self.assertRaises(RunnerError) as error:
                runner._do_compress_to_target_pressure_using_ruby(action, 0)
        self.assertEqual(error.exception.code, "runtime.compression_setpoint_changed")
        returning.assert_called_once()

    def test_initial_pressure_may_settle_during_arrival_wait(self):
        pace = FakePace()
        runner = self._runner(pace)
        actual = [0.8]
        pace.get_pressure = lambda: actual[0]

        def arrival(*args, **kwargs):
            actual[0] = pace.target
            return pace.target

        pace.wait_for_pressure = arrival

        def measure(*args, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            return SimpleNamespace(status="valid", pressure_gpa=2.2)

        action = CompressToTargetPressureUsingRubyAction(
            target_sample_pressure_gpa=2.0, max_allowed_gas_pressure=1.5,
        )
        with (patch.object(runner, "_do_take_spectrum", side_effect=measure),
              patch.object(runner, "_return_to_xrd_position") as returning):
            runner._do_compress_to_target_pressure_using_ruby(action, 0)
        returning.assert_called_once()
        self.assertEqual(pace.events, [])
        reached = [event for event in runner.science_events
                   if event[0][0] == "compression_reached"]
        self.assertEqual(reached, [(
            ("compression_reached",),
            {"step_index": 0,
             "note": "gas_setpoint_mpa=1.000000; sample_pressure_gpa=2.200000; "
                     "target_sample_pressure_gpa=2.000000"},
        )])

    def test_pressure_overshoot_skips_one_increment_and_sequence_continues(self):
        pace = FakePace()
        runner = self._runner(pace)
        ruby_pressures = iter((0.20, 0.31, 0.42, 0.42, 0.51))

        def measure(*args, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            return SimpleNamespace(status="valid", pressure_gpa=next(ruby_pressures))

        targets = (0.3, 0.4, 0.5)
        actions = []
        for target in targets:
            actions.extend((
                CompressToTargetPressureUsingRubyAction(
                    target_sample_pressure_gpa=target,
                    gas_pressure_increment=0.1,
                    max_allowed_gas_pressure=1.5,
                    stabilisation_duration_s=0,
                ),
                TakeXrdAction(),
            ))
        xrd_steps = []
        with (patch.object(runner, "_do_take_spectrum", side_effect=measure),
              patch.object(runner, "_return_to_xrd_position"),
              patch.object(runner, "_do_wait"),
              patch.object(runner, "_do_take_xrd",
                           side_effect=lambda action, idx: xrd_steps.append(idx))):
            runner._execute_actions(build_run_plan(actions))
        self.assertEqual(xrd_steps, [1, 3, 5])
        setpoints = [event[1] for event in pace.events if event[0] == "set"]
        self.assertEqual(len(setpoints), 2)
        self.assertAlmostEqual(setpoints[0], 1.1)
        self.assertAlmostEqual(setpoints[1], 1.2)
        reached = [kwargs["note"] for args, kwargs in runner.science_events
                   if args[0] == "compression_reached"]
        self.assertEqual(len(reached), 3)
        self.assertIn("sample_pressure_gpa=0.420000", reached[1])
        self.assertIn("gas_setpoint_mpa=1.100000", reached[1])

    def test_runtime_rejects_changed_setpoint_mode_source_and_gas_overshoot(self):
        pace = FakePace()
        runner = self._runner(pace)
        pace.target = 1.1
        with self.assertRaises(RunnerError) as changed:
            runner._check_compression_pace_state(1.0, 0.001, 2.0)
        self.assertEqual(changed.exception.code, "runtime.compression_setpoint_changed")
        pace.target = 1.0
        pace.get_output_state = lambda: "OFF"
        with self.assertRaises(RunnerError) as mode:
            runner._check_compression_pace_state(1.0, 0.001, 2.0)
        self.assertEqual(mode.exception.code, "runtime.compression_control_mode_changed")
        pace.get_output_state = lambda: "ON"
        pace.get_positive_source_pressure = lambda: 1.5
        with self.assertRaises(RunnerError) as source:
            runner._check_compression_pace_state(1.0, 0.001, 2.0)
        self.assertEqual(source.exception.code, "runtime.compression_source_pressure_low")
        pace.get_positive_source_pressure = lambda: 2.0
        with self.assertRaises(RunnerError) as equal_source:
            runner._check_compression_pace_state(1.0, 0.001, 2.0)
        self.assertEqual(equal_source.exception.code, "runtime.compression_source_pressure_low")
        pace.get_positive_source_pressure = lambda: 10.0
        pace.get_pressure = lambda: 2.1
        with self.assertRaises(RunnerError) as overshoot:
            runner._check_compression_pace_state(1.0, 0.001, 2.0)
        self.assertEqual(overshoot.exception.code, "runtime.compression_gas_pressure_out_of_band")

    def test_operator_stop_does_not_start_xrd_return(self):
        pace = FakePace()
        pace.wait_for_pressure = lambda *args, **kwargs: None
        runner = self._runner(pace)

        def measure(*args, **kwargs):
            runner._last_spectrum_xrd_position = {3: 0, 4: 0, 5: 0}
            return SimpleNamespace(status="valid", pressure_gpa=0.5)

        action = CompressToTargetPressureUsingRubyAction(
            target_sample_pressure_gpa=2.0, max_allowed_gas_pressure=1.5,
        )
        with (patch.object(runner, "_do_take_spectrum", side_effect=measure),
              patch.object(runner, "_return_to_xrd_position") as returning):
            with self.assertRaises(_StopRequested):
                runner._do_compress_to_target_pressure_using_ruby(action, 0)
        returning.assert_not_called()

    def test_simulated_ruby_can_stay_at_ruby_until_explicit_return(self):
        ctrl = PM16CControllerSim()
        ctrl.connect()
        self.addCleanup(ctrl.disconnect)
        with ctrl._state_lock:
            for ch, pulse in ((3, 40000), (4, 20000), (5, 20000)):
                ctrl._positions[ch] = ctrl._targets[ch] = pulse
        runner = SequenceRunner(
            Sequence(actions=[]), DeviceContext(controller=ctrl),
            global_spectrum=GlobalSpectrumSettings(
                offset_ch4_pulse=1, offset_ch5_pulse=0, settle_ms=0,
            ),
        )
        runner._logger = SimpleNamespace(
            log_ops=lambda *args: None, log_science=lambda *args, **kwargs: None,
        )
        runner._baseline_pos = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        runner._motion_lease = ctrl.acquire_motion("test", "compression ruby stay")
        self.addCleanup(lambda: ctrl.release_motion(runner._motion_lease))
        runner._do_take_spectrum(TakeSpectrumAction(save=False), 0, leave_at_ruby=True)
        self.assertEqual(int(ctrl.get_ch_pos(4)), 20001)
        self.assertEqual(runner._last_spectrum_xrd_position[4], 20000)
        runner._return_to_xrd_position(
            ctrl, runner._last_spectrum_xrd_position, runner._global_spectrum.speed,
        )
        self.assertEqual(int(ctrl.get_ch_pos(4)), 20000)


if __name__ == "__main__":
    unittest.main()
