"""
Pure, device-I/O-free safety rules shared by SequenceRunner and PreValidator.

Each function here only validates values and/or converts units — it never
touches a controller, backend, or the filesystem, so it can be unit-tested
without any fake device. See REORGANISATION_PLAN.md Phase 4.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Mapping

from utils.stage.control_stage import PULSE_SCALE

if TYPE_CHECKING:
    from .scheduler_settings import GlobalLimits

_OSC_SPEEDS = frozenset(("L", "M", "H"))

# Ch7 can change during gasket tracking without moving the registered beam.
GASKET_BEAM_CHANNELS = (6, 8)

# Channels read to decide the microscope / XRD stage mode.
STAGE_MODE_CHANNELS = (6, 8, 9)


# ---------------------------------------------------------------------------
# Stage mode (microscope / XRD), shared by PreValidator and SequenceRunner
#
# microscope: Ch6 and Ch8 exactly at the registered X-ray beam pulses (the
#             position the camera geometry was calibrated at) and the FPD
#             out of the way (Ch9 <= Ch9 OUT).
# xrd:        Ch9 exactly at Ch9 IN and the microscope out of the way
#             (Ch8 <= Ch8 OUT).
# Exact pulse matches: PM16C absolute moves always end on the target pulse,
# so any difference means the axis was stopped early or moved by hand.
# ---------------------------------------------------------------------------

def _mode_presets(stage_settings: Mapping | None) -> tuple[int, int, int] | None:
    """(ch8_out, ch9_out, ch9_in) from stage_settings-style keys, or None."""
    if stage_settings is None:
        return None
    try:
        return (int(stage_settings["ch8_out"]), int(stage_settings["det_out"]),
                int(stage_settings["det_in"]))
    except (KeyError, TypeError, ValueError):
        return None


def stage_mode_from_positions(
    positions: Mapping[int, int],
    stage_settings: Mapping | None,
    beam_pulses: Mapping[int, int] | None,
) -> str:
    """Return 'microscope', 'xrd' or 'unknown' for Ch6/Ch8/Ch9 `positions`.

    `stage_settings` uses the stage_settings.json keys (ch8_out, det_out,
    det_in) — e.g. `GlobalPositions.to_stage_settings()`. `beam_pulses` is
    the registered X-ray beam's microscope pulses (None when no beam is
    registered, which means microscope mode can never be confirmed).
    """
    presets = _mode_presets(stage_settings)
    if presets is None or any(ch not in positions for ch in STAGE_MODE_CHANNELS):
        return "unknown"
    ch8_out, ch9_out, ch9_in = presets
    if (beam_pulses is not None
            and all(ch in beam_pulses for ch in GASKET_BEAM_CHANNELS)
            and all(positions[ch] == beam_pulses[ch] for ch in GASKET_BEAM_CHANNELS)
            and positions[9] <= ch9_out):
        return "microscope"
    if positions[9] == ch9_in and positions[8] <= ch8_out:
        return "xrd"
    return "unknown"


def explain_stage_mode(
    positions: Mapping[int, int],
    stage_settings: Mapping | None,
    beam_pulses: Mapping[int, int] | None,
) -> str:
    """One-line explanation of the current positions and both mode criteria,
    for error messages."""
    now = ", ".join(
        f"Ch{ch}={positions[ch]:+d}" if ch in positions else f"Ch{ch}=unreadable"
        for ch in STAGE_MODE_CHANNELS
    )
    presets = _mode_presets(stage_settings)
    if presets is None:
        return f"now {now}; Ch8/Ch9 IN/OUT positions are not configured"
    ch8_out, ch9_out, ch9_in = presets
    if beam_pulses is None or any(ch not in beam_pulses for ch in GASKET_BEAM_CHANNELS):
        microscope = ("microscope mode needs a registered X-ray beam position "
                      "(Interactive Camera)")
    else:
        microscope = (f"microscope mode needs Ch6={beam_pulses[6]:+d} and "
                      f"Ch8={beam_pulses[8]:+d} (registered X-ray beam) with "
                      f"Ch9 <= {ch9_out:+d}")
    xrd = f"XRD mode needs Ch9={ch9_in:+d} with Ch8 <= {ch8_out:+d}"
    return f"now {now}; {microscope}; {xrd}"


def validate_ch11_oscillation_settings(
    pos_a_deg: float,
    pos_b_deg: float,
    dwell_ms: int,
    speed: str,
) -> tuple[int, int]:
    """Validate scheduler XRD-oscillation settings and return pulse targets.

    The conversion deliberately matches ``dac_oscillation``: user-entered
    degrees are rounded to Ch11 pulse positions before comparing endpoints.

    Was ``runner._validate_ch11_oscillation_settings`` (private) — moved
    here and made public so PreValidator no longer needs to import a
    private name from runner.py.
    """
    try:
        pos_a = float(pos_a_deg)
        pos_b = float(pos_b_deg)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ch11 oscillation positions must be numbers in degrees") from exc
    if not math.isfinite(pos_a) or not math.isfinite(pos_b):
        raise ValueError("Ch11 oscillation positions must be finite numbers")
    if isinstance(dwell_ms, bool) or not isinstance(dwell_ms, int) or dwell_ms < 0:
        raise ValueError("Ch11 oscillation dwell must be a non-negative integer in ms")
    if not isinstance(speed, str) or speed not in _OSC_SPEEDS:
        raise ValueError("Ch11 oscillation speed must be one of L, M, or H")

    pos_a_pulse = round(pos_a / PULSE_SCALE[11])
    pos_b_pulse = round(pos_b / PULSE_SCALE[11])
    if pos_a_pulse == pos_b_pulse:
        raise ValueError(
            "Ch11 oscillation endpoints must resolve to different pulse positions"
        )
    return pos_a_pulse, pos_b_pulse


# ---------------------------------------------------------------------------
# Global limits (Ch3/4/5 travel-from-baseline gate)
#
# Was independently re-implemented in both
# SequenceRunner._limits_for_ch()/_check_global_limits_before_move()/
# _check_global_limits() (runner.py) and
# validator.pre_validator._violates_global_limits() — same per-channel field
# lookup, same baseline-relative delta_mm formula, same ±mm threshold
# comparison. The two call sites differ only in what they *do* with the
# result: Runner stops motion, logs, and emits a Qt signal (device-I/O and
# QThread-state side effects deliberately left in runner.py, unlike the
# functions below); PreValidator turns it into a diagnostic string. Both now
# share the three pure functions below. See REORGANISATION_PLAN.md Phase 4.
# ---------------------------------------------------------------------------

def global_limits_for_channel(
    global_limits: "GlobalLimits | None", ch: int
) -> tuple[float | None, float | None] | None:
    """(minus_mm, plus_mm) bounds configured for `ch` in `global_limits`, or
    None if `global_limits` is unset or `ch` is not a Ch3/4/5 limited
    channel."""
    if global_limits is None or ch not in (3, 4, 5):
        return None
    return {
        3: (global_limits.ch3_minus_mm, global_limits.ch3_plus_mm),
        4: (global_limits.ch4_minus_mm, global_limits.ch4_plus_mm),
        5: (global_limits.ch5_minus_mm, global_limits.ch5_plus_mm),
    }[ch]


def global_limit_delta_mm(target_pos: int, baseline_pos: int, pulse_scale_um: float) -> float:
    """Displacement (mm) of `target_pos` from `baseline_pos` — the units
    GlobalLimits.ch{3,4,5}_{minus,plus}_mm are expressed in."""
    return (target_pos - baseline_pos) * pulse_scale_um / 1000.0


def exceeded_global_limit(
    delta_mm: float, minus_mm: float | None, plus_mm: float | None
) -> str | None:
    """Which bound (if any) `delta_mm` exceeds: 'plus', 'minus', or None if
    within the configured GlobalLimits range. `minus_mm`/`plus_mm` of None
    means that direction is unconfigured (not limited)."""
    if plus_mm is not None and delta_mm > plus_mm:
        return "plus"
    if minus_mm is not None and delta_mm < -minus_mm:
        return "minus"
    return None
