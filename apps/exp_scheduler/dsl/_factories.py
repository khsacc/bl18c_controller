"""
Action factories — the CommandSpec.factory implementation for every DSL
command (see dsl/_registry.py).

Each function takes the fully bound + defaulted keyword-argument dict for
its command — every parameter declared in the corresponding dsl/api.py
function signature is always present, so `kw["name"]` is always safe and
`kw.get(name, fallback)` is never needed for a required field; an optional
field's fallback is the signature's own default (applied by
inspect.Signature.apply_defaults() before this is called), not a second
hardcoded one here — and returns the Action it represents.

This is the single Action-construction implementation. dsl/parser.py's
SequenceBuilder calls it after binding AST call arguments against the
CommandSpec's signature; dsl/api.py's public functions call the same
factory with the arguments they were called with, so the exec()-context
entry point and the AST-direct-build entry point can never diverge (see
REORGANISATION_PLAN.md Phase 3 — before this module existed, dsl/api.py's
own function bodies were a second, independently hand-maintained Action
construction implementation, and had already drifted from
dsl/parser.py::SequenceBuilder's: the api.py body for set_temperature
called float(value) unconditionally, which would raise on a for-loop
variable reference such as set_temperature(value=t, ...) — dormant only
because dsl/api.py's functions are not on the production compile path).

A for-loop variable reference is only meaningful for the handful of
arguments actions.py's LOOP_VAR_FIELDS actually resolves at run time
(Runner._do_stage/_do_set_pressure/_do_set_temperature) — those fields are
passed through as-is (never float()/int()-coerced) so a loop-variable name
string survives intact; every other field is coerced to its expected type
exactly as dsl/api.py historically did.
"""
from __future__ import annotations

from ..actions import (
    AllHeatersOffAction,
    FpdOutMicroscopeInAction,
    FollowSampleAction,
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
)


def _to_seconds(value: float, unit: str) -> float:
    return float(value) * 60 if unit == "min" else float(value)


# ── General ──────────────────────────────────────────────────────────

def wait(kw: dict) -> WaitAction:
    return WaitAction(duration_s=_to_seconds(kw["duration"], kw["unit"]))


def log_message(kw: dict) -> LogAction:
    return LogAction(message=str(kw["message"]))


# ── Stage (primitive) ────────────────────────────────────────────────

def move_absolute(kw: dict) -> StageAction:
    return StageAction(
        operation="move_absolute",
        ch=int(kw["ch"]),
        value=kw["position"],
    )


def move_relative(kw: dict) -> StageAction:
    return StageAction(
        operation="move_relative",
        ch=int(kw["ch"]),
        value=kw["delta"],
    )


def set_speed(kw: dict) -> StageAction:
    return StageAction(
        operation="set_speed",
        ch=int(kw["ch"]),
        speed=str(kw["speed"]),
    )


def normal_stop(kw: dict) -> StageAction:
    return StageAction(operation="normal_stop")


def emergency_stop(kw: dict) -> StageAction:
    return StageAction(operation="emergency_stop")


def microscope_out_and_fpd_in(kw: dict) -> MicroscopeOutFpdInAction:
    return MicroscopeOutFpdInAction(
        microscope_out_pos=kw["microscope_out_pos"],
        fpd_in_pos=kw["fpd_in_pos"],
        speed=str(kw["speed"]),
    )


def fpd_out_and_microscope_in(kw: dict) -> FpdOutMicroscopeInAction:
    return FpdOutMicroscopeInAction(
        fpd_out_pos=kw["fpd_out_pos"],
        microscope_in_pos=kw["microscope_in_pos"],
        speed=str(kw["speed"]),
    )


# ── PACE5000 ─────────────────────────────────────────────────────────

def set_pressure(kw: dict) -> SetPressureAction:
    return SetPressureAction(
        pressure=kw["pressure"],
        unit=str(kw["unit"]),
        rate=kw["rate"],
        rate_unit=kw["rate_unit"],
    )


def wait_pressure(kw: dict) -> WaitPressureAction:
    return WaitPressureAction(
        tol=float(kw["tol"]),
        unit=str(kw["unit"]),
    )


def set_and_wait_pressure(kw: dict) -> SetAndWaitPressureAction:
    return SetAndWaitPressureAction(
        pressure=kw["pressure"],
        unit=str(kw["unit"]),
        rate=kw["rate"],
        rate_unit=kw["rate_unit"],
        tol=float(kw["tol"]),
    )


def set_control_mode(kw: dict) -> SetControlModeAction:
    return SetControlModeAction(enabled=bool(kw["enabled"]))


# ── LakeShore 335 ────────────────────────────────────────────────────

