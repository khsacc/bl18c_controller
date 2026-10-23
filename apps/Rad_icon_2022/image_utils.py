"""
Shared image-processing utilities for the Rad-icon 2022 detector.

Used by both RadiconWindow (radicon_ui.py) and SequenceRunner (exp_scheduler/runner.py).
Public API (no leading underscore) so that runner.py can import cleanly.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import cv2 as _cv2
    _TIFF_OPTS = [_cv2.IMWRITE_TIFF_COMPRESSION, 1]
except ImportError:
    _cv2 = None  # type: ignore[assignment]
    _TIFF_OPTS = []


# ---------------------------------------------------------------------------
# TIFF I/O
# ---------------------------------------------------------------------------

def save_tiff(path: "Path | str", arr: np.ndarray, metadata: dict) -> bool:
    """Save uint16 array as uncompressed TIFF with JSON metadata in Tag 270 (ImageDescription)."""
    try:
        import tifffile
        tifffile.imwrite(
            str(path), arr, metadata=metadata,
            compression=None, photometric="minisblack",
        )
        return True
    except ImportError:
        import warnings
        warnings.warn(
            "tifffile not installed; falling back to cv2 (no metadata). pip install tifffile"
        )
        if _cv2 is not None:
            return bool(_cv2.imwrite(str(path), arr, _TIFF_OPTS))
        return False
    except Exception:
        return False


def read_tiff_metadata(path: "Path | str") -> dict:
    """Read JSON metadata from TIFF Tag 270 (ImageDescription). Returns {} if unavailable."""
    try:
        import tifffile
        with tifffile.TiffFile(str(path)) as tif:
            desc = tif.pages[0].description
        if desc:
            return json.loads(desc)
    except Exception:
        pass
    return {}


def build_detector_metadata(
    image_type: str, exposure_ms: int, width: int,
    flip_v: bool, flip_h: bool, extra: dict | None = None,
) -> dict:
    """Build the detector fields shared by standalone and scheduled TIFFs."""
    metadata = {
        "image_type": image_type,
        "exposure_ms": exposure_ms,
        "binning": "2x2" if width < 2000 else "1x1",
        "flip_v": flip_v,
        "flip_h": flip_h,
        "detector": "Rad-icon 2022",
        "beamline": "BL-18C",
        "datetime": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        metadata.update(extra)
    return metadata


def build_instrument_metadata(
    controller=None, pace5000=None, lakeshore=None, *, logger_name: str = __name__,
) -> dict:
    """Read stage positions and recent cached environment values at save time."""
    meta = {}
    if controller is not None:
        for ch in (3, 4, 5, 7, 9):
            key = f"stage_ch{ch}_pulses"
            try:
                position = controller.get_ch_pos(ch)
                meta[key] = int(position) if position is not None else None
            except Exception as exc:
                logging.getLogger(logger_name).warning("Could not read %s: %s", key, exc)
                meta[key] = None

    for backend, timestamp_key, fields in (
        (pace5000, "pace5000_pressure_timestamp",
         {"pressure_mpa": "pace5000_pressure_mpa"}),
        (lakeshore, "lakeshore_temperature_timestamp", {
            "temperature_a_k": "lakeshore_t_a_k",
            "temperature_b_k": "lakeshore_t_b_k",
        }),
    ):
        if backend is None:
            continue
        values = {}
        sampled_at = None
        try:
            readings = backend.get_cached_readings()
            if readings.get("connected"):
                candidate = readings.get("timestamp")
                if (isinstance(candidate, (int, float)) and math.isfinite(candidate)
                        and 0 <= time.time() - candidate <= 10):
                    values = readings
                    sampled_at = candidate
        except Exception as exc:
            logging.getLogger(logger_name).warning("Could not read instrument cache: %s", exc)
        meta[timestamp_key] = sampled_at
        for source_key, metadata_key in fields.items():
            value = values.get(source_key)
            meta[metadata_key] = (value if isinstance(value, (int, float))
                                  and math.isfinite(value) else None)
    return meta


# ---------------------------------------------------------------------------
# Image transformations
# ---------------------------------------------------------------------------

def apply_flip(img: np.ndarray, flip_v: bool, flip_h: bool) -> np.ndarray:
    if flip_v and flip_h:
        return img[::-1, ::-1]
    if flip_v:
        return img[::-1, :]
    if flip_h:
        return img[:, ::-1]
    return img


def apply_dark_correction(img: np.ndarray, dark: np.ndarray) -> np.ndarray:
    """Subtract dark frame from image, clip to [0, 65535], return uint16."""
    if img.shape != dark.shape:
        raise ValueError(
            f"Dark image shape {dark.shape} does not match frame shape {img.shape}"
        )
    return (img.astype(np.float64) - dark).clip(0, 65535).astype(np.uint16)


def apply_defect_correction(
    img: np.ndarray, defect_mask: np.ndarray, kernel: int
) -> np.ndarray:
    """Replace each defect pixel with the median of valid (non-defect) pixels in a
    kernel×kernel neighbourhood.  Original pixel values are used as the source for
    all medians so that chains of adjacent defects do not corrupt each other."""
    result = img.copy()
    half = kernel // 2
    height, width = img.shape
    defect_rows, defect_cols = np.where(defect_mask)
    for r, c in zip(defect_rows.tolist(), defect_cols.tolist()):
        r0, r1 = max(0, r - half), min(height, r + half + 1)
        c0, c1 = max(0, c - half), min(width,  c + half + 1)
        valid = img[r0:r1, c0:c1][~defect_mask[r0:r1, c0:c1]]
        if valid.size > 0:
            result[r, c] = int(round(float(np.median(valid))))
    return result


def replace_defect_pixels(img: np.ndarray, defect_mask: np.ndarray) -> np.ndarray:
    """Mechanically replace every defect pixel with -1, reinterpreted in the
    image's dtype (65535 for uint16 — the all-ones bit pattern), as a sentinel
    value instead of median-filling."""
    result = img.copy()
    result[defect_mask] = np.array(-1).astype(img.dtype)
    return result


@dataclass(frozen=True)
class ProcessedFrame:
    image: np.ndarray
    dark_applied: bool = False
    defect_applied: bool = False
    dark_shape_mismatch: tuple[int, ...] | None = None
    defect_shape_mismatch: tuple[int, ...] | None = None


def process_frame(
    frame: np.ndarray, *, flip_v: bool, flip_h: bool,
    dark: np.ndarray | None = None, dark_enabled: bool = False,
    defect_mask: np.ndarray | None = None, defect_mode: str = "none",
    defect_kernel: int = 3, frame_already_flipped: bool = False,
) -> ProcessedFrame:
    """Apply flip, dark subtraction and defect correction in detector order.

    The dark image is already in saved (flipped) coordinates; the defect mask
    is always in raw detector coordinates and is flipped here to match.
    Size mismatches skip only the affected correction and are reported to the
    caller, which owns the user-facing warning and metadata policy.
    """
    if defect_mode not in ("none", "median", "neg1"):
        raise ValueError(f"Unknown defect correction mode: {defect_mode}")
    image = frame if frame_already_flipped else apply_flip(frame, flip_v, flip_h)
    dark_applied = False
    defect_applied = False
    dark_mismatch = None
    defect_mismatch = None

    if dark_enabled and dark is not None:
        if image.shape == dark.shape:
            image = apply_dark_correction(image, dark)
            dark_applied = True
        else:
            dark_mismatch = dark.shape

    if defect_mode != "none" and defect_mask is not None:
        mask = apply_flip(defect_mask, flip_v, flip_h)
        if image.shape == mask.shape:
            if defect_mode == "median":
                image = apply_defect_correction(image, mask, defect_kernel)
            else:
                image = replace_defect_pixels(image, mask)
            defect_applied = True
        else:
            defect_mismatch = mask.shape

    return ProcessedFrame(
        image, dark_applied, defect_applied, dark_mismatch, defect_mismatch,
    )


# ---------------------------------------------------------------------------
# Defect map parsing
# ---------------------------------------------------------------------------

# XFPCAP01.ini uses iSensorXoff=7 in 1x1 sensor pixels. Its 2x2 readout
# starts at floor(7 / 2)=3, while our backend crops h_blank=4 columns.
_XFPCAP_SENSOR_X_OFFSET = 7


def parse_defect_file(
    path: str, binning: str, h_blank: int, width: int, height: int
) -> set:
    """Parse an XFPCAP01-format defect file and return a set of (row, col) tuples
    in acquisition-image coordinates.

    The file uses 1×1 sensor coordinates throughout. XFPCAP01 applies the
    coordinates to its own cropped image, whose horizontal crop starts at
    floor(iSensorXoff / binning). Shift those coordinates to match our crop
    starting at h_blank. Out-of-bounds pixels are silently discarded.

    Supported line formats inside the $defect section:
        C,<col> <row_start>-<row_end>   — column-segment defect
        R,<row> <col_start>-<col_end>   — row-segment defect
        P,<col>,<row>                    — single-pixel defect
    """
    scale = 2 if binning == "2x2" else 1
    col_shift = _XFPCAP_SENSOR_X_OFFSET // scale - h_blank
    defects: set = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise OSError(f"欠陥ファイルを開けません: {exc}") from exc

    for line in lines:
        line = line.strip()
        if not line or line.startswith("$") or line.startswith("Sensor") \
                or line.startswith("Date"):
            continue
        try:
            if line.startswith("C,"):
                col_str, range_str = line[2:].split()
                col_s = int(col_str)
                rs, re_ = map(int, range_str.split("-"))
                c = col_s // scale + col_shift
                if not (0 <= c < width):
                    continue
                for r in range(max(0, rs // scale), min(height, re_ // scale + 1)):
                    defects.add((r, c))
            elif line.startswith("R,"):
                row_str, range_str = line[2:].split()
                row_s = int(row_str)
                cs, ce = map(int, range_str.split("-"))
                r = row_s // scale
                if not (0 <= r < height):
                    continue
                c_lo = max(0, cs // scale + col_shift)
                c_hi = min(width - 1, ce // scale + col_shift)
                for c in range(c_lo, c_hi + 1):
                    defects.add((r, c))
            elif line.startswith("P,"):
                col_s, row_s = map(int, line[2:].split(","))
                c = col_s // scale + col_shift
                r = row_s // scale
                if 0 <= c < width and 0 <= r < height:
                    defects.add((r, c))
        except (ValueError, IndexError):
            continue
    return defects


def build_defect_mask(defects: set, height: int, width: int) -> np.ndarray:
    """Convert a set of (row, col) defect positions to a boolean mask."""
    mask = np.zeros((height, width), dtype=bool)
    if defects:
        rows, cols = zip(*defects)
        mask[list(rows), list(cols)] = True
    return mask
