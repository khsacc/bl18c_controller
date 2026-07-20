"""
`Action.to_dsl() -> compile -> Action` round-trip tests — apps/exp_scheduler
REORGANISATION_PLAN.md Phase 0, items 5 and 6.

`ui/dsl_editor.py::set_sequence()` converts every Visual-editor Action to DSL
text by joining `action.to_dsl()` calls (see REORGANISATION_PLAN.md §2.1) —
so any field `to_dsl()` emits that the compiler (`ASTValidator` +
`SequenceBuilder`) can't fully understand is not just a DSL-authoring
footgun, it is the app silently mangling — or outright rejecting — its own
generated Script tab content. This file pins down, per Action type, which
fields currently survive that trip and which don't, so Phase 2/3 fixes turn
these into lossless-round-trip regression tests instead of leaving the gap
undiscovered.
"""
from __future__ import annotations

import ast
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
    AllHeatersOffAction,
    FollowSampleAction,
    FpdOutMicroscopeInAction,
    ForLoopAction,
    LogAction,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    SetAndWaitPressureAction,
    SetControlModeAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitPressureAction,
    WaitTemperatureAction,
    action_from_dict,
)
from apps.exp_scheduler.dsl.parser import SequenceBuilder, SequenceBuildError
from apps.exp_scheduler.dsl.validator import ASTValidator


def _round_trip(action):
    """Run the exact path ui/dsl_editor.py uses: to_dsl() -> ASTValidator ->
    SequenceBuilder. Returns (rebuilt_action, dsl_text, validator_errors)."""
    dsl_text = action.to_dsl() + "\n"
    errors = ASTValidator().validate(dsl_text)
    if errors:
        return None, dsl_text, errors
    sequence = SequenceBuilder().build(ast.parse(dsl_text))
    assert len(sequence.actions) == 1, (
        f"expected exactly 1 action from {dsl_text!r}, got {len(sequence.actions)}: "
        f"{sequence.actions}"
    )
    return sequence.actions[0], dsl_text, []


