"""Per-attempt ruby spectrum results recorded beside saved spectra."""
from __future__ import annotations

import csv
import math
from pathlib import Path

from .xrd_master import FIELDS as XRD_FIELDS, condition_columns


MASTER_FILENAME = "ruby_master_all_files.csv"
FIELDS = [
    "start_time", "end_time", "spectrum_file", "ruby_name", "attempt",
    *[field for field in XRD_FIELDS if field not in ("start_time", "end_time", "xrd_file")],
    "quality_outcome", "fit_r_squared",
    "peak1_nm", "peak1_error_nm", "peak2_nm", "peak2_error_nm",
    "pressure_status", "pressure_scale", "pressure_GPa", "pressure_error_GPa",
    "temperature_scale", "temperature_source", "temperature_used_K",
    "lambda_0_nm", "t0_K",
    "lambda_0_corrected_nm",
]


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _fit_peak(details: dict, number: int) -> tuple[float | None, float | None]:
    """Use FRP's ordered peaks list (also used for pressure), then PeakN."""
    position = error = None
    peaks = details.get("peaks")
    if isinstance(peaks, list) and len(peaks) >= number and isinstance(peaks[number - 1], dict):
        peak = peaks[number - 1]
        position = _finite(peak.get("position"))
        error = _finite(peak.get("position_err"))
    if position is None:
        position = _finite(details.get(f"Peak{number}"))
    if error is None:
        error = _finite(details.get(f"Peak{number}_Err"))
    return position, error


def append_ruby_record(
    spectrum_path: Path, start_time: str, end_time: str,
    start: dict, end: dict, *, ruby_name: str, attempt: int,
    outcome: str, acquired: dict, r_squared: float | None,
    pressure_record=None,
) -> Path:
    """Append a saved acquisition; reject an existing incompatible CSV."""
    master_path = spectrum_path.parent / MASTER_FILENAME
    if master_path.exists() and master_path.stat().st_size:
        with master_path.open("r", newline="", encoding="utf-8-sig") as handle:
            if next(csv.reader(handle), None) != FIELDS:
                raise ValueError(f"Ruby master CSV has incompatible columns: {master_path}")

    row = ruby_record_columns(
        spectrum_path, start_time, end_time, start, end,
        ruby_name=ruby_name, attempt=attempt, outcome=outcome,
        acquired=acquired, r_squared=r_squared, pressure_record=pressure_record,
    )
    new_file = not master_path.exists() or master_path.stat().st_size == 0
    with master_path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({field: "" if row.get(field) is None else row[field]
                         for field in FIELDS})
    return master_path


def ruby_record_columns(
    spectrum_path: Path, start_time: str, end_time: str,
    start: dict, end: dict, *, ruby_name: str, attempt: int,
    outcome: str, acquired: dict, r_squared: float | None,
    pressure_record=None,
) -> dict:
    """Build the same columns for the attempt and sequence master files."""
    fit = acquired.get("fit") if isinstance(acquired.get("fit"), dict) else {}
    details = (
        fit["fit"] if fit.get("success") is True and isinstance(fit.get("fit"), dict)
        else {}
    )
    peak1, peak1_error = _fit_peak(details, 1)
    peak2, peak2_error = _fit_peak(details, 2)
    row = {
        "start_time": start_time,
        "end_time": end_time,
        "spectrum_file": spectrum_path.name,
        "ruby_name": ruby_name,
        "attempt": attempt,
        **condition_columns(start, end),
        "quality_outcome": outcome,
        "fit_r_squared": _finite(r_squared),
        "peak1_nm": peak1,
        "peak1_error_nm": peak1_error,
        "peak2_nm": peak2,
        "peak2_error_nm": peak2_error,
    }
    if pressure_record is not None:
        row.update({
            "pressure_status": pressure_record.status,
            "pressure_scale": pressure_record.pressure_scale,
            "pressure_GPa": pressure_record.pressure_gpa,
            "pressure_error_GPa": pressure_record.pressure_err_gpa,
            "lambda_0_nm": pressure_record.lambda_0_nm,
        })
        if pressure_record.temperature_correction_enabled:
            row.update({
                "temperature_scale": pressure_record.temperature_scale,
                "temperature_source": pressure_record.temperature_source,
                "temperature_used_K": pressure_record.measured_temperature_k,
                "t0_K": pressure_record.t0_k,
                "lambda_0_corrected_nm": pressure_record.zero_pressure_peak_at_current_t_nm,
            })

    return row
