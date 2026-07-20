"""
Read-only device snapshot — REORGANISATION_PLAN.md Phase 6 (§7 Phase 6).

Before Phase 6, `validator/pre_validator.py`'s device-communication checks
each independently re-read the same physical values (Stage Ch8/Ch9 position
read twice for move-constraints vs. stage-mode; LakeShore setpoint read
twice; PACE5000 target/source pressure each preceded by their own
`write(":UNIT:PRES MPA")` — a read-only-invariant violation, §3.2/§15.1 #7).

This module is the single place that:

- decides, from an `ExecutionTrace` (+ `GlobalXrdSettings`), exactly which
  physical fields are needed at all (`determine_requirements` /
  `SnapshotRequirements` — each field mirrors a real, previously
  independently-hand-rolled gate condition in `pre_validator.py`, not a
  blanket "device is used somewhere" check: e.g. Stage `is_moving` is only
  read when a Stage action or an effective-`oscillate=True` `take_xrd` is
  present, never just because the controller happens to be connected);
- reads every required field exactly once per `validate()` call
  (`collect_snapshot` / the per-device `collect_*_snapshot` functions);
- owns the *one* Diagnostic emitted per failed physical read. A checker in
  `validator/checks/*.py` that depends on a `None` snapshot field must skip
  silently rather than emit its own duplicate error/warning (one documented
  exception: `StageSnapshot.stage_mode` collapses "Ch8/Ch9 unreadable" and
  "readable but no preset match" into the same `"unknown"` value, which
  predates Phase 6 and is preserved as-is — see `checks/stage.py`).

PACE5000: this module never calls `Pace5000Backend.write()` — pressure unit
is *read* via the non-mutating `query(":UNIT:PRES?")` and converted with
`PRESSURE_UNIT_TO_MPA`, fail-closed (an unrecognized/unreadable unit response
makes `unit` None, which in turn suppresses the target/source pressure reads
that would otherwise need it — see `collect_pace_snapshot`).
"""
from __future__ import annotations

import json
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Mapping

from ..actions import (
    Action,
    AllHeatersOffAction,
    FollowSampleAction,
    FpdOutMicroscopeInAction,
    MicroscopeOutFpdInAction,
    SetAndWaitPressureAction,
    SetControlModeAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitPressureAction,
    WaitTemperatureAction,
)
from ..device_context import DeviceContext
from apps.stage_fpd_scope.stage_settings import SETTINGS_FILE as _STAGE_SETTINGS_PATH

from .checks import action_params
from .execution_trace import ExecutionTrace, TraceEntry
from .models import emit_preflight

if TYPE_CHECKING:
    from ..scheduler_settings import GlobalXrdSettings
    from .pre_validator import PreCheckResult

_PACE_TO_MPA = action_params.PACE_TO_MPA

# PM16C position tolerance (pulses) used to match Ch8/Ch9 against the
# stage_settings.json presets — same value as the pre-Phase-6 _detect_stage_mode.
_STAGE_MODE_TOLERANCE = 2000


# ------------------------------------------------------------------ snapshot dataclasses

@dataclass(frozen=True)
class StageSnapshot:
    positions: Mapping[int, int]   # only the channels that were readable
    is_moving: bool | None         # None: not required this run, or read failed
    stage_mode: str                # 'microscope' | 'xrd' | 'unknown'


@dataclass(frozen=True)
class PaceSnapshot:
    connected: bool
    output_state: str | None                    # normalized; None if unread/unreadable
    unit: str | None                             # "MPa" | "Bar" | None (fail-closed)
    target_pressure_mpa: float | None            # soft use — no Diagnostic on failure
    positive_source_pressure_mpa: float | None   # hard-safety use — Diagnostic on failure


@dataclass(frozen=True)
class LakeShoreSnapshot:
    connected: bool
    setpoint: float | None
    heater_range: int | None
    has_data: bool | None   # True=has readings / False=empty (warn) / None=not attempted or read exception


@dataclass(frozen=True)
class RadiconSnapshot:
    available: bool   # ctx.radicon is not None — no liveness getter exists on RadiconBackend


@dataclass(frozen=True)
class ValidationSnapshot:
    stage: StageSnapshot | None = None
    pace: PaceSnapshot | None = None
    lakeshore: LakeShoreSnapshot | None = None
    radicon: RadiconSnapshot | None = None
    collected_at: datetime | None = None


