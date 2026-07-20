"""
Tests for apps/exp_scheduler/validator/snapshots.py —
REORGANISATION_PLAN.md Phase 6 (§7 Phase 6).

Covers `determine_requirements()` (every field-level read gate, matched
against the exact pre-Phase-6 gate condition each mirrors) and the
`collect_*_snapshot()` functions (physical-read sharing / Diagnostic
ownership / fail-closed PACE5000 unit handling), using the shared
hardware-free fakes in tests/exp_scheduler_fakes.py.
"""
import contextlib
import io
import sys
import types
import unittest

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

from apps.exp_scheduler.actions import (
    ForLoopAction,
    SetAndWaitPressureAction,
    SetControlModeAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitPressureAction,
    WaitTemperatureAction,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.scheduler_settings import GlobalXrdSettings
from apps.exp_scheduler.validator.execution_trace import ExecutionTrace
from apps.exp_scheduler.validator.models import Severity, ValidationPhase, emit_preflight
from apps.exp_scheduler.validator.pre_validator import PreCheckResult, PreValidator
from apps.exp_scheduler.validator import snapshots
from apps.exp_scheduler.sequence import Sequence

from tests.exp_scheduler_fakes import (
    FakeLakeshore,
    FakePace5000,
    FakeRadicon,
    FakeStageController,
)


def _trace(*actions):
    return ExecutionTrace.build(list(actions))


class DetermineRequirementsStageMovingTests(unittest.TestCase):
    def test_pressure_only_sequence_does_not_require_is_moving(self):
        trace = _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min"))
        req = snapshots.determine_requirements(trace, None)
        self.assertFalse(req.stage_moving)

    def test_plain_stage_action_requires_is_moving(self):
        trace = _trace(StageAction(operation="move_absolute", ch=4, value=100))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.stage_moving)

    def test_effective_oscillate_true_xrd_alone_requires_is_moving(self):
        trace = _trace(TakeXrdAction(oscillate=True))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.stage_moving)

    def test_effective_oscillate_true_via_global_xrd_requires_is_moving(self):
        trace = _trace(TakeXrdAction(oscillate=None))
        req = snapshots.determine_requirements(trace, GlobalXrdSettings(oscillate=True))
        self.assertTrue(req.stage_moving)

    def test_oscillate_false_xrd_alone_does_not_require_is_moving(self):
        trace = _trace(TakeXrdAction(oscillate=False))
        req = snapshots.determine_requirements(trace, None)
        self.assertFalse(req.stage_moving)

    def test_oscillate_unset_and_global_disabled_does_not_require_is_moving(self):
        trace = _trace(TakeXrdAction(oscillate=None))
        req = snapshots.determine_requirements(trace, GlobalXrdSettings(oscillate=False))
        self.assertFalse(req.stage_moving)


