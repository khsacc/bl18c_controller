"""One row per take_spectrum call, using only accepted ruby attempts."""
from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

from .ruby_master import FIELDS as ATTEMPT_FIELDS


MASTER_FILENAME = "ruby_master_summary.csv"
SUMMARY_FIELDS = [
    "sequence_start_time", "sequence_end_time", "step_index",
    "sequence_status", "ruby_count", "pressure_valid_count",
    "pressure_mean_GPa", "pressure_sd_GPa", "peak1_mean_nm",
    "peak2_mean_nm", "temperature_used_mean_K",
]


def fields(position_count: int) -> list[str]:
    """Place all summary columns before the repeated ruby columns."""
    columns = list(SUMMARY_FIELDS)
    for index in range(1, position_count + 1):
        prefix = f"ruby_{index:02d}_"
        columns.extend(prefix + ("name" if field == "ruby_name" else field)
                       for field in ATTEMPT_FIELDS)
    return columns


def _mean(rows: list[dict], field: str) -> float | None:
    values = []
    for row in rows:
        try:
            value = float(row.get(field))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            values.append(value)
    return statistics.fmean(values) if values else None


def append_ruby_sequence_record(
    directory: Path, *, position_names: tuple[str, ...],
    start_time: str, end_time: str, step_index: int, status: str,
    accepted: list[dict],
) -> Path:
    """Append one action result, rejecting an incompatible existing header."""
    master_path = directory / MASTER_FILENAME
    columns = fields(len(position_names))
    if master_path.exists() and master_path.stat().st_size:
        with master_path.open("r", newline="", encoding="utf-8-sig") as handle:
            if next(csv.reader(handle), None) != columns:
                raise ValueError(f"Ruby sequence master CSV has incompatible columns: {master_path}")

    by_name = {row["ruby_name"]: row for row in accepted}
    if len(by_name) != len(accepted) or set(by_name) - set(position_names):
        raise ValueError("Ruby sequence results do not match the registered positions")
    if any(row.get("quality_outcome") != "passed" for row in accepted):
        raise ValueError("Ruby sequence master accepts only quality-passed attempts")
    pressures = []
    for row in accepted:
        if row.get("pressure_status") == "valid":
            try:
                value = float(row.get("pressure_GPa"))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                pressures.append(value)
    row = {
        "sequence_start_time": start_time,
        "sequence_end_time": end_time,
        "step_index": step_index,
        "sequence_status": status,
        "ruby_count": len(accepted),
        "pressure_valid_count": len(pressures),
        "pressure_mean_GPa": statistics.fmean(pressures) if pressures else None,
        "pressure_sd_GPa": statistics.stdev(pressures) if len(pressures) >= 2 else None,
        "peak1_mean_nm": _mean(accepted, "peak1_nm"),
        "peak2_mean_nm": _mean(accepted, "peak2_nm"),
        "temperature_used_mean_K": _mean(accepted, "temperature_used_K"),
    }
    for index, name in enumerate(position_names, start=1):
        prefix = f"ruby_{index:02d}_"
        row[prefix + "name"] = name
        attempt = by_name.get(name)
        if attempt is not None:
            row.update({prefix + ("name" if field == "ruby_name" else field): attempt.get(field)
                        for field in ATTEMPT_FIELDS})

    new_file = not master_path.exists() or master_path.stat().st_size == 0
    with master_path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if new_file:
            writer.writeheader()
        writer.writerow({field: "" if row.get(field) is None else row[field]
                         for field in columns})
    return master_path
