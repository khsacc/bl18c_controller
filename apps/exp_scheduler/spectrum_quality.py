"""Pure validation and R² quality assessment for a final ruby spectrum."""
from __future__ import annotations

import math
from dataclasses import dataclass


EXPOSURE_MIN_S = 0.01
EXPOSURE_MAX_S = 5.0
MAX_EXPOSURE_HALVINGS = 3
FIT_FUNCTIONS = ("Moffat", "Pseudo Voigt", "Gauss", "Lorentz")


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