class DetermineRequirementsPaceTests(unittest.TestCase):
    def test_set_control_mode_alone_requires_nothing_but_pace_used(self):
        trace = _trace(SetControlModeAction(enabled=True))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.pace_used)
        self.assertFalse(req.pace_output_state)
        self.assertFalse(req.pace_target)
        self.assertIsNone(req.pace_max_set_pressure_mpa)
        self.assertFalse(req.pace_source)
        self.assertFalse(req.pace_unit)

    def test_set_pressure_alone_requires_output_state_unit_and_target(self):
        trace = _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min"))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.pace_output_state)
        self.assertTrue(req.pace_target)
        self.assertTrue(req.pace_unit)
        self.assertTrue(req.pace_source)  # a valid numeric pressure was set

    def test_wait_pressure_alone_requires_output_state_but_not_target_or_source(self):
        trace = _trace(WaitPressureAction(tol=0.01, unit="MPa"))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.pace_output_state)
        self.assertFalse(req.pace_target)
        self.assertIsNone(req.pace_max_set_pressure_mpa)
        self.assertFalse(req.pace_source)
        self.assertFalse(req.pace_unit)

    def test_non_numeric_pressure_still_requires_target_but_not_source(self):
        # "p" resolves (it IS a defined loop variable, unlike the unresolved
        # case below) but its bound value can't be float()-converted — the
        # distinct "resolved but non-numeric" path through
        # _find_max_set_pressure_mpa.
        trace = _trace(
            ForLoopAction(var="p", values=["not_numeric"], body=[
                SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"),
            ]),
        )
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.pace_target)  # gate is type-presence, not value validity
        self.assertIsNone(req.pace_max_set_pressure_mpa)
        self.assertFalse(req.pace_source)
        self.assertTrue(req.pace_unit)  # via pace_target

    def test_unresolved_loop_variable_pressure_requires_target_but_not_source(self):
        trace = _trace(SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.pace_target)
        self.assertIsNone(req.pace_max_set_pressure_mpa)
        self.assertFalse(req.pace_source)

    def test_one_valid_pressure_among_invalid_ones_still_requires_source(self):
        trace = _trace(
            ForLoopAction(var="p", values=["bogus", 3.0], body=[
                SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"),
            ]),
        )
        req = snapshots.determine_requirements(trace, None)
        self.assertEqual(req.pace_max_set_pressure_mpa, 3.0)
        self.assertTrue(req.pace_source)

    def test_set_and_wait_pressure_alone_requires_source_but_not_target(self):
        trace = _trace(
            SetAndWaitPressureAction(pressure=2.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01)
        )
        req = snapshots.determine_requirements(trace, None)
        self.assertFalse(req.pace_target)  # gate only looks at bare SetPressureAction
        self.assertEqual(req.pace_max_set_pressure_mpa, 2.0)
        self.assertTrue(req.pace_source)
        self.assertTrue(req.pace_unit)  # via pace_source

    def test_loop_limit_exceeded_disables_all_pace_detail_fields(self):
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        actions = [SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")] + actions
        trace = _trace(*actions)
        self.assertFalse(trace.stats.within_limits)
        req = snapshots.determine_requirements(trace, None)
        self.assertFalse(req.pace_output_state)
        self.assertFalse(req.pace_target)
        self.assertIsNone(req.pace_max_set_pressure_mpa)
        self.assertFalse(req.pace_source)
        self.assertFalse(req.pace_unit)
        # pace_used is trace.flat-based (not within_limits-gated) and stays True
        self.assertTrue(req.pace_used)


class DetermineRequirementsLakeshoreTests(unittest.TestCase):
    def test_no_lakeshore_action_requires_nothing(self):
        trace = _trace(WaitAction(duration_s=1.0))
        req = snapshots.determine_requirements(trace, None)
        self.assertFalse(req.lakeshore_used)
        self.assertFalse(req.lakeshore_heater_range)
        self.assertFalse(req.lakeshore_data)

    def test_set_temperature_requires_setpoint_but_not_data(self):
        trace = _trace(SetTemperatureAction(value_k=300.0, ramp_rate=1.0))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.lakeshore_used)
        self.assertTrue(req.lakeshore_heater_range)
        self.assertFalse(req.lakeshore_data)

    def test_wait_temperature_requires_data(self):
        trace = _trace(WaitTemperatureAction(tol_k=0.1))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.lakeshore_data)

    def test_loop_limit_exceeded_keeps_setpoint_and_data_but_drops_heater_range(self):
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        actions = [
            SetTemperatureAction(value_k=300.0, ramp_rate=1.0),
            WaitTemperatureAction(tol_k=0.1),
        ] + actions
        trace = _trace(*actions)
        self.assertFalse(trace.stats.within_limits)
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.lakeshore_used)
        self.assertFalse(req.lakeshore_heater_range)  # asymmetric gate
        self.assertTrue(req.lakeshore_data)


class DetermineRequirementsRadiconTests(unittest.TestCase):
    def test_take_xrd_requires_radicon(self):
        trace = _trace(TakeXrdAction())
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.radicon_used)

    def test_take_dark_requires_radicon(self):
        trace = _trace(TakeDarkAction(exposure_ms=100))
        req = snapshots.determine_requirements(trace, None)
        self.assertTrue(req.radicon_used)

    def test_no_xrd_action_does_not_require_radicon(self):
        trace = _trace(WaitAction(duration_s=1.0))
        req = snapshots.determine_requirements(trace, None)
        self.assertFalse(req.radicon_used)


