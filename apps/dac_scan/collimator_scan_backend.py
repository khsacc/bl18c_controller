"""Collimator Scan backend — scan worker thread and GPIB reader interface.

Same structure as DAC Scan (Normal) but scans Ch1 (X) and Ch2 (Y)
— the collimator translation axes.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    from utils.control_stage import PULSE_SCALE
except ImportError:
    import os, sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    from utils.control_stage import PULSE_SCALE

UM_PER_PULSE_CH1: float = PULSE_SCALE[1]   # 1.0  µm/pulse  Ch1
UM_PER_PULSE_CH2: float = PULSE_SCALE[2]   # 2.0  µm/pulse  Ch2

CH_X = 1
CH_Y = 2

# Ch1 backlash compensation: always approach from the negative side.
BACKLASH_PULSES_CH1: int = 5    # 5 pulses = 5 µm


# ---------------------------------------------------------------------------
# GPIB reader interface (shared with DAC Scan)
# ---------------------------------------------------------------------------

class GpibReader:
    """Interface for reading X-ray intensities over GPIB."""

    def set_current_position(self, x_pulse: int, y_pulse: int) -> None:
        pass

    def read_transmitted(self) -> float:
        return 0.0

    def read_incident(self) -> float:
        return 1.0


class GpibReaderSim(GpibReader):
    """Simulated GPIB reader for --debug / testing (Ch1/Ch2 scales)."""

    def __init__(
        self,
        center_x_pulse: int = 0,
        center_y_pulse: int = 0,
        peak_offset_x_um: float = 5.0,
        peak_offset_y_um: float = -3.0,
        sigma_um: float = 25.0,
        noise_level: float = 0.02,
        rng: np.random.Generator | None = None,
    ):
        self._peak_x_pulse = center_x_pulse + round(peak_offset_x_um / UM_PER_PULSE_CH1)
        self._peak_y_pulse = center_y_pulse + round(peak_offset_y_um / UM_PER_PULSE_CH2)
        self._sigma_um     = sigma_um
        self._noise        = noise_level
        self._rng          = rng or np.random.default_rng()
        self._cur_x        = center_x_pulse
        self._cur_y        = center_y_pulse

    def set_current_position(self, x_pulse: int, y_pulse: int) -> None:
        self._cur_x = x_pulse
        self._cur_y = y_pulse

    def read_transmitted(self) -> float:
        dx_um  = (self._cur_x - self._peak_x_pulse) * UM_PER_PULSE_CH1
        dy_um  = (self._cur_y - self._peak_y_pulse) * UM_PER_PULSE_CH2
        signal = np.exp(-(dx_um ** 2 + dy_um ** 2) / (2 * self._sigma_um ** 2))
        noise  = self._rng.normal(0.0, self._noise)
        return float(np.clip(signal + noise, 0.0, 2.0))

    def read_incident(self) -> float:
        noise = self._rng.normal(0.0, self._noise * 0.1)
        return float(np.clip(1.0 + noise, 0.5, 1.5))


# ---------------------------------------------------------------------------
# Scan worker
# ---------------------------------------------------------------------------

class CollimatorScanWorker(QThread):
    """Background thread that executes a 2-D grid scan over Ch1 (X) and Ch2 (Y).

    Ch1 backlash compensation: always approaches the first column from the
    negative side so each row's measurements are in the + direction.
    """

    point_measured = pyqtSignal(int, int, float, float)  # row, col, transmitted, incident
    scan_completed = pyqtSignal()
    scan_aborted   = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(
        self,
        controller,
        gpib_reader: GpibReader,
        x_pulses: list[int],
        y_pulses: list[int],
        center_x: int,
        center_y: int,
        speed: str = "H",
        backlash_pulses: int = BACKLASH_PULSES_CH1,
        accumulation: int = 1,
        parent=None,
    ):
        super().__init__(parent)
        self.controller      = controller
        self.gpib_reader     = gpib_reader
        self.x_pulses        = x_pulses
        self.y_pulses        = y_pulses
        self.center_x        = center_x
        self.center_y        = center_y
        self.speed           = speed
        self.backlash_pulses = backlash_pulses
        self.accumulation    = max(1, int(accumulation))
        self._abort          = False

    def abort(self) -> None:
        self._abort = True

    def _approach_ch1(self, ctrl, target_pulse: int) -> None:
        ctrl.move_ch_absolute(CH_X, target_pulse - self.backlash_pulses)
        ctrl.wait_until_stop(stay_in_rem=True)
        ctrl.move_ch_absolute(CH_X, target_pulse)
        ctrl.wait_until_stop(stay_in_rem=True)

    def run(self) -> None:
        ctrl     = self.controller
        x_pulses = self.x_pulses
        y_pulses = self.y_pulses
        n_cols   = len(x_pulses)
        n_rows   = len(y_pulses)

        ctrl.set_ch_speed(CH_X, self.speed)
        ctrl.set_ch_speed(CH_Y, self.speed)

        total = n_rows * n_cols
        done  = 0

        for row_idx, y_pulse in enumerate(y_pulses):
            if self._abort:
                break

            self.status_message.emit(f"Moving to row {row_idx + 1}/{n_rows}…")
            ctrl.move_ch_absolute(CH_Y, y_pulse)
            ctrl.wait_until_stop(stay_in_rem=True)

            self._approach_ch1(ctrl, x_pulses[0])

            for col_idx in range(n_cols):
                if self._abort:
                    break

                if col_idx > 0:
                    ctrl.move_ch_absolute(CH_X, x_pulses[col_idx])
                    ctrl.wait_until_stop(stay_in_rem=True)

                self.gpib_reader.set_current_position(x_pulses[col_idx], y_pulse)
                t_vals = [self.gpib_reader.read_transmitted() for _ in range(self.accumulation)]
                i_vals = [self.gpib_reader.read_incident()    for _ in range(self.accumulation)]
                transmitted = float(np.mean(t_vals))
                incident    = float(np.mean(i_vals))

                done += 1
                self.status_message.emit(f"Scanning: {done}/{total} points")
                self.point_measured.emit(row_idx, col_idx, transmitted, incident)

        if not self._abort:
            self.status_message.emit("Returning to start position…")
            ctrl.move_ch_absolute(CH_Y, self.center_y)
            ctrl.wait_until_stop(stay_in_rem=True)
            self._approach_ch1(ctrl, self.center_x)
            ctrl.switch_to_loc()
            self.scan_completed.emit()
        else:
            ctrl.switch_to_loc()
            self.scan_aborted.emit()