@dataclass(frozen=True)
class SnapshotRequirements:
    stage_moving: bool
    pace_used: bool
    pace_output_state: bool
    pace_target: bool
    pace_max_set_pressure_mpa: float | None
    pace_unit: bool
    lakeshore_used: bool
    lakeshore_heater_range: bool
    lakeshore_data: bool
    radicon_used: bool

    @property
    def pace_source(self) -> bool:
        return self.pace_max_set_pressure_mpa is not None


# ------------------------------------------------------------------ requirements

def _effective_oscillate(a: TakeXrdAction, global_xrd: "GlobalXrdSettings | None") -> bool:
    return a.oscillate if a.oscillate is not None else (
        global_xrd.oscillate if global_xrd is not None else False
    )


def determine_requirements(
    trace: ExecutionTrace, global_xrd: "GlobalXrdSettings | None"
) -> SnapshotRequirements:
    flat_actions = [e.action for e in trace.flat]
    within_limits = trace.stats.within_limits

    stage_moving = any(
        isinstance(a, (
            StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction,
            StartFollowingAction, FollowSampleAction,
        ))
        for a in flat_actions
    ) or any(
        isinstance(a, TakeXrdAction) and _effective_oscillate(a, global_xrd)
        for a in flat_actions
    )

    pace_used = any(
        isinstance(a, (SetPressureAction, WaitPressureAction, SetControlModeAction))
        for a in flat_actions
    )

    pace_primitives = [e.action for e in trace.pace_primitives()]
    pace_output_state = within_limits and any(
        isinstance(a, (SetPressureAction, WaitPressureAction)) for a in pace_primitives
    )

    ordered_actions = [e.action for e in trace.ordered]
    pace_target = within_limits and any(
        isinstance(a, SetPressureAction) for a in ordered_actions
    )

    pace_max_set_pressure_mpa = _find_max_set_pressure_mpa(trace.ordered)
    pace_unit = pace_target or (pace_max_set_pressure_mpa is not None)

    lakeshore_used = any(
        isinstance(a, (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction))
        for a in flat_actions
    )
    lakeshore_heater_range = lakeshore_used and within_limits
    lakeshore_data = any(isinstance(a, WaitTemperatureAction) for a in flat_actions)

    radicon_used = any(isinstance(a, (TakeXrdAction, TakeDarkAction)) for a in flat_actions)

    return SnapshotRequirements(
        stage_moving=stage_moving,
        pace_used=pace_used,
        pace_output_state=pace_output_state,
        pace_target=pace_target,
        pace_max_set_pressure_mpa=pace_max_set_pressure_mpa,
        pace_unit=pace_unit,
        lakeshore_used=lakeshore_used,
        lakeshore_heater_range=lakeshore_heater_range,
        lakeshore_data=lakeshore_data,
        radicon_used=radicon_used,
    )


def _find_max_set_pressure_mpa(ordered: list[TraceEntry]) -> float | None:
    """Scan the true execution order (SetAndWaitPressureAction left un-split,
    matched by the isinstance check below) and return the maximum
    SetPressureAction/SetAndWaitPressureAction target in MPa. None if the
    sequence has no resolvable numeric set-pressure target (loop variables
    that never resolve, or non-numeric literals, are skipped rather than
    counted as a candidate)."""
    max_mpa: float | None = None
    for entry in ordered:
        a = entry.action
        if isinstance(a, (SetPressureAction, SetAndWaitPressureAction)):
            pressure = a.pressure
            if isinstance(pressure, str):
                pressure = entry.variables.get(pressure)
                if pressure is None:
                    continue
            try:
                p_mpa = float(pressure) * _PACE_TO_MPA.get(a.unit, 1.0)
            except (TypeError, ValueError, OverflowError):
                continue  # invalid literal; already flagged by check_pace5000_params
            max_mpa = p_mpa if max_mpa is None else max(max_mpa, p_mpa)
    return max_mpa


# ------------------------------------------------------------------ collection orchestrator

def collect_snapshot(
    trace: ExecutionTrace,
    ctx: DeviceContext,
    r: "PreCheckResult",
    requirements: SnapshotRequirements,
) -> ValidationSnapshot:
    return ValidationSnapshot(
        stage=collect_stage_snapshot(ctx, r, requirements),
        pace=collect_pace_snapshot(ctx, r, requirements),
        lakeshore=collect_lakeshore_snapshot(ctx, r, requirements),
        radicon=collect_radicon_snapshot(ctx, requirements),
        collected_at=datetime.now(),
    )


# ------------------------------------------------------------------ Stage

