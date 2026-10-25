"""Per-image XRD conditions recorded alongside saved detector images."""
from __future__ import annotations

import csv
import math
from pathlib import Path


MASTER_FILENAME = "xrd_master.csv"
FIELDS = [
    "start_time", "end_time", "xrd_file",
    "Ch3_pulse", "Ch4_pulse", "Ch5_pulse", "Ch7_pulse", "Ch9_pulse",
    "pressure_start_MPa", "pressure_end_MPa",
    "temperature_a_start_K", "temperature_b_start_K",
    "temperature_a_end_K", "temperature_b_end_K",
    "temperature_a_mean_K", "temperature_b_mean_K",
]


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def capture_conditions(ctx, *, stage: bool = False) -> dict:
    """Read positions and cached instrument values without new PACE/GPIB I/O."""
    values = {}
    if stage and ctx.controller is not None:
        for ch in (3, 4, 5, 7, 9):
            try:
                values[f"Ch{ch}_pulse"] = ctx.controller.get_ch_pos(ch)
            except Exception:
                values[f"Ch{ch}_pulse"] = None

    if ctx.pace5000 is not None:
        try:
            readings = ctx.pace5000.get_cached_readings()
            if readings.get("connected"):
                values["pressure_MPa"] = _finite(readings.get("pressure_mpa"))
        except Exception:
            pass

    if ctx.lakeshore is not None:
        try:
            readings = ctx.lakeshore.get_cached_readings()
            if readings.get("connected"):
                values["temperature_a_K"] = _finite(readings.get("temperature_a_k"))
                values["temperature_b_K"] = _finite(readings.get("temperature_b_k"))
        except Exception:
            pass
    return values


def append_xrd_record(
    image_path: Path, start_time: str, end_time: str,
    start: dict, end: dict,
) -> Path:
    """Append a completed saved image; reject an existing incompatible CSV."""
    master_path = image_path.parent / MASTER_FILENAME
    if master_path.exists() and master_path.stat().st_size:
        with master_path.open("r", newline="", encoding="utf-8-sig") as handle:
            if next(csv.reader(handle), None) != FIELDS:
                raise ValueError(f"XRD master CSV has incompatible columns: {master_path}")

    row = {
        "start_time": start_time,
        "end_time": end_time,
        "xrd_file": image_path.name,
        **condition_columns(start, end),
    }

    new_file = not master_path.exists() or master_path.stat().st_size == 0
    with master_path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({key: "" if value is None else value for key, value in row.items()})
    return master_path


def condition_columns(start: dict, end: dict) -> dict:
    """Shared stage and environment columns for XRD and ruby masters."""
    row = {}
    for ch in (3, 4, 5, 7, 9):
        row[f"Ch{ch}_pulse"] = start.get(f"Ch{ch}_pulse")
    row["pressure_start_MPa"] = start.get("pressure_MPa")
    row["pressure_end_MPa"] = end.get("pressure_MPa")
    for axis in ("a", "b"):
        first = start.get(f"temperature_{axis}_K")
        last = end.get(f"temperature_{axis}_K")
        row[f"temperature_{axis}_start_K"] = first
        row[f"temperature_{axis}_end_K"] = last
        row[f"temperature_{axis}_mean_K"] = (
            (first + last) / 2 if first is not None and last is not None else None
        )
    return row
