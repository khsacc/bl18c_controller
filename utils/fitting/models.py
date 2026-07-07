"""Pure fit-model functions shared by the scan apps (no Qt, no I/O)."""
from __future__ import annotations

import numpy as np
from scipy.special import erf


def gaussian(x, A, x0, sigma, C):
    """Gaussian peak on a flat background."""
    return A * np.exp(-0.5 * ((x - x0) / sigma) ** 2) + C


def aperture_model(x, A, x1, x2, w, bg):
    """erf-based aperture (top-hat): a rising edge at x1 and a falling edge at x2."""
    return A * (erf((x - x1) / w) - erf((x - x2) / w)) + bg
