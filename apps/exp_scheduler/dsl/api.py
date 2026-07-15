"""
DSL API — functions callable from DSL scripts.

Each function appends an Action to the thread-local context list.
Use api_context() to set up the context, then exec the DSL in a namespace
built from DSL_NAMESPACE.

The function signatures here define the public DSL contract and must stay
in sync with ALLOWED_FUNCTIONS in dsl/__init__.py.

The @dsl_command decorator attaches category and example metadata that
prompt_builder.py uses to auto-generate the LLM System Prompt.  Docstrings
are written as LLM specifications — they are the primary source of truth for
what the LLM knows about each command's constraints.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Generator

from ..actions import (
    AllHeatersOffAction,
    FollowSampleAction,
    FpdOutMicroscopeInAction,
    LogAction,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
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
from ._registry import dsl_command

_local = threading.local()


def _ctx() -> list:
    try:
        return _local.actions
    except AttributeError:
        raise RuntimeError(
            "DSL functions must be called inside an api_context() block"
        )


@contextmanager
def api_context() -> Generator[list, None, None]:
    """Context manager that provides an Action accumulation list.

    Example::

        with api_context() as actions:
            exec(dsl_code, {**DSL_NAMESPACE})
        sequence = Sequence(actions=actions)
    """
    _local.actions = []
    try:
        yield _local.actions
    finally:
        del _local.actions


def _to_s(value: float, unit: str) -> float:
    return float(value) * 60.0 if unit == "min" else float(value)


# ── General ──────────────────────────────────────────────────────────────────

@dsl_command(
    category="General",
    example='wait(duration=5.0, unit="min")',
)
def wait(duration: float, unit: str = "min") -> None:
    """Wait for a fixed duration without doing anything else.

    Parameters
    ----------
    duration : float
        Duration to wait. Must be positive.
    unit : str
        Time unit. Must be "s" or "min".
    """
    _ctx().append(WaitAction(duration_s=_to_s(duration, unit)))


@dsl_command(category="General")
def log_message(message: str) -> None:
    """Write a free-text message to the sequence execution log.

    Parameters
    ----------
    message : str
        Text to record. Supports f-string variable interpolation.

    Notes
    -----
    This is a convenience step — it does not affect hardware.
    """
    _ctx().append(LogAction(message=str(message)))


# ── Stage (primitive) ─────────────────────────────────────────────────────────

@dsl_command(category="Stage")
def move_absolute(ch: int, position: float) -> None:
    """Move a stage channel to an absolute position (pulses).

    Parameters
    ----------
    ch : int
        Channel number (1–11). Common channels: 3=Z-focus, 4=Sample-X,
        5=Sample-Y, 8=Microscope arm, 9=Detector.
    position : float
        Target position in pulses. Range: ±2,147,483,647.

    Notes
    -----
    The move blocks until the channel stops.
    Inter-channel constraints (Ch8/Ch9 collision prevention) are enforced.
    """
    _ctx().append(StageAction(operation="move_absolute", ch=int(ch), value=position))


@dsl_command(category="Stage")
def move_relative(ch: int, delta: float) -> None:
    """Move a stage channel by a relative offset (pulses).

    Parameters
    ----------
    ch : int
        Channel number (1–11).
    delta : float
        Displacement in pulses. Positive = forward, negative = backward.

    Notes
    -----
    The move blocks until the channel stops.
    """
    _ctx().append(StageAction(operation="move_relative", ch=int(ch), value=delta))


@dsl_command(category="Stage")
def set_speed(ch: int, speed: str) -> None:
    """Set the movement speed for a stage channel.

    Parameters
    ----------
    ch : int
        Channel number (1–11).
    speed : str
        Speed level. Must be "H" (High), "M" (Medium), or "L" (Low).
    """
    _ctx().append(StageAction(operation="set_speed", ch=int(ch), speed=str(speed)))


@dsl_command(category="Stage")
def normal_stop() -> None:
    """Decelerate-stop all stage channels.

    Notes
    -----
    Use this for a controlled stop. Unlike emergency_stop(), the motors
    ramp down instead of stopping abruptly.
    """
    _ctx().append(StageAction(operation="normal_stop"))


@dsl_command(category="Stage")
def emergency_stop() -> None:
    """Emergency stop all stage channels immediately.

    Notes
    -----
    Use only in emergency situations. This is an abrupt stop (no deceleration).
    """
    _ctx().append(StageAction(operation="emergency_stop"))


@dsl_command(
    category="Stage",
    example="microscope_out_and_fpd_in()  # switch to XRD measurement mode",
)
def microscope_out_and_fpd_in(
    microscope_out_pos: int | None = None,
    fpd_in_pos: int | None = None,
    speed: str = "H",
) -> None:
    """Move microscope arm OUT (Ch8) then detector IN (Ch9) — XRD mode.

    Parameters
    ----------
    microscope_out_pos : int or None
        Ch8 target position (pulses). None uses the value from stage_settings.json.
    fpd_in_pos : int or None
        Ch9 target position (pulses). None uses the value from stage_settings.json.
    speed : str
        Movement speed. Must be "H", "M", or "L". Default "H".

    Notes
    -----
    Ch8 moves first; Ch9 moves only after Ch8 has completed.
    Omit position arguments in most cases to use calibrated presets.
    """
    _ctx().append(MicroscopeOutFpdInAction(
        microscope_out_pos=microscope_out_pos,
        fpd_in_pos=fpd_in_pos,
        speed=speed,
    ))


@dsl_command(
    category="Stage",
    example="fpd_out_and_microscope_in()  # switch to microscopy mode",
)
def fpd_out_and_microscope_in(
    fpd_out_pos: int | None = None,
    microscope_in_pos: int | None = None,
    speed: str = "H",
) -> None:
    """Move detector OUT (Ch9) then microscope arm IN (Ch8) — microscopy mode.

    Parameters
    ----------
    fpd_out_pos : int or None
        Ch9 target position (pulses). None uses the value from stage_settings.json.
    microscope_in_pos : int or None
        Ch8 target position (pulses). None uses the value from stage_settings.json.
    speed : str
        Movement speed. Must be "H", "M", or "L". Default "H".

    Notes
    -----
    Ch9 moves first; Ch8 moves only after Ch9 has completed.
    """
    _ctx().append(FpdOutMicroscopeInAction(
        fpd_out_pos=fpd_out_pos,
        microscope_in_pos=microscope_in_pos,
        speed=speed,
    ))


# ── PACE5000 ─────────────────────────────────────────────────────────────────

@dsl_command(
    category="Pressure",
    example="""\
