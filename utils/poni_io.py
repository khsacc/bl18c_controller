"""Shared pyFAI .poni file I/O helpers.

New code should import from here rather than re-implementing parse/build/write.
apps/xrd_scan/xrd_scan_backend.py and settings/pages/detector_calibration.py
still carry their own copies (pre-dating this module) — migrate them here if
you touch those files, per apps/calibrate_instruments/SPEC.md.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

try:
    import pyFAI.detectors as pf_detectors
    from pyFAI.integrator.azimuthal import AzimuthalIntegrator
except ImportError:
    pf_detectors = None
    AzimuthalIntegrator = object  # type: ignore[misc,assignment]


def parse_poni(path: pathlib.Path) -> dict:
    """Parse a pyFAI .poni file (UTF-8 safe; bypasses pyFAI's locale-dependent reader)."""
    result: dict = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key   = key.strip().lower()
        value = value.strip()
        if key == "detector_config":
            result[key] = json.loads(value)
        else:
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
    return result


def build_ai(poni_dict: dict) -> "AzimuthalIntegrator":
    dc  = poni_dict.get("detector_config", {})
    det = pf_detectors.Detector(
        pixel1=float(dc["pixel1"]),
        pixel2=float(dc["pixel2"]),
    )
    return AzimuthalIntegrator(
        dist=poni_dict["distance"],
        poni1=poni_dict["poni1"],
        poni2=poni_dict["poni2"],
        rot1=poni_dict["rot1"],
        rot2=poni_dict["rot2"],
        rot3=poni_dict["rot3"],
        wavelength=poni_dict["wavelength"],
        detector=det,
    )


def write_poni(ai, path: pathlib.Path, comments: list[str] | None = None) -> None:
    """Write an AzimuthalIntegrator's geometry as a pyFAI v2 poni file."""
    pixel1 = float(ai.detector.pixel1)
    pixel2 = float(ai.detector.pixel2)
    det_cfg = json.dumps({"pixel1": pixel1, "pixel2": pixel2})
    lines = [f"# pyFAI poni file - written {datetime.now().isoformat(timespec='seconds')}"]
    lines += list(comments or [])
    lines += [
        "poni_version: 2",
        "Detector: Flat",
        f"Detector_config: {det_cfg}",
        f"Distance: {ai.dist:.12e}",
        f"Poni1: {ai.poni1:.12e}",
        f"Poni2: {ai.poni2:.12e}",
        f"Rot1: {ai.rot1:.12e}",
        f"Rot2: {ai.rot2:.12e}",
        f"Rot3: {ai.rot3:.12e}",
        f"Wavelength: {ai.wavelength:.12e}",
    ]
    pathlib.Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
