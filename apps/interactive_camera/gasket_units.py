"""Gasket-tracking constants and pixel → Ch4/Ch5 micrometre conversion.

Shared by Interactive Camera and the Experimental Scheduler. Kept free of
OpenCV and Qt so that settings and safety-rule modules can import it.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from utils.stage.control_stage import PULSE_SCALE


CALIBRATION_PATH = Path(__file__).parent / "calibration.json"

# Ch7 can change during gasket tracking without moving the registered beam.
GASKET_BEAM_CHANNELS = (6, 8)

# Origin of recorded ruby Ch4/Ch5 offsets for each tracking method.
RUBY_ORIGIN_XRD_POSITION = "xrd_position"      # image-similarity tracking
RUBY_ORIGIN_GASKET_CENTRE = "gasket_centre"    # gasket centre on the X-ray beam


def ruby_origin_for(tracking_method: str) -> str:
    return (RUBY_ORIGIN_GASKET_CENTRE if tracking_method == "gasket_beta"
            else RUBY_ORIGIN_XRD_POSITION)


def stage_um_per_pixel(matrix_inv) -> np.ndarray:
    """Return the 2×2 mapping from camera pixels to Ch4/Ch5 micrometres."""
    matrix = np.asarray(matrix_inv, dtype=float)
    if (matrix.shape != (2, 2) or not np.isfinite(matrix).all()
            or abs(np.linalg.det(matrix)) < 1e-12):
        raise ValueError("Camera calibration matrix_inv must be a finite, invertible 2×2 matrix")
    scaled = np.diag([PULSE_SCALE[4], PULSE_SCALE[5]]) @ matrix
    if not np.isfinite(scaled).all():
        raise ValueError("Camera calibration produces a non-finite µm displacement")
    return scaled


def load_stage_um_per_pixel(path: Path = CALIBRATION_PATH) -> np.ndarray:
    data = json.loads(path.read_text(encoding="utf-8"))
    return stage_um_per_pixel(data["matrix_inv"])


def displacement_um(matrix_um: np.ndarray, dx_px: float, dy_px: float) -> tuple[float, float]:
    vector = matrix_um @ np.array([dx_px, dy_px], dtype=float)
    return float(vector[0]), float(vector[1])


def distance_um(matrix_um: np.ndarray, dx_px: float, dy_px: float) -> float:
    return math.hypot(*displacement_um(matrix_um, dx_px, dy_px))


def nominal_um_per_pixel(matrix_um: np.ndarray) -> float:
    """RMS axis scale for orientation-free detector residuals and jitter bounds."""
    return float(np.linalg.norm(matrix_um) / math.sqrt(2.0))


def legacy_tolerance_um(tolerance_px: float, matrix_um: np.ndarray) -> float:
    """Conservatively migrate a pixel circle to a physical-distance limit."""
    return float(tolerance_px * np.linalg.svd(matrix_um, compute_uv=False)[-1])
