"""
Shared image-processing utilities for the Rad-icon 2022 detector.

Used by both RadiconWindow (radicon_ui.py) and SequenceRunner (exp_scheduler/runner.py).
Public API (no leading underscore) so that runner.py can import cleanly.
"""
from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# Defect map parsing
# ---------------------------------------------------------------------------

def parse_defect_file(
    path: str, binning: str, h_blank: int, width: int, height: int
) -> set:
    """Parse an XFPCAP01-format defect file and return a set of (row, col) tuples
    in acquisition-image coordinates.

    The file uses 1×1 sensor coordinates throughout.  For 2×2 binning each
    sensor coordinate is halved (integer division) before the h_blank offset is
    applied.  Out-of-bounds pixels are silently discarded.

    Supported line formats inside the $defect section:
        C,<col> <row_start>-<row_end>   — column-segment defect
        R,<row> <col_start>-<col_end>   — row-segment defect
        P,<col>,<row>                    — single-pixel defect
    """
    scale = 2 if binning == "2x2" else 1
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
                c = col_s // scale - h_blank
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
                c_lo = max(0, cs // scale - h_blank)
                c_hi = min(width - 1, ce // scale - h_blank)
                for c in range(c_lo, c_hi + 1):
                    defects.add((r, c))
            elif line.startswith("P,"):
                col_s, row_s = map(int, line[2:].split(","))
                c = col_s // scale - h_blank
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
