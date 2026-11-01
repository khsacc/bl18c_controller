"""Mask geometry and IPAnalyzer-compatible mask file handling."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def shape_mask(shape: tuple[int, int], kind: str,
               start: tuple[float, float], end: tuple[float, float]) -> np.ndarray:
    """Return a rectangle or circle including the pixels under both drag ends."""
    height, width = shape
    x0, y0 = (min(max(int(np.floor(start[0])), 0), width - 1),
              min(max(int(np.floor(start[1])), 0), height - 1))
    x1, y1 = (min(max(int(np.floor(end[0])), 0), width - 1),
              min(max(int(np.floor(end[1])), 0), height - 1))
    yy, xx = np.ogrid[:height, :width]
    if kind == "rectangle":
        return ((xx >= min(x0, x1)) & (xx <= max(x0, x1))
                & (yy >= min(y0, y1)) & (yy <= max(y0, y1)))
    if kind == "circle":
        radius2 = (x1 - x0) ** 2 + (y1 - y0) ** 2
        return (xx - x0) ** 2 + (yy - y0) ** 2 <= radius2
    raise ValueError(f"Unknown mask shape: {kind}")


def save_mask(path: Path, mask: np.ndarray) -> None:
    """Save a mask, using IPA's .mas layout only when explicitly selected."""
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("Mask must be two-dimensional")
    if path.suffix.lower() == ".mas":
        # IPAnalyzer: little-endian Int32 pixel count, then row-major bits,
        # low bit first. Its writer only emits complete groups of 8 pixels.
        bits = np.packbits(mask.ravel()[:mask.size // 8 * 8], bitorder="little")
        path.write_bytes(struct.pack("<i", mask.size) + bits.tobytes())
    else:
        with path.open("wb") as stream:
            np.savez_compressed(stream, mask=mask)


def load_mask(path: Path, shape: tuple[int, int]) -> np.ndarray:
    """Load a native or IPA mask and reject incompatible dimensions."""
    if path.suffix.lower() == ".mas":
        raw = path.read_bytes()
        if len(raw) < 4:
            raise ValueError("Mask file is too short")
        count, = struct.unpack_from("<i", raw)
        if count != int(np.prod(shape)) or len(raw) != 4 + count // 8:
            raise ValueError("IPAnalyzer mask size does not match this image")
        bits = np.unpackbits(np.frombuffer(raw[4:], dtype=np.uint8), bitorder="little")
        mask = np.zeros(count, dtype=bool)
        mask[:bits.size] = bits.astype(bool)
        return mask.reshape(shape)
    with np.load(path, allow_pickle=False) as archive:
        mask = np.asarray(archive["mask"], dtype=bool)
    if mask.shape != shape:
        raise ValueError("Mask dimensions do not match this image")
    return mask
