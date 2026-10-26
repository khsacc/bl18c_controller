"""Pulse-aligned nearby-search step controls for ruby measurements."""

from PyQt6.QtWidgets import QSpinBox

from utils.stage.control_stage import PULSE_SCALE


def make_step_spin(ch: int) -> QSpinBox:
    """Edit an integer pulse count so the resulting µm step is always reachable."""
    spin = QSpinBox()
    spin.setObjectName(f"ruby_search_step_ch{ch}")
    spin.wheelEvent = lambda event: event.ignore()
    spin.setRange(1, int(100.0 / PULSE_SCALE[ch]))
    spin.setSuffix(" pulses")
    spin.setToolTip(f"1 pulse = {PULSE_SCALE[ch]:g} µm on Ch{ch}")
    set_step_um(spin, ch, 2.0)
    return spin


def step_um(spin: QSpinBox, ch: int) -> float:
    return round(spin.value() * PULSE_SCALE[ch], 2)


def set_step_um(spin: QSpinBox, ch: int, value: float) -> None:
    """Snap legacy saved values to the nearest reachable pulse count."""
    spin.setValue(max(1, round(value / PULSE_SCALE[ch])))
