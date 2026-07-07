"""DAC Scan (Rotation Centre) backend — scan worker thread and GPIB reader interface.

X-ray intensity reads are stub-only; real GPIB communication will be wired in later.
"""
from __future__ import annotations

import time

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

UM_PER_PULSE_CH10: float  = PULSE_SCALE[10]   # 2.0  µm/pulse
UM_PER_PULSE_CH3: float   = PULSE_SCALE[3]    # 2.0  µm/pulse
UM_PER_PULSE_CH4: float   = PULSE_SCALE[4]    # 2.0  µm/pulse
DEG_PER_PULSE_CH11: float = PULSE_SCALE[11]   # 0.004 deg/pulse

CH_ROT  = 11   # rotation stage
CH_SCAN = 10   # translation scan axis

BACKLASH_PULSES_CH10: int = 5


# ---------------------------------------------------------------------------
# GPIB reader interface
# ---------------------------------------------------------------------------

class GpibReader:
    """No-op stub — real GPIB implementation injected separately."""

    def set_theta(self, theta_deg: float) -> None:
        pass

    def set_current_position(self, scan_pulse: int) -> None:
        pass

    def read_transmitted(self) -> float:
        return 0.0

    def read_incident(self) -> float:
        return 1.0


class GpibReaderRotSim(GpibReader):
    """Simulated 1-D aperture reader for debug / testing.

    Aperture centre follows: center = A·sin(θ) + B·cos(θ) + C
    """

    def __init__(
        self,
        true_A: float = 50.0,           # pulse amplitude (X eccentricity)
        true_B: float = -30.0,          # pulse amplitude (Y eccentricity)
        true_C: float = 500.0,          # absolute Ch10 centre pulse
        aperture_half_width: float = 100.0,   # pulses
        edge_width: float = 5.0,
        noise_level: float = 0.02,
        rng: np.random.Generator | None = None,
    ):
        self._A      = true_A
        self._B      = true_B
        self._C      = true_C
        self._half_w = aperture_half_width
        self._edge_w = edge_width
        self._noise  = noise_level
        self._rng    = rng or np.random.default_rng()
        self._cur_pulse     = 0
        self._cur_theta_rad = 0.0

    def set_theta(self, theta_deg: float) -> None:
        self._cur_theta_rad = np.deg2rad(theta_deg)

    def set_current_position(self, scan_pulse: int) -> None:
        self._cur_pulse = scan_pulse

    def read_transmitted(self) -> float:
        from scipy.special import erf
        center = (self._A * np.sin(self._cur_theta_rad)
                  + self._B * np.cos(self._cur_theta_rad)
                  + self._C)
        x1 = center - self._half_w
        x2 = center + self._half_w
        x  = float(self._cur_pulse)
        signal = 0.5 * (erf((x - x1) / self._edge_w) - erf((x - x2) / self._edge_w))
        noise  = self._rng.normal(0.0, self._noise)
        return float(np.clip(signal + noise, 0.0, 2.0))

    def read_incident(self) -> float:
        noise = self._rng.normal(0.0, self._noise * 0.1)
        return float(np.clip(1.0 + noise, 0.5, 1.5))


# ---------------------------------------------------------------------------
# Scan worker
# ---------------------------------------------------------------------------

class DacScanRotWorker(QThread):
    """Background thread for rotation-centre DAC scan.

    For each theta in *theta_deg_list* (in user-supplied order):
      1. Move Ch11 to the corresponding pulse position.
      2. Scan Ch10 from *ch10_start* to *ch10_stop* in *ch10_step* increments
         with backlash compensation (approach from the − side).
      3. Emit point_measured for every Ch10 point.
      4. Emit theta_completed when the row finishes.
    """

    point_measured  = pyqtSignal(float, int, float, float)  # theta_deg, pulse_ch10, trans, inc
    theta_completed = pyqtSignal(float)                      # theta_deg
    scan_completed  = pyqtSignal()
    scan_aborted    = pyqtSignal()
    status_message  = pyqtSignal(str)

    def __init__(
        self,
        controller,
        gpib_reader: GpibReader,
        theta_deg_list: list[float],
        ch10_start: int,
        ch10_stop: int,
        ch10_step: int,
        speed: str = "M",
        backlash_pulses: int = BACKLASH_PULSES_CH10,
        settle_ms: int = 100,
        parent=None,
    ):
        super().__init__(parent)
        self.controller      = controller
        self.gpib_reader     = gpib_reader
        self.theta_deg_list  = list(theta_deg_list)
        self.ch10_start      = ch10_start
        self.ch10_stop       = ch10_stop
        self.ch10_step       = ch10_step
        self.speed           = speed
        self.backlash_pulses = backlash_pulses
        self.settle_ms       = settle_ms
        self._abort          = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        ctrl        = self.controller
        ch10_pulses = list(range(self.ch10_start, self.ch10_stop + 1, self.ch10_step))
        n_theta     = len(self.theta_deg_list)
        n_scan      = len(ch10_pulses)

        ctrl.set_ch_speed(CH_ROT,  self.speed)
        ctrl.set_ch_speed(CH_SCAN, self.speed)

        for t_idx, theta_deg in enumerate(self.theta_deg_list):
            if self._abort:
                break

            # 1. Move rotation stage to target angle
            rot_pulse = round(theta_deg / DEG_PER_PULSE_CH11)
            self.status_message.emit(
                f"θ = {theta_deg:.1f}°  ({t_idx + 1}/{n_theta}): moving Ch11…"
            )
            ctrl.move_ch_absolute(CH_ROT, rot_pulse)
            ctrl.wait_until_stop(stay_in_rem=True)

            if self._abort:
                break

            # Notify sim reader of current theta
            self.gpib_reader.set_theta(theta_deg)

            # 2. Backlash compensation: overshoot to the − side first
            ctrl.move_ch_absolute(CH_SCAN, ch10_pulses[0] - self.backlash_pulses)
            ctrl.wait_until_stop(stay_in_rem=True)

            # 3. Scan Ch10 in the + direction
            for s_idx, pulse in enumerate(ch10_pulses):
                if self._abort:
                    break

                ctrl.move_ch_absolute(CH_SCAN, pulse)
                ctrl.wait_until_stop(stay_in_rem=True)

                if self.settle_ms > 0:
                    time.sleep(self.settle_ms / 1000)

                self.gpib_reader.set_current_position(pulse)
                transmitted = self.gpib_reader.read_transmitted()
                incident    = self.gpib_reader.read_incident()

                self.status_message.emit(
                    f"θ = {theta_deg:.1f}°  Ch10 {s_idx + 1}/{n_scan} pts"
                )
                self.point_measured.emit(float(theta_deg), pulse, transmitted, incident)

            if not self._abort:
                self.theta_completed.emit(float(theta_deg))

        ctrl.switch_to_loc()
        if self._abort:
            self.scan_aborted.emit()
        else:
            self.scan_completed.emit()
