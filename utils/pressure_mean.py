"""Mean of several pressure-marker readings with its propagated uncertainty.

FluoRaPressée (``POST /acquire/pressure``) returns each pressure error split
into two parts whose quadrature sum is ``pressure_err_gpa``:

* ``pressure_err_peak_gpa`` — from the fitted peak position; independent
  between spectra;
* ``pressure_err_scale_gpa`` — from the pressure-scale parameters; shared by
  every reading calculated on the same scale, so it does not average down.

For the arithmetic mean P̄ = ΣPᵢ / n of n readings:

    σ_peak(P̄)  = √(Σᵢ σ_peak,i²) / n
    σ_scale(P̄) = √(Σ_g (Σ_{i∈g} σ_scale,i)²) / n      g: readings on one scale
    σ(P̄)       = √(σ_peak(P̄)² + σ_scale(P̄)²)

Treating the scale parts of one scale as fully correlated is exact for a
single-parameter scale and an upper bound for a scale with several independent
parameters (the per-reading part is already their quadrature sum). Different
scales are independent. The scatter between readings (e.g. a pressure
gradient across the cell) is not part of this uncertainty; report it
separately as a standard deviation.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

METHOD = (
    "arithmetic mean of valid points; err_peak = sqrt(sum(err_peak_i^2)) / n "
    "(independent); err_scale = sqrt(sum over scales of (sum(err_scale_i))^2) / n "
    "(fully correlated within one pressure scale); err = sqrt(err_peak^2 + err_scale^2)"
)


@dataclass(frozen=True)
class PressureReading:
    pressure_gpa: float
    err_peak_gpa: float | None
    err_scale_gpa: float | None
    scale: str            # readings with the same key share the scale-parameter error


@dataclass(frozen=True)
class PressureMean:
    mean_gpa: float
    err_gpa: float | None         # None when any reading lacks either error part
    count: int
    err_peak_gpa: float | None = None
    err_scale_gpa: float | None = None


def _error(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def error_parts(response: dict) -> tuple[float | None, float | None]:
    """``(peak, scale)`` error parts of an ``/acquire/pressure`` response.

    Both must be finite, non-negative JSON numbers; otherwise (e.g. an FRP
    version without the split) ``(None, None)`` so the error of a mean that
    uses this reading stays blank.
    """
    parts = []
    for key in ("pressure_err_peak_gpa", "pressure_err_scale_gpa"):
        value = response.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, None
        value = float(value)
        if not math.isfinite(value) or value < 0.0:
            return None, None
        parts.append(value)
    return parts[0], parts[1]


def mean_pressure(readings: Iterable[PressureReading]) -> PressureMean | None:
    """Mean and propagated uncertainty; ``None`` without any reading."""
    readings = list(readings)
    if not readings:
        return None
    n = len(readings)
    mean = math.fsum(r.pressure_gpa for r in readings) / n
    peaks = [_error(r.err_peak_gpa) for r in readings]
    scales = [_error(r.err_scale_gpa) for r in readings]
    if any(e is None for e in (*peaks, *scales)):
        return PressureMean(mean, None, n)
    by_scale: dict[str, float] = {}
    for reading, scale_err in zip(readings, scales):
        by_scale[reading.scale] = by_scale.get(reading.scale, 0.0) + scale_err
    err_peak = math.sqrt(math.fsum(e * e for e in peaks)) / n
    err_scale = math.sqrt(math.fsum(s * s for s in by_scale.values())) / n
    return PressureMean(mean, math.hypot(err_peak, err_scale), n, err_peak, err_scale)