class ActionDslRoundTripTests(unittest.TestCase):
    """One test per Action type — `Action.to_dsl()` output compiles back to
    an equivalent Action. Where it currently doesn't, the test asserts the
    *actual* (lossy) result, not the desired one — see module docstring."""

    def test_wait_action(self):
        original = WaitAction(duration_s=45.0)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertIsInstance(rebuilt, WaitAction)
        self.assertEqual(rebuilt.duration_s, 45.0)

    def test_wait_action_minute_formatting(self):
        original = WaitAction(duration_s=120.0)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.duration_s, 120.0)

    def test_log_action_with_embedded_quotes(self):
        original = LogAction(message='hello "world"')
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.message, 'hello "world"')

    def test_stage_action_move_absolute_without_speed(self):
        original = StageAction(operation="move_absolute", ch=4, value=1234.5)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.operation, "move_absolute")
        self.assertEqual(rebuilt.ch, 4)
        self.assertEqual(rebuilt.value, 1234.5)

    def test_stage_action_move_relative_with_unbound_loop_var_is_rejected(self):
        # Phase 2: a bare-name field is only valid DSL when it's actually
        # bound by an enclosing for-loop — see
        # test_exp_scheduler_dsl_contract.py's unbound-bare-name tests. A
        # StageAction built with value="p" outside of a ForLoopAction body
        # (as here) doesn't correspond to any well-formed Sequence, so its
        # to_dsl() output is correctly rejected, not silently accepted.
        original = StageAction(operation="move_relative", ch=5, value="p")
        dsl_text = original.to_dsl() + "\n"
        self.assertEqual(ASTValidator().validate(dsl_text), [])

        with self.assertRaises(SequenceBuildError) as cm:
            SequenceBuilder().build(ast.parse(dsl_text))
        self.assertTrue(
            any(d.code == "dsl.unbound_name" for d in cm.exception.diagnostics)
        )

    def test_stage_action_move_relative_with_bound_loop_var_round_trips(self):
        original = ForLoopAction(
            var="p",
            values=[1.0, 2.0],
            body=[StageAction(operation="move_relative", ch=5, value="p")],
        )
        dsl_text = original.to_dsl() + "\n"
        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        sequence = SequenceBuilder().build(ast.parse(dsl_text))

        self.assertEqual(len(sequence.actions), 1)
        rebuilt = sequence.actions[0]
        self.assertIsInstance(rebuilt, ForLoopAction)
        self.assertEqual(len(rebuilt.body), 1)
        self.assertEqual(rebuilt.body[0].value, "p")

    def test_stage_action_emergency_stop(self):
        original = StageAction(operation="emergency_stop")
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.operation, "emergency_stop")

    def test_stage_action_normal_stop_round_trips(self):
        """§2.1's self-destructive round trip, fixed in Phase 2: a Visual
        step built with StageAction(operation="normal_stop") renders as
        `normal_stop()` in the auto-generated Script tab text, and that text
        now compiles back cleanly — `normal_stop` was added to
        ALLOWED_FUNCTIONS (see test_exp_scheduler_dsl_contract.py)."""
        original = StageAction(operation="normal_stop")
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(dsl_text.strip(), "normal_stop()")
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.operation, "normal_stop")

    def test_stage_action_move_with_speed_expands_into_two_actions(self):
        """Undocumented-in-§2.2 finding: StageAction.to_dsl() emits a
        *combined* speed-setting Action as two DSL statements
        (`set_speed(...)\\nmove_absolute(...)`), so it round-trips as two
        separate Actions, not the original single Action with a `speed`
        field. Structurally lossy in a different way than a dropped field."""
        original = StageAction(operation="move_absolute", ch=8, value=100, speed="M")
        dsl_text = original.to_dsl() + "\n"
        self.assertEqual(dsl_text.strip().count("\n"), 1)  # two lines

        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        sequence = SequenceBuilder().build(ast.parse(dsl_text))

        self.assertEqual(len(sequence.actions), 2)
        set_speed_action, move_action = sequence.actions
        self.assertEqual(set_speed_action.operation, "set_speed")
        self.assertEqual(set_speed_action.ch, 8)
        self.assertEqual(set_speed_action.speed, "M")
        self.assertEqual(move_action.operation, "move_absolute")
        self.assertEqual(move_action.ch, 8)
        self.assertEqual(move_action.value, 100)
        # Neither rebuilt action alone carries both ch=8/value=100 AND speed="M"
        # the way the original single Action did.

    def test_microscope_out_and_fpd_in(self):
        original = MicroscopeOutFpdInAction(
            microscope_out_pos=100, fpd_in_pos=-200, speed="M"
        )
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.microscope_out_pos, 100)
        self.assertEqual(rebuilt.fpd_in_pos, -200)
        self.assertEqual(rebuilt.speed, "M")

    def test_fpd_out_and_microscope_in(self):
        original = FpdOutMicroscopeInAction(
            fpd_out_pos=-300, microscope_in_pos=50, speed="L"
        )
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.fpd_out_pos, -300)
        self.assertEqual(rebuilt.microscope_in_pos, 50)
        self.assertEqual(rebuilt.speed, "L")

    def test_set_pressure_action(self):
        original = SetPressureAction(pressure=2.5, unit="MPa", rate=0.1, rate_unit="MPa/min")
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.pressure, 2.5)
        self.assertEqual(rebuilt.unit, "MPa")
        self.assertEqual(rebuilt.rate, 0.1)
        self.assertEqual(rebuilt.rate_unit, "MPa/min")

    def test_set_and_wait_pressure_action(self):
        original = SetAndWaitPressureAction(
            pressure=3.0, unit="Bar", rate=0.05, rate_unit="Bar/min", tol=0.02
        )
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.pressure, 3.0)
        self.assertEqual(rebuilt.unit, "Bar")
        self.assertEqual(rebuilt.rate, 0.05)
        self.assertEqual(rebuilt.rate_unit, "Bar/min")
        self.assertEqual(rebuilt.tol, 0.02)

    def test_wait_pressure_action(self):
        original = WaitPressureAction(tol=0.01, unit="MPa")
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.tol, 0.01)
        self.assertEqual(rebuilt.unit, "MPa")

    def test_set_control_mode_action(self):
        original = SetControlModeAction(enabled=True)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.enabled, True)

    def test_set_temperature_action(self):
        original = SetTemperatureAction(value_k=310.5, ramp_rate=2.0)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.value_k, 310.5)
        self.assertEqual(rebuilt.ramp_rate, 2.0)

    def test_wait_temperature_action(self):
        original = WaitTemperatureAction(tol_k=0.5)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.tol_k, 0.5)

    def test_set_heater_action(self):
        original = SetHeaterAction(range_index=2)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.range_index, 2)

    def test_all_heaters_off_action(self):
        original = AllHeatersOffAction()
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertIsInstance(rebuilt, AllHeatersOffAction)

    def test_take_xrd_basic_fields_survive(self):
        original = TakeXrdAction(exposure_ms=500, save=False, prefix="run1")
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.exposure_ms, 500)
        self.assertEqual(rebuilt.save, False)
        self.assertEqual(rebuilt.prefix, "run1")

    def test_take_dark_action(self):
        original = TakeDarkAction(exposure_ms=750)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.exposure_ms, 750)

    def test_save_reference_image_action(self):
        original = SaveReferenceImageAction(path="ref.png", camera_index=1)
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.path, "ref.png")
        self.assertEqual(rebuilt.camera_index, 1)

    def test_save_snapshot_action(self):
        original = SaveSnapshotAction(save_dir="snap/")
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.save_dir, "snap/")

    def test_start_following_core_fields_survive(self):
        original = StartFollowingAction(
            reference_path="ref.png",
            interval_s=5.0,
            similarity_threshold=0.8,
            max_correction_per_step_um=2.0,
            camera_index=1,
        )
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.reference_path, "ref.png")
        self.assertEqual(rebuilt.interval_s, 5.0)
        self.assertEqual(rebuilt.similarity_threshold, 0.8)
        self.assertEqual(rebuilt.max_correction_per_step_um, 2.0)
        # Was previously lost — StartFollowingAction.to_dsl() unconditionally
        # omitted camera_index (unlike every other field, which is guarded
        # by `if self.x is not None`), silently reverting a non-default
        # camera to 0 on every Visual -> Script round trip. Now fixed.
        self.assertIn("camera_index=1", dsl_text)
        self.assertEqual(rebuilt.camera_index, 1)

    def test_start_following_autofocus_fields_round_trip(self):
        """dsl/api.py::start_following() previously had no autofocus_range_um
        / autofocus_steps parameters at all, even though to_dsl() emitted
        them (same pattern as take_xrd's 13 fields) — ASTValidator accepted
        the then-unknown kwargs and dsl/parser.py::_build_start_following()
        silently dropped both. Phase 2 added both to the signature and the
        builder, so they now survive the round trip."""
        original = StartFollowingAction(autofocus_range_um=10.0, autofocus_steps=5)
        dsl_text = original.to_dsl() + "\n"
        self.assertIn("autofocus_range_um=10.0", dsl_text)
        self.assertIn("autofocus_steps=5", dsl_text)

        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        rebuilt = SequenceBuilder().build(ast.parse(dsl_text)).actions[0]
        self.assertEqual(rebuilt.autofocus_range_um, 10.0)
        self.assertEqual(rebuilt.autofocus_steps, 5)

    def test_follow_sample_position_autofocus_fields_round_trip(self):
        original = FollowSampleAction(
            duration_s=5.0, autofocus_range_um=10.0, autofocus_steps=5
        )
        dsl_text = original.to_dsl() + "\n"
        self.assertIn("autofocus_range_um=10.0", dsl_text)
        self.assertIn("autofocus_steps=5", dsl_text)

        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        rebuilt = SequenceBuilder().build(ast.parse(dsl_text)).actions[0]
        self.assertEqual(rebuilt.autofocus_range_um, 10.0)
        self.assertEqual(rebuilt.autofocus_steps, 5)

    def test_start_following_autofocus_enabled_false_round_trips(self):
        # High-severity regression test found in external review:
        # StartFollowingAction.to_dsl() never emitted autofocus_enabled at
        # all, and from_dict()/dsl/api.py's start_following() had no way to
        # carry an explicit False through, so a step that disabled Ch3
        # autofocus silently came back as autofocus_enabled=True after any
        # DSL or JSON round trip — Ch3 would then move every follow cycle
        # despite the user having turned it off.
        original = StartFollowingAction(autofocus_enabled=False)
        dsl_text = original.to_dsl() + "\n"
        self.assertIn("autofocus_enabled=False", dsl_text)

        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        rebuilt = SequenceBuilder().build(ast.parse(dsl_text)).actions[0]
        self.assertFalse(rebuilt.autofocus_enabled)

        # JSON path too — from_dict() previously hardcoded True.
        json_rebuilt = action_from_dict(original.to_dict())
        self.assertFalse(json_rebuilt.autofocus_enabled)

    def test_start_following_autofocus_enabled_true_omits_the_field(self):
        # True is the default — to_dsl() should stay quiet about it (same
        # convention as TakeXrdAction.save), so ordinary generated scripts
        # aren't cluttered with a redundant explicit True on every step.
        original = StartFollowingAction(autofocus_enabled=True)
        self.assertNotIn("autofocus_enabled", original.to_dsl())

    def test_follow_sample_position_autofocus_enabled_false_round_trips(self):
        original = FollowSampleAction(duration_s=5.0, autofocus_enabled=False)
        dsl_text = original.to_dsl() + "\n"
        self.assertIn("autofocus_enabled=False", dsl_text)

        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        rebuilt = SequenceBuilder().build(ast.parse(dsl_text)).actions[0]
        self.assertFalse(rebuilt.autofocus_enabled)

        json_rebuilt = action_from_dict(original.to_dict())
        self.assertFalse(json_rebuilt.autofocus_enabled)

    def test_follow_sample_position_to_steps_preserves_autofocus_enabled(self):
        # FollowSampleAction.to_steps() (start_following + wait +
        # stop_following, used by Runner._execute_one()) previously built
        # the StartFollowingAction without passing autofocus_enabled at
        # all, silently reverting a disabled step back to the dataclass
        # default (True) at the exact point Runner._follow_loop() reads it.
        original = FollowSampleAction(duration_s=5.0, autofocus_enabled=False)
        start_action, _wait_action, _stop_action = original.to_steps()
        self.assertFalse(start_action.autofocus_enabled)

    def test_stop_following_action(self):
        original = StopFollowingAction()
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertIsInstance(rebuilt, StopFollowingAction)

    def test_follow_sample_position_core_fields_survive(self):
        original = FollowSampleAction(
            duration_s=90.0,
            reference_path="ref.png",
            interval_s=5.0,
            similarity_threshold=0.7,
            max_correction_per_step_um=3.0,
            camera_index=1,
        )
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])
        self.assertEqual(rebuilt.duration_s, 90.0)
        self.assertEqual(rebuilt.reference_path, "ref.png")
        self.assertEqual(rebuilt.interval_s, 5.0)
        self.assertEqual(rebuilt.similarity_threshold, 0.7)
        self.assertEqual(rebuilt.max_correction_per_step_um, 3.0)
        # Same camera_index gap as StartFollowingAction, above — now fixed.
        self.assertIn("camera_index=1", dsl_text)
        self.assertEqual(rebuilt.camera_index, 1)

    def test_for_loop_action_round_trips_with_body(self):
        original = ForLoopAction(
            var="p",
            values=[1.0, 2.0, 3.0],
            body=[
                SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"),
                WaitPressureAction(tol=0.01, unit="MPa"),
            ],
        )
        dsl_text = original.to_dsl() + "\n"
        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(errors, [])
        sequence = SequenceBuilder().build(ast.parse(dsl_text))

        self.assertEqual(len(sequence.actions), 1)
        rebuilt = sequence.actions[0]
        self.assertIsInstance(rebuilt, ForLoopAction)
        self.assertEqual(rebuilt.var, "p")
        self.assertEqual(rebuilt.values, [1.0, 2.0, 3.0])
        self.assertEqual(len(rebuilt.body), 2)
        self.assertIsInstance(rebuilt.body[0], SetPressureAction)
        self.assertEqual(rebuilt.body[0].pressure, "p")
        self.assertIsInstance(rebuilt.body[1], WaitPressureAction)


