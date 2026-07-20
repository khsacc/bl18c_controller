import collections
import math
import sys
import types
import unittest
import unittest.mock

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
    MicroscopeOutFpdInAction,
    SaveSnapshotAction,
    SetAndWaitPressureAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitPressureAction,
    WaitTemperatureAction,
    action_from_dict,
)
from apps.exp_scheduler.device_context import DeviceContext
from apps.exp_scheduler.scheduler_settings import GlobalLimits, GlobalXrdSettings
from apps.exp_scheduler.sequence import Sequence
from apps.exp_scheduler.validator.models import Severity, ValidationPhase
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


class Phase4PaceUnitDedupTests(unittest.TestCase):
    """pre_validator.py used to hand-maintain its own copy of the PACE5000
    pressure/rate-unit-to-MPa conversion tables (_PACE_TO_MPA,
    _PACE_VALID_UNITS) instead of importing
    apps.PACE5000.pace5000_backend.PRESSURE_UNIT_TO_MPA /
    RATE_UNIT_TO_MPA_PER_MIN — the same tables SequenceRunner uses. Phase 4
    replaced the local copy with an alias import; these tests pin that down
    so a future edit can't silently reintroduce a second copy that drifts
    from the PACE5000 submodule's own units."""

    def test_pressure_unit_table_is_the_same_object_as_the_pace_backend(self):
        from apps.exp_scheduler.validator import pre_validator
        from apps.PACE5000.pace5000_backend import PRESSURE_UNIT_TO_MPA

        self.assertIs(pre_validator._PACE_TO_MPA, PRESSURE_UNIT_TO_MPA)

    def test_valid_rate_units_are_derived_from_the_pace_backend_table(self):
        from apps.exp_scheduler.validator import pre_validator
        from apps.PACE5000.pace5000_backend import RATE_UNIT_TO_MPA_PER_MIN

        self.assertEqual(
            set(pre_validator._PACE_VALID_RATE_UNITS),
            set(RATE_UNIT_TO_MPA_PER_MIN),
        )


class SetAndWaitPressureActionAloneTests(unittest.TestCase):
    """REORGANISATION_PLAN.md Phase 5: ExecutionTrace.flat/pace_primitives()
    must split SetAndWaitPressureAction into its set/wait pair the same way
    the old _collect_all_actions/_walk_pace_actions did, so a sequence using
    ONLY set_and_wait_pressure (never a bare set_pressure/wait_pressure)
    still gets every PACE5000 check a sequence using the two separate calls
    would get."""

    def test_reports_pace5000_not_connected(self):
        sequence = Sequence(actions=[
            SetAndWaitPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01)
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("PACE5000 is not connected" in e for e in result.errors))

    def test_reports_control_mode_measure_error(self):
        from tests.exp_scheduler_fakes import FakePace5000

        sequence = Sequence(actions=[
            SetAndWaitPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min", tol=0.01)
        ])
        result = PreValidator().validate(
            sequence, DeviceContext(pace5000=FakePace5000(output_state="0"))
        )
        self.assertTrue(any("Control Mode" in e for e in result.errors))

    def test_check_pace5000_params_catches_invalid_unit(self):
        from tests.exp_scheduler_fakes import FakePace5000

        sequence = Sequence(actions=[
            SetAndWaitPressureAction(pressure=1.0, unit="GPa", rate=0.1, rate_unit="MPa/min", tol=0.01)
        ])
        result = PreValidator().validate(
            sequence, DeviceContext(pace5000=FakePace5000())
        )
        self.assertTrue(any("unit must be" in e for e in result.errors))


