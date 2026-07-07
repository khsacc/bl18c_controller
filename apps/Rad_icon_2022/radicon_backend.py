from __future__ import annotations

"""
Rad-icon 2022 Python backend.

Loads radicon_dll.dll (built from dll/radicon_dll.cpp) via ctypes and
provides a high-level class that returns numpy arrays.

The detector is controlled through TWO independent channels:

  1. Sapera LT (via DLL) — handles pixel-data DMA from sensor to PC memory.
  2. CameraLink serial port — sends ASCII commands to the camera's internal
     controller for exposure time and binning.  This is a separate RS-232-
     over-CameraLink channel; the Sapera API cannot change these settings.

Startup sequence (matches the commercial XFPCAP01.exe behaviour):

    Open COM2 @ 115200 baud
    → Send "sbn 0|1\\r"        (binning: 0=1x1, 1=2x2)
    → Send "seu 0\\r"           (camera init)
    → Send "set 100\\r"         (startup exposure 100 ms)
    → Wait up to 10 s for "USER" in receive buffer
    → rad_init() inside DLL    (creates Sapera objects + starts Grab)
    → Send "set <ms>\\r"        (set real exposure if provided)

Typical usage:

    from apps.Rad_icon_2022.radicon_backend import RadiconBackend, RADICON_CCF

    with RadiconBackend(ccf_path=RADICON_CCF["2x2"], binning="2x2") as det:
        det.set_exposure_ms(1000)          # 1-second exposure

        input("X線を止めてEnter")
        dark = det.acquire_sequence(n_frames=50)

        input("サンプルをセットしてEnter")
        img = det.snap()

Requirements:
    - radicon_dll.dll must be built first (run dll/build.bat or VS 2019)
    - Sapera LT runtime (SapClassBasic86.dll) on PATH
    - numpy
    - pyserial  (pip install pyserial)
"""

import ctypes
import logging
import time
import threading
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    import serial
except ImportError as e:
    raise ImportError(
        "pyserial is required for RadiconBackend.\n"
        "Install it with: pip install pyserial"
    ) from e

_log = logging.getLogger(__name__)

_DLL_PATH = Path(__file__).parent / "dll" / "Release" / "radicon_dll.dll"

RADICON_SERVER = "Xtium-CL_MX4_1"
RADICON_DEVICE = 0
RADICON_CCF = {
    "1x1": r"C:\Program Files\Teledyne DALSA\Sapera\CamFiles\User\T_Rad-icon_2022_Xtium_FullFOV_1x1_FreeRun.ccf",
    "2x2": r"C:\Program Files\Teledyne DALSA\Sapera\CamFiles\User\T_Rad-icon_2022_Xtium_FullFOV_2x2_FreeRun.ccf",
}
RADICON_SERIAL_PORT = "COM2"
RADICON_SERIAL_BAUD = 115_200

MAX_EXPOSURE_MS     = 60_000   # hard upper limit (60 s)
_TIMEOUT_MARGIN_MS  = 5_000    # added on top of exposure for snap timeout
_IDLE_EXPOSURE_MS   = 100      # short exposure used between triggered snaps


class RadiconError(RuntimeError):
    """Raised when a DLL call returns an error code."""


# ---------------------------------------------------------------------------
# Low-level DLL wrapper
# ---------------------------------------------------------------------------

