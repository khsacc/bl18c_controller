"""High-level 1-D profile fitting used by scan1d / scan2d.

:func:`fit_profile_1d` fits a sampled profile with either a Gaussian peak or an
erf aperture (top-hat) model and returns a :class:`ProfileFit` carrying the
fitted centre, a width, and a ready-to-plot fine-grid curve in the profile's own
orientation.  The model is selected by name using the same strings the UI combo
boxes show (:data:`GAUSSIAN` / :data:`APERTURE`).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit

from .models import aperture_model, gaussian

# UI combo-box labels — accepted by fit_profile_1d(..., model=...)
GAUSSIAN = "Gaussian"
APERTURE = "Aperture (erf)"


@dataclass
class ProfileFit:
    """Result of a 1-D profile fit.

    ``center`` / ``width`` are in the same units as the ``x`` passed to
    :func:`fit_profile_1d` (pulses, in the scan apps).  ``curve_x`` / ``curve_y``
    are a fine-grid sampling of the fitted model for plotting, already restored
    to the profile's original orientation (the aperture fit may flip internally).
    """

    model: str          # "gaussian" | "aperture"
    center: float
    width: float        # sigma (gaussian) or aperture width (erf)
    width_kind: str     # "σ" | "width" — short label hint for the UI
    popt: np.ndarray
    curve_x: np.ndarray
    curve_y: np.ndarray


def fit_aperture_1d(
    xp: np.ndarray, profile: np.ndarray
) -> tuple[float, float, np.ndarray, bool]:
    """Fit the erf aperture (top-hat) model to a 1-D profile.

    Returns ``(center_rel, aperture_width, popt, was_flipped)``.  ``was_flipped``
    is True when the raw profile was inverted before fitting so the caller can
    reconstruct the fit curve in the original coordinate space.
    """
    x = np.asarray(xp, dtype=float)
    y = np.where(np.isnan(profile), np.nanmean(profile), profile).astype(float)

    was_flipped = bool(
        np.nanmean(y[: max(1, len(y) // 4)])
        > np.nanmean(y[len(y) // 4 : 3 * len(y) // 4])
    )
    if was_flipped:
        y = float(np.nanmax(y)) - y

    A0 = float(np.nanmax(y) - np.nanmin(y))
    thr = float(np.nanmin(y) + 0.5 * A0)
    idx = np.where(y > thr)[0]
    x1_0 = float(x[idx[0]]) if len(idx) > 0 else float(x[len(x) // 4])
    x2_0 = float(x[idx[-1]]) if len(idx) > 0 else float(x[3 * len(x) // 4])
    w0 = max(abs(x2_0 - x1_0) * 0.1, (x[-1] - x[0]) / max(len(x), 1), 0.1)

    p0 = [A0, x1_0, x2_0, w0, float(np.nanmin(y))]
    popt, _ = curve_fit(aperture_model, x, y, p0=p0, maxfev=10_000)
    _, x1, x2, _, _ = popt
    center = (float(x1) + float(x2)) / 2.0
    width = abs(float(x2) - float(x1))
    return center, width, popt, was_flipped


def fit_profile_1d(
    x: np.ndarray, profile: np.ndarray, model: str, n_fine: int = 300
) -> ProfileFit | None:
    """Fit *profile* sampled at *x* with *model* (``GAUSSIAN`` or ``APERTURE``).

    Returns a :class:`ProfileFit`, or ``None`` if the fit fails.  Callers are
    expected to have stripped NaNs from *profile*/*x* where a partial scan may
    leave gaps (the aperture fit tolerates NaNs; the Gaussian fit does not).
    """
    x = np.asarray(x, dtype=float)
    profile = np.asarray(profile, dtype=float)
    if x.size < 2:
        return None
    x_fine = np.linspace(x[0], x[-1], n_fine)

    if model == GAUSSIAN:
        try:
            p0 = [
                float(np.nanmax(profile) - np.nanmin(profile)),
                float(x[int(np.nanargmax(profile))]),
                (x[-1] - x[0]) / 4.0,
                float(np.nanmin(profile)),
            ]
            popt, _ = curve_fit(gaussian, x, profile, p0=p0, maxfev=10_000)
        except Exception:
            return None
        return ProfileFit(
            model="gaussian",
            center=float(popt[1]),
            width=abs(float(popt[2])),
            width_kind="σ",
            popt=popt,
            curve_x=x_fine,
            curve_y=gaussian(x_fine, *popt),
        )

    # Aperture (erf)
    try:
        center, width, popt, flipped = fit_aperture_1d(x, profile)
    except Exception:
        return None
    curve_y = aperture_model(x_fine, *popt)
    if flipped:
        curve_y = float(np.nanmax(profile)) - curve_y
    return ProfileFit(
        model="aperture",
        center=center,
        width=width,
        width_kind="width",
        popt=popt,
        curve_x=x_fine,
        curve_y=curve_y,
    )
