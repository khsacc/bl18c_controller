"""
LakeShore 335 temperature controller backend (PyQt6 edition).

Communicates with the LakeShore 335 via GPIB using pyvisa (SCPI commands).
The class inherits from QObject and emits Qt signals so that PyQt6 UIs can
react to new data without polling.

Signals
-------
data_updated()
    Emitted from the polling thread after each successful measurement.
    Cross-thread delivery is handled automatically by Qt's queued-connection
    mechanism.
error_occurred(str)
    Emitted when a polling error is caught.
"""
from __future__ import annotations

import csv
import random
import threading
import time
from collections import deque
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal

MAX_DATA_POINTS = 7200   # 2 hours at 1 Hz
POLL_INTERVAL   = 1.0    # seconds

DEFAULT_GPIB_ADDRESS = "GPIB0::12::INSTR"


class DataPoint:
    __slots__ = ("timestamp", "temp_a_k", "temp_b_k", "eff_setpoint_k", "heater_range_idx")

    def __init__(self, timestamp: float, temp_a_k: float, temp_b_k: float,
                 eff_setpoint_k: float, heater_range_idx: int) -> None:
        self.timestamp        = timestamp
        self.temp_a_k         = temp_a_k
        self.temp_b_k         = temp_b_k
        self.eff_setpoint_k   = eff_setpoint_k
        self.heater_range_idx = heater_range_idx


