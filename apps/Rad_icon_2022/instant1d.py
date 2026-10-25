"""Shared Rad-icon 2022 one-dimensional XRD reduction and export.

The detector window and Experimental Scheduler pass the same corrected image
and calibrated pyFAI integrator here; no hardware or Qt objects are accessed.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


UNIT_INFO = {
    "2theta": ("2theta", "2theta_deg"),
    "q_A": ("q_A-1", "q_invA"),
    "q_nm": ("q_nm-1", "q_invnm"),
    "d_A": ("d_A", "d_A"),
    "d_nm": ("d_nm", "d_nm"),
}


def find_spot_mask(ai, image: np.ndarray, deviation_factor: float) -> np.ndarray:
    """Port IPAnalyzer Ring.FindSpots' radial test and 3-pixel spot expansion.

    Based on Crystallography/Detector/Ring.cs in Yusuke Seto's IPAnalyzer
    (MIT licence, Copyright (c) 2005-2026 Yusuke SETO; see
    IPANALYZER_LICENSE.md). pyFAI supplies the
    calibrated 2θ at each pixel centre; tan(2θ) * distance / horizontal pixel
    pitch is IPAnalyzer's perpendicular-plane radius in pixel units.
    """
    if image.ndim != 2:
        raise ValueError("Spot finding requires a 2D detector image")
    if not np.isfinite(deviation_factor) or not 0 <= deviation_factor <= 10:
        raise ValueError("Spot deviation factor must be between 0 and 10")
    pixel_width = float(ai.detector.pixel2)
    if not np.isfinite(pixel_width) or pixel_width <= 0:
        raise ValueError("Detector horizontal pixel size must be positive")
    if not np.isfinite(ai.dist) or ai.dist <= 0:
        raise ValueError("Detector distance must be positive")

    tth = np.asarray(ai.center_array(image.shape, unit="2th_deg", scale=True))
    if tth.shape != image.shape:
        raise ValueError("Calibration angle array does not match detector image")
    valid = np.isfinite(image) & np.isfinite(tth) & (tth >= 0) & (tth < 90)
    detector_mask = getattr(ai.detector, "mask", None)
    mask = np.zeros(image.shape, dtype=bool)
    if detector_mask is not None:
        detector_mask = np.asarray(detector_mask, dtype=bool)
        if detector_mask.shape != image.shape:
            raise ValueError("Detector mask does not match detector image")
        valid &= ~detector_mask
        mask |= detector_mask
    if not np.any(valid):
        return mask

    # IPAnalyzer assigns a pixel to (ushort)(radius / PixSizeX + 0.5).
    radius = np.tan(np.deg2rad(tth[valid])) * ai.dist / pixel_width
    if not np.all(np.isfinite(radius)) or np.any(radius >= 65535):
        raise ValueError("Calibration gives an invalid spot-finding radius")
    rings = np.zeros(image.shape, dtype=np.int32)
    rings[valid] = np.floor(radius + 0.5).astype(np.int32)
    indices = rings[valid]
    values = np.asarray(image[valid], dtype=np.float64)
    counts = np.bincount(indices)
    sums = np.bincount(indices, weights=values)
    squares = np.bincount(indices, weights=values * values)
    eligible = counts >= 2
    upper = np.full(len(counts), np.inf)
    variance = np.maximum(
        (counts[eligible] * squares[eligible] - sums[eligible] ** 2)
        / (counts[eligible] * (counts[eligible] - 1)), 0.0,
    )
    upper[eligible] = sums[eligible] / counts[eligible] + deviation_factor * np.sqrt(variance)
    candidates = np.zeros(image.shape, dtype=bool)
    candidates[valid] = values > upper[indices]

    # Retain IPAnalyzer's row-major behaviour: a pixel covered by an earlier
    # spot is not itself used as a new centre. Use true 2D bounds so a spot at
    # the edge of one row cannot spill into the next row.
    height, width = image.shape
    offsets = [(dy, dx) for dy in range(-2, 3) for dx in range(-2, 3)
               if dx * dx + dy * dy < 9]
    for flat_index in np.flatnonzero(candidates):
        y, x = divmod(int(flat_index), width)
        if mask[y, x]:
            continue
        for dy, dx in offsets:
            yy, xx = y + dy, x + dx
            if 0 <= yy < height and 0 <= xx < width:
                mask[yy, xx] = True
    return mask


def convert_radial(tth_deg: np.ndarray, wavelength_m: float, unit: str) -> np.ndarray:
    """Convert the pyFAI 2θ axis using the project's Q/d convention."""
    if unit == "2theta":
        return tth_deg
    lam_a = wavelength_m * 1e10
    with np.errstate(divide="ignore", invalid="ignore"):
        theta = np.deg2rad(tth_deg / 2)
        if unit == "q_A":
            return 4 * np.pi * np.sin(theta) / lam_a
        if unit == "q_nm":
            return 4 * np.pi * np.sin(theta) / (lam_a / 10)
        if unit == "d_A":
            return lam_a / (2 * np.sin(theta))
        if unit == "d_nm":
            return (lam_a / 10) / (2 * np.sin(theta))
    raise ValueError(f"unknown radial unit: {unit!r}")


