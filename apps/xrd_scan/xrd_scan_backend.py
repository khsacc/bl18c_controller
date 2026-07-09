"""XRD DAC Scan backend — ROI specification, poni helpers, scan worker thread."""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    import pyFAI.detectors as pf_detectors
    from pyFAI.integrator.azimuthal import AzimuthalIntegrator
    PYFAI_AVAILABLE = True
except ImportError:
    PYFAI_AVAILABLE = False
    AzimuthalIntegrator = object  # type: ignore[misc,assignment]

try:
    from utils.stage.control_stage import PULSE_SCALE
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from utils.stage.control_stage import PULSE_SCALE

# XRD Scan operates on the same physical Ch4 (X) / Ch5 (Y) sample stage axes
# as DAC Scan (Normal) — fixed here rather than imported since this app has
# not been migrated to the generic apps/scan2d scan engine.
CH_X = 4
CH_Y = 5
UM_PER_PULSE_CH4: float = PULSE_SCALE[4]
UM_PER_PULSE_CH5: float = PULSE_SCALE[5]

# Ch4 backlash compensation: always approach from the negative side so the
# final motion is in the + direction.
BACKLASH_PULSES_CH4: int = 5    # 5 pulses = 10 µm

# ── ROI colors (RGB) ───────────────────────────────────────────────────────────
ROI_COLORS: list[tuple[int, int, int]] = [
    (220,  60,  60),   # red
    ( 60, 110, 220),   # blue
    ( 50, 180,  70),   # green
    (220, 140,   0),   # orange
    (160,  60, 200),   # purple
    (  0, 180, 180),   # teal
    (200,  90, 150),   # pink
    (130, 170,  40),   # olive
]


# ── ROI specification ──────────────────────────────────────────────────────────

@dataclass
class RoiSpec:
    label:   str
    tth_min: float
    tth_max: float
    mode:    str   = "sum"                          # "sum" | "mean"
    color:   tuple = field(default_factory=lambda: ROI_COLORS[0])

    def compute(self, radial: np.ndarray, intensity: np.ndarray) -> float:
        mask = (radial >= self.tth_min) & (radial <= self.tth_max)
        if not mask.any():
            return 0.0
        vals = intensity[mask]
        return float(vals.sum() if self.mode == "sum" else vals.mean())


# ── poni helpers (UTF-8 safe; bypasses pyFAI's locale-dependent file reader) ──

def parse_poni(path: pathlib.Path) -> dict:
    result: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
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


def build_ai(poni_dict: dict) -> AzimuthalIntegrator:
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


# ── Scan worker ────────────────────────────────────────────────────────────────

