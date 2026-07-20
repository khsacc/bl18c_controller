"""
LakeShore 335 device-communication checks — REORGANISATION_PLAN.md Phase 6
(§7 Phase 6).

Moved from validator/pre_validator.py's `_check_lakeshore`,
`_check_lakeshore_sequence`, `_try_resolve_float`. `setpoint`/`heater_range`/
`has_data` now come from `snapshot.lakeshore`, collected once by
`validator.snapshots.collect_lakeshore_snapshot` — `get_setpoint()` used to
be called independently by both checks in this file; it is now read once
and shared.
"""
from __future__ import annotations

import math
from typing import Mapping, TYPE_CHECKING

from ...actions import (
    AllHeatersOffAction,
    FollowSampleAction,
    SetHeaterAction,
    SetTemperatureAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeXrdAction,
    WaitAction,
    WaitTemperatureAction,
)
from ..execution_trace import ExecutionTrace
from ..models import Severity, emit_preflight
from ..snapshots import ValidationSnapshot

if TYPE_CHECKING:
    from ..pre_validator import PreCheckResult

_DEVICE = "lakeshore"


def check_lakeshore(
    trace: ExecutionTrace, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    ls_actions = [
        e.action for e in trace.flat
        if isinstance(
            e.action,
            (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction),
        )
    ]
    if not ls_actions:
        return

    if snapshot.lakeshore is None or not snapshot.lakeshore.connected:
        emit_preflight(
            r, "preflight.lakeshore.controller_not_connected",
            "LakeShore 335 is not connected (required for temperature operations)",
            device=_DEVICE,
        )
        return

    # setpoint read failure (if any) was already reported once by
    # collect_lakeshore_snapshot — nothing further to check here.

    if any(isinstance(a, WaitTemperatureAction) for a in ls_actions):
        if snapshot.lakeshore.has_data is False:
            emit_preflight(
                r, "preflight.lakeshore.no_data_yet",
                "LakeShore has not produced any readings yet — "
                "wait_temperature may hang until the first reading arrives",
                device=_DEVICE, severity=Severity.WARNING,
            )
            # has_data is None (not attempted, or the read itself raised) ->
            # stay silent, matching the pre-Phase-6 silent-exception-swallow.


def check_lakeshore_sequence(
    trace: ExecutionTrace, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    """Single forward pass over the LakeShore-335-related command stream
    in execution order (trace.ordered — ForLoopAction bodies expanded
    per iteration), tracking the running setpoint / heater state at each
    step so that ordering and ramp-rate heuristic checks can all be
    evaluated together — analogous to how stage positions are simulated
    across every step in `checks.stage.check_stage_move_constraints`.

    Field-level validity (ramp_rate/value_k/tol_k/range_index) is not
    checked here — that's `action_params.check_lakeshore_params`. This
    function only needs the *resolved number* for its own heuristics,
    obtained via the non-error-emitting `_try_resolve_float`.
    """
    flat_actions = [e.action for e in trace.flat]
    if not any(
        isinstance(
            a,
            (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction),
        )
        for a in flat_actions
    ):
        return

    initial_setpoint: float | None = None
    initial_heater_on: bool | None = None
    if snapshot.lakeshore is not None and snapshot.lakeshore.connected:
        initial_setpoint = snapshot.lakeshore.setpoint
        heater_range = snapshot.lakeshore.heater_range
        if heater_range is None:
            return  # heater_range read failed; already reported by collect_lakeshore_snapshot
        initial_heater_on = heater_range != 0
    # else: not connected — already reported by check_lakeshore; the scan
    # below still runs (matching pre-Phase-6 behaviour) since most of its
    # checks (ordering / duplicate-setpoint / follow-pairing heuristics)
    # are independent of whether a real device is attached.

    ordered = trace.ordered

    current_setpoint = initial_setpoint
    heater_on = initial_heater_on
    seen_set_temp_ever = False
    heater_turned_on_before_first_set = False
    all_heaters_off_pending = False
    wait_temp_since_last_set = True
    since_set_has_wait_temp = False
    since_set_has_follow = False
    follow_open = False
    prev_was_wait_temp = False

    for i, entry in enumerate(ordered):
        a = entry.action
        vc = entry.variables
        label = entry.label

        if prev_was_wait_temp and isinstance(a, (FollowSampleAction, StartFollowingAction)):
            emit_preflight(
                r, "preflight.lakeshore.follow_right_after_wait_temperature",
                f"{label}: 直前の wait_temperature の直後に追従を開始しようとしています。"
                "wait_temperature の間に温度が変化しているため試料位置がずれている可能性が"
                "あります。set_temperature → start_following → wait_temperature の順に"
                "してください。",
                device=_DEVICE,
            )
        prev_was_wait_temp = isinstance(a, WaitTemperatureAction)

        if isinstance(a, SetTemperatureAction):
            ramp_rate = _try_resolve_float(a.ramp_rate, vc)
            val = _try_resolve_float(a.value_k, vc)

            if all_heaters_off_pending:
                emit_preflight(
                    r, "preflight.lakeshore.set_temperature_after_all_heaters_off",
                    f"{label}: 直前に all_heaters_off が実行されており、ヒーターOFFの状態の"
                    "まま温度設定を変更しようとしています。",
                    device=_DEVICE,
                )

            if not seen_set_temp_ever:
                if initial_heater_on is False and not heater_turned_on_before_first_set:
                    emit_preflight(
                        r, "preflight.lakeshore.heater_off_before_first_set",
                        f"{label}: 現在ヒーター出力がOFFです。最初の set_temperature より"
                        "前に set_heater でヒーター出力を入れていないため、温度制御が"
                        "できない可能性があります。",
                        device=_DEVICE, severity=Severity.WARNING,
                    )
            elif not wait_temp_since_last_set:
                emit_preflight(
                    r, "preflight.lakeshore.set_temperature_without_wait_between",
                    f"{label}: 直前の set_temperature との間に wait_temperature がないまま、"
                    "続けて set_temperature が実行されています。",
                    device=_DEVICE, severity=Severity.WARNING,
                )

            if val is not None and current_setpoint is not None:
                diff = val - current_setpoint
                if diff == 0:
                    emit_preflight(
                        r, "preflight.lakeshore.setpoint_unchanged",
                        f"{label}: 設定値が直前の setpoint ({current_setpoint} K) から"
                        "変化していません。意味のない温度設定コマンドです。",
                        device=_DEVICE, severity=Severity.WARNING,
                    )
                elif diff < 0 and ramp_rate is not None and ramp_rate >= 5:
                    emit_preflight(
                        r, "preflight.lakeshore.cooling_rate_may_be_slower",
                        f"{label}: 冷却方向 ({current_setpoint} → {val} K) で "
                        f"rate={ramp_rate} K/min（5 K/min以上）のため、実際の冷却速度が"
                        "設定より遅くなる可能性があります。",
                        device=_DEVICE, severity=Severity.WARNING,
                    )
                elif diff > 0 and ramp_rate is not None and ramp_rate >= 10:
                    emit_preflight(
                        r, "preflight.lakeshore.heating_rate_may_be_slower",
                        f"{label}: 加熱方向 ({current_setpoint} → {val} K) で "
                        f"rate={ramp_rate} K/min（10 K/min以上）のため、実際の加熱速度が"
                        "設定より遅くなる可能性があります。",
                        device=_DEVICE, severity=Severity.WARNING,
                    )

            # SetTemperature -> wait() [not wait_temperature] -> ... (until next set_temperature)
            if i + 1 < len(ordered) and isinstance(ordered[i + 1].action, WaitAction):
                wait_action = ordered[i + 1].action
                has_wait_temp = False
                for j in range(i + 2, len(ordered)):
                    nxt = ordered[j].action
                    if isinstance(nxt, SetTemperatureAction):
                        break
                    if isinstance(nxt, WaitTemperatureAction):
                        has_wait_temp = True
                        break
                if (
                    not has_wait_temp
                    and val is not None
                    and current_setpoint is not None
                    and ramp_rate is not None
                    and ramp_rate > 0
                ):
                    estimate_s = abs(val - current_setpoint) / ramp_rate * 60.0
                    if wait_action.duration_s < estimate_s:
                        emit_preflight(
                            r, "preflight.lakeshore.wait_shorter_than_ramp_estimate",
                            f"{label}: 直後の wait() の待機時間 "
                            f"({wait_action.duration_s:.0f} s) が、rate={ramp_rate} K/min "
                            f"での概算所要時間（約{estimate_s:.0f} s）より短く、"
                            "wait_temperature もないため、設定温度への到達前に次の動作へ"
                            "進む可能性があります。",
                            device=_DEVICE, severity=Severity.WARNING,
                        )

            if val is not None:
                current_setpoint = val
            seen_set_temp_ever = True
            wait_temp_since_last_set = False
            since_set_has_wait_temp = False
            since_set_has_follow = follow_open
            continue

        if isinstance(a, WaitTemperatureAction):
            tol_k = _try_resolve_float(a.tol_k, vc)
            if tol_k is not None and tol_k < 0.01:
                emit_preflight(
                    r, "preflight.lakeshore.tol_too_small",
                    f"{label}: tol ({tol_k} K) が小さすぎます — "
                    "収束に時間がかかる、または到達しない可能性があります。",
                    device=_DEVICE, severity=Severity.WARNING,
                )

            if not seen_set_temp_ever:
                emit_preflight(
                    r, "preflight.lakeshore.wait_temperature_without_preceding_set",
                    f"{label}: これより前に set_temperature が実行されていません。",
                    device=_DEVICE, severity=Severity.WARNING,
                )
            if heater_on is False:
                emit_preflight(
                    r, "preflight.lakeshore.wait_temperature_with_heater_off",
                    f"{label}: ヒーターがOFFのまま wait_temperature を実行しています。"
                    "設定温度に到達しない可能性が高いです。",
                    device=_DEVICE, severity=Severity.WARNING,
                )
            wait_temp_since_last_set = True
            since_set_has_wait_temp = True
            continue

        if isinstance(a, SetHeaterAction):
            if a.range_index in (0, 1, 2, 3):
                is_on = a.range_index != 0
                if is_on:
                    if not seen_set_temp_ever:
                        heater_turned_on_before_first_set = True
                    all_heaters_off_pending = False
                heater_on = is_on
            # else: invalid range_index — already flagged by
            # action_params.check_lakeshore_params; state tracking is
            # skipped for it, same as before Phase 5.
            continue

        if isinstance(a, AllHeatersOffAction):
            heater_on = False
            all_heaters_off_pending = True
            continue

        if isinstance(a, FollowSampleAction):
            since_set_has_follow = True
            continue

        if isinstance(a, StartFollowingAction):
            follow_open = True
            continue

        if isinstance(a, StopFollowingAction):
            if follow_open:
                since_set_has_follow = True
            follow_open = False
            continue

        if isinstance(a, TakeXrdAction) and seen_set_temp_ever:
            if not since_set_has_wait_temp:
                emit_preflight(
                    r, "preflight.lakeshore.xrd_without_wait_temperature",
                    f"{label}: 直前の set_temperature の後に wait_temperature がないため、"
                    "試料の温度が安定化していない可能性があります。",
                    device=_DEVICE, severity=Severity.WARNING,
                )
            if not (since_set_has_follow or follow_open):
                emit_preflight(
                    r, "preflight.lakeshore.xrd_without_follow",
                    f"{label}: 直前の set_temperature の後に follow_sample_position、"
                    "または start_following + stop_following のペアがないため、"
                    "試料位置がずれている可能性があります。",
                    device=_DEVICE, severity=Severity.WARNING,
                )


def _try_resolve_float(value, variables: Mapping[str, object]) -> float | None:
    """Resolve `value` (a literal, or a loop-variable *name* to look up in
    `variables`) to a float, returning None on any failure — never appends a
    Diagnostic. Used by `check_lakeshore_sequence`'s own cooling/heating-rate
    heuristics, which need a *resolved number* but must not re-validate
    `ramp_rate`/`value_k` themselves — that's
    `action_params.check_lakeshore_params`'s job."""
    v = value
    if isinstance(v, str):
        v = variables.get(v)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f