def angular_range(ai, shape: tuple[int, ...], min_2theta: float | None = None,
                  max_2theta: float | None = None) -> tuple[float, float] | None:
    """Return the requested 2θ interval, filling Auto ends from the detector."""
    if min_2theta is None and max_2theta is None:
        return None
    for value in (min_2theta, max_2theta):
        if value is not None and (not np.isfinite(value) or not 0 <= value <= 180):
            raise ValueError("2θ limits must be between 0 and 180 degrees")
    tth = np.asarray(ai.center_array(shape, unit="2th_deg", scale=True))
    if tth.shape != shape or not np.any(np.isfinite(tth)):
        raise ValueError("Cannot determine the detector's 2θ range from the calibration")
    detector_angles = tth[np.isfinite(tth)]
    detector_min = float(np.min(detector_angles))
    detector_max = float(np.max(detector_angles))
    lo = detector_min if min_2theta is None else min_2theta
    hi = detector_max if max_2theta is None else max_2theta
    if lo >= hi:
        raise ValueError("Minimum 2θ must be less than maximum 2θ")
    if lo > detector_max or hi < detector_min:
        raise ValueError("The selected 2θ range does not overlap the detector")
    return lo, hi


def excluded_angle_mask(ai, shape: tuple[int, ...], min_2theta: float | None,
                        max_2theta: float | None) -> np.ndarray | None:
    """Pixels outside the selected 2θ interval, for preview display only."""
    if min_2theta is None and max_2theta is None:
        return None
    angular_range(ai, shape, min_2theta, max_2theta)
    tth = np.asarray(ai.center_array(shape, unit="2th_deg", scale=True))
    mask = ~np.isfinite(tth)
    if min_2theta is not None:
        mask |= tth < min_2theta
    if max_2theta is not None:
        mask |= tth > max_2theta
    return mask


def bins_for_width(ai, shape: tuple[int, ...], bin_width_deg: float, *,
                   strict: bool = False, min_2theta: float | None = None,
                   max_2theta: float | None = None) -> int:
    """Match Rad-icon's full-detector angular bin calculation."""
    if not 0.001 <= bin_width_deg <= 1.0:
        raise ValueError("1D angular resolution must be between 0.001 and 1 degree/bin")
    try:
        selected = angular_range(ai, shape, min_2theta, max_2theta)
        if selected is None:
            tth = ai.center_array(shape, unit="2th_deg", scale=True)
            span = float(np.nanmax(tth) - np.nanmin(tth))
        else:
            span = selected[1] - selected[0]
        if not np.isfinite(span) or span <= 0:
            raise ValueError("Cannot determine the detector's 2θ range from the calibration.")
        return max(10, int(np.ceil(span / bin_width_deg)))
    except Exception:
        if strict or min_2theta is not None or max_2theta is not None:
            raise
        return 2000


def reduce_frame(ai, image: np.ndarray, bin_width_deg: float, *, npt: int | None = None,
                 find_spots: bool = False, spot_deviation_factor: float = 5.0,
                 min_2theta: float | None = None, max_2theta: float | None = None):
    """Integrate an already corrected frame with full pixel splitting."""
    selected = angular_range(ai, image.shape, min_2theta, max_2theta)
    if npt is None:
        npt = bins_for_width(ai, image.shape, bin_width_deg,
                             min_2theta=min_2theta, max_2theta=max_2theta)
    mask = find_spot_mask(ai, image, spot_deviation_factor) if find_spots else None
    options = {"radial_range": selected} if selected is not None else {}
    return ai.integrate1d(
        image.astype(np.float32), npt=npt, unit="2th_deg", **options,
        method=("full", "histogram", "cython"), correctSolidAngle=True,
        polarization_factor=0.95, mask=mask,
    )


