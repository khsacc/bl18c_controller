"""Keithley 2000 GPIB reader for DAC scan intensity measurement.

Two modes are auto-detected at init via *IDN?:

  Normal (SCPI mode)
      *IDN? returns the model string  →  uses :FETCH? for readings.
      Init: sets :FUNC "CURR:DC", auto-range, continuous trigger.

  Talk-Only mode
      *IDN? is ignored and a numeric measurement is returned instead  →
      uses a single read() to receive the next available measurement.

Only one input (the transmitted-intensity photodiode) is read. Investigation
confirmed the Model 2000 has no remote-switchable multi-input scanning — the
FRONT/REAR terminal selection cannot be driven over SCPI — so a second
(incident/ion-chamber) reading is not obtainable through this instrument.
"""
from __future__ import annotations

import pyvisa

KEITHLEY_ADDRESS = "GPIB0::2::INSTR"


class Keithley2000Reader:
    """Keithley 2000 DMM reader.  Works in both SCPI and Talk-Only modes.

    Compatible with GpibReader interfaces (apps/scan2d/free_2d_scan_backend.py,
    dac_scan_rot_backend) via duck typing — no base class needed.
    """

    def __init__(self, address: str = KEITHLEY_ADDRESS, timeout_ms: int = 5000):
        self._rm    = pyvisa.ResourceManager()
        self._instr = self._rm.open_resource(address)
        self._instr.timeout = timeout_ms

        # Detect mode: send *IDN? and check whether the response is numeric.
        # In Talk-Only mode the instrument ignores commands and returns a
        # measurement value; in normal mode it returns the model string.
        idn = self._instr.query("*IDN?").strip()
        try:
            float(idn)
            self._talk_only = True          # numeric → Talk-Only
        except ValueError:
            self._talk_only = False         # model string → normal SCPI
            self._instr.write(':FUNC "CURR:DC"')
            self._instr.write(':CURR:DC:RANG:AUTO ON')
            self._instr.write(':INIT:CONT ON')

    @property
    def is_talk_only(self) -> bool:
        return self._talk_only

    # --- GpibReader interface ---------------------------------------------

    def set_current_position(self, *args, **kwargs) -> None:
        pass

    def set_theta(self, theta_deg: float) -> None:
        pass

    def read_transmitted(self) -> float:
        """Read photodiode current (A).

        SCPI mode  : queries :FETCH? for the latest continuous-trigger reading.
        Talk-Only  : receives the next measurement via a single read().
        """
        if self._talk_only:
            return self._read_talk_only()
        try:
            return float(self._instr.query(':FETCH?'))
        except Exception as e:
            print(f"[Keithley2000] read_transmitted failed: {e}")
            return 0.0

    # --- raw SCPI passthrough (Development > Keithley Reader tool only) ---

    def query(self, command: str) -> str:
        """Send a raw SCPI command and return the response, stripped.

        For hardware exploration only (Development menu) — bypasses the
        talk-only/normal mode handling used by read_transmitted().
        """
        return self._instr.query(command).strip()

    def write(self, command: str) -> None:
        """Send a raw SCPI command with no response expected."""
        self._instr.write(command)

    # --- internal helpers -------------------------------------------------

    def _read_talk_only(self) -> float:
        """Read one measurement from the continuously-talking Keithley.

        Talk-Only instruments output data whenever they have a reading ready.
        A single read() receives the next available measurement; no drain loop
        is needed because GPIB synchronous reads are not buffered in the driver.
        """
        try:
            return float(self._instr.read())
        except Exception as e:
            print(f"[Keithley2000] read_transmitted (Talk-Only) failed: {e}")
            return 0.0

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if not self._talk_only:
            try:
                self._instr.write(':INIT:CONT OFF')
            except Exception:
                pass
        try:
            self._instr.close()
        except Exception:
            pass
        try:
            self._rm.close()
        except Exception:
            pass