class LakeShore335Backend(QObject):
    """Thread-safe backend for the LakeShore 335 temperature controller.

    Communicates via GPIB using raw SCPI commands through pyvisa.
    """

    HEATER_RANGES = ("OFF", "LOW", "MEDIUM", "HIGH")

    data_updated   = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, simulate: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._simulate = simulate
        self._device   = None

        self._dev_lock = threading.Lock()
        self._buf_lock = threading.Lock()
        self._log_lock = threading.Lock()

        self._connected = False
        self._data: deque[DataPoint] = deque(maxlen=MAX_DATA_POINTS)
        self._error: str | None = None

        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

        self._target_k:        float = 300.0
        self._ramp_from_k:     float = 300.0
        self._ramp_start_time: float = time.time()
        self._ramp_enabled:    bool  = False
        self._ramp_rate_kpm:   float = 0.0
        self._heater_range_idx: int  = 0

        self._sim_temp_a_k: float = 300.0
        self._sim_temp_b_k: float = 295.0

        self._log_file          = None
        self._log_writer        = None
        self._log_start_time:   float = 0.0
        self._logging:          bool  = False
        self._log_rows_written: int   = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        with self._buf_lock:
            return self._error

    @property
    def is_logging(self) -> bool:
        with self._log_lock:
            return self._logging

    @property
    def log_rows_written(self) -> int:
        with self._log_lock:
            return self._log_rows_written

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, gpib_address: str = DEFAULT_GPIB_ADDRESS) -> None:
        if self._simulate:
            self._connected = True
            self._start_polling()
            return

        import pyvisa
        rm = pyvisa.ResourceManager()
        device = rm.open_resource(gpib_address)
        device.timeout = 5000  # ms
        self._device = device

        with self._dev_lock:
            self._target_k        = float(self._device.query("SETP? 1").strip())
            self._ramp_from_k     = self._target_k
            self._ramp_start_time = time.time()
            ramp_parts            = self._device.query("RAMP? 1").strip().split(",")
            self._ramp_enabled    = bool(int(ramp_parts[0]))
            self._ramp_rate_kpm   = float(ramp_parts[1])
            self._heater_range_idx = int(self._device.query("RANGE? 1").strip())

        self._connected = True
        self._start_polling()

    def disconnect(self) -> None:
        self._stop_polling()
        self.stop_logging()
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
        self._connected = False

    # ------------------------------------------------------------------
    # Polling thread
    # ------------------------------------------------------------------

    def _start_polling(self) -> None:
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _stop_polling(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5.0)
            self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                temp_a, temp_b, eff_sp, hr_idx = self._fetch_readings()
                dp = DataPoint(time.time(), temp_a, temp_b, eff_sp, hr_idx)
                with self._buf_lock:
                    self._data.append(dp)
                    self._error = None
                self._write_log_row(dp)
                self.data_updated.emit()
            except Exception as exc:
                with self._buf_lock:
                    self._error = str(exc)
                self.error_occurred.emit(str(exc))
            self._stop_event.wait(POLL_INTERVAL)

    def _fetch_readings(self) -> tuple[float, float, float, int]:
        if self._simulate:
            return self._simulate_step()
        with self._dev_lock:
            temp_a = float(self._device.query("KRDG? A").strip())
            temp_b = float(self._device.query("KRDG? B").strip())
            eff_sp = self._compute_eff_setpoint_locked()
            hr_idx = self._heater_range_idx
        return temp_a, temp_b, eff_sp, hr_idx

    def _simulate_step(self) -> tuple[float, float, float, int]:
        with self._dev_lock:
            eff_sp = self._compute_eff_setpoint_locked()
            temp_a = self._sim_temp_a_k
            temp_b = self._sim_temp_b_k
            hr_idx = self._heater_range_idx

            if hr_idx > 0:
                alpha_a = 1.0 - POLL_INTERVAL / 60.0
                alpha_b = 1.0 - POLL_INTERVAL / 90.0
                self._sim_temp_a_k = alpha_a * temp_a + (1.0 - alpha_a) * eff_sp + random.gauss(0.0, 0.02)
                self._sim_temp_b_k = alpha_b * temp_b + (1.0 - alpha_b) * eff_sp + random.gauss(0.0, 0.02)
            else:
                alpha_a = 1.0 - POLL_INTERVAL / 300.0
                alpha_b = 1.0 - POLL_INTERVAL / 400.0
                self._sim_temp_a_k = max(77.0, alpha_a * temp_a + (1.0 - alpha_a) * 77.0)
                self._sim_temp_b_k = max(77.0, alpha_b * temp_b + (1.0 - alpha_b) * 77.0)

            return self._sim_temp_a_k, self._sim_temp_b_k, eff_sp, hr_idx

    def _compute_eff_setpoint_locked(self) -> float:
        """Compute ramp-adjusted setpoint. Must be called with _dev_lock held."""
        if not self._ramp_enabled or self._ramp_rate_kpm <= 0.0:
            return self._target_k
        elapsed = time.time() - self._ramp_start_time
        delta   = (self._ramp_rate_kpm / 60.0) * elapsed
        diff    = self._target_k - self._ramp_from_k
        if diff >= 0.0:
            return min(self._ramp_from_k + delta, self._target_k)
        return max(self._ramp_from_k - delta, self._target_k)

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_data(self) -> list[DataPoint]:
        with self._buf_lock:
            return list(self._data)

    # ------------------------------------------------------------------
    # Setpoint
    # ------------------------------------------------------------------

    def get_setpoint(self, output: int = 1) -> float:
        with self._dev_lock:
            if not self._simulate:
                self._target_k = float(self._device.query(f"SETP? {output}").strip())
            return self._target_k

    def set_setpoint(self, value_k: float, output: int = 1) -> None:
        with self._dev_lock:
            self._ramp_from_k     = self._compute_eff_setpoint_locked()
            self._ramp_start_time = time.time()
            self._target_k        = float(value_k)
            if not self._simulate:
                self._device.write(f"SETP {output},{value_k:.4f}")

    # ------------------------------------------------------------------
    # Ramp parameter
    # ------------------------------------------------------------------

    def get_ramp_parameter(self, output: int = 1) -> tuple[bool, float]:
        with self._dev_lock:
            if not self._simulate:
                parts = self._device.query(f"RAMP? {output}").strip().split(",")
                self._ramp_enabled  = bool(int(parts[0]))
                self._ramp_rate_kpm = float(parts[1])
            return self._ramp_enabled, self._ramp_rate_kpm

    def set_ramp_parameter(self, rate_kpm: float, enable: bool = True,
                           output: int = 1) -> None:
        with self._dev_lock:
            if self._ramp_enabled:
                self._ramp_from_k = self._compute_eff_setpoint_locked()
            self._ramp_start_time = time.time()
            self._ramp_enabled    = enable
            self._ramp_rate_kpm   = float(rate_kpm)
            if not self._simulate:
                self._device.write(f"RAMP {output},{int(enable)},{rate_kpm:.4f}")

    # ------------------------------------------------------------------
    # Heater range
    # ------------------------------------------------------------------

    def get_heater_range(self, output: int = 1) -> int:
        with self._dev_lock:
            if not self._simulate:
                self._heater_range_idx = int(self._device.query(f"RANGE? {output}").strip())
            return self._heater_range_idx

    def set_heater_range(self, range_index: int, output: int = 1) -> None:
        with self._dev_lock:
            self._heater_range_idx = int(range_index)
            if not self._simulate:
                self._device.write(f"RANGE {output},{range_index}")

    def all_off(self) -> None:
        with self._dev_lock:
            self._heater_range_idx = 0
            if not self._simulate:
                self._device.write("RANGE 1,0")
                self._device.write("RANGE 2,0")

    # ------------------------------------------------------------------
    # CSV Logging
    # ------------------------------------------------------------------

    def start_logging(self, filepath: str) -> None:
        with self._log_lock:
            if self._logging:
                return
            self._log_file   = open(filepath, "w", newline="", encoding="utf-8")
            self._log_writer = csv.writer(self._log_file)
            self._log_writer.writerow(
                ["Timestamp", "Elapsed_s", "Temp_A_K", "Temp_B_K", "Setpoint_K", "Heater_Range"]
            )
            self._log_file.flush()
            self._log_start_time   = time.time()
            self._log_rows_written = 0
            self._logging          = True

    def stop_logging(self) -> None:
        with self._log_lock:
            if not self._logging:
                return
            self._logging = False
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file   = None
                self._log_writer = None

    def _write_log_row(self, dp: DataPoint) -> None:
        with self._log_lock:
            if not self._logging or self._log_writer is None:
                return
            elapsed = dp.timestamp - self._log_start_time
            hr_name = self.HEATER_RANGES[dp.heater_range_idx]
            self._log_writer.writerow([
                datetime.fromtimestamp(dp.timestamp).isoformat(timespec="milliseconds"),
                f"{elapsed:.1f}",
                f"{dp.temp_a_k:.4f}",
                f"{dp.temp_b_k:.4f}",
                f"{dp.eff_setpoint_k:.4f}",
                hr_name,
            ])
            self._log_file.flush()
            self._log_rows_written += 1