class CollectStageSnapshotTests(unittest.TestCase):
    def test_no_controller_returns_none(self):
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        self.assertIsNone(snapshots.collect_stage_snapshot(DeviceContext(), r, req))

    def test_all_channels_readable_gives_full_positions_and_no_diagnostic(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        snap = snapshots.collect_stage_snapshot(DeviceContext(controller=controller), r, req)
        self.assertEqual(len(snap.positions), 11)
        self.assertEqual(r.errors, [])

    def test_every_channel_attempted_even_after_earlier_failures(self):
        # Ch5 and Ch7 both fail — both must be reported, not just the first.
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        controller.fail_on = {("get_ch_pos", 5), ("get_ch_pos", 7)}
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        snap = snapshots.collect_stage_snapshot(DeviceContext(controller=controller), r, req)
        self.assertEqual(controller.call_count("get_ch_pos"), 11)  # every channel attempted
        self.assertEqual(len(snap.positions), 9)
        self.assertNotIn(5, snap.positions)
        self.assertNotIn(7, snap.positions)
        self.assertTrue(any("Ch5" in e for e in r.errors))
        self.assertTrue(any("Ch7" in e for e in r.errors))

    def test_is_moving_not_read_when_not_required(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        self.assertFalse(req.stage_moving)
        snap = snapshots.collect_stage_snapshot(DeviceContext(controller=controller), r, req)
        self.assertEqual(controller.call_count("get_is_moving"), 0)
        self.assertIsNone(snap.is_moving)

    def test_is_moving_read_once_when_required(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)}, is_moving=True)
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(StageAction(operation="move_absolute", ch=4, value=100)), None
        )
        snap = snapshots.collect_stage_snapshot(DeviceContext(controller=controller), r, req)
        self.assertEqual(controller.call_count("get_is_moving"), 1)
        self.assertTrue(snap.is_moving)

    def test_is_moving_read_failure_gives_none_and_one_diagnostic(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        controller.fail_on = {"get_is_moving"}
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(StageAction(operation="move_absolute", ch=4, value=100)), None
        )
        snap = snapshots.collect_stage_snapshot(DeviceContext(controller=controller), r, req)
        self.assertIsNone(snap.is_moving)
        self.assertEqual(len(r.errors), 1)

    def test_stage_mode_unknown_when_ch8_ch9_unreadable(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        controller.fail_on = {("get_ch_pos", 8)}
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        snap = snapshots.collect_stage_snapshot(DeviceContext(controller=controller), r, req)
        self.assertEqual(snap.stage_mode, "unknown")


class CollectPaceSnapshotTests(unittest.TestCase):
    def test_pace_not_used_returns_none(self):
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        self.assertIsNone(snapshots.collect_pace_snapshot(DeviceContext(), r, req))

    def test_not_connected_gives_connected_false_and_all_none(self):
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(), r, req)
        self.assertFalse(snap.connected)
        self.assertIsNone(snap.output_state)
        self.assertIsNone(snap.unit)
        self.assertIsNone(snap.target_pressure_mpa)
        self.assertIsNone(snap.positive_source_pressure_mpa)

    def test_no_write_ever_called(self):
        pace = FakePace5000(output_state="1", target_pressure=1.0, positive_source_pressure=10.0)
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(
                SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min"),
                WaitPressureAction(tol=0.01, unit="MPa"),
            ),
            None,
        )
        snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertEqual(pace.call_count("write"), 0)
        self.assertEqual(pace.call_count("query", ":UNIT:PRES?"), 1)

    def test_set_control_mode_alone_reads_nothing_but_connected(self):
        pace = FakePace5000()
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(SetControlModeAction(enabled=True)), None)
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertTrue(snap.connected)
        self.assertEqual(pace.call_count("get_output_state"), 0)
        self.assertEqual(pace.call_count("query"), 0)
        self.assertEqual(pace.call_count("get_target_pressure"), 0)
        self.assertEqual(pace.call_count("get_positive_source_pressure"), 0)

    def test_default_unit_mpa_converts_with_factor_one(self):
        pace = FakePace5000(unit="MPA", positive_source_pressure=3.0)
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertEqual(snap.unit, "MPa")
        self.assertEqual(snap.positive_source_pressure_mpa, 3.0)

    def test_bar_unit_converts_with_factor_point_one(self):
        pace = FakePace5000(unit="BAR", positive_source_pressure=30.0)
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertEqual(snap.unit, "Bar")
        self.assertAlmostEqual(snap.positive_source_pressure_mpa, 3.0)

    def test_unknown_unit_response_is_fail_closed(self):
        pace = FakePace5000(unit="psi", positive_source_pressure=3.0)
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertIsNone(snap.unit)
        self.assertIsNone(snap.target_pressure_mpa)
        self.assertIsNone(snap.positive_source_pressure_mpa)
        self.assertEqual(pace.call_count("get_target_pressure"), 0)
        self.assertEqual(pace.call_count("get_positive_source_pressure"), 0)
        self.assertEqual(len(r.errors), 1)

    def test_output_state_read_independent_of_unit_failure(self):
        # requirements.pace_output_state is driven by SetPressureAction alone;
        # a failing/unknown unit response must not suppress it.
        pace = FakePace5000(unit="psi", output_state="1")
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertEqual(pace.call_count("get_output_state"), 1)
        self.assertEqual(snap.output_state, "1")
        self.assertEqual(pace.call_count("get_target_pressure"), 0)
        self.assertEqual(pace.call_count("get_positive_source_pressure"), 0)
        # exactly one diagnostic (unit failure) — output_state succeeded independently
        self.assertEqual(len(r.errors), 1)
        self.assertIn("圧力単位", r.errors[0])

    def test_output_state_exception_gives_none_and_one_diagnostic(self):
        pace = FakePace5000()
        pace.fail_on = {"get_output_state"}
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertIsNone(snap.output_state)
        self.assertEqual(len(r.errors), 1)

    def test_source_pressure_nan_is_rejected_fail_closed(self):
        pace = FakePace5000(positive_source_pressure=float("nan"))
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertIsNone(snap.positive_source_pressure_mpa)
        self.assertTrue(any("Source Pressure" in e for e in r.errors))

    def test_source_pressure_inf_is_rejected_fail_closed(self):
        pace = FakePace5000(positive_source_pressure=float("inf"))
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertIsNone(snap.positive_source_pressure_mpa)

    def test_target_pressure_non_finite_silently_becomes_none(self):
        pace = FakePace5000(target_pressure=float("nan"))
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")), None
        )
        snap = snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertIsNone(snap.target_pressure_mpa)
        self.assertEqual(r.errors, [])  # soft use — no Diagnostic

    def test_wait_pressure_alone_reads_output_state_but_not_target_or_source(self):
        pace = FakePace5000(output_state="1")
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitPressureAction(tol=0.01, unit="MPa")), None)
        snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertEqual(pace.call_count("get_output_state"), 1)
        self.assertEqual(pace.call_count("query"), 0)
        self.assertEqual(pace.call_count("get_target_pressure"), 0)
        self.assertEqual(pace.call_count("get_positive_source_pressure"), 0)

    def test_set_and_wait_pressure_alone_reads_source_but_not_target(self):
        pace = FakePace5000(output_state="1", positive_source_pressure=10.0)
        r = PreCheckResult()
        req = snapshots.determine_requirements(
            _trace(SetAndWaitPressureAction(pressure=2.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01)),
            None,
        )
        snapshots.collect_pace_snapshot(DeviceContext(pace5000=pace), r, req)
        self.assertEqual(pace.call_count("get_target_pressure"), 0)
        self.assertEqual(pace.call_count("get_positive_source_pressure"), 1)
        self.assertEqual(pace.call_count("query", ":UNIT:PRES?"), 1)


