"""Qt-independent, experimental tracking of a user-marked gasket aperture.

Only raw camera frames belong here.  Display marks and information bands must
never be included in either the reference or the live frame.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class GasketEllipse:
    cx: float
    cy: float
    rx: float
    ry: float

    @classmethod
    def from_dict(cls, value: dict) -> "GasketEllipse":
        ellipse = cls(*(float(value[key]) for key in ("cx", "cy", "rx", "ry")))
        if (not all(np.isfinite(v) for v in (ellipse.cx, ellipse.cy, ellipse.rx, ellipse.ry))
                or min(ellipse.rx, ellipse.ry) < 20):
            raise ValueError("Gasket ellipse must have finite coordinates and radii of at least 20 px")
        return ellipse

    def to_dict(self) -> dict[str, float]:
        return {"cx": self.cx, "cy": self.cy, "rx": self.rx, "ry": self.ry}


@dataclass(frozen=True)
class GasketDetection:
    ellipse: GasketEllipse
    score: float
    coverage: int
    residual_px: float


def reference_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _gradient(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    elif frame.ndim == 2:
        gray = frame
    else:
        raise ValueError("Expected an 8-bit grey or BGR camera image")
    if gray.dtype != np.uint8:
        raise ValueError("Expected an 8-bit camera image")
    smooth = cv2.GaussianBlur(gray, (9, 9), 2.0)
    gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _outline_points(gradient: np.ndarray, ellipse: GasketEllipse):
    """Find the strongest local edge along each outward ellipse normal."""
    h, w = gradient.shape
    angles = np.linspace(0, 2 * np.pi, 96, endpoint=False)
    ca, sa = np.cos(angles), np.sin(angles)
    x0 = ellipse.cx + ellipse.rx * ca
    y0 = ellipse.cy + ellipse.ry * sa
    nx, ny = ca / ellipse.rx, sa / ellipse.ry
    norm = np.hypot(nx, ny)
    nx, ny = nx / norm, ny / norm
    band = max(4.0, min(14.0, min(ellipse.rx, ellipse.ry) * 0.07))
    offsets = np.linspace(-band, band, 11)
    xx = x0[:, None] + nx[:, None] * offsets
    yy = y0[:, None] + ny[:, None] * offsets
    valid = (xx >= 2) & (xx < w - 2) & (yy >= 2) & (yy < h - 2)
    values = cv2.remap(gradient, xx.astype(np.float32), yy.astype(np.float32),
                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    values[~valid] = -1
    chosen = values.argmax(axis=1)
    strength = values[np.arange(len(angles)), chosen]
    points = np.column_stack((xx[np.arange(len(angles)), chosen],
                              yy[np.arange(len(angles)), chosen]))
    return points, strength, valid.any(axis=1)


def detect_gasket_hole(
    reference: np.ndarray, current: np.ndarray, marked: GasketEllipse,
    *, max_shift_px: int | None = None,
    expected_centre: tuple[float, float] | None = None,
) -> GasketDetection:
    """Track a marked outer rim and reject weak or geometrically inconsistent fits.

    The annular gradient template is insensitive to the sign of the image
    contrast. The fitted ellipse is a diagnostic as well as the motion source.
    No best-effort centre is returned when evidence is insufficient.

    The search covers ±max_shift_px around ``expected_centre`` (default: the
    marked centre). The template is always taken from the marked location in
    the reference, so only the search window moves.
    """
    if reference.shape != current.shape:
        raise ValueError("Live camera image size differs from the gasket reference image")
    h, w = reference.shape[:2]
    if (not all(np.isfinite(v) for v in (marked.cx, marked.cy, marked.rx, marked.ry))
            or min(marked.rx, marked.ry) < 20):
        raise ValueError("Gasket ellipse must have finite coordinates and radii of at least 20 px")
    if not (0 <= marked.cx < w and 0 <= marked.cy < h):
        raise ValueError("Marked gasket centre is outside the reference image")
    ref_grad = _gradient(reference)
    cur_grad = _gradient(current)
    radius = min(marked.rx, marked.ry)
    shift = int(max_shift_px if max_shift_px is not None else min(60, max(12, radius * 0.3)))
    pad = max(12, int(radius * 0.14))
    left = max(0, int(marked.cx - marked.rx - pad))
    top = max(0, int(marked.cy - marked.ry - pad))
    right = min(w, int(marked.cx + marked.rx + pad + 1))
    bottom = min(h, int(marked.cy + marked.ry + pad + 1))
    if right - left < 30 or bottom - top < 30:
        raise ValueError("Marked gasket region is too small")
    yy, xx = np.ogrid[top:bottom, left:right]
    radial = np.sqrt(((xx - marked.cx) / marked.rx) ** 2
                     + ((yy - marked.cy) / marked.ry) ** 2)
    mask = (np.abs(radial - 1) <= 0.09).astype(np.uint8)
    if np.count_nonzero(mask) < 100:
        raise ValueError("Marked gasket outline has too little visible area")
    template = ref_grad[top:bottom, left:right]
    ref_energy = float(np.mean(template[mask > 0]))
    if ref_energy < 2.0:
        raise ValueError("Reference gasket outline is too faint to track")
    base_dx = base_dy = 0
    if expected_centre is not None:
        ex, ey = (float(v) for v in expected_centre)
        if not (np.isfinite(ex) and np.isfinite(ey)):
            raise ValueError("Expected gasket centre is not finite")
        base_dx, base_dy = int(round(ex - marked.cx)), int(round(ey - marked.cy))
        if abs(base_dx) >= w or abs(base_dy) >= h:
            raise ValueError("Expected gasket centre is outside the camera image")
    # Pad the current gradient, allowing a partially clipped aperture to move
    # without changing the coordinate system of the template match.
    border = shift + max(abs(base_dx), abs(base_dy))
    search = cv2.copyMakeBorder(cur_grad, border, border, border, border,
                                cv2.BORDER_CONSTANT, value=0)
    y0 = top + base_dy + border - shift
    x0 = left + base_dx + border - shift
    search = search[y0:y0 + (bottom - top) + 2 * shift, x0:x0 + (right - left) + 2 * shift]
    scores = cv2.matchTemplate(search, template, cv2.TM_CCORR_NORMED, mask=mask)
    scores = np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)
    _, score, _, peak = cv2.minMaxLoc(scores)
    if score < 0.48:
        raise ValueError(f"Gasket outline match is weak ({score:.2f})")
    dx, dy = peak[0] - shift + base_dx, peak[1] - shift + base_dy
    predicted = GasketEllipse(marked.cx + dx, marked.cy + dy, marked.rx, marked.ry)
    points, strength, visible = _outline_points(cur_grad, predicted)
    if not np.any(visible):
        raise ValueError("Gasket outline is outside the camera image")
    # Measure support relative to the median edge strength in this image. A
    # texture-rich specimen can still yield candidate points, so coverage and
    # fit geometry are checked separately below.
    _, inner, _ = _outline_points(
        cur_grad, GasketEllipse(predicted.cx, predicted.cy,
                                predicted.rx * 0.82, predicted.ry * 0.82)
    )
    _, outer, _ = _outline_points(
        cur_grad, GasketEllipse(predicted.cx, predicted.cy,
                                predicted.rx * 1.18, predicted.ry * 1.18)
    )
    local_background = np.maximum(0, (inner + outer) / 2)
    noise = float(np.median(cur_grad))
    strong = visible & (strength >= np.maximum(3.0, np.maximum(
        noise * 1.8, local_background * 1.35
    )))
    sectors = np.array_split(np.arange(len(strong)), 8)
    coverage = sum(np.count_nonzero(strong[s]) >= 5 for s in sectors)
    if coverage < 5 or np.count_nonzero(strong) < 45:
        raise ValueError(f"Gasket outline is visible in only {coverage}/8 sectors")
    candidates = points[strong].astype(np.float32)
    if len(candidates) < 5:
        raise ValueError("Too few gasket edge points")
    fit = cv2.fitEllipse(candidates)
    (cx, cy), (diameter_a, diameter_b), _angle = fit
    # A free ellipse fit to a short arc is poorly constrained. Keep the marked
    # size as a strong prior; rotation is deliberately excluded in this beta.
    fitted = GasketEllipse(float(cx), float(cy), float(diameter_a / 2), float(diameter_b / 2))
    if (abs(fitted.cx - predicted.cx) > radius * 0.1
            or abs(fitted.cy - predicted.cy) > radius * 0.1):
        raise ValueError("Gasket ellipse fit disagrees with the outline match")
    # OpenCV may swap ellipse axes and rotate it. Use the tracked outline for
    # display/motion until rotated fits have their own validation.
    offsets = np.sqrt(((candidates[:, 0] - predicted.cx) / predicted.rx) ** 2
                      + ((candidates[:, 1] - predicted.cy) / predicted.ry) ** 2)
    residual = float(np.median(np.abs(offsets - 1)) * radius)
    if residual > max(5.0, radius * 0.05):
        raise ValueError(f"Gasket outline fit residual is too large ({residual:.1f} px)")
    if not np.isfinite(score):
        raise ValueError("Gasket outline match is not finite")
    return GasketDetection(predicted, float(score), coverage, residual)


def detect_gasket_hole_near(
    reference: np.ndarray, current: np.ndarray, marked: GasketEllipse,
    expected_centres,
) -> GasketDetection:
    """Try each expected centre in turn and return the first accepted fit.

    Every candidate passes the same rejection checks, so a fallback window
    never relaxes the evidence required. Raises the first failure if none fit.
    """
    first_error: ValueError | None = None
    tried = set()
    for centre in expected_centres:
        key = None if centre is None else (round(centre[0]), round(centre[1]))
        if key in tried:
            continue
        tried.add(key)
        try:
            return detect_gasket_hole(reference, current, marked, expected_centre=centre)
        except ValueError as exc:
            first_error = first_error or exc
    raise first_error or ValueError("No expected gasket centre was given")


def sample_offset(sample_point: dict, reference_centre: tuple[float, float]) -> tuple[float, float]:
    """Return the marked sample centre relative to the detected reference rim."""
    x, y = float(sample_point["x"]), float(sample_point["y"])
    if not (np.isfinite(x) and np.isfinite(y)):
        raise ValueError("Marked sample centre is not finite")
    return x - reference_centre[0], y - reference_centre[1]