def collect_stage_snapshot(
    ctx: DeviceContext, r: "PreCheckResult", requirements: SnapshotRequirements
) -> StageSnapshot | None:
    """Position/mode are collected whenever a stage controller is connected,
    regardless of whether the Sequence contains any Stage action — the UI's
    Validate-Run baseline-drift detection depends on this (see
    `checks.stage`'s baseline recording, which reads `positions` off this
    snapshot). `is_moving` is the one field gated by actual need
    (`requirements.stage_moving`) — reading it unconditionally would make an
    unrelated Stage comms fault fail a pressure-only sequence's validation.

    Every one of Ch1-11 is attempted even after an earlier channel fails
    (previously, `_check_stage_move_constraints` stopped at the first
    failing channel) — an intentional Phase 6 improvement so a second,
    independently broken channel is also reported instead of silently
    skipped.
    """
    if ctx.controller is None:
        return None

    positions: dict[int, int] = {}
    for ch in range(1, 12):
        try:
            positions[ch] = int(ctx.controller.get_ch_pos(ch))
        except Exception:
            emit_preflight(
                r, "preflight.stage.position_unreadable",
                f"Cannot read Ch{ch} position (required for move-constraint / stage-mode validation)",
                device="stage",
            )

    is_moving: bool | None = None
    if requirements.stage_moving:
        try:
            is_moving = ctx.controller.get_is_moving()
        except Exception:
            is_moving = None
            emit_preflight(
                r, "preflight.stage.is_moving_unreadable",
                "Cannot read stage moving status (required before a stage/oscillation operation)",
                device="stage",
            )

    stage_mode = "unknown"
    if 8 in positions and 9 in positions:
        stage_mode = _stage_mode_from_positions(positions[8], positions[9])

    return StageSnapshot(
        positions=types.MappingProxyType(dict(positions)),
        is_moving=is_moving,
        stage_mode=stage_mode,
    )


def _stage_mode_from_positions(pos8: int, pos9: int) -> str:
    settings = load_stage_settings_dict()
    if settings is None:
        return "unknown"
    try:
        ch8_in  = int(settings["ch8_in"])
        ch8_out = int(settings["ch8_out"])
        det_in  = int(settings["det_in"])
        det_out = int(settings["det_out"])
    except (KeyError, ValueError, TypeError):
        return "unknown"

    T = _STAGE_MODE_TOLERANCE
    near_ch8_in  = abs(pos8 - ch8_in)  < T
    near_ch8_out = abs(pos8 - ch8_out) < T
    near_det_in  = abs(pos9 - det_in)  < T
    near_det_out = abs(pos9 - det_out) < T

    if near_ch8_in and near_det_out:
        return "microscope"
    if near_ch8_out and near_det_in:
        return "xrd"
    return "unknown"