class CollectLakeshoreSnapshotTests(unittest.TestCase):
    def test_not_used_returns_none(self):
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        self.assertIsNone(snapshots.collect_lakeshore_snapshot(DeviceContext(), r, req))

    def test_not_connected(self):
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(SetTemperatureAction(value_k=300.0, ramp_rate=1.0)), None)
        snap = snapshots.collect_lakeshore_snapshot(DeviceContext(), r, req)
        self.assertFalse(snap.connected)
        self.assertIsNone(snap.setpoint)
        self.assertIsNone(snap.heater_range)
        self.assertIsNone(snap.has_data)

    def test_setpoint_read_once(self):
        ls = FakeLakeshore(setpoint=123.0)
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(SetTemperatureAction(value_k=300.0, ramp_rate=1.0)), None)
        snap = snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=ls), r, req)
        self.assertEqual(ls.call_count("get_setpoint"), 1)
        self.assertEqual(snap.setpoint, 123.0)

    def test_heater_range_not_read_when_over_limit(self):
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        actions = [SetTemperatureAction(value_k=300.0, ramp_rate=1.0)] + actions
        trace = _trace(*actions)
        self.assertFalse(trace.stats.within_limits)
        ls = FakeLakeshore()
        r = PreCheckResult()
        req = snapshots.determine_requirements(trace, None)
        snap = snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=ls), r, req)
        self.assertEqual(ls.call_count("get_setpoint"), 1)
        self.assertEqual(ls.call_count("get_heater_range"), 0)
        self.assertIsNone(snap.heater_range)

    def test_heater_range_read_failure_gives_none_and_one_diagnostic(self):
        ls = FakeLakeshore()
        ls.fail_on = {"get_heater_range"}
        r = PreCheckResult()
        req = snapshots.determine_requirements(_trace(SetTemperatureAction(value_k=300.0, ramp_rate=1.0)), None)
        snap = snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=ls), r, req)
        self.assertIsNone(snap.heater_range)
        self.assertEqual(len(r.errors), 1)

    def test_has_data_true_false_none(self):
        req = snapshots.determine_requirements(_trace(WaitTemperatureAction(tol_k=0.1)), None)

        r1 = PreCheckResult()
        snap1 = snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=FakeLakeshore(data=[1, 2])), r1, req)
        self.assertTrue(snap1.has_data)

        r2 = PreCheckResult()
        snap2 = snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=FakeLakeshore(data=[])), r2, req)
        self.assertFalse(snap2.has_data)

        ls3 = FakeLakeshore()
        ls3.fail_on = {"get_data"}
        r3 = PreCheckResult()
        snap3 = snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=ls3), r3, req)
        self.assertIsNone(snap3.has_data)
        self.assertEqual(r3.errors, [])  # silent-swallow preserved

    def test_data_read_even_when_over_limit(self):
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        actions = [WaitTemperatureAction(tol_k=0.1)] + actions
        trace = _trace(*actions)
        self.assertFalse(trace.stats.within_limits)
        ls = FakeLakeshore(data=[1])
        r = PreCheckResult()
        req = snapshots.determine_requirements(trace, None)
        snapshots.collect_lakeshore_snapshot(DeviceContext(lakeshore=ls), r, req)
        self.assertEqual(ls.call_count("get_data"), 1)


