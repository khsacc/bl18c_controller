"""Shared input definitions for XRD 1D reduction panels."""
from __future__ import annotations

from PyQt6.QtWidgets import QDoubleSpinBox


FORMAT_LABELS = (
    ("csv", "CSV"),
    ("tsv", "TSV"),
    ("gsas", "GSAS (.gsa)"),
    ("igor", "Z-Rietveld (.histogramIgor)"),
)
UNIT_LABELS = (
    ("2theta", "2θ (deg)"),
    ("q_A", "Q (Å⁻¹)"),
    ("q_nm", "Q (nm⁻¹)"),
    ("d_A", "d (Å)"),
    ("d_nm", "d (nm)"),
)


def bin_width_spin(value: float) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.wheelEvent = lambda event: event.ignore()
    spin.setRange(0.001, 1.0)
    spin.setDecimals(3)
    spin.setSingleStep(0.001)
    spin.setKeyboardTracking(False)
    spin.setValue(value)
    return spin


def angle_limit_spin(value: float | None = None) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.wheelEvent = lambda event: event.ignore()
    spin.setRange(-1.0, 180.0)
    spin.setDecimals(3)
    spin.setSingleStep(0.1)
    spin.setSpecialValueText("Auto")
    spin.setKeyboardTracking(False)
    spin.setValue(-1.0 if value is None else value)
    return spin
