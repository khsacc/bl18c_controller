"""2D Scan backend — scan worker thread and GPIB reader interface.

Generalisation of ``dac_scan_backend`` / ``collimator_scan_backend``: instead
of a hard-coded axis pair (Ch4/Ch5, Ch1/Ch2), the caller picks any two
translation channels (Ch1-Ch10 — Ch11 is a rotation stage in deg/pulse and is
intentionally excluded from this generic scan).
"""
from __future__ import annotations

import time
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    from utils.stage.control_stage import PULSE_SCALE
    from settings.i18n import tr
except ImportError:
    import os, sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    from utils.stage.control_stage import PULSE_SCALE
    from settings.i18n import tr

# Ch11 is a rotation stage (deg/pulse) — not a translation axis, excluded here.
CHANNEL_CHOICES: list[int] = list(range(1, 11))

# Backlash compensation on the fast (X) axis: always approach from the
# negative side so the final motion into each measurement point is in the
# + direction. Matches the convention used by DacScanWorker / CollimatorScanWorker.
BACKLASH_PULSES_X: int = 5


def um_per_pulse(ch: int) -> float:
    return PULSE_SCALE[ch]


# ---------------------------------------------------------------------------
# GPIB reader interface (same shape as dac_scan_backend.GpibReader)
# ---------------------------------------------------------------------------

class GpibReader:
    """Interface for reading transmitted X-ray intensity over GPIB.

    Transmitted intensity : photodiode voltage (V) — post-sample X-ray.

    Base class is a no-op stub returning a safe constant value.
    """

    def set_current_position(self, x_pulse: int, y_pulse: int) -> None:
        """Notify the reader of the current stage position (in pulses)."""

    def read_transmitted(self) -> float:
        return 0.0


class GpibReaderSim(GpibReader):
    """Simulated GPIB reader for --debug / testing.

    Generates a 2D Gaussian intensity profile with a configurable peak
    offset from the scan centre and random noise. Unlike the fixed-axis
    versions in dac_scan_backend / collimator_scan_backend, the µm/pulse
    scale of each axis is passed in explicitly since the axes are
    user-selectable here.
    """

    def __init__(
        self,
        um_per_pulse_x: float,
        um_per_pulse_y: float,
        center_x_pulse: int = 0,
        center_y_pulse: int = 0,
        peak_offset_x_um: float = 5.0,
        peak_offset_y_um: float = -3.0,
        sigma_um: float = 25.0,
        noise_level: float = 0.02,
        rng: np.random.Generator | None = None,
    ):
        self._um_per_pulse_x = um_per_pulse_x
        self._um_per_pulse_y = um_per_pulse_y
        self._peak_x_pulse = center_x_pulse + round(peak_offset_x_um / um_per_pulse_x)
        self._peak_y_pulse = center_y_pulse + round(peak_offset_y_um / um_per_pulse_y)
        self._sigma_um      = sigma_um
        self._noise         = noise_level
        self._rng           = rng or np.random.default_rng()
        self._cur_x         = center_x_pulse
        self._cur_y         = center_y_pulse

    def set_current_position(self, x_pulse: int, y_pulse: int) -> None:
        self._cur_x = x_pulse
        self._cur_y = y_pulse

    def read_transmitted(self) -> float:
        dx_um  = (self._cur_x - self._peak_x_pulse) * self._um_per_pulse_x
        dy_um  = (self._cur_y - self._peak_y_pulse) * self._um_per_pulse_y
        signal = np.exp(-(dx_um ** 2 + dy_um ** 2) / (2 * self._sigma_um ** 2))
        noise  = self._rng.normal(0.0, self._noise)
        return float(np.clip(signal + noise, 0.0, 2.0))


# ---------------------------------------------------------------------------
# Scan worker
# ---------------------------------------------------------------------------