def load_stage_settings_dict() -> dict | None:
    """Shared by `collect_stage_snapshot` (silent — a missing/corrupt file
    just means stage_mode stays 'unknown') and `checks.stage` (which DOES
    report a missing/corrupt file as an error when a compound stage action
    or its settings are actually being validated)."""
    if not _STAGE_SETTINGS_PATH.exists():
        return None
    try:
        return json.loads(_STAGE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ PACE5000

_UNIT_RESPONSE_TO_KEY = {"MPA": "MPa", "BAR": "Bar"}


def _normalize_output_state(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    if raw.strip() == "":
        return None
    return raw  # case preserved — callers compare `.strip() in ("1", "ON")` as before


def _normalize_pressure_unit(raw) -> str | None:
    """Fail-closed: only a recognized "MPA"/"BAR" (any case/whitespace)
    response resolves to a unit; anything else (wrong firmware response
    format, comms garbage, None) is treated as unreadable rather than
    guessed."""
    if not isinstance(raw, str):
        return None
    return _UNIT_RESPONSE_TO_KEY.get(raw.strip().upper())


def collect_pace_snapshot(
    ctx: DeviceContext, r: "PreCheckResult", requirements: SnapshotRequirements
) -> PaceSnapshot | None:
    if not requirements.pace_used:
        return None

    if ctx.pace5000 is None or not ctx.pace5000.connected:
        return PaceSnapshot(
            connected=False, output_state=None, unit=None,
            target_pressure_mpa=None, positive_source_pressure_mpa=None,
        )

    output_state: str | None = None
    if requirements.pace_output_state:
        try:
            raw_state = ctx.pace5000.get_output_state()
        except Exception:
            raw_state = None
        output_state = _normalize_output_state(raw_state)
        if output_state is None:
            emit_preflight(
                r, "preflight.pace5000.output_state_unreadable",
                "PACE5000 の Control Mode (Output State) を取得できませんでした — "
                "通信に問題がある可能性があります",
                device="pace5000",
            )

    unit: str | None = None
    if requirements.pace_unit:
        try:
            raw_unit = ctx.pace5000.query(":UNIT:PRES?")
        except Exception:
            raw_unit = None
        unit = _normalize_pressure_unit(raw_unit)
        if unit is None:
            emit_preflight(
                r, "preflight.pace5000.unit_unreadable",
                "PACE5000 の圧力単位を取得できませんでした（通信に問題があるか、"
                "想定外の応答形式です） — 圧力関連の検証をスキップします",
                device="pace5000",
            )

    target_pressure_mpa: float | None = None
    if unit is not None and requirements.pace_target:
        try:
            raw_target = ctx.pace5000.get_target_pressure()
            target_pressure_mpa = (
                float(raw_target) * _PACE_TO_MPA[unit] if raw_target is not None else None
            )
        except Exception:
            target_pressure_mpa = None
        if target_pressure_mpa is not None:
            val, _err = action_params.parse_finite_number(target_pressure_mpa)
            target_pressure_mpa = val  # non-finite -> None, silently (soft use)

    positive_source_pressure_mpa: float | None = None
    if unit is not None and requirements.pace_source:
        try:
            raw_source = ctx.pace5000.get_positive_source_pressure()
            positive_source_pressure_mpa = (
                float(raw_source) * _PACE_TO_MPA[unit] if raw_source is not None else None
            )
        except Exception:
            positive_source_pressure_mpa = None
        if positive_source_pressure_mpa is not None:
            val, err = action_params.parse_finite_number(positive_source_pressure_mpa)
            positive_source_pressure_mpa = val
            if err is not None:
                emit_preflight(
                    r, "preflight.pace5000.source_pressure_unreadable",
                    "PACE5000 の +ve Source Pressure を取得できませんでした — "
                    "通信に問題がある可能性があります",
                    device="pace5000",
                )
        else:
            emit_preflight(
                r, "preflight.pace5000.source_pressure_unreadable",
                "PACE5000 の +ve Source Pressure を取得できませんでした — "
                "通信に問題がある可能性があります",
                device="pace5000",
            )

    return PaceSnapshot(
        connected=True,
        output_state=output_state,
        unit=unit,
        target_pressure_mpa=target_pressure_mpa,
        positive_source_pressure_mpa=positive_source_pressure_mpa,
    )


# ------------------------------------------------------------------ LakeShore 335

def collect_lakeshore_snapshot(
    ctx: DeviceContext, r: "PreCheckResult", requirements: SnapshotRequirements
) -> LakeShoreSnapshot | None:
    if not requirements.lakeshore_used:
        return None

    if ctx.lakeshore is None or not ctx.lakeshore.is_connected:
        return LakeShoreSnapshot(connected=False, setpoint=None, heater_range=None, has_data=None)

    setpoint: float | None = None
    try:
        setpoint = ctx.lakeshore.get_setpoint()
    except Exception:
        emit_preflight(
            r, "preflight.lakeshore.setpoint_unreadable",
            "LakeShore 335 の現在の設定値を読み出せませんでした — "
            "通信に問題がある可能性があります",
            device="lakeshore",
        )

    heater_range: int | None = None
    if requirements.lakeshore_heater_range:
        try:
            heater_range = ctx.lakeshore.get_heater_range()
        except Exception:
            heater_range = None
            emit_preflight(
                r, "preflight.lakeshore.heater_range_unreadable",
                "LakeShore 335 の現在のヒーターレンジを読み出せませんでした — "
                "通信に問題がある可能性があります",
                device="lakeshore",
            )

    has_data: bool | None = None
    if requirements.lakeshore_data:
        try:
            data = ctx.lakeshore.get_data()
            has_data = bool(data)
        except Exception:
            has_data = None  # existing silent-swallow behaviour — no Diagnostic

    return LakeShoreSnapshot(
        connected=True, setpoint=setpoint, heater_range=heater_range, has_data=has_data,
    )


# ------------------------------------------------------------------ Rad-icon 2022

def collect_radicon_snapshot(
    ctx: DeviceContext, requirements: SnapshotRequirements
) -> RadiconSnapshot | None:
    if not requirements.radicon_used:
        return None
    return RadiconSnapshot(available=ctx.radicon is not None)
