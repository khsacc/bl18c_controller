"""
PACE5000 device-communication checks — REORGANISATION_PLAN.md Phase 6
(§7 Phase 6).

Moved from validator/pre_validator.py's `_check_pace5000`,
`_check_pace5000_control_mode`, `_check_pace5000_adjacency`,
`_check_pace5000_ordering`, `_check_pace5000_wait_duration`,
`_check_pace5000_source_pressure`. Every physical PACE5000 read (unit,
output_state, target/source pressure) now comes from `snapshot.pace`,
collected once by `validator.snapshots.collect_pace_snapshot` — in
particular, this file never calls `Pace5000Backend.write()` (the pre-Phase-6
`write(":UNIT:PRES MPA")` calls violated the read-only preflight invariant,
§3.2/§15.1 #7); the unit is read via the non-mutating `query(":UNIT:PRES?")`
instead, fail-closed.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ...actions import (
    Action,
    SetControlModeAction,
    SetPressureAction,
    SetAndWaitPressureAction,
    CompressToTargetPressureUsingRubyAction,
    WaitAction,
    WaitPressureAction,
)
from ..execution_trace import ExecutionTrace, format_loop_context, walk_raw
from ..models import Severity, emit_preflight
from ..snapshots import SnapshotRequirements, ValidationSnapshot

from . import action_params
from ... import compression
from apps.PACE5000.pace5000_backend import MIN_SLEW_RATE_MPA_PER_SEC

if TYPE_CHECKING:
    from ..pre_validator import PreCheckResult

_DEVICE = "pace5000"
_PACE_TO_MPA = action_params.PACE_TO_MPA
_PACE_VALID_RATE_UNITS = action_params.PACE_VALID_RATE_UNITS


def check_pace5000(
    trace: ExecutionTrace,
    snapshot: ValidationSnapshot,
    requirements: SnapshotRequirements,
    r: "PreCheckResult",
) -> None:
    if not requirements.pace_used:
        return

    if snapshot.pace is None or not snapshot.pace.connected:
        emit_preflight(
            r, "preflight.pace5000.controller_not_connected",
            "PACE5000 is not connected (required for pressure operations)",
            device=_DEVICE,
        )
        return

    # Validation: find max set pressure across the whole sequence and
    # compare against the current +ve source pressure. Needs the true
    # per-iteration unroll (a loop-variable pressure sweep's max is not
    # visible in the static `flat` projection), so it only runs once
    # trace.ordered is populated (within_limits).
    if trace.ordered:
        check_pace5000_source_pressure(requirements, snapshot, r)


def check_compression_sequence(
    trace: ExecutionTrace, snapshot: ValidationSnapshot, r: "PreCheckResult",
) -> None:
    """Check the PACE state *at each* compression, including preceding actions.

    The source-pressure ceiling is checked separately against every maximum.
    This read-only pass models setpoints, slew rates and Control mode, while
    leaving actual post-compression pressure unknown because ruby feedback is
    dynamic. Runtime rechecks the live device before and after every ramp.
    """
    if not any(isinstance(e.action, CompressToTargetPressureUsingRubyAction)
               for e in trace.ordered):
        return
    pace = snapshot.pace
    if pace is None or not pace.connected or pace.unit is None:
        return  # snapshot collection already reported the device/unit error
    initial_state = pace.output_state.strip().upper() if pace.output_state else None
    if initial_state not in ("1", "ON", "0", "OFF"):
        emit_preflight(
            r, "preflight.pace5000.compression_mode_unreadable",
            "Cannot establish PACE5000 Control/Measure mode for compression",
            device=_DEVICE,
        )
        return
    controlled = initial_state in ("1", "ON")
    target_mpa = pace.target_pressure_mpa
    rate_mpa_per_min = pace.slew_rate_mpa_per_min
    measured_mpa = pace.measured_pressure_mpa
    arrival_confirmed = False
    dynamic_target = False
    preceding_set_without_arrival = False

    for entry in trace.ordered:
        action = entry.action
        path = entry.action_path
        lc = format_loop_context(entry.loop_context)
        if isinstance(action, SetControlModeAction):
            if action.enabled and not controlled:
                arrival_confirmed = False
            controlled = action.enabled
            continue
        if isinstance(action, (SetPressureAction, SetAndWaitPressureAction)):
            dynamic_target = False
            pressure = action.pressure
            if isinstance(pressure, str):
                pressure = entry.variables.get(pressure)
            try:
                candidate = float(pressure) * _PACE_TO_MPA[action.unit]
                rate = float(action.rate) * compression.RATE_TO_MPA_PER_MIN[action.rate_unit]
            except (KeyError, TypeError, ValueError, OverflowError):
                target_mpa = rate_mpa_per_min = None
            else:
                target_mpa = candidate if math.isfinite(candidate) else None
                rate_mpa_per_min = rate if math.isfinite(rate) else None
            measured_mpa = None
            arrival_confirmed = isinstance(action, SetAndWaitPressureAction)
            preceding_set_without_arrival = not arrival_confirmed
            continue
        if isinstance(action, WaitPressureAction):
            arrival_confirmed = True
            preceding_set_without_arrival = False
            continue
        if not isinstance(action, CompressToTargetPressureUsingRubyAction):
            continue

        if not controlled:
            emit_preflight(
                r, "preflight.pace5000.compression_control_mode_required",
                "PACE5000 must be in Control mode when compression starts",
                device=_DEVICE, action_path=path, loop_context=lc,
            )
        if compression.parameters_error(action):
            continue  # static check owns invalid fields
        maximum_mpa = (action.max_allowed_gas_pressure
                       * compression.UNIT_TO_MPA[action.gas_pressure_unit])
        tolerance_mpa = (action.gas_pressure_tolerance
                         * compression.UNIT_TO_MPA[action.gas_pressure_unit])
        if (pace.positive_source_pressure_mpa is not None
                and maximum_mpa >= pace.positive_source_pressure_mpa):
            emit_preflight(
                r, "preflight.pace5000.compression_source_headroom_missing",
                "Maximum allowed gas pressure must be below the current +ve source pressure",
                device=_DEVICE, action_path=path, loop_context=lc,
            )
        if target_mpa is None and dynamic_target:
            emit_preflight(
                r, "preflight.pace5000.compression_dynamic_setpoint",
                "A preceding ruby-guided compression has an unknown final gas setpoint; "
                "the next compression checks its live setpoint against the limit at runtime",
                device=_DEVICE, action_path=path, loop_context=lc,
                severity=Severity.WARNING,
            )
        elif target_mpa is None:
            emit_preflight(
                r, "preflight.pace5000.compression_setpoint_unreadable",
                "Cannot determine the PACE5000 setpoint at compression start",
                device=_DEVICE, action_path=path, loop_context=lc,
            )
        elif target_mpa < 0 or target_mpa > maximum_mpa + 1e-9:
            emit_preflight(
                r, "preflight.pace5000.compression_above_maximum",
                "PACE5000 setpoint at compression start is negative or exceeds its maximum allowed gas pressure",
                device=_DEVICE, action_path=path, loop_context=lc,
            )
        elif ((maximum_mpa - target_mpa) /
              (action.gas_pressure_increment
               * compression.UNIT_TO_MPA[action.gas_pressure_unit])) > 1000:
            emit_preflight(
                r, "preflight.pace5000.compression_many_increments",
                "More than 1000 pressure increments and ruby measurements may be needed "
                "to reach the gas-pressure limit; review the increment and stabilisation duration",
                device=_DEVICE, action_path=path, loop_context=lc,
                severity=Severity.WARNING,
            )
        if not arrival_confirmed:
            if measured_mpa is None:
                emit_preflight(
                    r, ("preflight.pace5000.compression_waits_for_prior_set"
                        if preceding_set_without_arrival else
                        "preflight.pace5000.compression_pressure_unreadable"),
                    ("A preceding set_pressure has not confirmed arrival; compression will wait "
                     "for its setpoint before the first ruby measurement"
                     if preceding_set_without_arrival else
                     "Cannot read the initial measured gas pressure before ruby-guided compression"),
                    device=_DEVICE, action_path=path, loop_context=lc,
                    severity=(Severity.WARNING if preceding_set_without_arrival
                              else Severity.ERROR),
                )
            elif target_mpa is not None and abs(measured_mpa - target_mpa) > tolerance_mpa:
                emit_preflight(
                    r, "preflight.pace5000.compression_not_at_setpoint",
                    "PACE5000 measured pressure is outside the arrival band; compression will "
                    "wait for its setpoint before the first ruby measurement",
                    device=_DEVICE, action_path=path, loop_context=lc,
                    severity=Severity.WARNING,
                )
            if measured_mpa is not None and measured_mpa > maximum_mpa + 1e-5:
                emit_preflight(
                    r, "preflight.pace5000.compression_measured_above_maximum",
                    "PACE5000 measured gas pressure already exceeds the maximum allowed gas pressure",
                    device=_DEVICE, action_path=path, loop_context=lc,
                )
        effective_rate = (action.gas_pressure_rate
                          * compression.RATE_TO_MPA_PER_MIN[action.gas_pressure_rate_unit]
                          if action.gas_pressure_rate is not None else rate_mpa_per_min)
        if effective_rate is None or not math.isfinite(effective_rate):
            emit_preflight(
                r, "preflight.pace5000.compression_slew_unreadable",
                "Cannot determine the PACE5000 slew rate inherited by compression",
                device=_DEVICE, action_path=path, loop_context=lc,
            )
        elif effective_rate / 60 < MIN_SLEW_RATE_MPA_PER_SEC:
            emit_preflight(
                r, "preflight.pace5000.compression_slew_below_minimum",
                "PACE5000 slew rate is below its hardware minimum for compression",
                device=_DEVICE, action_path=path, loop_context=lc,
            )
        # Ruby feedback can finish at any setpoint up to the configured cap.
        # A later compression requires a fresh live check at runtime.
        target_mpa = measured_mpa = None
        dynamic_target = True
        rate_mpa_per_min = effective_rate
        arrival_confirmed = True
        preceding_set_without_arrival = False


def check_pace5000_control_mode(
    snapshot: ValidationSnapshot, r: "PreCheckResult", trace: ExecutionTrace
) -> None:
    """Detect sequences that set/wait on pressure while the PACE5000 is
    still in Measure mode (Pressure Control : OFF), so the commands
    would silently have no effect.

    Step 1: pressure ops exist but set_control_mode is never called.
    Step 2: set_control_mode is called, but the run-up to the first
    enabling call doesn't match one of the two orderings that guarantee
    the setpoint is actually applied:
      (1) set_pressure → set_control_mode(True) → wait_pressure
      (2) set_control_mode(True) → set_pressure → wait_pressure
    This catches both more than one set_pressure before the first
    enabling call (ambiguous which setpoint applies), and a
    wait_pressure that starts before Control Mode is ever enabled
    (e.g. set_pressure → wait_pressure → set_control_mode(True)) —
    the wait may never converge since the setpoint change had no
    effect while still in Measure mode.
    """
    pace_related = [
        e.action for e in trace.pace_primitives()
        if isinstance(e.action, (SetPressureAction, WaitPressureAction, SetControlModeAction,
                                 CompressToTargetPressureUsingRubyAction))
    ]
    if not any(isinstance(a, (SetPressureAction, WaitPressureAction,
                              CompressToTargetPressureUsingRubyAction)) for a in pace_related):
        return

    if snapshot.pace is None or not snapshot.pace.connected:
        return  # already reported by check_pace5000

    output_state = snapshot.pace.output_state
    if output_state is None:
        return  # already reported by collect_pace_snapshot (output_state_unreadable)
    if output_state.strip() in ("1", "ON"):
        return  # already in Control mode

    msg = (
        "圧力を変更するコマンドが送信されますが、Control ModeがMeasureのままのため、"
        "実際には圧力が変化しません。"
    )

    if not any(isinstance(a, SetControlModeAction) for a in pace_related):
        emit_preflight(r, "preflight.pace5000.control_mode_measure", msg, device=_DEVICE)
        return

    state = {"count": 0, "controlled": False, "violation": False}

    def _check2(a: Action) -> None:
        if state["controlled"] or state["violation"]:
            return
        if isinstance(a, SetPressureAction):
            state["count"] += 1
            if state["count"] > 1:
                state["violation"] = True
        elif isinstance(a, WaitPressureAction):
            # A wait_pressure reached before Control Mode was ever
            # enabled means the preceding set_pressure had no effect —
            # only set_pressure -> set_control_mode(True) -> wait_pressure
            # and set_control_mode(True) -> set_pressure -> wait_pressure
            # are valid, and both enable Control Mode before any wait.
            if state["count"] >= 1:
                state["violation"] = True
        elif isinstance(a, CompressToTargetPressureUsingRubyAction):
            state["violation"] = True
        elif isinstance(a, SetControlModeAction) and a.enabled:
            state["controlled"] = True

    for a in pace_related:
        _check2(a)
    if state["violation"]:
        emit_preflight(r, "preflight.pace5000.control_mode_measure", msg, device=_DEVICE)


def check_pace5000_adjacency(actions: list, r: "PreCheckResult") -> None:
    """Warn when a set_pressure is not immediately followed by a wait,
    since the sequence will keep going before the setpoint is reached.

    Gated by `_run_structural` (depth_safe) — `walk_raw` is plain recursion
    over raw ForLoopAction bodies (visited once, not per value), so it is
    safe whenever nesting depth alone is within the limit."""
    for a, _path, siblings, i, _loop_values in walk_raw(actions):
        if not isinstance(a, SetPressureAction):
            continue
        nxt = siblings[i + 1] if i + 1 < len(siblings) else None
        if not isinstance(nxt, (WaitAction, WaitPressureAction)):
            emit_preflight(
                r, "preflight.pace5000.set_pressure_not_followed_by_wait",
                f"{a.describe()}: 圧力変更後、設定圧力に到達するのを待たずに"
                "次の動作が始まります。問題ないか確認してください。",
                device=_DEVICE, severity=Severity.WARNING,
            )


def check_pace5000_ordering(trace: ExecutionTrace, r: "PreCheckResult") -> None:
    """Error when wait_pressure appears with no preceding set_pressure;
    warn when consecutive set_pressure calls have no wait_pressure
    between them."""
    state = {"seen_set_pressure": False, "wait_since_last_set": True}

    for entry in trace.pace_primitives():
        a = entry.action
        lc = format_loop_context(entry.loop_context)
        if isinstance(a, SetPressureAction):
            if state["seen_set_pressure"] and not state["wait_since_last_set"]:
                emit_preflight(
                    r, "preflight.pace5000.set_pressure_without_wait_between",
                    f"{a.describe()}: 直前の set_pressure との間に wait_pressure が"
                    "ないまま、続けて set_pressure が実行されています。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                    severity=Severity.WARNING,
                )
            state["seen_set_pressure"] = True
            state["wait_since_last_set"] = False
        elif isinstance(a, WaitPressureAction):
            if not state["seen_set_pressure"]:
                emit_preflight(
                    r, "preflight.pace5000.wait_pressure_without_preceding_set",
                    f"{a.describe()}: 直前に set_pressure が実行されていません。",
                    device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                )
            state["wait_since_last_set"] = True


def check_pace5000_wait_duration(
    trace: ExecutionTrace, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    """Warn when a generic wait() (not wait_pressure) immediately follows
    set_pressure and its duration is shorter than the time the pressure
    change should take at the given slew rate — analogous to the
    set_temperature -> wait() ramp-time check in checks.lakeshore.

    Tracks a running "current pressure" across the sequence in execution
    order (trace.ordered — ForLoopAction expanded), seeded from
    `snapshot.pace.target_pressure_mpa` (the device's actual current target
    pressure, already read once and converted to MPa) so the very first
    set_pressure's estimate is meaningful too.
    """
    ordered = trace.ordered
    if not any(isinstance(e.action, SetPressureAction) for e in ordered):
        return

    current_pressure_mpa: float | None = (
        snapshot.pace.target_pressure_mpa if snapshot.pace is not None else None
    )

    for i, entry in enumerate(ordered):
        a = entry.action
        if not isinstance(a, SetPressureAction):
            continue
        label = entry.label
        lc = format_loop_context(entry.loop_context)

        pressure = a.pressure
        if isinstance(pressure, str):
            pressure = entry.variables.get(pressure)
        try:
            target_mpa = (
                float(pressure) * _PACE_TO_MPA.get(a.unit, 1.0)
                if pressure is not None else None
            )
        except (TypeError, ValueError, OverflowError):
            target_mpa = None  # invalid literal; already flagged by check_pace5000_params

        rate_mpa_per_sec: float | None = None
        try:
            rate = float(a.rate)
        except (TypeError, ValueError, OverflowError):
            rate = None
        if rate is not None and rate > 0 and a.rate_unit in _PACE_VALID_RATE_UNITS:
            rate_mpa_per_sec = action_params.pace_rate_to_mpa_per_sec(rate, a.rate_unit)

        if i + 1 < len(ordered) and isinstance(ordered[i + 1].action, WaitAction):
            wait_action = ordered[i + 1].action
            has_wait_pressure = False
            for j in range(i + 2, len(ordered)):
                nxt = ordered[j].action
                if isinstance(nxt, SetPressureAction):
                    break
                if isinstance(nxt, WaitPressureAction):
                    has_wait_pressure = True
                    break
            if (
                not has_wait_pressure
                and target_mpa is not None
                and current_pressure_mpa is not None
                and rate_mpa_per_sec is not None
            ):
                estimate_s = abs(target_mpa - current_pressure_mpa) / rate_mpa_per_sec
                if wait_action.duration_s < estimate_s:
                    emit_preflight(
                        r, "preflight.pace5000.wait_shorter_than_ramp_estimate",
                        f"{label}: 直後の wait() の待機時間 "
                        f"({wait_action.duration_s:.0f} s) が、rate={a.rate} {a.rate_unit} "
                        f"での概算所要時間（約{estimate_s:.0f} s）より短く、"
                        "wait_pressure もないため、設定圧力への到達前に次の動作へ"
                        "進む可能性があります。",
                        device=_DEVICE, action_path=entry.action_path, loop_context=lc,
                        severity=Severity.WARNING,
                    )

        if target_mpa is not None:
            current_pressure_mpa = target_mpa


def check_pace5000_source_pressure(
    requirements: SnapshotRequirements, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    """Error if the maximum set pressure in the sequence exceeds the current
    +ve source pressure. Both values are already resolved (never
    recomputed here): `requirements.pace_max_set_pressure_mpa` is the same
    computation `determine_requirements` used to decide whether to read
    source pressure at all; `snapshot.pace.positive_source_pressure_mpa` is
    the one physical read `collect_pace_snapshot` already performed."""
    max_mpa = requirements.pace_max_set_pressure_mpa
    if max_mpa is None:
        return
    if snapshot.pace is None:
        return
    pos_source = snapshot.pace.positive_source_pressure_mpa
    if pos_source is None:
        return  # already reported (not connected / unit unreadable / read failure) upstream
    if max_mpa > pos_source:
        emit_preflight(
            r, "preflight.pace5000.source_pressure_insufficient",
            f"現状のSource Pressure ({pos_source:.4g} MPa) が"
            f"シーケンス中の最大設定圧力 ({max_mpa:.4g} MPa) を下回っているため、"
            "Source Pressureを上げてから再度validateしてください。",
            device=_DEVICE,
        )