class LoopCrossIterationStateTests(unittest.TestCase):
    """REORGANISATION_PLAN.md Phase 5: the ordered/pace_primitives consumers
    (previously each hand-rolling their own ForLoopAction walk) now share
    ExecutionTrace.ordered — these tests exercise a state carried from one
    loop iteration into the next for each consumer that didn't already have
    a ForLoopAction-based test (§8.3 test matrix)."""

    def test_pace5000_ordering_detects_missing_wait_between_iterations(self):
        # 2 iterations, each body a single set_pressure with no wait_pressure
        # between them — the violation is only visible because the 2nd
        # iteration's set_pressure sees "wait_since_last_set=False" left by
        # the 1st iteration.
        sequence = Sequence(actions=[
            ForLoopAction(var="p", values=[1.0, 2.0], body=[
                SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"),
            ]),
        ])
        from tests.exp_scheduler_fakes import FakePace5000
        result = PreValidator().validate(sequence, DeviceContext(pace5000=FakePace5000()))
        self.assertTrue(
            any("直前の set_pressure との間に wait_pressure が" in w for w in result.warnings)
        )

    def test_pace5000_wait_duration_estimate_uses_previous_iteration_target(self):
        # iteration 1 sets 1.0 MPa, iteration 2 jumps to 5.0 MPa at a slow
        # rate with only a short wait() — the ramp-time estimate for
        # iteration 2 depends on current_pressure_mpa carried forward from
        # iteration 1's target (1.0 MPa), not the device's initial reading.
        sequence = Sequence(actions=[
            ForLoopAction(var="p", values=[1.0, 5.0], body=[
                SetPressureAction(pressure="p", unit="MPa", rate=0.001, rate_unit="MPa/sec"),
                WaitAction(duration_s=1.0),
            ]),
        ])
        from tests.exp_scheduler_fakes import FakePace5000
        result = PreValidator().validate(
            sequence, DeviceContext(pace5000=FakePace5000(target_pressure=0.0))
        )
        self.assertTrue(
            any("概算所要時間" in w for w in result.warnings)
        )

    def test_lakeshore_sequence_diff_zero_warning_uses_previous_iteration_setpoint(self):
        # Same setpoint (300 K) requested on both iterations via a loop
        # variable — the "no change from previous setpoint" warning on
        # iteration 2 depends on current_setpoint carried from iteration 1.
        sequence = Sequence(actions=[
            ForLoopAction(var="t", values=[300.0, 300.0], body=[
                SetTemperatureAction(value_k="t", ramp_rate=1.0),
                WaitTemperatureAction(tol_k=0.1),
            ]),
        ])
        from tests.exp_scheduler_fakes import FakeLakeshore
        result = PreValidator().validate(sequence, DeviceContext(lakeshore=FakeLakeshore()))
        self.assertTrue(
            any("変化していません" in w for w in result.warnings)
        )

    def test_stage_mode_ordering_state_survives_past_the_loop(self):
        # Both loop iterations run microscope_out_and_fpd_in (stage_mode ->
        # "xrd"); the resulting error on the *following* camera action can
        # only be correct if trace.ordered actually visited both iterations
        # (not just the first) before carrying stage_mode forward.
        sequence = Sequence(actions=[
            ForLoopAction(var="i", values=[1.0, 2.0], body=[
                MicroscopeOutFpdInAction(microscope_out_pos=-1000, fpd_in_pos=-1000),
            ]),
            SaveSnapshotAction(),
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(
            any(
                "Step3" in e and "カメラ操作を" in e
                for e in result.errors
            )
        )

    def test_emergency_stop_confirmation_rearms_on_each_iteration(self):
        # Per emergency_stop() call, only the first following move is
        # flagged; a 2nd emergency_stop() in the next iteration must
        # re-arm the nudge rather than staying silenced from iteration 1.
        sequence = Sequence(actions=[
            ForLoopAction(var="i", values=[1.0, 2.0], body=[
                StageAction(operation="emergency_stop"),
                StageAction(operation="move_absolute", ch=4, value=100),
                StageAction(operation="move_absolute", ch=4, value=200),
            ]),
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        confirmations = [w for w in result.warnings if "emergency_stop()" in w]
        self.assertEqual(len(confirmations), 2)

    def test_source_pressure_finds_max_across_loop_iterations_not_just_first(self):
        # The loop-variable pressure sweep's true maximum (5.0 MPa, on the
        # 2nd iteration) must be found — not just the first iteration's
        # value (1.0 MPa, which alone would not exceed the source pressure).
        sequence = Sequence(actions=[
            ForLoopAction(var="p", values=[1.0, 5.0, 2.0], body=[
                SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"),
                WaitPressureAction(unit="MPa", tol=0.01),
            ]),
        ])
        from tests.exp_scheduler_fakes import FakePace5000
        result = PreValidator().validate(
            sequence,
            DeviceContext(pace5000=FakePace5000(output_state="1", positive_source_pressure=3.0)),
        )
        self.assertTrue(
            any("Source Pressureを上げてから" in e for e in result.errors)
        )


class LoopLimitSafetyRegressionTests(unittest.TestCase):
    """REORGANISATION_PLAN.md Phase 5 completion criterion: a Sequence that
    exceeds the loop-expansion limits (depth, single-loop width, or total
    expanded steps) must still validate quickly and safely — connectivity
    checks (which use ExecutionTrace.flat, always fully populated) keep
    reporting, while the ordered/pace_primitives-dependent checks are
    skipped rather than materialising an oversized or unbounded-recursion
    unroll."""

    def test_deep_nesting_does_not_crash_and_still_reports_connectivity(self):
        actions = [WaitAction(duration_s=1.0)]
        for i in range(100):
            actions = [ForLoopAction(var=f"v{i}", values=[1.0], body=actions)]
        sequence = Sequence(actions=actions)

        result = PreValidator().validate(sequence, DeviceContext())  # must not raise

        self.assertFalse(result.ok)
        self.assertTrue(any("ネスト深度が上限" in e for e in result.errors))

    def test_single_loop_width_exceeded_still_reports_stage_connectivity(self):
        sequence = Sequence(actions=[
            ForLoopAction(
                var="i", values=[float(x) for x in range(3000)],
                body=[StageAction(operation="move_absolute", ch=4, value=100)],
            ),
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("反復回数が上限" in e for e in result.errors))
        self.assertTrue(
            any("Stage controller is not connected" in e for e in result.errors)
        )

    def test_total_steps_exceeded_still_reports_pace5000_connectivity(self):
        sequence = Sequence(actions=[
            ForLoopAction(var="i", values=[float(x) for x in range(200)], body=[
                ForLoopAction(var="j", values=[float(x) for x in range(200)], body=[
                    SetPressureAction(pressure=1.0, unit="MPa", rate=0.1, rate_unit="MPa/min"),
                ]),
            ]),
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("総ステップ数が上限" in e for e in result.errors))
        self.assertTrue(
            any("PACE5000 is not connected" in e for e in result.errors)
        )


class DiagnosticCodeConsistencyTests(unittest.TestCase):
    """REORGANISATION_PLAN.md Phase 5 completion criterion: DSL / Visual /
    JSON must produce the same Diagnostic.code for the same underlying
    invalid Action. Uses a boundary example that DSL compile does NOT
    reject (ASTValidator has no hardware-slew-rate rule) but
    action_params.check_pace5000_params does — the same case a DslCompiler
    self-fix loop would need a stable code for."""

    def test_rate_below_hardware_min_slew_same_code_via_dsl_and_direct_construction(self):
        from apps.exp_scheduler.dsl.compiler import DslCompiler
        from tests.exp_scheduler_fakes import FakePace5000

        dsl_source = (
            'set_pressure(pressure=1.0, unit="MPa", rate=0.0001, rate_unit="MPa/sec")'
        )
        compiled = DslCompiler().compile(dsl_source)
        self.assertIsNotNone(compiled.sequence, "boundary case must compile successfully")

        dsl_result = PreValidator().validate(
            compiled.sequence, DeviceContext(pace5000=FakePace5000())
        )
        direct_sequence = Sequence(actions=[
            SetPressureAction(pressure=1.0, unit="MPa", rate=0.0001, rate_unit="MPa/sec")
        ])
        direct_result = PreValidator().validate(
            direct_sequence, DeviceContext(pace5000=FakePace5000())
        )

        target_code = "static.pace5000.rate_below_min_slew"
        dsl_codes = [d.code for d in dsl_result.diagnostics if d.code == target_code]
        direct_codes = [d.code for d in direct_result.diagnostics if d.code == target_code]
        self.assertEqual(dsl_codes, [target_code])
        self.assertEqual(direct_codes, [target_code])


class StaticCheckRobustnessTests(unittest.TestCase):
    """External-review follow-up (post-Phase-5): a handful of static Action
    checks either bypassed NaN via a direct comparison, left an
    OverflowError uncaught, or hadn't been migrated to the Diagnostic model
    yet. Each fix gets a regression test here."""

    def test_autofocus_range_and_steps_reject_nan(self):
        # Direct comparisons (range_um <= 0 / steps < 2) are False for NaN
        # (every comparison against NaN is False) — require_finite_number
        # rejects NaN/Inf/non-numeric explicitly instead.
        sequence = Sequence(actions=[
            StartFollowingAction(autofocus_range_um=math.nan, autofocus_steps=math.nan)
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("autofocus_range_um" in e for e in result.errors))
        self.assertTrue(any("autofocus_steps" in e for e in result.errors))

    def test_autofocus_range_and_steps_reject_infinite(self):
        sequence = Sequence(actions=[
            StartFollowingAction(autofocus_range_um=math.inf, autofocus_steps=math.inf)
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("autofocus_range_um" in e for e in result.errors))
        self.assertTrue(any("autofocus_steps" in e for e in result.errors))

    def test_oversized_stage_position_is_rejected_not_an_internal_error(self):
        # float(10**500) raises OverflowError, which parse_stage_position
        # must catch (alongside TypeError/ValueError) — an uncaught
        # OverflowError previously surfaced as
        # "check_stage_schema: internal validation error (...)" instead of
        # a clean static.stage.invalid_position Diagnostic.
        sequence = Sequence(actions=[
            StageAction(operation="move_absolute", ch=4, value=10 ** 500)
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertFalse(any("internal validation error" in e for e in result.errors))
        self.assertTrue(
            any(d.code == "static.stage.invalid_position" for d in result.diagnostics)
        )

    def test_xrd_save_dir_warning_is_a_diagnostic(self):
        sequence = Sequence(actions=[
            TakeXrdAction(save_dir="/definitely/does/not/exist/xyz")
        ])
        result = PreValidator().validate(
            sequence, DeviceContext(radicon=_FakeRadicon())
        )
        self.assertTrue(
            any(d.code == "static.xrd.save_dir_will_be_created" for d in result.diagnostics)
        )

    def test_follow_not_closed_warning_is_a_diagnostic(self):
        sequence = Sequence(actions=[StartFollowingAction()])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(
            any(d.code == "static.sequence.follow_not_closed" for d in result.diagnostics)
        )

    def test_follow_not_closed_diagnostic_carries_action_path_and_loop_context(self):
        # 2nd-round external review: the Diagnostic must point at the
        # specific unclosed start_following, including its loop iteration —
        # not carry action_path=None/loop_context=None.
        sequence = Sequence(actions=[
            ForLoopAction(var="i", values=[1.0], body=[StartFollowingAction()]),
        ])
        result = PreValidator().validate(sequence, DeviceContext())
        diag = next(
            d for d in result.diagnostics if d.code == "static.sequence.follow_not_closed"
        )
        self.assertIsNotNone(diag.action_path)
        self.assertIsNotNone(diag.loop_context)

    def test_numeric_string_follow_param_is_rejected_not_silently_accepted(self):
        # A hand-edited Sequence JSON can leave interval_s/similarity_threshold/
        # max_correction_per_step_um as a numeric-looking str (StartFollowingAction.
        # from_dict() does not coerce them) — float("1.5") would parse fine, but
        # nothing converts the Action field itself, so it reaches
        # `time.monotonic() + interval_s` in runner.py as a str and raises TypeError.
        sequence = Sequence(actions=[StartFollowingAction(interval_s="1.5")])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("interval_s" in e for e in result.errors))

    def test_autofocus_steps_rejects_non_integer_float(self):
        sequence = Sequence(actions=[StartFollowingAction(autofocus_steps=2.5)])
        result = PreValidator().validate(sequence, DeviceContext())
        self.assertTrue(any("autofocus_steps" in e for e in result.errors))


class Phase7DiagnosticCoverageRegressionTests(unittest.TestCase):
    """REORGANISATION_PLAN.md Phase 7 §7 item: eight call sites used to
    write directly to PreCheckResult.errors/.warnings without also
    recording a Diagnostic, which would have made ValidationReport.errors/
    .warnings (computed from .diagnostics — validation_service.py) silently
    drop these messages. Each now goes through emit_static/emit_diagnostic;
    these tests assert code/severity/phase (not just message text, since
    the whole point of the migration is the Diagnostic itself)."""

    def _diag(self, result, code):
        matches = [d for d in result.diagnostics if d.code == code]
        self.assertEqual(len(matches), 1, f"expected exactly one {code!r} diagnostic")
        return matches[0]

    def test_global_limits_not_configured_is_a_static_diagnostic(self):
        global_limits = GlobalLimits()  # all None -> not fully configured
        sequence = Sequence(actions=[LogAction(message="noop")])
        result = PreValidator().validate(
            sequence, DeviceContext(), global_limits=global_limits,
        )
        diag = self._diag(result, "static.global_limits.not_configured")
        self.assertEqual(diag.severity, Severity.ERROR)
        self.assertEqual(diag.phase, ValidationPhase.STATIC)

    def test_global_limits_non_finite_value_is_a_static_diagnostic(self):
        global_limits = GlobalLimits(
            ch3_minus_mm=math.nan, ch3_plus_mm=1.0,
            ch4_minus_mm=1.0, ch4_plus_mm=1.0,
            ch5_minus_mm=1.0, ch5_plus_mm=1.0,
        )
        sequence = Sequence(actions=[LogAction(message="noop")])
        result = PreValidator().validate(
            sequence, DeviceContext(), global_limits=global_limits,
        )
        diag = self._diag(result, "static.global_limits.non_finite")
        self.assertEqual(diag.severity, Severity.ERROR)
        self.assertEqual(diag.phase, ValidationPhase.STATIC)

    def test_xrd_dark_file_missing_is_a_static_warning_diagnostic(self):
        sequence = Sequence(actions=[TakeXrdAction()])
        global_xrd = GlobalXrdSettings(
            dark_enabled=True, dark_file="/definitely/does/not/exist/dark.tif",
        )
        result = PreValidator().validate(
            sequence, DeviceContext(radicon=_FakeRadicon()), global_xrd=global_xrd,
        )
        diag = self._diag(result, "static.xrd.dark_file_missing")
        self.assertEqual(diag.severity, Severity.WARNING)
        self.assertEqual(diag.phase, ValidationPhase.STATIC)

    def test_xrd_defect_file_missing_is_a_static_warning_diagnostic(self):
        sequence = Sequence(actions=[TakeXrdAction()])
        global_xrd = GlobalXrdSettings(
            defect_enabled=True, defect_file="/definitely/does/not/exist/defect.tif",
        )
        result = PreValidator().validate(
            sequence, DeviceContext(radicon=_FakeRadicon()), global_xrd=global_xrd,
        )
        diag = self._diag(result, "static.xrd.defect_file_missing")
        self.assertEqual(diag.severity, Severity.WARNING)
        self.assertEqual(diag.phase, ValidationPhase.STATIC)

    def test_autofocus_ch3_limits_unset_is_a_static_warning_diagnostic(self):
        sequence = Sequence(actions=[StartFollowingAction()])
        result = PreValidator().validate(sequence, DeviceContext())
        diag = self._diag(result, "static.follow.autofocus_ch3_limits_unset")
        self.assertEqual(diag.severity, Severity.WARNING)
        self.assertEqual(diag.phase, ValidationPhase.STATIC)

    def test_execution_trace_build_failure_is_a_static_diagnostic(self):
        sequence = Sequence(actions=[LogAction(message="noop")])
        with unittest.mock.patch(
            "apps.exp_scheduler.validator.pre_validator.ExecutionTrace.build",
            side_effect=RuntimeError("boom"),
        ):
            result = PreValidator().validate(sequence, DeviceContext())
        diag = self._diag(result, "internal.execution_trace_build_error")
        self.assertEqual(diag.severity, Severity.ERROR)
        self.assertEqual(diag.phase, ValidationPhase.STATIC)

    def test_snapshot_collection_failure_is_a_preflight_diagnostic(self):
        sequence = Sequence(actions=[LogAction(message="noop")])
        with unittest.mock.patch(
            "apps.exp_scheduler.validator.pre_validator.snapshots.collect_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            result = PreValidator().validate(sequence, DeviceContext())
        diag = self._diag(result, "internal.snapshot_collection_error")
        self.assertEqual(diag.severity, Severity.ERROR)
        self.assertEqual(diag.phase, ValidationPhase.PREFLIGHT)
        self.assertIsNone(diag.device)

    def test_run_safety_net_tags_static_checker_failure_as_static(self):
        # check_empty_sequence lives in sequence_structure.py (STATIC).
        sequence = Sequence(actions=[LogAction(message="noop")])
        with unittest.mock.patch(
            "apps.exp_scheduler.validator.checks.sequence_structure.check_empty_sequence",
            side_effect=RuntimeError("boom"),
        ):
            result = PreValidator().validate(sequence, DeviceContext())
        diag = self._diag(result, "internal.check_error")
        self.assertEqual(diag.phase, ValidationPhase.STATIC)
        self.assertIsNone(diag.device)

    def test_run_safety_net_tags_preflight_checker_failure_as_preflight(self):
        # check_stage lives in validator/checks/stage.py (PREFLIGHT, device="stage").
        sequence = Sequence(actions=[LogAction(message="noop")])
        with unittest.mock.patch(
            "apps.exp_scheduler.validator.checks.stage.check_stage",
            side_effect=RuntimeError("boom"),
        ):
            result = PreValidator().validate(sequence, DeviceContext())
        diag = self._diag(result, "internal.check_error")
        self.assertEqual(diag.phase, ValidationPhase.PREFLIGHT)
        self.assertEqual(diag.device, "stage")

    def test_no_orphan_messages_across_multiple_simultaneous_checkers(self):
        # Regression guard for the Phase 7 Diagnostic-coverage migration
        # itself: every message PreValidator appends to result.errors/
        # .warnings must also appear in result.diagnostics (and vice
        # versa), as a *multiset* — Counter, not set, because the same
        # message text can legitimately be produced by more than one
        # action (e.g. the repeated "microscope_out_and_fpd_in() has not
        # been called" stage-mode warning below), so a set comparison
        # would not catch a missing/duplicated Diagnostic in that case.
        # This is the property validation_service.py's ValidationReport
        # relies on: it discards PreCheckResult.errors/.warnings entirely
        # and treats .diagnostics as the sole source of truth.
        sequence = Sequence(actions=[
            TakeXrdAction(),
            TakeDarkAction(exposure_ms=0),
            StartFollowingAction(),
        ])
        global_limits = GlobalLimits()  # not fully configured -> error
        global_xrd = GlobalXrdSettings(
            dark_enabled=True, dark_file="/nope/dark.tif",
            defect_enabled=True, defect_file="/nope/defect.tif",
        )
        result = PreValidator().validate(
            sequence, DeviceContext(radicon=_FakeRadicon()),
            global_limits=global_limits, global_xrd=global_xrd,
        )
        self.assertTrue(result.errors, "fixture must actually produce errors")
        self.assertTrue(result.warnings, "fixture must actually produce warnings")

        diagnostic_messages = collections.Counter(d.message for d in result.diagnostics)
        list_messages = collections.Counter(result.errors + result.warnings)
        self.assertEqual(diagnostic_messages, list_messages)


if __name__ == "__main__":
    unittest.main()