class Free2DScanWorker(QThread):
    """Background thread that executes a 2-D grid scan over two user-chosen channels.

    Generalises ``DacScanWorker``: ``ch_x`` / ``ch_y`` select which controller
    channels are driven instead of hard-coded constants. Backlash compensation
    (always + direction final approach) applies to ``ch_x`` only, matching the
    convention of the fixed-axis scan apps.

    Because the axes are user-selectable, a move can hit an inter-channel
    safety rule in ``MOVE_CONSTRAINTS`` (e.g. Ch8/Ch9 collision guard) that the
    fixed-axis scans never encounter. Any exception raised during the scan
    aborts cleanly (``scan_aborted``) with the reason reported via
    ``status_message`` instead of silently killing the thread.
    """

    point_measured = pyqtSignal(int, int, float)  # row, col, transmitted
    scan_completed = pyqtSignal()
    scan_aborted   = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(
        self,
        controller,
        gpib_reader: GpibReader,
        ch_x: int,
        ch_y: int,
        x_pulses: list[int],     # absolute ch_x pulse positions per column
        y_pulses: list[int],     # absolute ch_y pulse positions per row
        center_x: int,            # scan centre — stage returns here after scan
        center_y: int,
        speed: str = "H",
        backlash_pulses: int = BACKLASH_PULSES_X,
        settle_ms: int = 100,
        accumulation: int = 1,
        parent=None,
    ):
        super().__init__(parent)
        self.controller      = controller
        self.gpib_reader     = gpib_reader
        self.ch_x            = ch_x
        self.ch_y            = ch_y
        self.x_pulses        = x_pulses
        self.y_pulses        = y_pulses
        self.center_x        = center_x
        self.center_y        = center_y
        self.speed           = speed
        self.backlash_pulses = backlash_pulses
        self.settle_ms       = settle_ms
        self.accumulation    = max(1, int(accumulation))
        self._abort          = False

    def abort(self) -> None:
        self._abort = True

    def _approach_x(self, ctrl, target_pulse: int) -> None:
        """Move ch_x to *target_pulse* with + direction final approach."""
        ctrl.move_ch_absolute(self.ch_x, target_pulse - self.backlash_pulses)
        ctrl.wait_until_stop(stay_in_rem=True)
        ctrl.move_ch_absolute(self.ch_x, target_pulse)
        ctrl.wait_until_stop(stay_in_rem=True)

    def run(self) -> None:
        ctrl     = self.controller
        x_pulses = self.x_pulses
        y_pulses = self.y_pulses
        n_cols   = len(x_pulses)
        n_rows   = len(y_pulses)

        try:
            ctrl.set_ch_speed(self.ch_x, self.speed)
            ctrl.set_ch_speed(self.ch_y, self.speed)

            total = n_rows * n_cols
            done  = 0

            for row_idx, y_pulse in enumerate(y_pulses):
                if self._abort:
                    break

                self.status_message.emit(tr("Moving to row {row}/{total}…", row=row_idx + 1, total=n_rows))
                ctrl.move_ch_absolute(self.ch_y, y_pulse)
                ctrl.wait_until_stop(stay_in_rem=True)

                # Approach the first column from the negative side so ch_x
                # moves in the + direction for every measurement in this row.
                self._approach_x(ctrl, x_pulses[0])

                for col_idx in range(n_cols):
                    if self._abort:
                        break

                    # col_idx 0 is already at position; subsequent columns are
                    # always in the + direction — no correction needed.
                    if col_idx > 0:
                        ctrl.move_ch_absolute(self.ch_x, x_pulses[col_idx])
                        ctrl.wait_until_stop(stay_in_rem=True)

                    if self.settle_ms > 0:
                        time.sleep(self.settle_ms / 1000)

                    self.gpib_reader.set_current_position(x_pulses[col_idx], y_pulse)
                    t_vals = [self.gpib_reader.read_transmitted() for _ in range(self.accumulation)]
                    transmitted = float(np.mean(t_vals))

                    done += 1
                    self.status_message.emit(tr("Scanning: {done}/{total} points", done=done, total=total))
                    self.point_measured.emit(row_idx, col_idx, transmitted)

            if not self._abort:
                # Return to scan centre with + direction approach on ch_x
                self.status_message.emit(tr("Returning to start position…"))
                ctrl.move_ch_absolute(self.ch_y, self.center_y)
                ctrl.wait_until_stop(stay_in_rem=True)
                self._approach_x(ctrl, self.center_x)
                ctrl.switch_to_loc()
                self.scan_completed.emit()
            else:
                ctrl.switch_to_loc()
                self.scan_aborted.emit()
        except Exception as e:
            self.status_message.emit(tr("Scan error: {error}", error=e))
            try:
                ctrl.switch_to_loc()
            except Exception:
                pass
            self.scan_aborted.emit()