def write_gsas_fxye(path: Path, tth_deg: np.ndarray, intensity: np.ndarray,
                    title: str, comments: list[str], bank: int = 1) -> None:
    """Write GSAS FXYE with 2θ in centidegrees and sqrt(I) ESD."""
    x_cd = np.asarray(tth_deg, dtype=float) * 100.0
    y = np.asarray(intensity, dtype=float)
    e = np.where(y > 0, np.sqrt(y), 1.0)
    n = len(x_cd)
    step_cd = (x_cd[1] - x_cd[0]) if n > 1 else 1.0
    lines = [title[:80], *(f"# {comment}" for comment in comments)]
    lines.append(f"BANK {bank} {n} {n} CONST {x_cd[0]:.5f} {step_cd:.5f} 0 0 FXYE")
    for xi, yi, ei in zip(x_cd, y, e):
        record = f"{xi:15.5f}{yi:15.5f}{ei:15.5f}"
        lines.append(f"{record:<80}")
    with open(path, "w", newline="\r\n") as stream:
        stream.write("\n".join(lines) + "\n")


def write_histogram_igor(path: str, tth_deg, intensity, esd=None,
                         wave_names: tuple[str, ...] = ("twotheta", "yint", "yerr"),
                         newline: str = "\n") -> str:
    """Write a Z-Rietveld Igor Text pattern with 2θ in degrees."""
    x = np.asarray(tth_deg, dtype=float)
    y = np.asarray(intensity)
    if x.shape != y.shape:
        raise ValueError(f"tth_deg and intensity length mismatch: {x.shape} vs {y.shape}")
    errors = None if esd is None else np.asarray(esd, dtype=float)
    if errors is not None and errors.shape != x.shape:
        raise ValueError(f"esd length mismatch: {errors.shape} vs {x.shape}")
    names = list(wave_names[:3 if errors is not None else 2])
    if any(not name.isidentifier() for name in names):
        raise ValueError(f"invalid Igor wave name: {names!r}")
    is_int = np.issubdtype(y.dtype, np.integer)
    lines = ["IGOR", f"WAVES/O {', '.join(names)}", "BEGIN"]
    for index in range(x.size):
        row = [f"{x[index]:.8g}", str(int(y[index])) if is_int else f"{float(y[index]):.8g}"]
        if errors is not None:
            row.append(f"{errors[index]:.8g}")
        lines.append("\t".join(row))
    lines.append("END")
    with open(path, "w", newline="", encoding="ascii") as stream:
        stream.write(newline.join(lines) + newline)
    return str(path)


def save_profile(tth_deg: np.ndarray, intensity: np.ndarray,
                 wavelength_m: float, dist_m: float, image_path: Path, *, formats: tuple[str, ...],
                 units: tuple[str, ...], exposure_ms: int, binning: str,
                 source: str = "Rad-icon 2022",
                 ) -> tuple[list[Path], list[str]]:
    """Save selected 1D formats beside a TIFF; return paths and write errors."""
    saved: list[Path] = []
    errors: list[str] = []

    def write(path: Path, callback) -> None:
        try:
            callback()
            saved.append(path)
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    for unit in units:
        if unit not in UNIT_INFO:
            errors.append(f"Unknown 1D axis unit: {unit}")
            continue
        suffix, header = UNIT_INFO[unit]
        if "csv" in formats or "tsv" in formats:
            x = convert_radial(tth_deg, wavelength_m, unit)
            for ext, delim in (("csv", ","), ("tsv", "\t")):
                if ext in formats:
                    path = image_path.with_name(f"{image_path.stem}_{suffix}.{ext}")
                    write(path, lambda p=path, d=delim, h=header, values=x: np.savetxt(
                        str(p), np.column_stack([values, intensity]),
                        delimiter=d, header=f"{h}{d}intensity", comments="",
                    ))
    if "gsas" in formats:
        path = image_path.with_suffix(".gsa")
        comments = [
            f"wavelength = {wavelength_m * 1e10:.6f} A",
            f"detector distance = {dist_m * 1e3:.3f} mm",
            f"{source} exposure = {exposure_ms} ms, binning {binning}",
        ]
        write(path, lambda: write_gsas_fxye(path, tth_deg, intensity,
                                            title=path.name, comments=comments))
    if "igor" in formats:
        path = image_path.with_suffix(".histogramIgor")
        write(path, lambda: write_histogram_igor(str(path), tth_deg, intensity))
    return saved, errors