def set_temperature(kw: dict) -> SetTemperatureAction:
    # DSL uses keyword "value"; Action stores as value_k
    return SetTemperatureAction(
        value_k=kw["value"],
        ramp_rate=kw["ramp_rate"],
    )


def wait_temperature(kw: dict) -> WaitTemperatureAction:
    return WaitTemperatureAction(tol_k=float(kw["tol"]))


def set_heater(kw: dict) -> SetHeaterAction:
    return SetHeaterAction(range_index=int(kw["range_index"]))


def all_heaters_off(kw: dict) -> AllHeatersOffAction:
    return AllHeatersOffAction()


# ── Rad-icon 2022 ────────────────────────────────────────────────────

def take_xrd(kw: dict) -> TakeXrdAction:
    exposure_ms = kw["exposure_ms"]
    defect_kernel = kw["defect_kernel"]
    # oscillate itself is preserved exactly as bound (None="inherit
    # global", True/False are both explicit per-step overrides —
    # collapsing False into None here would silently re-enable an
    # explicitly-disabled step whenever the global XRD setting is on).
    # The osc_pos_*/osc_dwell_ms/osc_speed sub-fields only reach the
    # Action when oscillate is truthy, since they're meaningless
    # (nothing to configure) when oscillation isn't happening.
    oscillate = kw["oscillate"]
    return TakeXrdAction(
        exposure_ms=int(exposure_ms) if exposure_ms is not None else None,
        save=bool(kw["save"]),
        prefix=str(kw["prefix"]),
        save_dir=kw["save_dir"],
        dark_file=kw["dark_file"],
        dark_enabled=kw["dark_enabled"],
        defect_file=kw["defect_file"],
        defect_enabled=kw["defect_enabled"],
        defect_kernel=int(defect_kernel) if defect_kernel is not None else None,
        flip_v=kw["flip_v"],
        flip_h=kw["flip_h"],
        oscillate=oscillate,
        osc_pos_a_deg=float(kw["osc_pos_a_deg"]) if oscillate else None,
        osc_pos_b_deg=float(kw["osc_pos_b_deg"]) if oscillate else None,
        osc_dwell_ms=int(kw["osc_dwell_ms"]) if oscillate else None,
        osc_speed=str(kw["osc_speed"]) if oscillate else None,
    )


def take_dark(kw: dict) -> TakeDarkAction:
    return TakeDarkAction(exposure_ms=int(kw["exposure_ms"]))


# ── Camera ───────────────────────────────────────────────────────────

def save_reference_image(kw: dict) -> SaveReferenceImageAction:
    return SaveReferenceImageAction(
        path=kw["path"],
        camera_index=int(kw["camera_index"]),
    )


def save_snapshot(kw: dict) -> SaveSnapshotAction:
    return SaveSnapshotAction(save_dir=kw["save_dir"])


def start_following(kw: dict) -> StartFollowingAction:
    interval = kw["interval"]
    interval_s = (
        _to_seconds(interval, kw["interval_unit"])
        if interval is not None
        else None
    )
    autofocus_range_um = kw["autofocus_range_um"]
    autofocus_steps = kw["autofocus_steps"]
    return StartFollowingAction(
        reference_path=kw["reference_path"],
        interval_s=interval_s,
        similarity_threshold=kw["similarity_threshold"],
        max_correction_per_step_um=kw["max_correction_per_step_um"],
        camera_index=int(kw["camera_index"]),
        autofocus_enabled=bool(kw["autofocus_enabled"]),
        autofocus_range_um=(
            float(autofocus_range_um) if autofocus_range_um is not None else None
        ),
        autofocus_steps=(
            int(autofocus_steps) if autofocus_steps is not None else None
        ),
    )


def stop_following(kw: dict) -> StopFollowingAction:
    return StopFollowingAction()


def follow_sample_position(kw: dict) -> FollowSampleAction:
    duration_s = _to_seconds(kw["duration"], kw["unit"])
    interval = kw["interval"]
    interval_s = (
        _to_seconds(interval, kw["interval_unit"])
        if interval is not None
        else None
    )
    autofocus_range_um = kw["autofocus_range_um"]
    autofocus_steps = kw["autofocus_steps"]
    return FollowSampleAction(
        duration_s=duration_s,
        reference_path=kw["reference_path"],
        interval_s=interval_s,
        similarity_threshold=kw["similarity_threshold"],
        max_correction_per_step_um=kw["max_correction_per_step_um"],
        camera_index=int(kw["camera_index"]),
        autofocus_enabled=bool(kw["autofocus_enabled"]),
        autofocus_range_um=(
            float(autofocus_range_um) if autofocus_range_um is not None else None
        ),
        autofocus_steps=(
            int(autofocus_steps) if autofocus_steps is not None else None
        ),
    )
