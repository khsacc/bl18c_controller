"""Pure validation and quality metrics for a final ruby spectrum."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


EXPOSURE_MIN_S = 0.01
EXPOSURE_MAX_S = 5.0
MAX_EXPOSURE_HALVINGS = 3
FIT_FUNCTIONS = ("Moffat", "Pseudo Voigt", "Gauss", "Lorentz")


@dataclass(frozen=True)
class SpectrumQuality:
    saturated: bool
    fit_success: bool
    snr: float | None = None
    r_squared: float | None = None
    fit_error: str | None = None


def assess_spectrum(response: dict) -> SpectrumQuality:
    """Use FRP's saturation/fit status and measure peak S/N from fit residuals.

    The maximum fitted peak amplitude is compared with 1.4826 times the MAD
    of the residuals. A perfectly fitted, nonzero peak has infinite S/N.
    Missing or malformed fit data is a fit failure, not a passing result.
    """
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
        y = np.asarray(response["y"], dtype=float)
        y_fit = np.asarray(fit["y_fit"], dtype=float)
        peaks = details["peaks"]
        amplitudes = [float(peak["amplitude"]) for peak in peaks]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return SpectrumQuality(False, False, fit_error=f"FRP fit data are invalid: {exc}")
    if (not math.isfinite(r_squared) or y.ndim != 1 or y.size < 3
            or y_fit.shape != y.shape or not np.all(np.isfinite(y_fit))
            or len(amplitudes) != 2 or not all(math.isfinite(a) for a in amplitudes)
            or max(amplitudes) <= 0):
        return SpectrumQuality(False, False, fit_error="FRP fit data are invalid or incomplete")
    residual = y - y_fit
    centre = float(np.median(residual))
    noise = 1.4826 * float(np.median(np.abs(residual - centre)))
    if not math.isfinite(noise):
        return SpectrumQuality(False, False, fit_error="FRP fit residuals are invalid")
    if noise == 0:
        noise = float(np.std(residual))
    snr = max(amplitudes) / noise if noise > 0 else math.inf
    return SpectrumQuality(False, True, snr=snr, r_squared=r_squared)
