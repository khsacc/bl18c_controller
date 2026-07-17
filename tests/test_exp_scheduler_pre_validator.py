import math
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
    FollowSampleAction,
    ForLoopAction,
    LogAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitTemperatureAction,
    action_from_dict,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.runner import GlobalLimits, GlobalXrdSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.pre_validator import PreValidator

from tests.exp_scheduler_fakes import (
    FakeLakeshore as _FakeLakeshore,
    FakeRadicon as _FakeRadicon,
    FakeStageController as _FakeStageController,
)


class ExpSchedulerPreValidatorTests(unittest.TestCase):
    def test_warns_for_unused_for_loop_variable(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="p",
                values=[1.0, 2.0],
                body=[LogAction(message="constant step")],
            )
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("for ループ変数 'p'" in w for w in result.warnings))

    def test_accepts_referenced_for_loop_variable(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="p",
                values=[1.0, 2.0],
                body=[LogAction(message="pressure={p}")],
            )
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertFalse(any("for ループ変数 'p'" in w for w in result.warnings))

    def test_detects_move_constraint_violation_inside_for_loop(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="det_pos",
                values=[-40000, 1000],
                body=[
                    StageAction(
                        operation="move_absolute",
                        ch=9,
                        value="det_pos",
                    )
                ],
            )
        ])
        controller = _FakeStageController({8: 100, 9: -40000})

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=controller),
        )

        self.assertTrue(any("Move blocked: Ch9" in e for e in result.errors))

    def test_rejects_oscillation_endpoints_that_round_to_same_pulse(self):
        sequence = Sequence(actions=[TakeXrdAction(
            oscillate=True,
            osc_pos_a_deg=0.0,
            osc_pos_b_deg=0.001,
        )])

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=_FakeStageController({}), radicon=_FakeRadicon()),
        )

        self.assertTrue(any("different pulse positions" in e for e in result.errors))

    @unittest.skip(
        "Ch8/Ch11 collision rule is currently commented out in "
        "MOVE_CONSTRAINTS (utils/stage/control_stage.py) — re-enable this "
        "test once that constraint is restored."
    )
    def test_rejects_oscillation_when_ch8_is_extended(self):
        sequence = Sequence(actions=[TakeXrdAction(oscillate=True)])
        controller = _FakeStageController({8: 1})

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=controller, radicon=_FakeRadicon()),
        )

        self.assertTrue(any("Move blocked: Ch11" in e for e in result.errors))

    def test_oscillation_requires_stage_controller(self):
        sequence = Sequence(actions=[TakeXrdAction(oscillate=True)])

        result = PreValidator().validate(
            sequence,
            DeviceContext(radicon=_FakeRadicon()),
        )

        self.assertTrue(any("required for Ch11 oscillation" in e for e in result.errors))

    def test_validates_global_oscillation_settings_used_by_step(self):
        sequence = Sequence(actions=[TakeXrdAction(oscillate=None)])

        result = PreValidator().validate(
            sequence,
            DeviceContext(controller=_FakeStageController({}), radicon=_FakeRadicon()),
            global_xrd=GlobalXrdSettings(
                oscillate=True,
                osc_speed="invalid",
            ),
        )

        self.assertTrue(any("speed must be one of L, M, or H" in e for e in result.errors))

    # ── numeric validation: WaitAction / FollowSampleAction duration_s ──────

    def test_wait_duration_infinite_is_rejected(self):
        sequence = Sequence(actions=[WaitAction(duration_s=math.inf)])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("duration_s" in e and "NaN/Inf" in e for e in result.errors))

    def test_wait_duration_zero_is_rejected(self):
        sequence = Sequence(actions=[WaitAction(duration_s=0.0)])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("duration_s" in e for e in result.errors))

    def test_wait_duration_positive_is_accepted(self):
        sequence = Sequence(actions=[WaitAction(duration_s=5.0)])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertFalse(any("duration_s" in e for e in result.errors))

    def test_follow_sample_position_duration_infinite_is_rejected(self):
        sequence = Sequence(actions=[FollowSampleAction(duration_s=math.inf)])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("duration_s" in e and "NaN/Inf" in e for e in result.errors))

    # ── numeric validation: LakeShore 335 ramp_rate / tol_k ─────────────────

    def test_set_temperature_missing_ramp_rate_does_not_crash_prevalidator(self):
        # Simulates the DSL path (`set_temperature(value=300.0)` omitting the
        # required ramp_rate): dsl/parser.py's SequenceBuilder builds this
        # with ramp_rate=None rather than raising. Before the fix, PreValidator
        # compared `a.ramp_rate < 0` directly and raised an unhandled
        # TypeError, aborting validate() entirely instead of reporting a
        # validation error.
        sequence = Sequence(actions=[
            SetTemperatureAction(value_k=300.0, ramp_rate=None)
        ])

        result = PreValidator().validate(
            sequence, DeviceContext(lakeshore=_FakeLakeshore())
        )

        self.assertFalse(result.ok)
        self.assertTrue(
            any("ramp_rate" in e and "not numeric" in e for e in result.errors)
        )

    def test_set_temperature_ramp_rate_infinite_is_rejected(self):
        sequence = Sequence(actions=[
            SetTemperatureAction(value_k=300.0, ramp_rate=math.inf)
        ])

        result = PreValidator().validate(
            sequence, DeviceContext(lakeshore=_FakeLakeshore())
        )

        self.assertTrue(
            any("ramp_rate" in e and "NaN/Inf" in e for e in result.errors)
        )

    def test_wait_temperature_tol_k_nan_is_rejected(self):
        sequence = Sequence(actions=[WaitTemperatureAction(tol_k=math.nan)])

        result = PreValidator().validate(
            sequence, DeviceContext(lakeshore=_FakeLakeshore())
        )

        self.assertTrue(
            any("tol_k" in e and "NaN/Inf" in e for e in result.errors)
        )

    # ── numeric validation: Global limits ───────────────────────────────────

    def test_global_limits_negative_value_is_rejected(self):
        global_limits = GlobalLimits(
            ch3_minus_mm=1.0, ch3_plus_mm=1.0,
            ch4_minus_mm=1.0, ch4_plus_mm=1.0,
            ch5_minus_mm=-1.0, ch5_plus_mm=1.0,
        )
        sequence = Sequence(actions=[LogAction(message="noop")])

        result = PreValidator().validate(
            sequence, DeviceContext(), global_limits=global_limits,
        )

        self.assertTrue(any("Ch5 -mm" in e for e in result.errors))

    def test_global_limits_all_zero_is_accepted(self):
        # 0.0 is documented as "locked" (no movement allowed), not an error.
        global_limits = GlobalLimits(
            ch3_minus_mm=0.0, ch3_plus_mm=0.0,
            ch4_minus_mm=0.0, ch4_plus_mm=0.0,
            ch5_minus_mm=0.0, ch5_plus_mm=0.0,
        )
        sequence = Sequence(actions=[LogAction(message="noop")])

        result = PreValidator().validate(
            sequence, DeviceContext(), global_limits=global_limits,
        )

        self.assertFalse(any("Global limits" in e for e in result.errors))

    # ── numeric validation: follow-action per-step overrides ────────────────

    def test_follow_similarity_threshold_out_of_range_is_rejected(self):
        sequence = Sequence(actions=[
            FollowSampleAction(duration_s=5.0, similarity_threshold=1.5)
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(
            any("similarity_threshold" in e for e in result.errors)
        )

    def test_follow_interval_non_positive_is_rejected(self):
        sequence = Sequence(actions=[
            StartFollowingAction(interval_s=0.0)
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("interval_s" in e for e in result.errors))

    def test_follow_max_correction_negative_is_rejected(self):
        sequence = Sequence(actions=[
            FollowSampleAction(duration_s=5.0, max_correction_per_step_um=-1.0)
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(
            any("max_correction_per_step_um" in e for e in result.errors)
        )

    # ── numeric validation: Rad-icon exposure_ms ────────────────────────────

    def test_take_dark_exposure_ms_zero_is_rejected(self):
        sequence = Sequence(actions=[TakeDarkAction(exposure_ms=0)])

        result = PreValidator().validate(
            sequence, DeviceContext(radicon=_FakeRadicon())
        )

        self.assertTrue(any("exposure_ms" in e for e in result.errors))


class ExpSchedulerPreValidatorDirectActionInjectionTests(unittest.TestCase):
    """REORGANISATION_PLAN.md Phase 0 item 8: the Visual editor and JSON
    Sequence load both build Action objects directly, never passing through
    dsl.validator.ASTValidator (which only ever sees DSL *text* — see
    ui/dsl_editor.py). These tests construct such Actions directly, and via
    action_from_dict() (the JSON-load path), to confirm PreValidator's own
    field-level checks are an independent safety net rather than something
    that only happens to work because the DSL layer already filtered the
    input."""

    def test_invalid_pressure_unit_from_direct_action_construction_is_caught(self):
        # "GPa" is rejected by ASTValidator._VALID_UNITS when it appears as
        # a DSL literal, but a directly-constructed Action object (as the
        # Visual editor produces) has no such gate — only
        # PreValidator._check_pace5000_params stands between this and the
        # runner.
        sequence = Sequence(actions=[
            SetPressureAction(pressure=1.0, unit="GPa", rate=0.1, rate_unit="MPa/min")
        ])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("unit must be" in e for e in result.errors))

    def test_invalid_pressure_unit_loaded_from_json_dict_is_caught(self):
        action = action_from_dict({
            "type": "set_pressure",
            "pressure": 1.0,
            "unit": "GPa",
            "rate": 0.1,
            "rate_unit": "MPa/min",
        })
        sequence = Sequence(actions=[action])

        result = PreValidator().validate(sequence, DeviceContext())

        self.assertTrue(any("unit must be" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
