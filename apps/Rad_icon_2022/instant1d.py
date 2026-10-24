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


def bins_for_width(ai, shape: tuple[int, ...], bin_width_deg: float, *,
                   strict: bool = False) -> int:
    """Match Rad-icon's full-detector angular bin calculation."""
    if not 0.001 <= bin_width_deg <= 1.0:
        raise ValueError("1D angular resolution must be between 0.001 and 1 degree/bin")
    try:
        tth = ai.center_array(shape, unit="2th_deg", scale=True)
        span = float(np.nanmax(tth) - np.nanmin(tth))
        if not np.isfinite(span) or span <= 0:
            raise ValueError("Cannot determine the detector's 2θ range from the calibration.")
        return max(10, int(np.ceil(span / bin_width_deg)))
    except Exception:
        if strict:
            raise
        return 2000


def reduce_frame(ai, image: np.ndarray, bin_width_deg: float, *, npt: int | None = None):
    """Integrate an already corrected frame with full pixel splitting."""
    if npt is None:
        npt = bins_for_width(ai, image.shape, bin_width_deg)
    return ai.integrate1d(
        image.astype(np.float32), npt=npt, unit="2th_deg",
        method=("full", "histogram", "cython"), correctSolidAngle=True,
        polarization_factor=0.95,
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
