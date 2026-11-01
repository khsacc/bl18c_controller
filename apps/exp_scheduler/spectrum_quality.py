"""Pure validation and R² quality assessment for a fluorescence spectrum.

Shared by the Experimental Scheduler and Sequential Spectroscopic
Measurements: ``assess_spectrum`` judges one FRP response and
``next_retry`` decides how the next attempt should gain signal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


EXPOSURE_MIN_S = 0.01
EXPOSURE_MAX_S = 5.0
MAX_EXPOSURE_HALVINGS = 3
FIT_FUNCTIONS = ("Moffat", "Pseudo Voigt", "Gauss", "Lorentz")
BASELINE_MODELS = ("constant", "linear", "quadratic", "auto_polynomial")


def next_em_gain(current: int, maximum: int, minimum: int = 1,
                 increment: int = 1) -> int | None:
    """Double towards the configured ceiling on the device's valid gain grid."""
    if increment < 1 or current < minimum or maximum <= current:
        return None
    highest_index = (maximum - minimum) // increment
    wanted = max(current + increment, current * 2)
    wanted_index = (wanted - minimum + increment - 1) // increment
    result = minimum + min(wanted_index, highest_index) * increment
    return result if result > current else None


# Kinds of RetryDecision.
RETRY_HALVE_EXPOSURE = "halve_exposure"
RETRY_RAISE_EM_GAIN = "raise_em_gain"
RETRY_ADD_ACCUMULATION = "add_accumulation"
RETRY_SATURATED_LIMIT = "saturated_limit"   # give up: still saturated
RETRY_QUALITY_LIMIT = "quality_limit"       # give up: R² still too low


@dataclass(frozen=True)
class RetryState:
    """Acquisition conditions of one attempt."""
    exposure_s: float
    accumulations: int
    em_gain: int | None      # None: EM gain unavailable, retries use accumulations
    halvings: int = 0


@dataclass(frozen=True)
class RetryDecision:
    kind: str
    state: RetryState        # conditions for the next attempt (unchanged when giving up)

    @property
    def gave_up(self) -> bool:
        return self.kind in (RETRY_SATURATED_LIMIT, RETRY_QUALITY_LIMIT)


def next_retry(state: RetryState, *, saturated: bool, max_accumulations: int,
               em_gain_ceiling: int | None = None, em_gain_minimum: int = 1,
               em_gain_increment: int = 1, max_halvings: int = MAX_EXPOSURE_HALVINGS,
               exposure_min_s: float = EXPOSURE_MIN_S) -> RetryDecision:
    """Decide the next attempt after saturation, fit failure, or low R².

    * Saturated: halve the exposure (resetting accumulations to 1), at most
      ``max_halvings`` times and never below ``exposure_min_s``.
    * Fit failure or low R²: double EM gain towards ``em_gain_ceiling`` on the device's
      gain grid; once that is exhausted, add one accumulation up to
      ``max_accumulations``.
    """
    if saturated:
        if state.halvings >= max_halvings or state.exposure_s / 2 < exposure_min_s:
            return RetryDecision(RETRY_SATURATED_LIMIT, state)
        return RetryDecision(RETRY_HALVE_EXPOSURE, RetryState(
            state.exposure_s / 2, 1, state.em_gain, state.halvings + 1))
    if state.em_gain is not None and em_gain_ceiling is not None:
        gain = next_em_gain(state.em_gain, em_gain_ceiling, em_gain_minimum, em_gain_increment)
        if gain is not None:
            return RetryDecision(RETRY_RAISE_EM_GAIN, RetryState(
                state.exposure_s, state.accumulations, gain, state.halvings))
    if state.accumulations >= max_accumulations:
        return RetryDecision(RETRY_QUALITY_LIMIT, state)
    return RetryDecision(RETRY_ADD_ACCUMULATION, RetryState(
        state.exposure_s, state.accumulations + 1, state.em_gain, state.halvings))


@dataclass(frozen=True)
class SpectrumQuality:
    saturated: bool
    fit_success: bool
    r_squared: float | None = None
    fit_error: str | None = None


def assess_spectrum(response: dict) -> SpectrumQuality:
    """Use FRP's saturation/fit status and reported R² for quality control."""
    saturated = response.get("saturated")
    if type(saturated) is not bool:
        return SpectrumQuality(False, False, fit_error="FRP saturation flag is missing or invalid")
    if saturated:
        return SpectrumQuality(True, False)
    fit = response.get("fit")
    if not isinstance(fit, dict) or fit.get("success") is not True:
        return SpectrumQuality(False, False, fit_error="FRP two-peak fit failed")
    details = fit.get("fit")
    if not isinstance(details, dict):
        return SpectrumQuality(False, False, fit_error="FRP fit details are missing")
    try:
        r_squared = float(details["R2"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return SpectrumQuality(False, False, fit_error=f"FRP fit data are invalid: {exc}")
    if not math.isfinite(r_squared):
        return SpectrumQuality(False, False, fit_error="FRP fit R² is invalid")
    return SpectrumQuality(False, True, r_squared=r_squared)