class _RadiconDLL:
    """Thin ctypes wrapper — one instance shared across all RadiconBackend objects."""

    _instance: "_RadiconDLL | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self) -> None:
        if not _DLL_PATH.exists():
            raise FileNotFoundError(
                f"radicon_dll.dll not found at {_DLL_PATH}\n"
                f"Build it first: open dll\\RadiconDll_2019.vcxproj in VS 2019 "
                f"(Release | x64) or run dll\\build.bat"
            )
        self._lib = ctypes.CDLL(str(_DLL_PATH))
        self._setup_signatures()

    def _setup_signatures(self) -> None:
        lib = self._lib

        # int rad_init(const char*, int, const char*, void**)
        lib.rad_init.argtypes = [
            ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.rad_init.restype = ctypes.c_int

        # int rad_shutdown(void*)
        lib.rad_shutdown.argtypes = [ctypes.c_void_p]
        lib.rad_shutdown.restype  = ctypes.c_int

        # int rad_get_width(void*)
        lib.rad_get_width.argtypes = [ctypes.c_void_p]
        lib.rad_get_width.restype  = ctypes.c_int

        # int rad_get_height(void*)
        lib.rad_get_height.argtypes = [ctypes.c_void_p]
        lib.rad_get_height.restype  = ctypes.c_int

        # int rad_snap(void*, uint16_t*, int, int)
        lib.rad_snap.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.rad_snap.restype = ctypes.c_int

        # int rad_acquire_sequence(void*, uint16_t*, int, int, int)
        lib.rad_acquire_sequence.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.rad_acquire_sequence.restype = ctypes.c_int

        # const char* rad_get_last_error(void)
        lib.rad_get_last_error.argtypes = []
        lib.rad_get_last_error.restype  = ctypes.c_char_p

    def last_error(self) -> str:
        raw = self._lib.rad_get_last_error()
        return raw.decode(errors="replace") if raw else "(no error message)"

    def _check(self, ret: int) -> None:
        if ret != 0:
            raise RadiconError(self.last_error())


# ---------------------------------------------------------------------------
# Public high-level class
# ---------------------------------------------------------------------------

class RadiconBackend:
    """
    Controls the Rad-icon 2022 detector via a Sapera LT C++ DLL and the
    CameraLink serial port.

    Parameters
    ----------
    server_name
        Sapera server name of the Xtium-CL MX4 frame grabber.
        Run CamExpert (Start → Sapera LT → CamExpert) to find it.
    device_index
        Acquisition resource index (almost always 0).
    ccf_path
        Full path to the FreeRun .ccf file.  Use RADICON_CCF["1x1"] or
        RADICON_CCF["2x2"] for the standard BL-18C configurations.
    binning
        "1x1" or "2x2".  Controls the "sbn" serial command sent at startup.
        Must match the CCF file.
    serial_port
        Windows COM port for the CameraLink serial interface (default "COM2").
    snap_timeout_ms
        Per-frame timeout for snap() and acquire_sequence() calls in ms.
        Must be longer than the exposure time plus a safety margin (~2 s).
    """

    _SERIAL_BAUD          = RADICON_SERIAL_BAUD
    _STARTUP_EXPOSURE_MS  = 100   # brief exposure used during Grab() startup
    _SERIAL_READY_TIMEOUT = 10.0  # seconds to wait for "USER" response

    def __init__(
        self,
        server_name: str = RADICON_SERVER,
        device_index: int = RADICON_DEVICE,
        ccf_path: str = RADICON_CCF["2x2"],
        binning: str = "2x2",
        serial_port: str = RADICON_SERIAL_PORT,
        snap_timeout_ms: int | None = None,
    ) -> None:
        if binning not in ("1x1", "2x2"):
            raise ValueError(f"binning must be '1x1' or '2x2', got {binning!r}")

        self._dll = _RadiconDLL()
        # Default: _STARTUP_EXPOSURE_MS + margin; updated by set_exposure_ms().
        self._snap_timeout_ms = snap_timeout_ms if snap_timeout_ms is not None \
            else self._STARTUP_EXPOSURE_MS + _TIMEOUT_MARGIN_MS
        self._exposure_ms: int | None = None
        self._ser: serial.Serial | None = None

        # ------------------------------------------------------------------ #
        # Step 1: open serial port and run camera startup sequence.
        # This must happen BEFORE rad_init (which starts Grab) so the camera
        # is in a known state when the frame grabber starts capturing.
        # ------------------------------------------------------------------ #
        self._serial_init(serial_port, binning)

        # ------------------------------------------------------------------ #
        # Step 2: initialize Sapera (creates objects, starts Grab).
        # ------------------------------------------------------------------ #
        self._handle = ctypes.c_void_p(None)
        ret = self._dll._lib.rad_init(
            server_name.encode(),
            device_index,
            ccf_path.encode(),
            ctypes.byref(self._handle),
        )
        self._dll._check(ret)

        self._raw_width = self._dll._lib.rad_get_width(self._handle)
        self._height    = self._dll._lib.rad_get_height(self._handle)
        # Each CameraLink tap outputs a few invalid blanking pixels at its
        # outer edge.  Remove them symmetrically (4 px per side for 1x1;
        # verify with hardware for 2x2).
        self._h_blank = 4
        self._width   = self._raw_width - 2 * self._h_blank

        _log.debug(
            "RadiconBackend ready: %dx%d (raw %dx%d), exposure=%s ms, port=%s",
            self._width, self._height, self._raw_width, self._height,
            self._exposure_ms, serial_port,
        )

    # ---------------------------------------------------------------------- #
    # Serial port helpers
    # ---------------------------------------------------------------------- #

    def _serial_init(self, port: str, binning: str) -> None:
        """Open COM port and run the camera startup sequence."""
        self._ser = serial.Serial(port, self._SERIAL_BAUD, timeout=0.1)
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

        sbn_cmd = "sbn 1" if binning == "2x2" else "sbn 0"
        self._send_serial(sbn_cmd)
        self._send_serial("seu 0")
        self._send_serial(f"set {self._STARTUP_EXPOSURE_MS}")

        # Wait for "USER" acknowledgement (camera controller ready).
        deadline = time.monotonic() + self._SERIAL_READY_TIMEOUT
        recv_buf = ""
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                recv_buf += chunk.decode(errors="replace")
                if "USER" in recv_buf:
                    _log.debug("Camera serial ready (received 'USER')")
                    break
            time.sleep(0.05)
        else:
            warnings.warn(
                f"Camera did not respond with 'USER' on {port} within "
                f"{self._SERIAL_READY_TIMEOUT:.0f} s.  "
                "Check serial port, camera power, and binning setting.",
                RuntimeWarning,
                stacklevel=3,
            )

    def _send_serial(self, cmd: str) -> None:
        """Send one ASCII command to the camera (appends \\r terminator)."""
        if self._ser and self._ser.is_open:
            self._ser.write((cmd + "\r").encode())
            _log.debug("Serial → %s", cmd)

    def _wait_for_user_response(self, timeout: float = 3.0) -> bool:
        """Drain serial buffer until 'USER' is received. Returns True if found."""
        if not (self._ser and self._ser.is_open):
            return False
        deadline = time.monotonic() + timeout
        recv_buf = ""
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                recv_buf += chunk.decode(errors="replace")
                if "USER" in recv_buf:
                    return True
            time.sleep(0.02)
        return False

    # ---------------------------------------------------------------------- #
    # Properties
    # ---------------------------------------------------------------------- #

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def n_pixels(self) -> int:
        return self._width * self._height

    # ---------------------------------------------------------------------- #
    # Exposure control (serial port)
    # ---------------------------------------------------------------------- #

    def set_exposure_ms(self, ms: int) -> None:
        """Set integration time in **milliseconds** via the CameraLink serial port.

        The command sent is ``set <ms>\\r`` at 115200 baud on the configured
        serial port.  The per-frame snap timeout is automatically updated to
        ``ms + _TIMEOUT_MARGIN_MS`` (currently 5 s).
        Maximum allowed value is MAX_EXPOSURE_MS (60 000 ms).
        """
        if not self._handle:
            raise RadiconError("Backend is closed")
        if ms <= 0:
            raise ValueError(f"exposure must be positive, got {ms}")
        if ms > MAX_EXPOSURE_MS:
            raise ValueError(
                f"exposure {ms} ms exceeds MAX_EXPOSURE_MS ({MAX_EXPOSURE_MS} ms)"
            )
        self._send_serial(f"set {ms}")
        self._exposure_ms = ms
        self._snap_timeout_ms = ms + _TIMEOUT_MARGIN_MS

    def set_exposure_us(self, exposure_us: int) -> None:
        """Convenience wrapper: converts µs → ms and calls set_exposure_ms()."""
        ms = max(1, round(exposure_us / 1000))
        self.set_exposure_ms(ms)

    def _update_stored_exposure(self, ms: int) -> None:
        """Update stored exposure value and timeout without sending serial command.

        Used by triggered-mode workers to set the target exposure before calling
        snap_triggered(), which sends the actual set command at the right moment.
        """
        if ms <= 0 or ms > MAX_EXPOSURE_MS:
            return
        self._exposure_ms = ms
        self._snap_timeout_ms = ms + _TIMEOUT_MARGIN_MS

    # ---------------------------------------------------------------------- #
    # Acquisition
    # ---------------------------------------------------------------------- #

    def snap_triggered(self, timeout_ms: int | None = None) -> np.ndarray:
        """Triggered snap with sub-idle-exposure latency.

        The camera idles at _IDLE_EXPOSURE_MS (100 ms) between calls.
        On trigger:
          1. Send set <real_ms> — camera switches as soon as the current idle
             cycle ends (at most _IDLE_EXPOSURE_MS ms latency).
          2. If the camera finishes the idle cycle before switching (Scenario B),
             snap() returns quickly with a transition frame — discard it.
             If the camera switched immediately (Scenario A), the short snap()
             times out harmlessly.
          3. Wait for the clean real-exposure frame.
          4. Revert to idle exposure so the next trigger fires quickly.
        """
        if not self._handle:
            raise RadiconError("Backend is closed")
        ms = self._exposure_ms if self._exposure_ms is not None else self._STARTUP_EXPOSURE_MS
        if timeout_ms is None:
            timeout_ms = ms + _TIMEOUT_MARGIN_MS

        # Tell the camera to switch to real exposure.
        self._send_serial(f"set {ms}")

        try:
            # Catch the tail-end idle frame (Scenario B).
            # Timeout = 3× idle period — more than enough for one 100 ms cycle.
            self.snap(timeout_ms=_IDLE_EXPOSURE_MS * 3)
        except RadiconError as e:
            if "timeout" not in str(e).lower():
                raise  # hardware error — propagate
            # Scenario A: camera switched immediately; real frame is still coming.

        # Clean real-exposure frame.
        frame = self.snap(timeout_ms=timeout_ms)

        # Revert to idle so the next trigger starts quickly.
        self._send_serial(f"set {_IDLE_EXPOSURE_MS}")
        return frame

    def snap(self, timeout_ms: int | None = None) -> np.ndarray:
        """Wait for one frame and return it as a (height, width) uint16 array.

        Pixel values are raw 14-bit packed into uint16 (upper 2 bits zero).
        """
        if not self._handle:
            raise RadiconError("Backend is closed")
        if timeout_ms is None:
            timeout_ms = self._snap_timeout_ms

        buf = np.empty((self._height, self._raw_width), dtype=np.uint16)
        ret = self._dll._lib.rad_snap(
            self._handle,
            buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
            buf.size,
            timeout_ms,
        )
        self._dll._check(ret)
        return buf[:, self._h_blank:-self._h_blank]

    def acquire_sequence(
        self,
        n_frames: int,
        timeout_ms_per_frame: int | None = None,
    ) -> np.ndarray:
        """Acquire *n_frames* and return them as a (n_frames, height, width) uint16 array.

        Memory: 1000 frames at 1032×774 (2x2) ≈ 1.6 GB, or 2064×1549 (1x1) ≈ 6.4 GB — ensure enough RAM.
        """
        if not self._handle:
            raise RadiconError("Backend is closed")
        if timeout_ms_per_frame is None:
            timeout_ms_per_frame = self._snap_timeout_ms

        buf = np.empty((n_frames, self._height, self._raw_width), dtype=np.uint16)
        ret = self._dll._lib.rad_acquire_sequence(
            self._handle,
            buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
            n_frames,
            self._height * self._raw_width,
            timeout_ms_per_frame,
        )
        self._dll._check(ret)
        return buf[:, :, self._h_blank:-self._h_blank]

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    def close(self) -> None:
        """Release all resources.  Safe to call multiple times."""
        if self._handle:
            self._dll._lib.rad_shutdown(self._handle)
            self._handle = ctypes.c_void_p(None)
        if self._ser and self._ser.is_open:
            self._ser.close()
            self._ser = None

    def __enter__(self) -> "RadiconBackend":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Simulated backend (no hardware) — for --debug mode
# ---------------------------------------------------------------------------

class RadiconBackendSim:
    """Drop-in stand-in for RadiconBackend with no DLL/serial access.

    Implements enough of the public surface for RadiconWindow to build its
    layout and for the acquisition buttons to work; images returned are
    synthetic noise, not representative of real detector data.
    """

    def __init__(self, width: int = 1032, height: int = 774):
        self.width = width
        self.height = height
        self._h_blank = 4

    def _update_stored_exposure(self, ms: int) -> None:
        pass

    def set_exposure_us(self, exposure_us: int) -> None:
        pass

    def set_exposure_ms(self, ms: int) -> None:
        pass

    def snap(self, timeout_ms: int | None = None) -> np.ndarray:
        return np.random.randint(0, 4096, (self.height, self.width), dtype=np.uint16)

    def snap_triggered(self, timeout_ms: int | None = None) -> np.ndarray:
        return self.snap()

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# XRD oscillation scan worker
# ---------------------------------------------------------------------------

_DEG_PER_PULSE_CH11 = 0.004   # degrees per pulse for Ch11 rotation stage
_CH_ROTATION        = 11


class XrdOscillationWorker(QThread):
    """Oscillation XRD scan: rotates Ch11 while acquiring one frame per step.

    For each step the camera ``snap()`` and the Ch11 sub-pulse movement run
    concurrently in separate OS threads.  The sub-move loop sends 1-pulse
    SPDL commands at ``exposure_ms / step_pulses`` intervals so that Ch11
    sweeps exactly ``step_pulses`` pulses during each exposure.

    Parameters
    ----------
    backend
        Connected ``RadiconBackend`` (Grab already running).
    controller
        Connected ``PM16CController`` (or Sim).
    min_pulse
        Absolute Ch11 pulse value for the START of the first frame.
    step_pulses
        Signed pulse count swept during each exposure (positive = increasing
        angle).  Minimum absolute value is 1.
    n_steps
        Total number of frames to acquire.
    exposure_ms
        Exposure time in milliseconds.
    """

    frame_acquired  = pyqtSignal(int, float, object)  # (step_idx, omega_start_deg, ndarray)
    progress        = pyqtSignal(int, int, float)      # (done, total, current_omega_deg)
    scan_finished   = pyqtSignal()
    scan_aborted    = pyqtSignal()
    error           = pyqtSignal(str)
    overrun_warning = pyqtSignal(int, float)           # (step_idx, overrun_s)

    def __init__(
        self,
        backend: RadiconBackend,
        controller,
        min_pulse: int,
        step_pulses: int,
        n_steps: int,
        exposure_ms: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._backend      = backend
        self._controller   = controller
        self._min_pulse    = min_pulse
        self._step_pulses  = step_pulses
        self._n_steps      = n_steps
        self._exposure_ms  = exposure_ms
        self._abort        = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        ctrl       = self._controller
        backend    = self._backend
        ch_str     = ctrl.stringify_ch_numbers(_CH_ROTATION)
        timeout_ms = self._exposure_ms + _TIMEOUT_MARGIN_MS

        # desired rotation speed in pps so Ch11 sweeps exactly step_pulses during exposure
        desired_pps   = max(1, round(abs(self._step_pulses) / (self._exposure_ms / 1000.0)))
        original_lspd: "int | None" = None

        try:
            backend.set_exposure_ms(self._exposure_ms)

            # ── Save LSPD and set to desired rotation speed ──────────────────
            original_lspd = ctrl.get_ch_lspd(_CH_ROTATION)
            ctrl.set_ch_lspd(_CH_ROTATION, desired_pps)

            # ── Move Ch11 to scan start (high speed) ────────────────────────
            ctrl.set_ch_speed(_CH_ROTATION, 'H')
            ctrl.move_ch_absolute(_CH_ROTATION, self._min_pulse)
            ctrl.wait_until_stop()

            if self._abort:
                self.scan_aborted.emit()
                return

            # ── Select LSPD mode for the scan loop; stay in REM ─────────────
            ctrl.switch_to_rem()
            ctrl.send_cmd(f"SPDL{ch_str}", has_response=False)

            # ── Step loop ───────────────────────────────────────────────────
            for step_i in range(self._n_steps):
                if self._abort:
                    break

                omega_start_deg = (self._min_pulse + step_i * self._step_pulses) * _DEG_PER_PULSE_CH11
                self.progress.emit(step_i, self._n_steps, omega_start_deg)

                result_box: list = [None]
                exc_box:    list = [None]

                def _snap(rb=result_box, eb=exc_box, tms=timeout_ms):
                    try:
                        rb[0] = backend.snap(timeout_ms=tms)
                    except Exception as exc:
                        eb[0] = exc

                # Start snap, then immediately send the single SPDL move command.
                # The motor sweeps step_pulses at desired_pps ≈ exposure_s, so
                # both finish at roughly the same time without any OS-level timing.
                snap_thr = threading.Thread(target=_snap, daemon=True)
                snap_thr.start()
                ctrl.send_cmd(f"REL{ch_str}{self._step_pulses:+}", has_response=False)

                snap_thr.join(timeout=timeout_ms / 1000.0 + 2.0)

                if exc_box[0] is not None:
                    raise exc_box[0]
                if result_box[0] is None:
                    raise RadiconError(f"snap() timed out at step {step_i}")

                if not self._abort:
                    self.frame_acquired.emit(step_i, omega_start_deg, result_box[0])

            # ── Wait for any move still in progress ──────────────────────────
            try:
                ctrl.wait_until_stop()
            except Exception:
                pass

            if self._abort:
                self.scan_aborted.emit()
            else:
                self.scan_finished.emit()

        except Exception as exc:
            try:
                ctrl.normal_stop()
            except Exception:
                pass
            self.error.emit(str(exc))

        finally:
            # Restore original LSPD (regardless of how we exited)
            if original_lspd is not None:
                try:
                    ctrl.set_ch_lspd(_CH_ROTATION, original_lspd)
                except Exception:
                    pass
            # Return Ch11 to 0 at high speed
            try:
                ctrl.set_ch_speed(_CH_ROTATION, 'H')
                ctrl.move_ch_absolute(_CH_ROTATION, 0)
                ctrl.wait_until_stop()
            except Exception:
                pass