class Scan1DWorker(QThread):
    """Background thread that executes a 1-D grid scan over a single channel.

    A specialisation of :class:`Free2DScanWorker`'s inner scan line: one
    translation channel is stepped across a list of absolute pulse positions
    while the GPIB reader is sampled at each point. The backlash convention is
    identical — every measurement point is approached in the ``+`` direction
    (the first point from the negative side by ``backlash_pulses``) so
    mechanical backlash is removed. After the scan the stage returns to
    ``center`` with the same ``+`` approach.

    As with the 2-D worker, any exception (including a ``ValueError`` raised by
    ``move_ch_absolute`` for a ``MOVE_CONSTRAINTS`` violation) aborts cleanly via
    ``scan_aborted`` with the reason reported through ``status_message`` instead
    of silently killing the ``QThread``.
    """

    point_measured = pyqtSignal(int, float)  # col, transmitted
    scan_completed = pyqtSignal()
    scan_aborted   = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(
        self,
        controller,
        gpib_reader: GpibReader,
        ch: int,
        pulses: list[int],       # absolute ch pulse positions per point
        center: int,             # scan centre — stage returns here after scan
        speed: str = "H",
        backlash_pulses: int = BACKLASH_PULSES_X,
        settle_ms: int = 100,
        accumulation: int = 1,
        parent=None,
    ):
        super().__init__(parent)
        self.controller      = controller
        self.gpib_reader     = gpib_reader
        self.ch              = ch
        self.pulses          = pulses
        self.center          = center
        self.speed           = speed
        self.backlash_pulses = backlash_pulses
        self.settle_ms       = settle_ms
        self.accumulation    = max(1, int(accumulation))
        self._abort          = False

    def abort(self) -> None:
        self._abort = True

    def _approach(self, ctrl, target_pulse: int) -> None:
        """Move ch to *target_pulse* with a + direction final approach."""
        ctrl.move_ch_absolute(self.ch, target_pulse - self.backlash_pulses)
        ctrl.wait_until_stop(stay_in_rem=True)
        ctrl.move_ch_absolute(self.ch, target_pulse)
        ctrl.wait_until_stop(stay_in_rem=True)

    def run(self) -> None:
        ctrl   = self.controller
        pulses = self.pulses
        n      = len(pulses)

        try:
            ctrl.set_ch_speed(self.ch, self.speed)

            # Approach the first point from the negative side so ch moves in the
            # + direction for every measurement.
            self._approach(ctrl, pulses[0])

            for i in range(n):
                if self._abort:
                    break

                # i == 0 is already in position; subsequent points are always in
                # the + direction — no backlash correction needed.
                if i > 0:
                    ctrl.move_ch_absolute(self.ch, pulses[i])
                    ctrl.wait_until_stop(stay_in_rem=True)

                if self.settle_ms > 0:
                    time.sleep(self.settle_ms / 1000)

                self.gpib_reader.set_current_position(pulses[i], 0)
                t_vals = [self.gpib_reader.read_transmitted() for _ in range(self.accumulation)]
                transmitted = float(np.mean(t_vals))

                self.status_message.emit(tr("Scanning: {done}/{total} points", done=i + 1, total=n))
                self.point_measured.emit(i, transmitted)

            if not self._abort:
                self.status_message.emit(tr("Returning to start position…"))
                self._approach(ctrl, self.center)
                ctrl.switch_to_loc()
                self.scan_completed.emit()
            else:
                ctrl.switch_to_loc()
                self.scan_aborted.emit()
        except Exception as e:
            self.status_message.emit(tr("Scan error: {error}", error=e))
            try:
                ctrl.switch_to_loc()
            except Exception:
                pass
            self.scan_aborted.emit()