# Canonical pressure scan with XRD measurement
microscope_out_and_fpd_in()
for p in [1.0, 2.0, 3.0, 4.0, 5.0]:
    set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")
    wait_pressure(tol=0.01, unit="MPa")
    take_xrd(exposure_ms=1000, save=True, prefix="scan")
fpd_out_and_microscope_in()""",
)
def set_pressure(
    pressure: float,
    unit: str,
    rate: float,
    rate_unit: str,
) -> None:
    """Set the PACE5000 target pressure.

    Parameters
    ----------
    pressure : float
        Target pressure. Must be non-negative.
    unit : str
        Pressure unit. Must be "MPa" or "Bar". "GPa" is NOT supported.
    rate : float
        Slew rate (required). Must be positive.
    rate_unit : str
        Slew rate unit. Must be "MPa/min" or "Bar/min".

    Notes
    -----
    This function issues the setpoint immediately and returns.
    It does NOT wait for the pressure to stabilise.
    Call wait_pressure() afterward to block until the target is reached.
    """
    _ctx().append(SetPressureAction(
        pressure=pressure, unit=unit, rate=rate, rate_unit=rate_unit,
    ))


@dsl_command(category="Pressure")
def wait_pressure(tol: float, unit: str) -> None:
    """Block until the current pressure is within tol of the setpoint.

    Parameters
    ----------
    tol : float
        Acceptable deviation from setpoint. Must be positive.
    unit : str
        Pressure unit. Must be "MPa" or "Bar".

    Notes
    -----
    This function polls the pressure sensor every 200 ms.
    Use after set_pressure() to ensure the target is reached before proceeding.
    """
    _ctx().append(WaitPressureAction(tol=float(tol), unit=unit))


@dsl_command(category="Pressure")
def set_control_mode(enabled: bool) -> None:
    """Enable or disable PACE5000 closed-loop pressure control.

    Parameters
    ----------
    enabled : bool
        True to enable control mode; False to disable.
    """
    _ctx().append(SetControlModeAction(enabled=bool(enabled)))


# ── LakeShore 335 ─────────────────────────────────────────────────────────────

@dsl_command(
    category="Temperature",
    example="""\
