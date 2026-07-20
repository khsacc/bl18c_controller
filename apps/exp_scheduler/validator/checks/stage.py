"""
Stage (PM16C) device-communication checks — REORGANISATION_PLAN.md Phase 6
(§7 Phase 6).

Moved from validator/pre_validator.py's `_check_stage`,
`_check_xrd_oscillation_stage`, `_check_stage_compound`,
`_check_stage_move_constraints`, `_check_stage_mode_ordering`,
`_check_emergency_stop_confirmation` (plus the now-dissolved
`_detect_stage_mode`, whose Ch8/Ch9 read is absorbed into
`validator.snapshots.collect_stage_snapshot`). All position/mode/is_moving
reads now come from a single `ValidationSnapshot` built once per `validate()`
call — see that module's docstring for the read-sharing/Diagnostic-ownership
rules this file depends on.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...actions import (
    FollowSampleAction,
    FpdOutMicroscopeInAction,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    StageAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
)
from ...device_context import DeviceContext
from ...safety_rules import (
    exceeded_global_limit,
    global_limit_delta_mm,
    global_limits_for_channel,
    validate_ch11_oscillation_settings,
)
from apps.stage_fpd_scope.stage_settings import SETTINGS_FILE as _STAGE_SETTINGS_PATH
from utils.stage import move_constraints
from utils.stage.control_stage import PULSE_SCALE

from . import action_params
from ..execution_trace import ExecutionTrace, format_loop_context
from ..models import Severity, emit_preflight
from ..snapshots import ValidationSnapshot, load_stage_settings_dict

if TYPE_CHECKING:
    from ...scheduler_settings import GlobalLimits, GlobalXrdSettings
    from ..pre_validator import PreCheckResult

_DEVICE = "stage"


def check_stage(
    trace: ExecutionTrace, ctx: DeviceContext, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    stage_actions = [
        e.action for e in trace.flat
        if isinstance(e.action, (
            StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction,
            # start_following / follow_sample_position move Ch4/Ch5 (XY
            # tracking) and Ch3 (autofocus) directly via ctx.controller —
            # they are stage operations even though they're triggered
            # from the camera/follow UI.
            StartFollowingAction, FollowSampleAction,
        ))
    ]
    if not stage_actions:
        return

    if ctx.controller is None:
        emit_preflight(
            r, "preflight.stage.controller_not_connected",
            "Stage controller is not connected (required for stage operations)",
            device=_DEVICE,
        )
        return

    try:
        from utils.stage.control_stage_sim import PM16CControllerSim
        if isinstance(ctx.controller, PM16CControllerSim):
            emit_preflight(
                r, "preflight.stage.simulation_mode",
                "Stage is running in simulation mode (PM16CControllerSim)",
                device=_DEVICE, severity=Severity.WARNING,
            )
    except ImportError:
        pass

    if snapshot.stage is not None and snapshot.stage.is_moving:
        emit_preflight(
            r, "preflight.stage.currently_moving",
            "Stage is currently moving — wait until all axes stop before starting a sequence",
            device=_DEVICE,
        )


def check_xrd_oscillation_stage(
    trace: ExecutionTrace,
    ctx: DeviceContext,
    snapshot: ValidationSnapshot,
    global_xrd: "GlobalXrdSettings | None",
    r: "PreCheckResult",
) -> None:
    """Ch11 oscillation makes an XRD action a stage operation too."""
    oscillating_actions = [
        e.action for e in trace.flat
        if isinstance(e.action, TakeXrdAction) and (
            e.action.oscillate if e.action.oscillate is not None
            else (global_xrd.oscillate if global_xrd is not None else False)
        )
    ]
    if not oscillating_actions:
        return
    if ctx.controller is None:
        emit_preflight(
            r, "preflight.stage.oscillation_controller_not_connected",
            "Stage controller is not connected (required for Ch11 oscillation)",
            device=_DEVICE,
        )
        return
    if snapshot.stage is not None and snapshot.stage.is_moving:
        emit_preflight(
            r, "preflight.stage.oscillation_currently_moving",
            "Stage is currently moving — wait until all axes stop before starting Ch11 oscillation",
            device=_DEVICE,
        )


def check_stage_compound(trace: ExecutionTrace, r: "PreCheckResult") -> None:
    flat_actions = [e.action for e in trace.flat]
    for a in flat_actions:
        if isinstance(a, MicroscopeOutFpdInAction):
            if a.microscope_out_pos is None or a.fpd_in_pos is None:
                _check_stage_settings(
                    r,
                    required_keys=["ch8_out", "det_in"],
                    action_name="microscope_out_and_fpd_in",
                )
                break  # one check is enough even if action appears multiple times

    for a in flat_actions:
        if isinstance(a, FpdOutMicroscopeInAction):
            if a.fpd_out_pos is None or a.microscope_in_pos is None:
                _check_stage_settings(
                    r,
                    required_keys=["det_out", "ch8_in"],
                    action_name="fpd_out_and_microscope_in",
                )
                break


def check_stage_move_constraints(
    actions: list,
    snapshot: ValidationSnapshot,
    r: "PreCheckResult",
    global_xrd: "GlobalXrdSettings | None",
    global_limits: "GlobalLimits | None",
    trace: ExecutionTrace,
) -> None:
    """Simulate every stage move in the sequence (including for-loop
    iterations and microscope/FPD compound-action expansions) starting
    from the snapshot's baseline position, verifying MOVE_CONSTRAINTS
    (Ch8/Ch9 interlock) is never violated at any point, and — for Ch3/4/5
    — that GlobalLimits (SequenceRunner._check_global_limits_before_move)
    would never block the move either.

    Also records the current all-11-channel position onto `r` — the UI
    uses this as the baseline to detect stage moves between Validate and
    Run. Only recorded when all 11 channels were actually readable (a
    partial read has already been reported, channel by channel, by
    `validator.snapshots.collect_stage_snapshot`; simulating on top of an
    incomplete snapshot would be misleading rather than merely incomplete).
    """
    if snapshot.stage is None:
        return  # no controller connected — already reported by check_stage

    positions: dict[int, int] = dict(snapshot.stage.positions)
    if len(positions) < 11:
        return  # incomplete snapshot; already reported per-channel

    r.baseline_positions = dict(positions)
    # SequenceRunner.run() reads this same snapshot into self._baseline_pos
    # right before the sequence starts moving — mirror it here so simulated
    # deltas match what _check_global_limits_before_move will see.
    baseline_345 = {ch: positions[ch] for ch in (3, 4, 5)}

    for msg in move_constraints.list_snapshot_violations(positions):
        emit_preflight(
            r, "preflight.stage.move_constraint_violation_at_baseline",
            f"現在位置: {msg}", device=_DEVICE,
        )

    if not trace.ordered:
        return  # per-step simulation skipped; already reported by check_loop_expansion_limits

    stage_settings = load_stage_settings_dict()

    def _apply(step: StageAction, var_context: dict, label: str, action_path: str, loop_context) -> None:
        if step.operation not in ("move_absolute", "move_relative"):
            return
        if step.ch not in positions:
            return  # invalid channel; already flagged by check_stage_schema
        value = step.value
        if isinstance(value, str):
            value = var_context.get(value)
            if value is None:
                return  # unresolved loop variable; already flagged elsewhere
        try:
            value = int(value)
        except (TypeError, ValueError, OverflowError):
            return  # invalid value; already flagged by check_stage_schema
        if not (-action_params.PM16C_PULSE_MAX <= value <= action_params.PM16C_PULSE_MAX):
            return  # out-of-range value; already flagged by check_stage_schema
        target = value if step.operation == "move_absolute" else positions[step.ch] + value
        lc = format_loop_context(loop_context)
        for msg in move_constraints.list_move_violations(positions, step.ch, target):
            emit_preflight(
                r, "preflight.stage.move_blocked", f"{label}: {msg}",
                device=_DEVICE, action_path=action_path, loop_context=lc,
            )
        if step.ch in (3, 4, 5):
            msg = _violates_global_limits(
                global_limits, step.ch, target, baseline_345[step.ch]
            )
            if msg is not None:
                emit_preflight(
                    r, "preflight.stage.global_limit_exceeded", f"{label}: {msg}",
                    device=_DEVICE, action_path=action_path, loop_context=lc,
                )
        positions[step.ch] = target

    # entry.step mirrors SequenceRunner._flat_index (1-based here to
    # match the "Step N" label shown during an actual run): every leaf
    # action (i.e. everything except ForLoopAction itself) advances the
    # counter once, regardless of action type, so numbers line up with
    # the run log even when non-stage actions are interleaved.
    for entry in trace.ordered:
        a = entry.action
        label = entry.label
        if isinstance(a, (MicroscopeOutFpdInAction, FpdOutMicroscopeInAction)):
            if stage_settings is None:
                continue  # already reported by check_stage_compound
            try:
                steps = a.to_steps(stage_settings)
            except (KeyError, TypeError, ValueError):
                continue  # invalid stage_settings value; already flagged by check_stage_compound
            for step in steps:
                _apply(step, entry.variables, label, entry.action_path, entry.loop_context)
        elif isinstance(a, StageAction):
            _apply(a, entry.variables, label, entry.action_path, entry.loop_context)
        elif isinstance(a, TakeXrdAction):
            oscillate = (
                a.oscillate if a.oscillate is not None
                else (global_xrd.oscillate if global_xrd is not None else False)
            )
            if not oscillate:
                continue
            pos_a_deg = (
                a.osc_pos_a_deg if a.osc_pos_a_deg is not None
                else (global_xrd.osc_pos_a_deg if global_xrd is not None else -5.0)
            )
            pos_b_deg = (
                a.osc_pos_b_deg if a.osc_pos_b_deg is not None
                else (global_xrd.osc_pos_b_deg if global_xrd is not None else 20.0)
            )
            dwell_ms = (
                a.osc_dwell_ms if a.osc_dwell_ms is not None
                else (global_xrd.osc_dwell_ms if global_xrd is not None else 0)
            )
            speed = (
                a.osc_speed if a.osc_speed is not None
                else (global_xrd.osc_speed if global_xrd is not None else "M")
            )
            try:
                targets = validate_ch11_oscillation_settings(
                    pos_a_deg, pos_b_deg, dwell_ms, speed
                )
            except ValueError:
                continue  # check_xrd_params reports the configuration error.
            lc = format_loop_context(entry.loop_context)
            for target in targets:
                for msg in move_constraints.list_move_violations(positions, 11, target):
                    emit_preflight(
                        r, "preflight.stage.move_blocked", f"{label}: {msg}",
                        device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                    )


def check_stage_mode_ordering(
    trace: ExecutionTrace, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    """State-machine scan, in true execution order (trace.ordered), to
    detect camera / XRD ordering violations.

    Tracks two flags across every step:
    - stage_mode: 'microscope' | 'xrd' | 'unknown'
    - follow_active: True between start_following and stop_following

    Errors:
      - camera op while stage_mode == 'xrd'
      - XRD op while stage_mode == 'microscope'
      - microscope_out_and_fpd_in while follow_active

    Warnings:
      - XRD op while stage_mode == 'unknown' (FPD position unverified —
        this includes both "no controller connected" and "Ch8/Ch9 could
        not be read", the latter already reported once by
        collect_stage_snapshot; this warning is a deliberately preserved
        pre-existing double-message case, not a new one — see
        validator/snapshots.py docstring)
    """
    stage_mode = snapshot.stage.stage_mode if snapshot.stage is not None else "unknown"
    follow_active = False

    for entry in trace.ordered:
        a = entry.action
        label = entry.label
        lc = format_loop_context(entry.loop_context)

        if isinstance(a, MicroscopeOutFpdInAction):
            if follow_active:
                emit_preflight(
                    r, "preflight.stage.mode_ordering_follow_still_active",
                    f"{label}: バックグラウンド追従スレッド (start_following) が"
                    "停止していません。microscope_out_and_fpd_in の前に "
                    "stop_following() を呼んでください。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                )
            stage_mode = "xrd"

        elif isinstance(a, FpdOutMicroscopeInAction):
            stage_mode = "microscope"

        elif isinstance(a, StartFollowingAction):
            if stage_mode == "xrd":
                emit_preflight(
                    r, "preflight.stage.mode_ordering_camera_blocked",
                    f"{label}: microscope_out_and_fpd_in の後はカメラ操作を"
                    "実行できません（顕微鏡がサンプル軸上にない）。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                )
            follow_active = True

        elif isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, FollowSampleAction)):
            if stage_mode == "xrd":
                emit_preflight(
                    r, "preflight.stage.mode_ordering_camera_blocked",
                    f"{label}: microscope_out_and_fpd_in の後はカメラ操作を"
                    "実行できません（顕微鏡がサンプル軸上にない）。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                )
            # FollowSampleAction: follow_active unchanged (internally paired)

        elif isinstance(a, StopFollowingAction):
            follow_active = False

        elif isinstance(a, (TakeXrdAction, TakeDarkAction)):
            if stage_mode == "microscope":
                emit_preflight(
                    r, "preflight.stage.mode_ordering_xrd_blocked",
                    f"{label}: FPD がサンプル軸上にないため XRD 測定は"
                    "実行できません。先に microscope_out_and_fpd_in() を呼んでください。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                )
            elif stage_mode == "unknown":
                emit_preflight(
                    r, "preflight.stage.mode_ordering_xrd_mode_unknown",
                    f"{label}: 事前に microscope_out_and_fpd_in() が"
                    "呼ばれていません。FPD がすでに軸上にある場合は問題ありませんが、"
                    "確認してください。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                    severity=Severity.WARNING,
                )


def check_emergency_stop_confirmation(trace: ExecutionTrace, r: "PreCheckResult") -> None:
    """Nudge the author to confirm intent when a normal move follows
    `emergency_stop()`.

    The DSL's `emergency_stop()` (StageAction(operation="emergency_stop"))
    is, unlike the Stop button's request_emergency_stop(), designed to be
    resumable: SequenceRunner._resume_motion_after_self_stop() silently
    re-acquires the motion lease right after it, so the sequence keeps
    going by design. A move immediately following it is therefore not
    wrong — but "emergency stop" reads as "the run ends here" to a human
    author, so this is a soft confirmation, not an error. Only the first
    move after each emergency_stop() is flagged, to avoid repeating the
    same nudge for every subsequent move.
    """
    pending_confirm = False

    for entry in trace.ordered:
        a = entry.action
        if not isinstance(a, StageAction):
            continue
        if a.operation == "emergency_stop":
            pending_confirm = True
        elif pending_confirm and a.operation in ("move_absolute", "move_relative"):
            emit_preflight(
                r, "preflight.stage.emergency_stop_confirmation",
                f"{entry.label}: 直前に emergency_stop() が"
                "呼ばれています。emergency_stop() の後もシーケンスは続行される"
                "設計ですが、意図した動作か確認してください。",
                device=_DEVICE, action_path=entry.action_path,
                loop_context=format_loop_context(entry.loop_context),
                severity=Severity.WARNING,
            )
            pending_confirm = False


# ------------------------------------------------------------------ helpers

def _violates_global_limits(
    global_limits: "GlobalLimits | None", ch: int, target_pos: int, baseline_pos: int
) -> str | None:
    """Evaluate a prospective Ch3/4/5 target position against GlobalLimits
    using the same shared judgment (safety_rules.global_limits_for_channel /
    global_limit_delta_mm / exceeded_global_limit) as
    SequenceRunner._check_global_limits_before_move — so a move the runner
    would refuse to send is caught here instead of aborting mid-sequence."""
    limits = global_limits_for_channel(global_limits, ch)
    if limits is None:
        return None
    minus_mm, plus_mm = limits
    if minus_mm is None and plus_mm is None:
        return None
    delta_mm = global_limit_delta_mm(target_pos, baseline_pos, PULSE_SCALE[ch])
    exceeded = exceeded_global_limit(delta_mm, minus_mm, plus_mm)
    if exceeded == "plus":
        return (
            f"Global limit exceeded: Ch{ch} → {target_pos:+} is "
            f"{delta_mm:+.3f} mm from the validation-time position, "
            f"beyond the +{plus_mm:.3f} mm limit"
        )
    if exceeded == "minus":
        return (
            f"Global limit exceeded: Ch{ch} → {target_pos:+} is "
            f"{delta_mm:+.3f} mm from the validation-time position, "
            f"beyond the -{minus_mm:.3f} mm limit"
        )
    return None


def _check_stage_settings(
    r: "PreCheckResult", required_keys: list[str], action_name: str
) -> None:
    if not _STAGE_SETTINGS_PATH.exists():
        emit_preflight(
            r, "preflight.stage.settings_file_not_found",
            f"{action_name}: stage_settings.json not found at {_STAGE_SETTINGS_PATH}",
            device=_DEVICE,
        )
        return
    try:
        settings = json.loads(_STAGE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        emit_preflight(
            r, "preflight.stage.settings_parse_error",
            f"{action_name}: failed to parse stage_settings.json — {exc}",
            device=_DEVICE,
        )
        return
    for key in required_keys:
        if key not in settings:
            emit_preflight(
                r, "preflight.stage.settings_missing_key",
                f"{action_name}: stage_settings.json is missing key {key!r} "
                f"(required when position is not specified explicitly)",
                device=_DEVICE,
            )
            continue
        # Not an Action-level STATIC check (this validates a config file
        # entry, not an Action field) — uses the pure parser directly, same
        # as Global limits validation in pre_validator.py.
        _val, err = action_params.parse_stage_position(settings[key])
        if err is not None:
            emit_preflight(
                r, "preflight.stage.settings_invalid_position",
                f"{action_name}: stage_settings.json[{key!r}]: {err}",
                device=_DEVICE,
            )