# ── take_xrd: the 13-field per-step-override matrix ──────────────────────

#: The 8 "acquisition/correction" fields TakeXrdAction.to_dict()/to_dsl()
#: know about, that dsl/api.py::take_xrd() never declared as parameters at
#: all (so no CommandSpec/signature work can save them without first
#: deciding, per REORGANISATION_PLAN.md §7 Phase 0 item 10, to add them).
_TAKE_XRD_ACQUISITION_FIELDS: dict[str, object] = {
    "save_dir": "D:/data",
    "dark_file": "dark.tif",
    "dark_enabled": True,
    "defect_file": "defect.tif",
    "defect_enabled": True,
    "defect_kernel": 3,
    "flip_v": True,
    "flip_h": True,
}

#: The 5 oscillation fields that DO exist in dsl/api.py::take_xrd()'s
#: signature, but dsl/parser.py::_build_take_xrd() drops anyway.
_TAKE_XRD_OSCILLATION_FIELDS: dict[str, object] = {
    "oscillate": True,
    "osc_pos_a_deg": -3.0,
    "osc_pos_b_deg": 15.0,
    "osc_dwell_ms": 200,
    "osc_speed": "L",
}


class TakeXrdPerStepOverrideRoundTripTests(unittest.TestCase):
    """REORGANISATION_PLAN.md §2.2's flagship example, pinned down field by
    field: TakeXrdAction.to_dsl() emits all 13 fields, ASTValidator accepts
    all of them (it doesn't check kwargs against a function signature at
    all — that's SequenceBuilder's job via inspect.Signature.bind(), see
    test_exp_scheduler_dsl_contract.py's unknown-keyword test), and — since
    Phase 2 added all 13 to dsl/api.py::take_xrd()'s signature and fixed
    dsl/parser.py::_build_take_xrd() — every one now reaches the rebuilt
    Action."""

    def _make_fully_populated(self) -> TakeXrdAction:
        return TakeXrdAction(
            exposure_ms=1000,
            save=True,
            prefix="scan",
            **_TAKE_XRD_ACQUISITION_FIELDS,
            **_TAKE_XRD_OSCILLATION_FIELDS,
        )

    def test_to_dsl_emits_all_13_fields(self):
        original = self._make_fully_populated()
        dsl_text = original.to_dsl()
        for name in {**_TAKE_XRD_ACQUISITION_FIELDS, **_TAKE_XRD_OSCILLATION_FIELDS}:
            self.assertIn(name + "=", dsl_text, f"to_dsl() should still emit {name}=")

    def test_ast_validator_accepts_all_13_unknown_fields(self):
        original = self._make_fully_populated()
        dsl_text = original.to_dsl() + "\n"
        errors = ASTValidator().validate(dsl_text)
        self.assertEqual(
            errors, [],
            "ASTValidator has no per-function signature/kwarg check, so it "
            "accepts take_xrd(save_dir=..., dark_file=..., ...) even though "
            "dsl/api.py::take_xrd() has no such parameters",
        )

    def test_all_13_fields_are_preserved_by_the_builder(self):
        original = self._make_fully_populated()
        rebuilt, dsl_text, errors = _round_trip(original)
        self.assertEqual(errors, [])

        self.assertEqual(rebuilt.exposure_ms, 1000)
        self.assertEqual(rebuilt.save, True)
        self.assertEqual(rebuilt.prefix, "scan")

        for name in {**_TAKE_XRD_ACQUISITION_FIELDS, **_TAKE_XRD_OSCILLATION_FIELDS}:
            with self.subTest(field=name):
                self.assertEqual(getattr(rebuilt, name), getattr(original, name))


