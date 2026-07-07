"""1-D profile fitting shared by the scan apps (scan1d / scan2d).

Peak (Gaussian) and aperture (erf top-hat) models plus a single high-level
entry point, :func:`fit_profile_1d`, that both the 1-D and 2-D scanners call so
the fit maths lives in exactly one place.
"""
from .models import gaussian, aperture_model
from .profile_fit import (
    APERTURE,
    GAUSSIAN,
    ProfileFit,
    fit_aperture_1d,
    fit_profile_1d,
)

__all__ = [
    "gaussian",
    "aperture_model",
    "APERTURE",
    "GAUSSIAN",
    "ProfileFit",
    "fit_aperture_1d",
    "fit_profile_1d",
]