class XrdScanWorker(QThread):
    """
    Moves Ch4/Ch5 over a pre-computed grid, acquires an XRD image at each
    point via RadiconBackend.snap_triggered(), runs pyFAI azimuthal integration
    in memory, and emits the raw 1D spectrum for each point.

    ROI computation is intentionally left to the main thread so that ROIs can
    be redefined after the scan without re-scanning.

    Ch4 backlash compensation (same pattern as DacScanWorker):
    Before each row, Ch4 approaches the first column from
    (x_pulses[0] - backlash_pulses) so all measurements are made on the
    + direction stroke.
    """

    # row, col, radial (ndarray), intensity (ndarray)
    point_measured = pyqtSignal(int, int, object, object)
    scan_completed = pyqtSignal()
    scan_aborted   = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(
        self,
        controller,
        backend,                          # RadiconBackend
        ai:            AzimuthalIntegrator,
        x_pulses:      list[int],         # absolute Ch4 pulse positions per column
        y_pulses:      list[int],         # absolute Ch5 pulse positions per row
        center_x:      int,
        center_y:      int,
        n_bins:        int,
        exposure_ms:   int,
        speed:         str,               # "H" | "M" | "L"
        dark:          np.ndarray | None = None,
        backlash_pulses: int = BACKLASH_PULSES_CH4,
        settle_ms:     int  = 100,
        save_tiff:     bool = False,
        tiff_dir:      pathlib.Path | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._ctrl            = controller
        self._backend         = backend
        self._ai              = ai
        self._dark            = dark.astype(np.float32) if dark is not None else None
        self._x_pulses        = x_pulses
        self._y_pulses        = y_pulses
        self._center_x        = center_x
        self._center_y        = center_y
        self._n_bins          = n_bins
        self._exposure_ms     = exposure_ms
        self._speed           = speed
        self._backlash_pulses = backlash_pulses
        self._settle_ms       = settle_ms
        self._save_tiff       = save_tiff
        self._tiff_dir        = tiff_dir
        self._abort           = False

    def abort(self) -> None:
        self._abort = True

    def _approach_ch4(self, target: int) -> None:
        """Approach Ch4 from the negative side (backlash compensation)."""
        self._ctrl.move_ch_absolute(CH_X, target - self._backlash_pulses)
        self._ctrl.wait_until_stop(stay_in_rem=True)
        self._ctrl.move_ch_absolute(CH_X, target)
        self._ctrl.wait_until_stop(stay_in_rem=True)

    def run(self) -> None:
        if self._save_tiff and self._tiff_dir is not None:
            import tifffile as _tf
        else:
            _tf = None  # type: ignore[assignment]

        ctrl     = self._ctrl
        x_pulses = self._x_pulses
        y_pulses = self._y_pulses
        n_cols   = len(x_pulses)
        n_rows   = len(y_pulses)
        total    = n_rows * n_cols
        done     = 0

        ctrl.set_ch_speed(CH_X, self._speed)
        ctrl.set_ch_speed(CH_Y, self._speed)

        try:
            for row_idx, y_pulse in enumerate(y_pulses):
                if self._abort:
                    break

                self.status_message.emit(f"Row {row_idx + 1}/{n_rows}  (Ch5={y_pulse})")
                ctrl.move_ch_absolute(CH_Y, y_pulse)
                ctrl.wait_until_stop(stay_in_rem=True)

                self._approach_ch4(x_pulses[0])

                for col_idx in range(n_cols):
                    if self._abort:
                        break

                    if col_idx > 0:
                        ctrl.move_ch_absolute(CH_X, x_pulses[col_idx])
                        ctrl.wait_until_stop(stay_in_rem=True)

                    if self._settle_ms > 0:
                        time.sleep(self._settle_ms / 1000.0)

                    img = self._backend.snap_triggered(self._exposure_ms)

                    if _tf is not None and self._tiff_dir is not None:
                        fname = self._tiff_dir / f"r{row_idx:03d}_c{col_idx:03d}.tif"
                        _tf.imwrite(str(fname), img)

                    img_f = img.astype(np.float32)
                    if self._dark is not None:
                        img_f = np.clip(img_f - self._dark, 0.0, None)

                    result = self._ai.integrate1d(
                        img_f,
                        npt=self._n_bins,
                        unit="2th_deg",
                        method=("no", "histogram", "cython"),
                        correctSolidAngle=True,
                        polarization_factor=0.95,
                    )

                    done += 1
                    self.status_message.emit(f"Scanning: {done}/{total} points")
                    self.point_measured.emit(
                        row_idx, col_idx,
                        result.radial,
                        result.intensity,
                    )

            if not self._abort:
                self.status_message.emit("Returning to centre…")
                ctrl.move_ch_absolute(CH_Y, self._center_y)
                ctrl.wait_until_stop(stay_in_rem=True)
                self._approach_ch4(self._center_x)
                ctrl.switch_to_loc()
                self.scan_completed.emit()
            else:
                ctrl.switch_to_loc()
                self.scan_aborted.emit()

        except Exception as exc:
            try:
                ctrl.switch_to_loc()
            except Exception:
                pass
            self.status_message.emit(f"Error: {exc}")
            self.scan_aborted.emit()