set_temperature(value=300.0, unit="K", ramp_rate=5.0)
wait_temperature(tol=1.0, unit="K")""",
)
def set_temperature(
    value: float,
    *,
    unit: str = "K",
    ramp_rate: float,
) -> None:
    """Set the LakeShore 335 target temperature.

    Parameters
    ----------
    value : float
        Target temperature. Must be positive.
    unit : str
        Temperature unit. Only "K" is supported.
    ramp_rate : float
        Ramp rate in K/min (required). Must be positive.

    Notes
    -----
    This function issues the setpoint immediately and returns.
    It does NOT wait for the temperature to stabilise.
    Call wait_temperature() afterward if stabilisation is required before
    the next step.
    """
    _ctx().append(SetTemperatureAction(value_k=float(value), ramp_rate=float(ramp_rate)))


@dsl_command(category="Temperature")
def wait_temperature(tol: float, unit: str = "K") -> None:
    """Block until the temperature is within tol of the setpoint.

    Parameters
    ----------
    tol : float
        Acceptable deviation from setpoint in Kelvin. Must be positive.
    unit : str
        Temperature unit. Only "K" is supported.

    Notes
    -----
    Polls the LakeShore sensor every 200 ms.
    Use after set_temperature() to ensure the target is reached.
    """
    _ctx().append(WaitTemperatureAction(tol_k=float(tol)))


@dsl_command(category="Temperature")
def set_heater(range_index: int) -> None:
    """Set the LakeShore heater output range.

    Parameters
    ----------
    range_index : int
        Heater range. Must be one of:
        0 = Off, 1 = Low, 2 = Medium, 3 = High.

    Notes
    -----
    Call all_heaters_off() at the end of a heating sequence for safety.
    """
    _ctx().append(SetHeaterAction(range_index=int(range_index)))


@dsl_command(category="Temperature")
def all_heaters_off() -> None:
    """Turn off both LakeShore heater channels.

    Notes
    -----
    Always call this at the end of any sequence that uses set_heater().
    """
    _ctx().append(AllHeatersOffAction())


# ── Rad-icon 2022 ─────────────────────────────────────────────────────────────

@dsl_command(
    category="Measurement",
    example='take_xrd(exposure_ms=1000, save=True, prefix="scan")',
)
def take_xrd(
    exposure_ms: int | None = None,
    save: bool = True,
    prefix: str = "scan",
    oscillate: bool = False,
    osc_pos_a_deg: float = -5.0,
    osc_pos_b_deg: float = 20.0,
    osc_dwell_ms: int = 0,
    osc_speed: str = "M",
) -> None:
    """Take an XRD frame with the Rad-icon 2022 detector.

    Parameters
    ----------
    exposure_ms : int or None
        Shutter time in milliseconds. None uses the global XRD settings.
    save : bool
        Whether to save the frame to disk. Default True.
    prefix : str
        File name prefix for the saved frame. Default "scan".
    oscillate : bool
        If True, oscillate Ch11 between osc_pos_a_deg and osc_pos_b_deg
        throughout the exposure, then return Ch11 to 0° before completing.
        Default False.
    osc_pos_a_deg : float
        Oscillation endpoint A in degrees. Used only when oscillate=True.
    osc_pos_b_deg : float
        Oscillation endpoint B in degrees. Used only when oscillate=True.
    osc_dwell_ms : int
        Dwell time at each endpoint in ms (0 = no dwell). Used only when
        oscillate=True.
    osc_speed : str
        Ch11 oscillation speed. Must be "H", "M", or "L". Used only when
        oscillate=True.

    Notes
    -----
    When oscillate=True, the step completes only after Ch11 has returned to 0°.
    """
    _ctx().append(TakeXrdAction(
        exposure_ms=int(exposure_ms) if exposure_ms is not None else None,
        save=bool(save),
        prefix=str(prefix),
        oscillate=bool(oscillate) if oscillate else None,
        osc_pos_a_deg=float(osc_pos_a_deg) if oscillate else None,
        osc_pos_b_deg=float(osc_pos_b_deg) if oscillate else None,
        osc_dwell_ms=int(osc_dwell_ms) if oscillate else None,
        osc_speed=str(osc_speed) if oscillate else None,
    ))


@dsl_command(category="Measurement")
def take_dark(exposure_ms: int) -> None:
    """Take a dark (background) frame with the Rad-icon 2022 detector.

    Parameters
    ----------
    exposure_ms : int
        Shutter time in milliseconds. Must match the exposure_ms used for
        the corresponding XRD frames.

    Notes
    -----
    Acquire dark frames before the main measurement sequence.
    Dark correction is applied automatically in subsequent take_xrd() steps
    if enabled in the global XRD settings.
    """
    _ctx().append(TakeDarkAction(exposure_ms=int(exposure_ms)))


# ── Camera ────────────────────────────────────────────────────────────────────

@dsl_command(category="Camera")
def save_snapshot(save_dir: str | None = None) -> None:
    """Capture one USB-camera frame and save it as a timestamped image.

    Parameters
    ----------
    save_dir : str or None
        Directory to save the snapshot image. The filename is generated from
        the current timestamp. None uses the global snapshot save directory.
    """
    _ctx().append(SaveSnapshotAction(save_dir=save_dir))


@dsl_command(category="Camera")
def save_reference_image(
    path: str | None = None,
    camera_index: int = 0,
) -> None:
    """Capture and save a reference frame for sample-position following.

    Parameters
    ----------
    path : str or None
        Save path for the reference image (.png or .jpg).
        None uses the default: __localdata/reference_frame.png.
    camera_index : int
        Camera device index. Default 0.

    Notes
    -----
    Call this before start_following() or follow_sample_position() when you
    want to record the initial sample position within the sequence.
    """
    _ctx().append(SaveReferenceImageAction(path=path, camera_index=int(camera_index)))


@dsl_command(category="Camera")
def start_following(
    reference_path: str | None = None,
    interval: float | None = None,
    interval_unit: str = "s",
    similarity_threshold: float | None = None,
    max_correction_per_step_um: float | None = None,
    camera_index: int = 0,
) -> None:
    """Start background sample-position following (returns immediately).

    Parameters
    ----------
    reference_path : str or None
        Path to the reference image (.png or .jpg).
        None uses __localdata/reference_frame.png.
    interval : float or None
        Correction attempt interval. None uses the preset from
        scheduler_presets.json.
    interval_unit : str
        Time unit for interval. Must be "s" or "min".
    similarity_threshold : float or None
        Minimum template-match score (0–1) to accept a correction.
        None uses the preset.
    max_correction_per_step_um : float or None
        Maximum XY correction per cycle in micrometres. None uses the preset.
    camera_index : int
        Camera device index. Default 0.

    Notes
    -----
    Returns immediately; following runs in a background thread.
    You MUST call stop_following() later to end the following thread.
    If you want fixed-duration following, use follow_sample_position() instead.
    Calling start_following() twice without stop_following() is invalid.
    """
    interval_s: float | None = None
    if interval is not None:
        interval_s = _to_s(interval, interval_unit)
    _ctx().append(StartFollowingAction(
        reference_path=reference_path,
        interval_s=interval_s,
        similarity_threshold=similarity_threshold,
        max_correction_per_step_um=max_correction_per_step_um,
        camera_index=int(camera_index),
    ))


@dsl_command(category="Camera")
def stop_following() -> None:
    """Stop the background sample-position following thread.

    Notes
    -----
    Blocks until the following thread has fully stopped.
    Must be preceded by start_following().
    """
    _ctx().append(StopFollowingAction())


@dsl_command(
    category="Camera",
    example='follow_sample_position(duration=30.0, unit="min", interval=5.0, interval_unit="min")',
)
def follow_sample_position(
    duration: float,
    unit: str = "min",
    reference_path: str | None = None,
    interval: float | None = None,
    interval_unit: str = "s",
    similarity_threshold: float | None = None,
    max_correction_per_step_um: float | None = None,
    camera_index: int = 0,
) -> None:
    """Follow sample position for a fixed duration (blocking).

    Convenience shorthand for start_following() + wait() + stop_following().

    Parameters
    ----------
    duration : float
        Total following duration. Must be positive.
    unit : str
        Time unit for duration. Must be "s" or "min".
    reference_path : str or None
        Path to the reference image. None uses __localdata/reference_frame.png.
    interval : float or None
        Correction attempt interval. None uses the preset.
    interval_unit : str
        Time unit for interval. Must be "s" or "min".
    similarity_threshold : float or None
        Minimum template-match score. None uses the preset.
    max_correction_per_step_um : float or None
        Maximum XY correction per cycle in micrometres. None uses the preset.
    camera_index : int
        Camera device index. Default 0.

    Notes
    -----
    Blocks for the full duration.  Use start_following() + stop_following()
    if you need interleaved steps (e.g., set_pressure() while following).
    """
    duration_s = _to_s(duration, unit)
    interval_s: float | None = None
    if interval is not None:
        interval_s = _to_s(interval, interval_unit)
    _ctx().append(FollowSampleAction(
        duration_s=duration_s,
        reference_path=reference_path,
        interval_s=interval_s,
        similarity_threshold=similarity_threshold,
        max_correction_per_step_um=max_correction_per_step_um,
        camera_index=int(camera_index),
    ))


# ── Public namespace ──────────────────────────────────────────────────────────

#: Mapping of function name → function, suitable for use as exec() globals.
DSL_NAMESPACE: dict[str, object] = {
    fn.__name__: fn
    for fn in (
        wait, log_message,
        move_absolute, move_relative, set_speed, normal_stop, emergency_stop,
        microscope_out_and_fpd_in, fpd_out_and_microscope_in,
        set_pressure, wait_pressure, set_control_mode,
        set_temperature, wait_temperature, set_heater, all_heaters_off,
        take_xrd, take_dark,
        save_snapshot, save_reference_image, start_following, stop_following,
        follow_sample_position,
    )
}
