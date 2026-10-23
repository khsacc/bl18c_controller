"""CSV and PNG export of a FluoRaPressée ruby spectrum (Qt-free).

The NPZ written by the runner remains the complete archive; these files are
the human-readable companions: a CSV of the spectrum and fit curve with the
acquisition conditions as ``#`` header lines, and a matplotlib plot that shows
the fit and the fitted peak positions.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np


def _float_array(values) -> np.ndarray:
    if values is None:
        return np.zeros(0, dtype=float)
    return np.asarray(values, dtype=float).ravel()


def fit_details(acquired: dict) -> dict:
    """Return FRP's inner fit dictionary, or ``{}`` when no fit is available."""
    fit = acquired.get("fit")
    if not isinstance(fit, dict) or fit.get("success") is not True:
        return {}
    details = fit.get("fit")
    return details if isinstance(details, dict) else {}


def fitted_peaks(acquired: dict) -> list[dict]:
    """``[{"index", "position", "error", "width", "width_error"}, ...]`` from FRP's PeakN keys."""
    details = fit_details(acquired)
    peaks = []
    index = 1
    while f"Peak{index}" in details:
        def value(key):
            try:
                result = float(details.get(key))
            except (TypeError, ValueError):
                return None
            return result if math.isfinite(result) else None
        position = value(f"Peak{index}")
        if position is not None:
            peaks.append({
                "index": index,
                "position": position,
                "error": value(f"Peak{index}_Err"),
                "width": value(f"Width{index}"),
                "width_error": value(f"Width{index}_Err"),
            })
        index += 1
    return peaks


def _fit_curves(acquired: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fit = acquired.get("fit")
    if not isinstance(fit, dict) or fit.get("success") is not True:
        empty = np.zeros(0, dtype=float)
        return empty, empty, empty
    x_fit = _float_array(fit.get("x_fit"))
    y_fit = _float_array(fit.get("y_fit"))
    y_baseline = _float_array(fit_details(acquired).get("y_baseline"))
    if len(y_fit) != len(x_fit):
        y_fit = np.zeros(0, dtype=float)
    if len(y_baseline) != len(x_fit):
        y_baseline = np.zeros(0, dtype=float)
    return x_fit, y_fit, y_baseline


def _format_cell(value) -> str:
    return "" if value is None else repr(float(value))


def save_spectrum_csv(path: Path, acquired: dict, metadata: dict) -> None:
    """Write the spectrum, its fit curve and ``metadata`` as ``#`` header lines.

    Columns: ``x, y_raw, y, x_fit, y_fit, y_baseline``. The fit columns use
    FRP's own fit grid, which may be shorter than the spectrum when a fit
    range is set, so they are padded with empty cells.
    """
    x = _float_array(acquired.get("x"))
    y_raw = _float_array(acquired.get("y_raw"))
    y = _float_array(acquired.get("y"))
    x_fit, y_fit, y_baseline = _fit_curves(acquired)
    columns = [x, y_raw, y, x_fit, y_fit, y_baseline]
    rows = max(len(column) for column in columns)

    with open(path, "w", newline="", encoding="utf-8") as handle:
        for key, value in metadata.items():
            handle.write(f"# {key}: {value}\n")
        for peak in fitted_peaks(acquired):
            for name, key in (("position", "position"), ("position_error", "error"),
                              ("width", "width"), ("width_error", "width_error")):
                if peak[key] is not None:
                    handle.write(f"# peak{peak['index']}_{name}: {peak[key]}\n")
        writer = csv.writer(handle)
        writer.writerow(["x", "y_raw", "y", "x_fit", "y_fit", "y_baseline"])
        for row in range(rows):
            writer.writerow([
                _format_cell(column[row]) if row < len(column) else ""
                for column in columns
            ])


_FOOTER_LINE_IN = 0.17


def save_spectrum_plot(path: Path, acquired: dict, title: str,
                       info_lines: list[str],
                       footer_lines: list[str] | None = None) -> None:
    """Plot the spectrum with fit, baseline and peak positions to a PNG.

    ``footer_lines`` (e.g. the pressure calculation) are printed below the
    axes so that they never cover the peaks.

    Uses the object-oriented Agg API (no pyplot), so it is safe to call from
    the runner's worker thread.
    """
    footer_lines = list(footer_lines or [])
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    x = _float_array(acquired.get("x"))
    y = _float_array(acquired.get("y"))
    x_fit, y_fit, y_baseline = _fit_curves(acquired)
    peaks = fitted_peaks(acquired)

    footer_in = _FOOTER_LINE_IN * len(footer_lines) + 0.3 if footer_lines else 0.0
    fig = Figure(figsize=(8, 5 + footer_in), dpi=120)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    if len(x) == len(y) and len(x):
        ax.plot(x, y, color="#333333", linewidth=1.0, label="Spectrum")
    if len(y_fit):
        ax.plot(x_fit, y_fit, color="#d62728", linewidth=1.4, label="Fit")
    if len(y_baseline):
        ax.plot(x_fit, y_baseline, color="#1f77b4", linewidth=1.0,
                linestyle="--", label="Baseline")
    peak_lines = []
    for peak in peaks:
        ax.axvline(peak["position"], color="#2ca02c", linewidth=0.9, linestyle=":")
        ax.annotate(
            f"P{peak['index']}",
            xy=(peak["position"], 1.0), xycoords=("data", "axes fraction"),
            xytext=(2, -3), textcoords="offset points",
            ha="left", va="top", fontsize=8, color="#2ca02c",
        )
        error = f" ± {peak['error']:.3f}" if peak["error"] is not None else ""
        peak_lines.append(f"P{peak['index']}: {peak['position']:.3f}{error} nm")
    info_lines = peak_lines + list(info_lines)
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Intensity (counts)")
    ax.set_title(title, fontsize=9)
    ax.legend(loc="upper left", fontsize=8)
    if info_lines:
        ax.text(
            0.99, 0.80, "\n".join(info_lines), transform=ax.transAxes,
            ha="right", va="top", fontsize=8, family="monospace",
            bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85,
                  "edgecolor": "#999999"},
        )
    if footer_lines:
        footer_fraction = footer_in / (5 + footer_in)
        fig.tight_layout(rect=(0, footer_fraction, 1, 1))
        fig.text(
            0.02, footer_fraction - 0.01, "\n".join(footer_lines),
            ha="left", va="top", fontsize=8.5, family="monospace",
        )
    else:
        fig.tight_layout()
    fig.savefig(str(path))