class CollectRadiconSnapshotTests(unittest.TestCase):
    def test_not_used_returns_none(self):
        req = snapshots.determine_requirements(_trace(WaitAction(duration_s=1.0)), None)
        self.assertIsNone(snapshots.collect_radicon_snapshot(DeviceContext(), req))

    def test_available_true(self):
        req = snapshots.determine_requirements(_trace(TakeXrdAction()), None)
        snap = snapshots.collect_radicon_snapshot(DeviceContext(radicon=FakeRadicon()), req)
        self.assertTrue(snap.available)

    def test_available_false_when_not_connected(self):
        req = snapshots.determine_requirements(_trace(TakeXrdAction()), None)
        snap = snapshots.collect_radicon_snapshot(DeviceContext(), req)
        self.assertFalse(snap.available)


class EndToEndDedupRegressionTests(unittest.TestCase):
    """Runs the full PreValidator().validate() facade and asserts on the
    injected fakes' call counts — the actual regression Phase 6 exists to
    fix (each physical value read exactly once per validate() call)."""

    def test_stage_move_and_oscillating_xrd_reads_ch8_ch9_once_each(self):
        sequence = Sequence(actions=[
            StageAction(operation="move_absolute", ch=4, value=100),
            TakeXrdAction(oscillate=True),
        ])
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        controller.positions[8] = 0
        controller.positions[9] = -40000
        PreValidator().validate(
            sequence, DeviceContext(controller=controller, radicon=FakeRadicon()),
        )
        self.assertEqual(controller.call_count("get_ch_pos", 8), 1)
        self.assertEqual(controller.call_count("get_ch_pos", 9), 1)
        self.assertEqual(controller.call_count("get_is_moving"), 1)

    def test_set_temperature_and_wait_temperature_reads_setpoint_once(self):
        sequence = Sequence(actions=[
            SetTemperatureAction(value_k=300.0, ramp_rate=1.0),
            WaitTemperatureAction(tol_k=0.1),
        ])
        ls = FakeLakeshore()
        PreValidator().validate(sequence, DeviceContext(lakeshore=ls))
        self.assertEqual(ls.call_count("get_setpoint"), 1)

    def test_set_pressure_and_wait_pressure_never_writes_and_queries_unit_once(self):
        sequence = Sequence(actions=[
            SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min"),
            WaitPressureAction(tol=0.01, unit="MPa"),
        ])
        pace = FakePace5000(output_state="1", positive_source_pressure=10.0)
        PreValidator().validate(sequence, DeviceContext(pace5000=pace))
        self.assertEqual(pace.call_count("write"), 0)
        self.assertEqual(pace.call_count("query", ":UNIT:PRES?"), 1)

    def test_baseline_positions_set_without_any_stage_action(self):
        sequence = Sequence(actions=[SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min")])
        controller = FakeStageController({ch: ch * 10 for ch in range(1, 12)})
        pace = FakePace5000()
        result = PreValidator().validate(sequence, DeviceContext(controller=controller, pace5000=pace))
        self.assertEqual(len(result.baseline_positions), 11)
        self.assertEqual(controller.call_count("get_is_moving"), 0)

    def test_ch8_ch9_unreadable_gives_communication_error_and_xrd_mode_unknown_warning(self):
        # Deliberately preserved pre-Phase-6 double-message case (see
        # validator/snapshots.py docstring): a physical Ch8/Ch9 read
        # failure both reports the read error AND (since a take_xrd
        # follows) the "physical arrangement unknown" ordering warning.
        sequence = Sequence(actions=[TakeXrdAction()])
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        controller.fail_on = {("get_ch_pos", 8)}
        result = PreValidator().validate(
            sequence, DeviceContext(controller=controller, radicon=FakeRadicon()),
        )
        self.assertTrue(any("Ch8" in e for e in result.errors))
        self.assertTrue(any("事前に microscope_out_and_fpd_in" in w for w in result.warnings))


class HugeIntegerPressureOverflowRegressionTests(unittest.TestCase):
    """A hand-edited/corrupted Sequence JSON (or a DSL literal like
    10**500) can carry a pressure value too large for float() —
    `float(10**500)` raises OverflowError, not TypeError/ValueError.
    `_find_max_set_pressure_mpa` (and the analogous conversion in
    checks/pace5000.py's wait-duration estimate) must treat that the same
    as any other invalid literal: skip it, don't let it blow up
    `determine_requirements()`/`collect_snapshot()` and take the whole
    snapshot down with it."""

    def test_find_max_set_pressure_mpa_does_not_raise(self):
        trace = _trace(SetPressureAction(pressure=10**500, unit="MPa", rate=0.1, rate_unit="MPa/min"))
        req = snapshots.determine_requirements(trace, None)
        self.assertIsNone(req.pace_max_set_pressure_mpa)
        self.assertFalse(req.pace_source)
        # gate is type-presence only, so target/unit are still required
        self.assertTrue(req.pace_target)
        self.assertTrue(req.pace_unit)

    def test_full_validate_does_not_report_internal_error_and_skips_source_read(self):
        sequence = Sequence(actions=[
            SetPressureAction(pressure=10**500, unit="MPa", rate=0.1, rate_unit="MPa/min"),
        ])
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        pace = FakePace5000(output_state="1", target_pressure=1.0, positive_source_pressure=10.0)
        result = PreValidator().validate(
            sequence, DeviceContext(controller=controller, pace5000=pace),
        )
        self.assertFalse(any("internal validation error" in e for e in result.errors))
        self.assertTrue(any("pressure is not numeric" in e for e in result.errors))
        # Stage baseline contract preserved even though the PACE literal is broken
        self.assertEqual(len(result.baseline_positions), 11)
        # invalid literal never resolves to a candidate max, so pace_source
        # stays False and the source-pressure getter is never called
        self.assertEqual(pace.call_count("get_positive_source_pressure"), 0)

    def test_wait_duration_rate_conversion_does_not_raise(self):
        sequence = Sequence(actions=[
            SetPressureAction(pressure=1.0, unit="MPa", rate=10**500, rate_unit="MPa/min"),
        ])
        pace = FakePace5000(output_state="1", target_pressure=1.0, positive_source_pressure=10.0)
        result = PreValidator().validate(sequence, DeviceContext(pace5000=pace))
        self.assertFalse(any("internal validation error" in e for e in result.errors))


class SnapshotDiagnosticLoggingTests(unittest.TestCase):
    """PreValidator.validate()'s snapshot-collection step runs outside the
    per-checker `_run()` wrapper, so its Diagnostics need their own
    logging hookup (`_log_diff`) to show up in the same ``✗``/``⚠``-prefixed
    lines the validation-details log saves for every other checker."""

    def test_stage_position_read_failure_is_logged_under_collect_snapshot(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        controller.fail_on = {("get_ch_pos", 5)}
        sequence = Sequence(actions=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = PreValidator().validate(sequence, DeviceContext(controller=controller))
        output = buf.getvalue()
        self.assertIn("Cannot read Ch5 position", output)
        lines = output.splitlines()
        snapshot_line_idx = next(
            i for i, line in enumerate(lines) if "collect_snapshot" in line
        )
        self.assertIn("ERROR", lines[snapshot_line_idx])
        self.assertIn("✗ Cannot read Ch5 position", lines[snapshot_line_idx + 1])
        self.assertTrue(any("Cannot read Ch5 position" in e for e in result.errors))

    def test_pace_unit_failure_is_logged_under_collect_snapshot(self):
        pace = FakePace5000(unit="psi")
        sequence = Sequence(actions=[
            SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min"),
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            PreValidator().validate(sequence, DeviceContext(pace5000=pace))
        output = buf.getvalue()
        lines = output.splitlines()
        snapshot_line_idx = next(
            i for i, line in enumerate(lines) if "collect_snapshot" in line
        )
        self.assertIn("ERROR", lines[snapshot_line_idx])
        self.assertTrue(
            any("圧力単位" in line for line in lines[snapshot_line_idx:snapshot_line_idx + 3])
        )

    def test_no_diagnostics_logs_ok(self):
        controller = FakeStageController({ch: 0 for ch in range(1, 12)})
        sequence = Sequence(actions=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            PreValidator().validate(sequence, DeviceContext(controller=controller))
        output = buf.getvalue()
        lines = output.splitlines()
        snapshot_line_idx = next(
            i for i, line in enumerate(lines) if "collect_snapshot" in line
        )
        self.assertIn("OK", lines[snapshot_line_idx])


class EmitPreflightTests(unittest.TestCase):
    def test_sets_device_and_phase_and_mirrors_into_errors(self):
        r = PreCheckResult()
        d = emit_preflight(r, "preflight.test.code", "boom", device="stage")
        self.assertEqual(d.phase, ValidationPhase.PREFLIGHT)
        self.assertEqual(d.device, "stage")
        self.assertEqual(d.severity, Severity.ERROR)
        self.assertEqual(r.errors, ["boom"])
        self.assertEqual(r.diagnostics, [d])

    def test_warning_severity_mirrors_into_warnings_only(self):
        r = PreCheckResult()
        emit_preflight(r, "preflight.test.code", "careful", device="pace5000", severity=Severity.WARNING)
        self.assertEqual(r.warnings, ["careful"])
        self.assertEqual(r.errors, [])


if __name__ == "__main__":
    unittest.main()
