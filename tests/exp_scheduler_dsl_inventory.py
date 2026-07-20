"""
Per-command DSL inventory — the test-data table required by
apps/exp_scheduler/REORGANISATION_PLAN.md Phase 0, item 3.

This is a *test fixture*, not a production registry: it independently
records, for every name in `dsl.ALLOWED_FUNCTIONS` (plus the one known
orphan, `normal_stop`), the minimal valid call text, the required/optional
keyword split, the Action type `dsl.parser.SequenceBuilder` currently
produces, and which keyword arguments accept a bound `for`-loop variable
(per `actions.LOOP_VAR_FIELDS`).

Recording this by hand (rather than deriving it from `dsl/api.py` /
`dsl/parser.py`) is intentional for Phase 0: the whole point of
characterization testing is to have an independent expectation to compare
the current implementation against, so a Phase-3 `CommandSpec` registry bug
can't silently make this table agree with itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.exp_scheduler.actions import (
    Action,
    AllHeatersOffAction,
    FpdOutMicroscopeInAction,
    FollowSampleAction,
    MicroscopeOutFpdInAction,
    LogAction,
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
)


@dataclass(frozen=True)
class CommandEntry:
    name: str
    min_call: str
    required_kwargs: frozenset[str]
    optional_kwargs: frozenset[str]
    action_type: type[Action]
    loop_var_kwargs: frozenset[str] = field(default_factory=frozenset)
    # Was False only for `normal_stop` pre-Phase-2: defined in dsl/api.py,
    # dsl/parser.py's SequenceBuilder._BUILDERS, and dsl/_registry.py, but
    # missing from dsl/__init__.py's ALLOWED_FUNCTIONS — see
    # REORGANISATION_PLAN.md 2.2 / 12.5 decision #... Phase 2 added it to
    # ALLOWED_FUNCTIONS, so every entry is now in_allowed_functions=True and
    # this field is kept only so the table's shape doesn't need to change
    # again if a future command ever needs it.
    in_allowed_functions: bool = True


COMMAND_INVENTORY: tuple[CommandEntry, ...] = (
    CommandEntry(
        "wait",
        'wait(duration=1.0, unit="s")',
        required_kwargs=frozenset({"duration"}),
        optional_kwargs=frozenset({"unit"}),
        action_type=WaitAction,
    ),
    CommandEntry(
        "log_message",
        'log_message(message="x")',
        required_kwargs=frozenset({"message"}),
        optional_kwargs=frozenset(),
        action_type=LogAction,
        loop_var_kwargs=frozenset({"message"}),
    ),
    CommandEntry(
        "move_absolute",
        "move_absolute(ch=4, position=1000.0)",
        required_kwargs=frozenset({"ch", "position"}),
        optional_kwargs=frozenset(),
        action_type=StageAction,
        loop_var_kwargs=frozenset({"position"}),
    ),
    CommandEntry(
        "move_relative",
        "move_relative(ch=4, delta=100.0)",
        required_kwargs=frozenset({"ch", "delta"}),
        optional_kwargs=frozenset(),
        action_type=StageAction,
        loop_var_kwargs=frozenset({"delta"}),
    ),
    CommandEntry(
        "set_speed",
        'set_speed(ch=4, speed="H")',
        required_kwargs=frozenset({"ch", "speed"}),
        optional_kwargs=frozenset(),
        action_type=StageAction,
    ),
    CommandEntry(
        "normal_stop",
        "normal_stop()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset(),
        action_type=StageAction,
    ),
    CommandEntry(
        "emergency_stop",
        "emergency_stop()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset(),
        action_type=StageAction,
    ),
    CommandEntry(
        "microscope_out_and_fpd_in",
        "microscope_out_and_fpd_in()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset({"microscope_out_pos", "fpd_in_pos", "speed"}),
        action_type=MicroscopeOutFpdInAction,
    ),
    CommandEntry(
        "fpd_out_and_microscope_in",
        "fpd_out_and_microscope_in()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset({"fpd_out_pos", "microscope_in_pos", "speed"}),
        action_type=FpdOutMicroscopeInAction,
    ),
    CommandEntry(
        "set_pressure",
        'set_pressure(pressure=1.0, unit="MPa", rate=0.2, rate_unit="MPa/min")',
        required_kwargs=frozenset({"pressure", "unit", "rate", "rate_unit"}),
        optional_kwargs=frozenset(),
        action_type=SetPressureAction,
        loop_var_kwargs=frozenset({"pressure"}),
    ),
    CommandEntry(
        "set_and_wait_pressure",
        'set_and_wait_pressure(pressure=1.0, unit="MPa", rate=0.2, '
        'rate_unit="MPa/min", tol=0.01)',
        required_kwargs=frozenset({"pressure", "unit", "rate", "rate_unit", "tol"}),
        optional_kwargs=frozenset(),
        action_type=SetAndWaitPressureAction,
        loop_var_kwargs=frozenset({"pressure"}),
    ),
    CommandEntry(
        "wait_pressure",
        'wait_pressure(tol=0.01, unit="MPa")',
        required_kwargs=frozenset({"tol", "unit"}),
        optional_kwargs=frozenset(),
        action_type=WaitPressureAction,
    ),
    CommandEntry(
        "set_control_mode",
        "set_control_mode(enabled=True)",
        required_kwargs=frozenset({"enabled"}),
        optional_kwargs=frozenset(),
        action_type=SetControlModeAction,
    ),
    CommandEntry(
        "set_temperature",
        "set_temperature(value=300.0, ramp_rate=5.0)",
        required_kwargs=frozenset({"value", "ramp_rate"}),
        optional_kwargs=frozenset({"unit"}),
        action_type=SetTemperatureAction,
        loop_var_kwargs=frozenset({"value"}),
    ),
    CommandEntry(
        "wait_temperature",
        "wait_temperature(tol=1.0)",
        required_kwargs=frozenset({"tol"}),
        optional_kwargs=frozenset({"unit"}),
        action_type=WaitTemperatureAction,
    ),
    CommandEntry(
        "set_heater",
        "set_heater(range_index=1)",
        required_kwargs=frozenset({"range_index"}),
        optional_kwargs=frozenset(),
        action_type=SetHeaterAction,
    ),
    CommandEntry(
        "all_heaters_off",
        "all_heaters_off()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset(),
        action_type=AllHeatersOffAction,
    ),
    CommandEntry(
        "take_xrd",
        "take_xrd()",
        required_kwargs=frozenset(),
        # Phase 2 added the 8 acquisition/correction fields to
        # dsl/api.py::take_xrd()'s signature and fixed _build_take_xrd() to
        # pass all 13 override fields through to the Action — see
        # test_exp_scheduler_dsl_roundtrip.py::TakeXrdPerStepOverrideRoundTripTests.
        optional_kwargs=frozenset({
            "exposure_ms", "save", "prefix",
            "save_dir", "dark_file", "dark_enabled", "defect_file",
            "defect_enabled", "defect_kernel", "flip_v", "flip_h",
            "oscillate", "osc_pos_a_deg", "osc_pos_b_deg", "osc_dwell_ms", "osc_speed",
        }),
        action_type=TakeXrdAction,
    ),
    CommandEntry(
        "take_dark",
        "take_dark(exposure_ms=1000)",
        required_kwargs=frozenset({"exposure_ms"}),
        optional_kwargs=frozenset(),
        action_type=TakeDarkAction,
    ),
    CommandEntry(
        "save_snapshot",
        "save_snapshot()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset({"save_dir"}),
        action_type=SaveSnapshotAction,
    ),
    CommandEntry(
        "save_reference_image",
        "save_reference_image()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset({"path", "camera_index"}),
        action_type=SaveReferenceImageAction,
    ),
    CommandEntry(
        "start_following",
        "start_following()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset({
            "reference_path", "interval", "interval_unit",
            "similarity_threshold", "max_correction_per_step_um", "camera_index",
            "autofocus_range_um", "autofocus_steps",
        }),
        action_type=StartFollowingAction,
    ),
    CommandEntry(
        "stop_following",
        "stop_following()",
        required_kwargs=frozenset(),
        optional_kwargs=frozenset(),
        action_type=StopFollowingAction,
    ),
    CommandEntry(
        "follow_sample_position",
        'follow_sample_position(duration=1.0, unit="s")',
        required_kwargs=frozenset({"duration"}),
        optional_kwargs=frozenset({
            "unit", "reference_path", "interval", "interval_unit",
            "similarity_threshold", "max_correction_per_step_um", "camera_index",
            "autofocus_range_um", "autofocus_steps",
        }),
        action_type=FollowSampleAction,
    ),
)

#: Convenience view — only commands actually reachable through the DSL today.
ALLOWED_COMMAND_INVENTORY: tuple[CommandEntry, ...] = tuple(
    c for c in COMMAND_INVENTORY if c.in_allowed_functions
)