class JsonRoundTripTests(unittest.TestCase):
    """to_dict() -> from_dict() -> to_dict() idempotency, independent of the
    DSL — this is the path Visual/JSON load always takes, per §3.4/§5.5."""

    def _sample_actions(self) -> list:
        return [
            WaitAction(duration_s=45.0),
            LogAction(message='hello "world"'),
            StageAction(operation="move_absolute", ch=4, value=1234.5, speed="M"),
            StageAction(operation="move_relative", ch=5, value="p"),
            MicroscopeOutFpdInAction(microscope_out_pos=100, fpd_in_pos=-200, speed="M"),
            FpdOutMicroscopeInAction(fpd_out_pos=-300, microscope_in_pos=50, speed="L"),
            SetPressureAction(pressure=2.5, unit="MPa", rate=0.1, rate_unit="MPa/min"),
            SetPressureAction(pressure="p", unit="MPa", rate=0.1, rate_unit="MPa/min"),
            SetAndWaitPressureAction(
                pressure=3.0, unit="Bar", rate=0.05, rate_unit="Bar/min", tol=0.02
            ),
            WaitPressureAction(tol=0.01, unit="MPa"),
            SetControlModeAction(enabled=True),
            SetTemperatureAction(value_k=310.5, ramp_rate=2.0),
            SetTemperatureAction(value_k="t", ramp_rate=2.0),
            WaitTemperatureAction(tol_k=0.5),
            SetHeaterAction(range_index=2),
            AllHeatersOffAction(),
            self._sample_take_xrd(),
            TakeDarkAction(exposure_ms=750),
            SaveReferenceImageAction(path="ref.png", camera_index=1),
            SaveSnapshotAction(save_dir="snap/"),
            StartFollowingAction(
                reference_path="ref.png", interval_s=5.0, similarity_threshold=0.8,
                max_correction_per_step_um=2.0, camera_index=1,
                autofocus_range_um=10.0, autofocus_steps=5,
            ),
            StopFollowingAction(),
            FollowSampleAction(
                duration_s=90.0, reference_path="ref.png", interval_s=5.0,
                similarity_threshold=0.7, max_correction_per_step_um=3.0,
                camera_index=1, autofocus_range_um=10.0, autofocus_steps=5,
            ),
            ForLoopAction(
                var="p", values=[1.0, 2.0],
                body=[WaitAction(duration_s=1.0)],
            ),
        ]

    def _sample_take_xrd(self) -> TakeXrdAction:
        return TakeXrdAction(
            exposure_ms=1000, save=True, prefix="scan",
            **_TAKE_XRD_ACQUISITION_FIELDS, **_TAKE_XRD_OSCILLATION_FIELDS,
        )

    def test_to_dict_from_dict_round_trip_is_idempotent(self):
        for action in self._sample_actions():
            with self.subTest(action=type(action).__name__):
                first = action.to_dict()
                rebuilt = action_from_dict(first)
                second = rebuilt.to_dict()
                self.assertEqual(first, second)

    def test_take_xrd_json_round_trip_preserves_all_13_fields(self):
        # Unlike the DSL path, JSON round-trip is lossless today — this is
        # the contrast case showing the bug is specific to the DSL compiler,
        # not to TakeXrdAction itself.
        original = self._sample_take_xrd()
        rebuilt = action_from_dict(original.to_dict())
        for name in {**_TAKE_XRD_ACQUISITION_FIELDS, **_TAKE_XRD_OSCILLATION_FIELDS}:
            with self.subTest(field=name):
                self.assertEqual(getattr(rebuilt, name), getattr(original, name))


if __name__ == "__main__":
    unittest.main()
