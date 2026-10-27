"""Pure bounds checks for ruby-guided PACE5000 pressure changes."""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

from apps.PACE5000.pace5000_backend import MIN_SLEW_RATE_MPA_PER_SEC

from .actions import CompressToTargetPressureUsingRubyAction, DecompressToTargetPressureUsingRubyAction

RubyGuidedAction = CompressToTargetPressureUsingRubyAction | DecompressToTargetPressureUsingRubyAction

UNIT_TO_MPA = {"MPa": 1.0, "Bar": 0.1}
RATE_TO_MPA_PER_MIN = {"MPa/min": 1.0, "Bar/min": 0.1}


def numeric_field(value: object, name: str) -> float:
    """Preserve numeric-string compatibility without accepting bool as 0/1."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a boolean")
    return float(value)


def resolve_target_pressure(
    action: RubyGuidedAction, variables: Mapping[str, object],
) -> RubyGuidedAction:
    """Resolve a loop target without changing the saved action."""
    target = action.target_sample_pressure_gpa
    if not isinstance(target, str):
        return action
    if target not in variables:
        raise ValueError(f"Target sample pressure loop variable {target!r} is not defined")
    return replace(action, target_sample_pressure_gpa=variables[target])


def parameters_error(action: RubyGuidedAction) -> str | None:
    decompress = isinstance(action, DecompressToTargetPressureUsingRubyAction)
    direction = "decompression" if decompress else "compression"
    if action.all_positions:
        return f"Choose one registered ruby position for {direction}"
    if action.stop_pressure_target_gpa is not None:
        return f"The ruby sequence-stop option cannot be used during {direction}"
    if action.gas_pressure_unit not in UNIT_TO_MPA:
        return "Gas pressure unit must be MPa or Bar"
    if action.gas_pressure_rate_unit not in RATE_TO_MPA_PER_MIN:
        return "Gas pressure rate unit must be MPa/min or Bar/min"
    values = (
        ("Target sample pressure", action.target_sample_pressure_gpa, False),
        ("Gas pressure increment", action.gas_pressure_increment, False),
        ("Pressure stabilisation duration", action.stabilisation_duration_s, True),
        (("Minimum" if decompress else "Maximum") + " allowed gas pressure",
         action.min_allowed_gas_pressure if decompress else action.max_allowed_gas_pressure,
         decompress),
        ("Gas pressure tolerance", action.gas_pressure_tolerance, False),
    )
    if action.gas_pressure_rate is not None:
        values += (("Gas pressure rate", action.gas_pressure_rate, False),)
    for label, value, allow_zero in values:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (value == 0 and not allow_zero)):
            return f"{label} must be finite and {'non-negative' if allow_zero else 'positive'}"
    if (action.gas_pressure_tolerance
            * UNIT_TO_MPA[action.gas_pressure_unit] < 0.0001):
        return "Gas pressure arrival tolerance must be at least 0.0001 MPa"
    if action.gas_pressure_tolerance >= action.gas_pressure_increment:
        return "Gas pressure arrival tolerance must be smaller than each increment"
    if (action.gas_pressure_rate is not None
            and action.gas_pressure_rate
            * RATE_TO_MPA_PER_MIN[action.gas_pressure_rate_unit] / 60
            < MIN_SLEW_RATE_MPA_PER_SEC):
        return ("Gas pressure rate is below the PACE5000 minimum slew rate "
                f"({MIN_SLEW_RATE_MPA_PER_SEC:g} MPa/s)")
    return None


def next_setpoint_mpa(
    current_mpa: float, increment_mpa: float, maximum_mpa: float,
) -> float | None:
    """Cap the final increment; never issue a setpoint above the operator limit."""
    if current_mpa >= maximum_mpa - 1e-9:
        return None
    return min(current_mpa + increment_mpa, maximum_mpa)


def previous_setpoint_mpa(
    current_mpa: float, decrement_mpa: float, minimum_mpa: float,
) -> float | None:
    """Cap the final decrement; never issue a setpoint below the operator limit."""
    if current_mpa <= minimum_mpa + 1e-9:
        return None
    return max(current_mpa - decrement_mpa, minimum_mpa)
