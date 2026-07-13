from __future__ import annotations

"""
Rad-icon 2022 PyQt6 UI — single-shot, sequential, and dark-current acquisition
"""

import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

try:
    from .radicon_backend import (
        RadiconBackend, RadiconBackendSim, RadiconError,
        RADICON_SERVER, RADICON_DEVICE, RADICON_CCF,
    )
except ImportError:
    import sys as _sys
    _pkg = str(Path(__file__).parent.parent.parent)
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from apps.Rad_icon_2022.radicon_backend import (
        RadiconBackend, RadiconBackendSim, RadiconError,
        RADICON_SERVER, RADICON_DEVICE, RADICON_CCF,
    )

_HERE = Path(__file__).parent
_LOCALDATA = _HERE / "__localdata"
_PREFS_FILE = _LOCALDATA / "radicon_ui_prefs.json"

_TIFF_OPTS = [cv2.IMWRITE_TIFF_COMPRESSION, 1]   # uncompressed, matches XFPCAP01
_DEFAULT_DEFECT_FILE = _HERE / "__localdata" / "XFPCAP01_defects" / "欠陥ファイル03.txt"

# ---------------------------------------------------------------------------
# Sound notification  (implementation lives in settings.notification_sound)
# ---------------------------------------------------------------------------

try:
    from settings.notification_sound import SOUND_OPTIONS, play_done_sound, play_current_sound
except ImportError:
    _pkg = str(Path(__file__).parent.parent.parent)
    import sys as _sys
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.notification_sound import SOUND_OPTIONS, play_done_sound, play_current_sound

try:
    from settings.i18n import tr
except ImportError:
    _pkg = str(Path(__file__).parent.parent.parent)
    import sys as _sys
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.i18n import tr

try:
    from settings.poni_state import PoniState
    from settings.settings_window import SettingsWindow
except ImportError:
    _pkg = str(Path(__file__).parent.parent.parent)
    import sys as _sys
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from settings.poni_state import PoniState
    from settings.settings_window import SettingsWindow

try:
    from apps.calibrate_instruments.calibrate_instruments_app import CalibrateInstrumentsWindow
except ImportError:
    _pkg = str(Path(__file__).parent.parent.parent)
    import sys as _sys
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from apps.calibrate_instruments.calibrate_instruments_app import CalibrateInstrumentsWindow

try:
    from .image_utils import (
        save_tiff as _save_tiff,
        read_tiff_metadata as _read_tiff_metadata,
        parse_defect_file as _parse_defect_file,
        build_defect_mask as _build_defect_mask,
        apply_defect_correction as _apply_defect_correction,
        replace_defect_pixels as _replace_defect_pixels,
    )
except ImportError:
    import sys as _sys
    _pkg = str(Path(__file__).parent.parent.parent)
    if _pkg not in _sys.path:
        _sys.path.insert(0, _pkg)
    from apps.Rad_icon_2022.image_utils import (
        save_tiff as _save_tiff,
        read_tiff_metadata as _read_tiff_metadata,
        parse_defect_file as _parse_defect_file,
        build_defect_mask as _build_defect_mask,
        apply_defect_correction as _apply_defect_correction,
        replace_defect_pixels as _replace_defect_pixels,
    )


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class _SnapWorker(QtCore.QThread):
    done = QtCore.pyqtSignal(object, str)   # (np.ndarray, warning_or_empty)
    error = QtCore.pyqtSignal(str)

    def __init__(self, backend: RadiconBackend, exposure_us: int, timeout_ms: int):
        super().__init__()
        self._backend = backend
        self._exposure_us = exposure_us
        self._timeout_ms = timeout_ms
        self.stop_requested = False

    def request_stop(self):
        """Best-effort stop: the underlying hardware call cannot be interrupted
        mid-flight (no DLL-level cancel API), so this only flags the result to
        be discarded once the blocking snap call returns or times out."""
        self.stop_requested = True

    def run(self):
        try:
            # Always use triggered mode: store exposure without serial send;
            # snap_triggered() sends set <ms> at the right moment.
            self._backend._update_stored_exposure(
                max(1, round(self._exposure_us / 1000))
            )
            img = self._backend.snap_triggered(timeout_ms=self._timeout_ms)
            self.done.emit(img, "")
        except Exception as exc:
            self.error.emit(str(exc))


class _SeqWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int)   # (acquired, total)
    frame_ready = QtCore.pyqtSignal(int, object)  # (0-based index, np.ndarray)
    done = QtCore.pyqtSignal(object)         # list[np.ndarray]
    error = QtCore.pyqtSignal(str)

    def __init__(self, backend: RadiconBackend, exposure_us: int,
                 n_frames: int, interval_ms: int, timeout_ms: int):
        super().__init__()
        self._backend = backend
        self._exposure_us = exposure_us
        self._n_frames = n_frames
        self._interval_ms = interval_ms
        self._timeout_ms = timeout_ms
        self._abort = False

    def request_stop(self):
        """Aborts before the next frame and interrupts any in-progress
        inter-frame interval sleep. Frames already captured are kept and
        emitted via done() as usual."""
        self._abort = True

    def run(self):
        try:
            try:
                self._backend.set_exposure_us(self._exposure_us)
            except RadiconError:
                pass
            frames: list[np.ndarray] = []
            for i in range(self._n_frames):
                if self._abort:
                    break
                frame = self._backend.snap(timeout_ms=self._timeout_ms)
                frames.append(frame)
                self.frame_ready.emit(i, frame)
                self.progress.emit(i + 1, self._n_frames)
                if self._abort:
                    break
                if self._interval_ms > 0 and i < self._n_frames - 1:
                    remaining = self._interval_ms
                    step = 50
                    while remaining > 0 and not self._abort:
                        QtCore.QThread.msleep(min(step, remaining))
                        remaining -= step
            self.done.emit(frames)
        except Exception as exc:
            self.error.emit(str(exc))


class _DarkWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int)
    done = QtCore.pyqtSignal(object)         # np.ndarray uint16
    error = QtCore.pyqtSignal(str)

    def __init__(self, backend: RadiconBackend, exposure_us: int,
                 n_frames: int, timeout_ms: int):
        super().__init__()
        self._backend = backend
        self._exposure_us = exposure_us
        self._n_frames = n_frames
        self._timeout_ms = timeout_ms
        self._abort = False

    def request_stop(self):
        """Aborts before the next frame, mirroring _SeqWorker. Frames already
        acquired are discarded — a dark average is meaningless if it doesn't
        include all requested frames."""
        self._abort = True

    def run(self):
        try:
            try:
                self._backend.set_exposure_us(self._exposure_us)
            except RadiconError:
                pass
            acc: np.ndarray | None = None
            for i in range(self._n_frames):
                if self._abort:
                    return
                frame = self._backend.snap(timeout_ms=self._timeout_ms).astype(np.float64)
                acc = frame if acc is None else acc + frame
                self.progress.emit(i + 1, self._n_frames)
            avg = np.round(acc / self._n_frames).clip(0, 65535).astype(np.uint16)
            self.done.emit(avg)
        except Exception as exc:
            self.error.emit(str(exc))


class _LiveWorker(QtCore.QThread):
    """Continuously captures frames at a fixed exposure until request_stop() is
    called. Frames are only ever emitted for display — nothing is saved."""

    frame_ready = QtCore.pyqtSignal(object)  # np.ndarray
    error = QtCore.pyqtSignal(str)

    def __init__(self, backend: RadiconBackend, exposure_us: int, timeout_ms: int):
        super().__init__()
        self._backend = backend
        self._exposure_us = exposure_us
        self._timeout_ms = timeout_ms
        self._abort = False
        # Backpressure: only one frame is ever in flight to the GUI thread.
        # If the exposure is short enough that snap() outruns display/render
        # time, later frames are dropped rather than queued — only the latest
        # image matters for a live view.
        self._display_busy = False

    def request_stop(self):
        self._abort = True

    def frame_displayed(self):
        self._display_busy = False

    def run(self):
        try:
            try:
                self._backend.set_exposure_us(self._exposure_us)
            except RadiconError:
                pass
            while not self._abort:
                frame = self._backend.snap(timeout_ms=self._timeout_ms)
                if self._abort:
                    break
                if self._display_busy:
                    continue
                self._display_busy = True
                self.frame_ready.emit(frame)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timeout_ms(exposure_s: float) -> int:
    return int(exposure_s * 2000) + 10_000


# ---------------------------------------------------------------------------
# Instant 1D reduction (XRD) — unit conversion for auto-save
# ---------------------------------------------------------------------------

# unit key -> (filename suffix, CSV/TSV column header)
_UNIT_INFO = {
    "2theta": ("2theta",  "2theta_deg"),
    "q_A":    ("q_A-1",   "q_invA"),
    "q_nm":   ("q_nm-1",  "q_invnm"),
    "d_A":    ("d_A",     "d_A"),
    "d_nm":   ("d_nm",    "d_nm"),
}


def _convert_radial(tth_deg: np.ndarray, wavelength_m: float, unit: str) -> np.ndarray:
    """Convert pyFAI's native 2theta_deg radial axis into another unit.

    Derived directly from theta and wavelength (rather than via pyFAI's own
    radial-unit strings) per the /pyfai-integration skill: this project's
    pyFAI version does not reliably support a d-spacing radial unit, and this
    formula matches the Q-axis convention already used in
    pyFAIwork/integrate_2d_to_1d.py.
    """
    if unit == "2theta":
        return tth_deg
    lam_A = wavelength_m * 1e10
    with np.errstate(divide="ignore", invalid="ignore"):
        theta_rad = np.deg2rad(tth_deg / 2)
        if unit == "q_A":
            return 4 * np.pi * np.sin(theta_rad) / lam_A
        if unit == "q_nm":
            return 4 * np.pi * np.sin(theta_rad) / (lam_A / 10)
        if unit == "d_A":
            return lam_A / (2 * np.sin(theta_rad))
        if unit == "d_nm":
            return (lam_A / 10) / (2 * np.sin(theta_rad))
    raise ValueError(f"unknown radial unit: {unit!r}")


def _write_gsas_fxye(
    path: Path, tth_deg: np.ndarray, intensity: np.ndarray,
    title: str, comments: list[str], bank: int = 1,
) -> None:
    """Write a CW powder pattern as a GSAS FXYE (.gsa) file.

    ESD (per-point uncertainty) is estimated as sqrt(I) (I<=0 -> 1), matching
    GSAS's own convention for count data with no directly measured error bars.
    """
    tth_deg = np.asarray(tth_deg, dtype=float)
    y = np.asarray(intensity, dtype=float)
    e = np.where(y > 0, np.sqrt(y), 1.0)

    x_cd = tth_deg * 100.0   # centidegrees
    n = len(x_cd)
    step_cd = (x_cd[1] - x_cd[0]) if n > 1 else 1.0

    lines = [title[:80]]
    for c in comments:
        lines.append(f"# {c}")
    lines.append(
        f"BANK {bank} {n} {n} CONST "
        f"{x_cd[0]:.5f} {step_cd:.5f} 0 0 FXYE"
    )
    for xi, yi, ei in zip(x_cd, y, e):
        rec = f"{xi:15.5f}{yi:15.5f}{ei:15.5f}"
        lines.append(f"{rec:<80}")

    with open(path, "w", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")


def write_histogram_igor(
    path: str,
    tth_deg,
    intensity,
    esd=None,
    wave_names: tuple[str, ...] = ("twotheta", "yint", "yerr"),
    newline: str = "\n",
) -> str:
    """Write a CW powder pattern as a Z-Rietveld / Igor Text (.histogramIgor) file.

    Unlike GSAS FXYE, the 2theta column is written in degrees as-is (NOT
    multiplied by 100), and esd is never derived internally — pass it
    explicitly (e.g. sqrt(I)) or leave it None for a 2-column file.
    """
    tth_arr = np.asarray(tth_deg, dtype=float)
    int_arr = np.asarray(intensity)
    if tth_arr.shape != int_arr.shape:
        raise ValueError(
            f"tth_deg and intensity length mismatch: {tth_arr.shape} vs {int_arr.shape}"
        )
    esd_arr = None
    if esd is not None:
        esd_arr = np.asarray(esd, dtype=float)
        if esd_arr.shape != tth_arr.shape:
            raise ValueError(
                f"esd length mismatch: {esd_arr.shape} vs {tth_arr.shape}"
            )

    names = list(wave_names[: 3 if esd_arr is not None else 2])
    for name in names:
        if not name.isidentifier():
            raise ValueError(f"invalid Igor wave name: {name!r}")

    is_int = np.issubdtype(int_arr.dtype, np.integer)

    lines = ["IGOR", f"WAVES/O {', '.join(names)}", "BEGIN"]
    for i in range(tth_arr.size):
        cols = [f"{tth_arr[i]:.8g}"]
        cols.append(str(int(int_arr[i])) if is_int else f"{float(int_arr[i]):.8g}")
        if esd_arr is not None:
            cols.append(f"{esd_arr[i]:.8g}")
        lines.append("\t".join(cols))
    lines.append("END")

    with open(path, "w", newline="", encoding="ascii") as f:
        f.write(newline.join(lines) + newline)
    return str(path)


def _save_multiframe_tiff(path: Path, frames: list[np.ndarray]) -> bool:
    """Save a list of uint16 frames as a multi-page uncompressed TIFF.
    Falls back to PIL if available; otherwise saves individual files in a subfolder.
    Returns True on success."""
    try:
        from PIL import Image
        pil = [Image.fromarray(f) for f in frames]
        pil[0].save(str(path), save_all=True, append_images=pil[1:], compression=None)
        return True
    except ImportError:
        pass
    # Fallback: numbered files in a subdirectory
    sub = path.with_suffix("")
    sub.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(frames):
        cv2.imwrite(str(sub / f"{i+1:04d}.tif"), f, _TIFF_OPTS)
    return True


# ---------------------------------------------------------------------------
# Image display widget
# ---------------------------------------------------------------------------

class _ImageLabel(QtWidgets.QLabel):
    """Grayscale image display that scales to fill available space while preserving aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QtGui.QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.setText(tr("(No image)"))
        self.setStyleSheet("color: #666; background: #111; font-size: 13px;")

    def setSourcePixmap(self, px: QtGui.QPixmap):
        self._src = px
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._src is None:
            return
        scaled = self._src.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        painter = QtGui.QPainter(scaled)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1))
        painter.drawRect(0, 0, scaled.width() - 1, scaled.height() - 1)
        painter.end()
        super().setPixmap(scaled)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class RadiconWindow(QtWidgets.QWidget):

    def __init__(self, backend: RadiconBackend, poni_state: "PoniState | None" = None,
                 controller=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rad-icon 2022")
        self._backend = backend
        self._poni_state = poni_state
        self._controller = controller
        self._calib_window: CalibrateInstrumentsWindow | None = None
        self._prefs = self._load_prefs()
        self._worker: _SnapWorker | None = None
        self._seq_worker: _SeqWorker | None = None
        self._dark_worker: _DarkWorker | None = None
        self._live_worker: _LiveWorker | None = None
        self._snap_stop_requested: bool = False
        self._seq_stop_requested: bool = False
        self._dark_stop_requested: bool = False
        self._dark_img: np.ndarray | None = None
        self._dark_path: Path | None = None
        self._dark_exposure_ms: int | None = None
        self._dark_flip_v: bool | None = None
        self._dark_flip_h: bool | None = None
        self._img_arr: np.ndarray | None = None
        self._defect_mask: np.ndarray | None = None
        self._defect_file_path: Path | None = None
        self._defect_n_pixels: int = 0
        self._settings_window: SettingsWindow | None = None
        self._instant1d_npt_cache: tuple | None = None   # (id(ai), img_shape, bin_width_deg) -> npt
        self._build_ui()

        if self._poni_state is not None:
            self._poni_state.poni_changed.connect(self._on_poni_changed)
        self._refresh_instant1d_status()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # ── 左: 画像表示パネル ────────────────────────────────────────
        left_widget = QtWidgets.QWidget()
        left_widget.setStyleSheet("background: #111;")
        left_vbox = QtWidgets.QVBoxLayout(left_widget)
        left_vbox.setContentsMargins(4, 4, 4, 4)
        left_vbox.setSpacing(2)

        self._img_label = _ImageLabel()
        left_vbox.addWidget(self._img_label, 1)

        # Min / Max スライダー
        slider_widget = QtWidgets.QWidget()
        slider_widget.setStyleSheet(
            "background: #1e1e1e; color: #ccc; font-size: 11px;"
        )
        sg = QtWidgets.QGridLayout(slider_widget)
        sg.setContentsMargins(6, 4, 6, 4)
        sg.setSpacing(4)

        sg.addWidget(QtWidgets.QLabel("Min"), 0, 0)
        self._min_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._min_slider.setRange(0, 65535)
        self._min_slider.setValue(0)
        sg.addWidget(self._min_slider, 0, 1)
        self._min_spin = QtWidgets.QSpinBox()
        self._min_spin.setRange(0, 65535)
        self._min_spin.setValue(0)
        self._min_spin.setFixedWidth(68)
        sg.addWidget(self._min_spin, 0, 2)

        sg.addWidget(QtWidgets.QLabel("Max"), 1, 0)
        self._max_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._max_slider.setRange(0, 65535)
        self._max_slider.setValue(65535)
        sg.addWidget(self._max_slider, 1, 1)
        self._max_spin = QtWidgets.QSpinBox()
        self._max_spin.setRange(0, 65535)
        self._max_spin.setValue(65535)
        self._max_spin.setFixedWidth(68)
        sg.addWidget(self._max_spin, 1, 2)

        auto_btn = QtWidgets.QPushButton("Auto")
        auto_btn.setFixedWidth(64)
        auto_btn.clicked.connect(self._auto_levels)
        sg.addWidget(auto_btn, 0, 3, 2, 1)

        # slider ↔ spinbox 双方向バインド
        self._min_slider.valueChanged.connect(self._min_spin.setValue)
        self._min_spin.valueChanged.connect(self._min_slider.setValue)
        self._max_slider.valueChanged.connect(self._max_spin.setValue)
        self._max_spin.valueChanged.connect(self._max_slider.setValue)
        self._min_slider.valueChanged.connect(self._render_preview)
        self._max_slider.valueChanged.connect(self._render_preview)

        left_vbox.addWidget(slider_widget)

        self._img_info_label = QtWidgets.QLabel("—")
        self._img_info_label.setStyleSheet(
            "color: #aaa; font-size: 11px; padding: 2px 4px;"
            "background: #1e1e1e;"
        )
        self._img_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_info_label.setWordWrap(True)
        left_vbox.addWidget(self._img_info_label)

        # ── 右: コントロールパネル ────────────────────────────────────
        right_scroll = QtWidgets.QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_inner = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(right_inner)
        root.setSpacing(8)
        right_scroll.setWidget(right_inner)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_scroll)
        splitter.setSizes([560, 560])
        outer.addWidget(splitter, 1)

        # ── 下部: Instant 1D reduction (XRD) パネル ────────────────────
        self._instant1d_panel = self._build_instant1d_panel()
        self._instant1d_panel.setVisible(False)
        outer.addWidget(self._instant1d_panel, 0)

        # ── Detector settings ──────────────────────────────────────────
        det_box = QtWidgets.QGroupBox(tr("Detector settings"))
        det_form = QtWidgets.QFormLayout(det_box)
        # macOS style defaults SH_FormLayoutFormAlignment to center the whole
        # form when it doesn't fill the width — force it flush left instead,
        # matching every other section. Right-aligned labels keep the ":" lined up.
        det_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        det_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        det_form.setVerticalSpacing(10)
        det_form.setHorizontalSpacing(12)

        binning_label = tr("2 × 2") if self._backend.width < 2000 else tr("None")
        info = QtWidgets.QLabel(
            tr("{width} × {height} px  (binning: {binning})",
               width=self._backend.width, height=self._backend.height, binning=binning_label)
        )
        info.setStyleSheet("color: gray;")
        det_form.addRow(tr("Resolution:"), info)

        self._exp_spin = QtWidgets.QDoubleSpinBox()
        self._exp_spin.setRange(0.001, 60.0)
        self._exp_spin.setDecimals(3)
        self._exp_spin.setSuffix(" s")
        self._exp_spin.setSingleStep(0.1)
        self._exp_spin.setValue(self._prefs.get("exposure_s", 60.0))
        det_form.addRow(tr("Exposure time:"), self._exp_spin)

        flip_widget = QtWidgets.QWidget()
        flip_hbox = QtWidgets.QHBoxLayout(flip_widget)
        flip_hbox.setContentsMargins(0, 0, 0, 0)
        flip_hbox.setSpacing(12)
        self._flip_v_chk = QtWidgets.QCheckBox(tr("Vertical"))
        self._flip_v_chk.setChecked(self._prefs.get("flip_v", True))
        self._flip_v_chk.setMinimumWidth(120)
        self._flip_h_chk = QtWidgets.QCheckBox(tr("Horizontal"))
        self._flip_h_chk.setChecked(self._prefs.get("flip_h", False))
        self._flip_h_chk.setMinimumWidth(120)
        flip_hbox.addWidget(self._flip_v_chk)
        flip_hbox.addWidget(self._flip_h_chk)
        flip_hbox.addStretch()
        det_form.addRow(tr("Flip:"), flip_widget)
        self._flip_v_chk.toggled.connect(self._on_flip_toggle)
        self._flip_h_chk.toggled.connect(self._on_flip_toggle)

        root.addWidget(det_box)

        # ── Dark current ─────────────────────────────────────────────
        dark_box = QtWidgets.QGroupBox(tr("Dark current"))
        dark_layout = QtWidgets.QVBoxLayout(dark_box)
        dark_layout.setSpacing(8)

        dark_load_row = QtWidgets.QHBoxLayout()
        self._dark_load_btn = QtWidgets.QPushButton(tr("Load"))
        self._dark_load_btn.clicked.connect(self._load_dark)
        dark_load_row.addWidget(self._dark_load_btn)
        dark_load_row.addStretch()
        dark_layout.addLayout(dark_load_row)

        dark_acq_row = QtWidgets.QHBoxLayout()
        self._dark_acq_btn = QtWidgets.QPushButton(tr("Acquire"))
        self._dark_acq_btn.clicked.connect(self._acquire_dark)
        dark_acq_row.addWidget(self._dark_acq_btn)
        dark_acq_row.addWidget(QtWidgets.QLabel(tr("Accumulations:")))
        self._dark_n_edit = QtWidgets.QLineEdit(str(self._prefs.get("dark_n_frames", 10)))
        self._dark_n_edit.setFixedWidth(50)
        self._dark_n_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        dark_acq_row.addWidget(self._dark_n_edit)
        dark_acq_row.addStretch()
        dark_layout.addLayout(dark_acq_row)

        self._dark_apply_chk = QtWidgets.QCheckBox(tr("Apply after saving"))
        self._dark_apply_chk.setChecked(self._prefs.get("dark_apply_after_save", True))
        dark_layout.addWidget(self._dark_apply_chk)

        self._dark_status_label = QtWidgets.QLabel(tr("Current dark current: none"))
        self._dark_status_label.setStyleSheet("color: gray;")
        self._dark_status_label.setWordWrap(True)
        dark_layout.addWidget(self._dark_status_label)

        root.addWidget(dark_box)

        # ── Save settings ───────────────────────────────────────────────
        save_box = QtWidgets.QGroupBox(tr("Save settings"))
        save_vbox = QtWidgets.QVBoxLayout(save_box)
        save_vbox.setSpacing(8)

        save_row = QtWidgets.QHBoxLayout()
        save_row.addWidget(QtWidgets.QLabel(tr("Save to:")))
        self._dir_edit = QtWidgets.QLineEdit(
            self._prefs.get("save_dir", str(Path.home()))
        )
        self._browse_btn = QtWidgets.QPushButton(tr("Browse..."))
        self._browse_btn.setFixedWidth(70)
        self._browse_btn.clicked.connect(self._browse_dir)
        save_row.addWidget(self._dir_edit)
        save_row.addWidget(self._browse_btn)
        save_vbox.addLayout(save_row)

        filename_row = QtWidgets.QHBoxLayout()
        filename_row.addWidget(QtWidgets.QLabel(tr("Filename:")))
        self._filename_edit = QtWidgets.QLineEdit(self._prefs.get("filename", "image"))
        filename_row.addWidget(self._filename_edit)
        save_vbox.addLayout(filename_row)

        # Filename suffix mode
        save_vbox.addWidget(QtWidgets.QLabel(tr("Suffix:")))
        suffix_grid = QtWidgets.QGridLayout()
        self._suffix_none_radio = QtWidgets.QRadioButton(tr("None"))
        self._suffix_datetime_radio = QtWidgets.QRadioButton("YYYY-DD-MM-HH-MM-SS")
        self._suffix_time_radio = QtWidgets.QRadioButton("HH-MM-SS")
        self._suffix_index_radio = QtWidgets.QRadioButton("_index")
        self._suffix_index3_radio = QtWidgets.QRadioButton("_index固定長3桁")

        _suffix_mode_by_radio = {
            self._suffix_none_radio: "none",
            self._suffix_datetime_radio: "datetime_full",
            self._suffix_time_radio: "datetime_time",
            self._suffix_index_radio: "index",
            self._suffix_index3_radio: "index3",
        }
        saved_suffix_mode = self._prefs.get("filename_suffix_mode", "index")
        suffix_grp = QtWidgets.QButtonGroup(self)
        for radio, mode in _suffix_mode_by_radio.items():
            suffix_grp.addButton(radio)
            radio.setChecked(mode == saved_suffix_mode)
        if not suffix_grp.checkedButton():
            self._suffix_index_radio.setChecked(True)

        suffix_grid.addWidget(self._suffix_none_radio, 0, 0)
        suffix_grid.addWidget(self._suffix_datetime_radio, 0, 1)
        suffix_grid.addWidget(self._suffix_time_radio, 1, 0)
        suffix_grid.addWidget(self._suffix_index_radio, 1, 1)
        suffix_grid.addWidget(self._suffix_index3_radio, 2, 0)
        save_vbox.addLayout(suffix_grid)

        # Dark-current correction checkbox
        self._dark_correct_chk = QtWidgets.QCheckBox(tr("Dark-current correction"))
        self._dark_correct_chk.setChecked(self._prefs.get("dark_correct_enabled", True))
        self._dark_correct_chk.setToolTip(tr("When checked, subtracts the dark image from acquired images"))
        save_vbox.addWidget(self._dark_correct_chk)

        # Pixel-defect correction
        save_vbox.addWidget(QtWidgets.QLabel(tr("Pixel-defect correction:")))
        defect_chk_row = QtWidgets.QHBoxLayout()
        self._defect_none_radio = QtWidgets.QRadioButton(tr("None"))
        self._defect_median_radio = QtWidgets.QRadioButton("median")
        self._defect_neg1_radio = QtWidgets.QRadioButton(tr("Replace with -1"))

        _defect_mode_by_radio = {
            self._defect_none_radio: "none",
            self._defect_median_radio: "median",
            self._defect_neg1_radio: "neg1",
        }
        # Back-compat: fall back to the old boolean pref if the new mode key
        # has never been saved yet.
        _default_defect_mode = "median" if self._prefs.get("defect_correct_enabled", True) else "none"
        saved_defect_mode = self._prefs.get("defect_correct_mode", _default_defect_mode)
        defect_grp = QtWidgets.QButtonGroup(self)
        for radio, mode in _defect_mode_by_radio.items():
            defect_grp.addButton(radio)
            radio.setChecked(mode == saved_defect_mode)
        if not defect_grp.checkedButton():
            self._defect_median_radio.setChecked(True)

        defect_chk_row.addWidget(self._defect_none_radio)
        defect_chk_row.addWidget(self._defect_median_radio)
        self._defect_kernel_combo = QtWidgets.QComboBox()
        self._defect_kernel_combo.addItems(["3×3", "4×4", "5×5", "6×6"])
        self._defect_kernel_combo.setCurrentText(self._prefs.get("defect_kernel_size", "3×3"))
        self._defect_kernel_combo.setEnabled(self._defect_median_radio.isChecked())
        defect_chk_row.addWidget(self._defect_kernel_combo)
        defect_chk_row.addWidget(self._defect_neg1_radio)
        defect_chk_row.addStretch()
        save_vbox.addLayout(defect_chk_row)

        defect_file_row = QtWidgets.QHBoxLayout()
        self._defect_file_edit = QtWidgets.QLineEdit()
        self._defect_file_edit.setReadOnly(True)
        self._defect_file_edit.setPlaceholderText(tr("No defect file selected"))
        defect_file_row.addWidget(self._defect_file_edit)
        self._defect_file_btn = QtWidgets.QPushButton(tr("Browse..."))
        self._defect_file_btn.setFixedWidth(70)
        self._defect_file_btn.clicked.connect(self._browse_defect_file)
        defect_file_row.addWidget(self._defect_file_btn)
        save_vbox.addLayout(defect_file_row)

        self._defect_status_label = QtWidgets.QLabel()
        self._defect_status_label.setStyleSheet("color: gray; font-size: 11px;")
        save_vbox.addWidget(self._defect_status_label)

        self._defect_median_radio.toggled.connect(self._defect_kernel_combo.setEnabled)
        self._defect_none_radio.toggled.connect(lambda _: self._save_prefs())
        self._defect_median_radio.toggled.connect(lambda _: self._save_prefs())
        self._defect_neg1_radio.toggled.connect(lambda _: self._save_prefs())
        self._defect_kernel_combo.currentTextChanged.connect(lambda _: self._save_prefs())

        root.addWidget(save_box)

        # ── Acquisition ──────────────────────────────────────────────────
        acq_box = QtWidgets.QGroupBox(tr("Acquisition"))
        acq_vbox = QtWidgets.QVBoxLayout(acq_box)
        acq_vbox.setSpacing(6)

        # Subtle background so the three acquisition modes read as distinct
        # groups instead of one continuous block.
        _mode_frame_style = (
            "QFrame { background-color: rgba(0, 0, 0, 18); border-radius: 6px; }"
        )

        # ─ Live view ──────────────────────────────────────────────────
        live_frame = QtWidgets.QFrame()
        live_frame.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        live_frame.setStyleSheet(_mode_frame_style)
        live_vbox = QtWidgets.QVBoxLayout(live_frame)
        live_vbox.setContentsMargins(10, 8, 10, 10)
        live_vbox.setSpacing(6)

        live_title = QtWidgets.QLabel(tr("Live view"))
        live_title.setStyleSheet("font-weight: bold; background: transparent;")
        live_vbox.addWidget(live_title)

        live_ctrl = QtWidgets.QHBoxLayout()

        self._live_btn = QtWidgets.QPushButton("LIVE")
        self._live_btn.setFixedHeight(40)
        self._live_btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum,
                                      QtWidgets.QSizePolicy.Policy.Fixed)
        self._live_btn.setToolTip(tr("Continuously captures at the current exposure time without saving, until stopped"))
        self._live_btn.clicked.connect(self._start_live)
        live_ctrl.addWidget(self._live_btn)
        live_ctrl.addStretch()

        live_vbox.addLayout(live_ctrl)
        acq_vbox.addWidget(live_frame)

        # ─ Single shot ────────────────────────────────────────────────
        snap_frame = QtWidgets.QFrame()
        snap_frame.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        snap_frame.setStyleSheet(_mode_frame_style)
        snap_vbox = QtWidgets.QVBoxLayout(snap_frame)
        snap_vbox.setContentsMargins(10, 8, 10, 10)
        snap_vbox.setSpacing(6)

        snap_title = QtWidgets.QLabel(tr("Single shot"))
        snap_title.setStyleSheet("font-weight: bold; background: transparent;")
        snap_vbox.addWidget(snap_title)

        snap_ctrl = QtWidgets.QHBoxLayout()

        self._snap_btn = QtWidgets.QPushButton(tr("Single shot"))
        self._snap_btn.setFixedHeight(40)
        self._snap_btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum,
                                      QtWidgets.QSizePolicy.Policy.Fixed)
        self._snap_btn.clicked.connect(self._snap)
        snap_ctrl.addWidget(self._snap_btn)

        snap_ctrl.addStretch()

        self._snap_tiff_radio = QtWidgets.QRadioButton("Tiff")
        self._snap_tiff_radio.setChecked(True)
        self._snap_raw_radio = QtWidgets.QRadioButton("RAW")
        self._snap_raw_radio.setEnabled(False)
        snap_fmt_grp = QtWidgets.QButtonGroup(self)
        snap_fmt_grp.addButton(self._snap_tiff_radio)
        snap_fmt_grp.addButton(self._snap_raw_radio)
        snap_ctrl.addWidget(self._snap_tiff_radio)
        snap_ctrl.addWidget(self._snap_raw_radio)

        snap_vbox.addLayout(snap_ctrl)
        acq_vbox.addWidget(snap_frame)

        # ─ Sequential acquisition ─────────────────────────────────────
        seq_frame = QtWidgets.QFrame()
        seq_frame.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        seq_frame.setStyleSheet(_mode_frame_style)
        seq_vbox = QtWidgets.QVBoxLayout(seq_frame)
        seq_vbox.setContentsMargins(10, 8, 10, 10)
        seq_vbox.setSpacing(6)

        seq_title = QtWidgets.QLabel(tr("Sequential acquisition"))
        seq_title.setStyleSheet("font-weight: bold; background: transparent;")
        seq_vbox.addWidget(seq_title)

        # Row 1: button / frame count / interval / format
        seq_row1 = QtWidgets.QHBoxLayout()

        self._seq_btn = QtWidgets.QPushButton(tr("Sequential acquisition"))
        self._seq_btn.setFixedHeight(40)
        self._seq_btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum,
                                     QtWidgets.QSizePolicy.Policy.Fixed)
        self._seq_btn.clicked.connect(self._seq_start)
        seq_row1.addWidget(self._seq_btn)

        seq_row1.addWidget(QtWidgets.QLabel(tr("Frames:")))
        self._seq_n_edit = QtWidgets.QLineEdit(str(self._prefs.get("seq_n_frames", 20)))
        self._seq_n_edit.setFixedWidth(52)
        self._seq_n_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        seq_row1.addWidget(self._seq_n_edit)

        seq_row1.addWidget(QtWidgets.QLabel(tr("Interval [ms]:")))
        self._seq_interval_edit = QtWidgets.QLineEdit(
            str(self._prefs.get("seq_interval_ms", 0))
        )
        self._seq_interval_edit.setFixedWidth(52)
        self._seq_interval_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        seq_row1.addWidget(self._seq_interval_edit)

        seq_row1.addStretch()

        self._seq_tiff_radio = QtWidgets.QRadioButton("Tiff")
        self._seq_tiff_radio.setChecked(True)
        self._seq_raw_radio = QtWidgets.QRadioButton("RAW")
        self._seq_raw_radio.setEnabled(False)
        seq_fmt_grp = QtWidgets.QButtonGroup(self)
        seq_fmt_grp.addButton(self._seq_tiff_radio)
        seq_fmt_grp.addButton(self._seq_raw_radio)
        seq_row1.addWidget(self._seq_tiff_radio)
        seq_row1.addWidget(self._seq_raw_radio)

        seq_vbox.addLayout(seq_row1)

        # Row 2: save-mode checkboxes + estimated time
        seq_row2 = QtWidgets.QHBoxLayout()
        self._seq_indiv_chk = QtWidgets.QCheckBox(tr("Save individually"))
        self._seq_indiv_chk.setChecked(self._prefs.get("seq_indiv_save", True))
        self._seq_indiv_chk.setMinimumWidth(180)
        # self._seq_batch_chk = QtWidgets.QCheckBox(tr("Save as stack"))
        # self._seq_batch_chk.setChecked(self._prefs.get("seq_batch_save", False))
        self._seq_avg_chk = QtWidgets.QCheckBox(tr("Save average"))
        self._seq_avg_chk.setChecked(self._prefs.get("seq_avg_save", False))
        seq_row2.addWidget(self._seq_indiv_chk)
        # seq_row2.addWidget(self._seq_batch_chk)
        seq_row2.addWidget(self._seq_avg_chk)
        seq_row2.addStretch()
        self._seq_time_label = QtWidgets.QLabel()
        self._seq_time_label.setStyleSheet("color: gray;")
        seq_row2.addWidget(self._seq_time_label)

        seq_vbox.addLayout(seq_row2)
        acq_vbox.addWidget(seq_frame)

        # Shared status label
        self._status_label = QtWidgets.QLabel(tr("Idle"))
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        acq_vbox.addWidget(self._status_label)

        self._stop_btn = QtWidgets.QPushButton(tr("Stop acquisition"))
        self._stop_btn.setFixedHeight(64)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet("QPushButton { color: #b00; font-weight: bold; }")
        self._stop_btn.clicked.connect(self._stop_acquisition)
        acq_vbox.addWidget(self._stop_btn)

        root.addWidget(acq_box)

        root.addStretch()

        self._instant1d_chk = QtWidgets.QCheckBox(tr("Instant 1D reduction (XRD)"))
        # Connect before setChecked so the initial state always drives
        # _on_instant1d_toggled (panel visibility / status refresh) in sync —
        # previously setChecked ran before the connect, so a checked initial
        # state (restored from prefs) left the checkbox showing checked while
        # the panel/backend stayed in its unchecked (hidden, inactive) state.
        self._instant1d_chk.toggled.connect(self._on_instant1d_toggled)
        self._instant1d_chk.setChecked(False)
        root.addWidget(self._instant1d_chk)

        right_inner.setMinimumWidth(520)
        self.resize(1160, 760)

        # Connect estimated-time signals after all widgets are created
        self._exp_spin.valueChanged.connect(self._update_seq_est_time)
        self._seq_n_edit.textChanged.connect(self._update_seq_est_time)
        self._seq_interval_edit.textChanged.connect(self._update_seq_est_time)
        self._update_seq_est_time()
        self._exp_spin.valueChanged.connect(lambda _: self._refresh_dark_exposure_warning())

        # Auto-load defect file from prefs or fall back to the bundled default
        _saved_defect = self._prefs.get("defect_file_path", "")
        try:
            if _saved_defect and Path(_saved_defect).exists():
                self._load_defect_file(Path(_saved_defect))
            elif _DEFAULT_DEFECT_FILE.exists():
                self._load_defect_file(_DEFAULT_DEFECT_FILE)
        except Exception as _exc:
            self._defect_status_label.setText(tr("Load error: {error}", error=_exc))
            self._defect_status_label.setStyleSheet("color: red; font-size: 11px;")

    # ------------------------------------------------------------------
    # Instant 1D reduction (XRD) — UI construction
    # ------------------------------------------------------------------

    def _build_instant1d_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QGroupBox(tr("Instant 1D reduction (XRD)"))
        panel.setMaximumHeight(280)
        p_vbox = QtWidgets.QVBoxLayout(panel)
        p_vbox.setSpacing(6)

        # ── Status row ───────────────────────────────────────────────
        status_row = QtWidgets.QHBoxLayout()
        self._instant1d_status_label = QtWidgets.QLabel()
        self._instant1d_status_label.setWordWrap(True)
        status_row.addWidget(self._instant1d_status_label, 1)
        self._instant1d_settings_btn = QtWidgets.QPushButton()
        self._instant1d_settings_btn.clicked.connect(self._open_detector_calibration)
        status_row.addWidget(self._instant1d_settings_btn, 0)
        self._calib_wizard_btn = QtWidgets.QPushButton(tr("Calibration wizard…"))
        self._calib_wizard_btn.clicked.connect(self._open_calibrate_instruments)
        if self._controller is None:
            self._calib_wizard_btn.setEnabled(False)
            self._calib_wizard_btn.setToolTip(
                tr("Stage controller not available (opened without a controller).")
            )
        status_row.addWidget(self._calib_wizard_btn, 0)
        p_vbox.addLayout(status_row)

        # ── Controls row: angular resolution + auto-save + format ────
        ctrl_row = QtWidgets.QHBoxLayout()
        ctrl_row.addWidget(QtWidgets.QLabel(tr("Angular resolution (deg/bin):")))
        self._instant1d_binwidth_spin = QtWidgets.QDoubleSpinBox()
        self._instant1d_binwidth_spin.setRange(0.001, 1.0)
        self._instant1d_binwidth_spin.setDecimals(3)
        self._instant1d_binwidth_spin.setSingleStep(0.001)
        self._instant1d_binwidth_spin.setValue(self._prefs.get("instant1d_bin_width_deg", 0.01))
        self._instant1d_binwidth_spin.valueChanged.connect(self._on_instant1d_binwidth_changed)
        ctrl_row.addWidget(self._instant1d_binwidth_spin)

        ctrl_row.addSpacing(12)
        self._instant1d_autosave_chk = QtWidgets.QCheckBox(tr("Auto-save 1D data"))
        self._instant1d_autosave_chk.setChecked(self._prefs.get("instant1d_autosave", False))
        self._instant1d_autosave_chk.toggled.connect(lambda _: self._save_prefs())
        ctrl_row.addWidget(self._instant1d_autosave_chk)

        ctrl_row.addWidget(QtWidgets.QLabel(tr("Format:")))
        self._instant1d_fmt_csv_chk = QtWidgets.QCheckBox("CSV")
        self._instant1d_fmt_csv_chk.setChecked(self._prefs.get("instant1d_fmt_csv", False))
        self._instant1d_fmt_csv_chk.toggled.connect(lambda _: self._save_prefs())
        self._instant1d_fmt_tsv_chk = QtWidgets.QCheckBox("TSV")
        self._instant1d_fmt_tsv_chk.setChecked(self._prefs.get("instant1d_fmt_tsv", False))
        self._instant1d_fmt_tsv_chk.toggled.connect(lambda _: self._save_prefs())
        self._instant1d_fmt_gsas_chk = QtWidgets.QCheckBox("GSAS (.gsa)")
        self._instant1d_fmt_gsas_chk.setChecked(self._prefs.get("instant1d_fmt_gsas", False))
        self._instant1d_fmt_gsas_chk.setToolTip(
            tr("GSAS FXYE format, 2θ axis only (fixed-step 2theta, as required by the format).")
        )
        self._instant1d_fmt_gsas_chk.toggled.connect(lambda _: self._save_prefs())
        self._instant1d_fmt_igor_chk = QtWidgets.QCheckBox("Z-Rietveld (.histogramIgor)")
        self._instant1d_fmt_igor_chk.setChecked(self._prefs.get("instant1d_fmt_igor", False))
        self._instant1d_fmt_igor_chk.setToolTip(
            tr("Igor Text (ITX) format, 2θ axis in degrees (not centidegrees).")
        )
        self._instant1d_fmt_igor_chk.toggled.connect(lambda _: self._save_prefs())
        ctrl_row.addWidget(self._instant1d_fmt_csv_chk)
        ctrl_row.addWidget(self._instant1d_fmt_tsv_chk)
        ctrl_row.addWidget(self._instant1d_fmt_gsas_chk)
        ctrl_row.addWidget(self._instant1d_fmt_igor_chk)
        ctrl_row.addStretch()
        p_vbox.addLayout(ctrl_row)

        # ── Units row ─────────────────────────────────────────────────
        unit_row = QtWidgets.QHBoxLayout()
        unit_row.addWidget(QtWidgets.QLabel(tr("X-axis units to save:")))
        saved_units = self._prefs.get("instant1d_units", ["2theta"])
        self._instant1d_unit_chks: dict[str, QtWidgets.QCheckBox] = {}
        _unit_labels = {
            "2theta": "2θ (deg)",
            "q_A":    "Q (Å⁻¹)",
            "q_nm":   "Q (nm⁻¹)",
            "d_A":    "d (Å)",
            "d_nm":   "d (nm)",
        }
        for key, label in _unit_labels.items():
            chk = QtWidgets.QCheckBox(label)
            chk.setChecked(key in saved_units)
            chk.toggled.connect(lambda _: self._save_prefs())
            self._instant1d_unit_chks[key] = chk
            unit_row.addWidget(chk)
        unit_row.addStretch()
        p_vbox.addLayout(unit_row)

        # ── Plot ──────────────────────────────────────────────────────
        self._instant1d_plot = pg.PlotWidget(background="w")
        self._instant1d_plot.setLabel("bottom", tr("2θ (deg)"))
        self._instant1d_plot.setLabel("left", tr("Intensity (a.u.)"))
        self._instant1d_plot.showGrid(x=True, y=True, alpha=0.3)
        for axis in ("bottom", "left"):
            self._instant1d_plot.getAxis(axis).setTextPen("k")
            self._instant1d_plot.getAxis(axis).setPen("k")
        self._instant1d_plot.setMinimumHeight(140)
        self._instant1d_curve = self._instant1d_plot.plot(
            pen=pg.mkPen((40, 80, 160), width=1)
        )
        p_vbox.addWidget(self._instant1d_plot)

        return panel

    # ------------------------------------------------------------------
    # Instant 1D reduction (XRD) — behaviour
    # ------------------------------------------------------------------

    def _on_instant1d_toggled(self, checked: bool) -> None:
        self._instant1d_panel.setVisible(checked)
        self._save_prefs()
        self._refresh_instant1d_status()
        if checked and self._img_arr is not None:
            self._maybe_run_instant_1d(self._img_arr, save_path=None)

    def _on_instant1d_binwidth_changed(self, _value: float) -> None:
        self._instant1d_npt_cache = None   # angular resolution changed — recompute npt
        self._save_prefs()

    def _refresh_instant1d_status(self) -> None:
        s = self._poni_state
        if s is None:
            text  = tr("PoniState unavailable (standalone mode).")
            style = "color: gray;"
            btn_visible = False
        elif not s.is_calibrated():
            text  = tr("✕ No poni file registered.")
            style = "color: #a00; font-weight: bold;"
            btn_visible = True
            self._instant1d_settings_btn.setText(tr("Register poni file…"))
        else:
            name = s.prm_path.name if s.prm_path else "?"
            text  = tr("● poni file registered ({name})", name=name)
            style = "color: green; font-weight: bold;"
            btn_visible = True
            self._instant1d_settings_btn.setText(tr("View / change…"))
        self._instant1d_status_label.setText(text)
        self._instant1d_status_label.setStyleSheet(style)
        self._instant1d_settings_btn.setVisible(btn_visible)

    def _on_poni_changed(self) -> None:
        self._instant1d_npt_cache = None   # geometry changed — recompute npt
        self._refresh_instant1d_status()
        if self._instant1d_chk.isChecked() and self._img_arr is not None:
            self._maybe_run_instant_1d(self._img_arr, save_path=None)

    def _open_detector_calibration(self) -> None:
        """Open Settings on the Detector Calibration page (default page 0)."""
        if self._poni_state is None:
            return
        if self._settings_window is None:
            self._settings_window = SettingsWindow(
                poni_state=self._poni_state,
                parent=self,
            )
            self._settings_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._settings_window.destroyed.connect(
                lambda: setattr(self, "_settings_window", None)
            )
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _open_calibrate_instruments(self) -> None:
        """Open the multi-position calibration wizard, sharing this window's
        backend/controller/poni_state and flip settings."""
        if self._calib_window is None:
            self._calib_window = CalibrateInstrumentsWindow(
                backend=self._backend,
                controller=self._controller,
                poni_state=self._poni_state,
                get_radicon_window=lambda: self,
                parent=self,
            )
            self._calib_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._calib_window.destroyed.connect(
                lambda: setattr(self, "_calib_window", None)
            )
        self._calib_window.show()
        self._calib_window.raise_()
        self._calib_window.activateWindow()

    def _compute_npt_for_bin_width(self, ai, img_shape: tuple, bin_width_deg: float) -> int:
        """Number of radial (2theta) bins so each bin spans ~bin_width_deg,
        derived from the full 2theta range covered by the detector geometry
        for this image shape (not from pixel intensities, so this only needs
        recomputing when the geometry or image shape changes)."""
        try:
            tth = ai.center_array(img_shape, unit="2th_deg", scale=True)
            tth_min, tth_max = float(np.nanmin(tth)), float(np.nanmax(tth))
            return max(10, int(np.ceil((tth_max - tth_min) / bin_width_deg)))
        except Exception:
            return 2000

    def _get_cached_npt(self, ai, img_shape: tuple, bin_width_deg: float) -> int:
        key = (id(ai), img_shape, bin_width_deg)
        if self._instant1d_npt_cache is not None and self._instant1d_npt_cache[0] == key:
            return self._instant1d_npt_cache[1]
        npt = self._compute_npt_for_bin_width(ai, img_shape, bin_width_deg)
        self._instant1d_npt_cache = (key, npt)
        return npt

    def _maybe_run_instant_1d(self, img: np.ndarray, save_path: Path | None) -> None:
        if not self._instant1d_chk.isChecked():
            return
        s = self._poni_state
        if s is None or not s.is_calibrated():
            return
        try:
            npt = self._get_cached_npt(
                s.ai, img.shape, self._instant1d_binwidth_spin.value()
            )
            result = s.ai.integrate1d(
                img.astype(np.float32),
                npt=npt,
                unit="2th_deg",
                method=("no", "histogram", "cython"),
                correctSolidAngle=True,
                polarization_factor=0.95,
            )
        except Exception as exc:
            self._instant1d_status_label.setText(tr("[1D reduction error] {error}", error=exc))
            self._instant1d_status_label.setStyleSheet("color: red;")
            return
        self._instant1d_curve.setData(result.radial, result.intensity)
        if save_path is not None and self._instant1d_autosave_chk.isChecked():
            self._auto_save_instant_1d(
                result.radial, result.intensity, s.ai.wavelength, s.ai.dist, save_path
            )

    def _auto_save_instant_1d(
        self, tth_deg: np.ndarray, intensity: np.ndarray,
        wavelength_m: float, dist_m: float, save_path: Path,
    ) -> None:
        formats = []
        if self._instant1d_fmt_csv_chk.isChecked():
            formats.append(("csv", ","))
        if self._instant1d_fmt_tsv_chk.isChecked():
            formats.append(("tsv", "\t"))
        units = [key for key, chk in self._instant1d_unit_chks.items() if chk.isChecked()]

        if formats and units:
            for unit in units:
                x = _convert_radial(tth_deg, wavelength_m, unit)
                suffix, header = _UNIT_INFO[unit]
                for ext, delim in formats:
                    path = save_path.with_name(f"{save_path.stem}_{suffix}.{ext}")
                    try:
                        np.savetxt(
                            str(path), np.column_stack([x, intensity]),
                            delimiter=delim, header=f"{header}{delim}intensity",
                            comments="",
                        )
                    except Exception as exc:
                        _log.warning("Failed to save 1D reduction file %s: %s", path, exc)

        if self._instant1d_fmt_gsas_chk.isChecked():
            gsas_path = save_path.with_name(f"{save_path.stem}.gsa")
            try:
                _write_gsas_fxye(
                    gsas_path, tth_deg, intensity,
                    title=gsas_path.name,
                    comments=[
                        f"wavelength = {wavelength_m * 1e10:.6f} A",
                        f"detector distance = {dist_m * 1e3:.3f} mm",
                        f"Rad-icon 2022 exposure = {round(self._exp_spin.value() * 1000)} ms, "
                        f"binning {'2x2' if self._backend.width < 2000 else '1x1'}",
                    ],
                )
            except Exception as exc:
                _log.warning("Failed to save GSAS file %s: %s", gsas_path, exc)

        if self._instant1d_fmt_igor_chk.isChecked():
            igor_path = save_path.with_name(f"{save_path.stem}.histogramIgor")
            try:
                write_histogram_igor(str(igor_path), tth_deg, intensity)
            except Exception as exc:
                _log.warning("Failed to save Z-Rietveld file %s: %s", igor_path, exc)

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def _build_metadata(self, image_type: str, extra: dict | None = None) -> dict:
        binning = "2x2" if self._backend.width < 2000 else "1x1"
        meta = {
            "image_type": image_type,
            "exposure_ms": round(self._exp_spin.value() * 1000),
            "binning": binning,
            "flip_v": self._flip_v_chk.isChecked(),
            "flip_h": self._flip_h_chk.isChecked(),
            "detector": "Rad-icon 2022",
            "beamline": "BL-18C",
            "datetime": datetime.now().isoformat(timespec="seconds"),
        }
        if extra:
            meta.update(extra)
        return meta

    # ------------------------------------------------------------------
    # Filename / suffix generation
    # ------------------------------------------------------------------

    def _current_suffix_mode(self) -> str:
        if self._suffix_datetime_radio.isChecked():
            return "datetime_full"
        if self._suffix_time_radio.isChecked():
            return "datetime_time"
        if self._suffix_index_radio.isChecked():
            return "index"
        if self._suffix_index3_radio.isChecked():
            return "index3"
        return "none"

    def _next_index_for_prefix(self, save_dir: Path, prefix: str) -> int:
        """Sequential number (starting at 1) among existing files in *save_dir*
        that share the same *prefix* (base filename)."""
        if not save_dir.exists():
            return 1
        count = sum(1 for f in save_dir.iterdir() if f.is_file() and f.name.startswith(prefix))
        return count + 1

    def _build_save_path(self, save_dir: Path, extension: str = ".tif") -> Path:
        prefix = self._filename_edit.text().strip() or "image"
        mode = self._current_suffix_mode()
        if mode == "none":
            suffix_str = ""
        elif mode == "datetime_full":
            suffix_str = "_" + datetime.now().strftime("%Y-%d-%m-%H-%M-%S")
        elif mode == "datetime_time":
            suffix_str = "_" + datetime.now().strftime("%H-%M-%S")
        elif mode == "index":
            suffix_str = f"_{self._next_index_for_prefix(save_dir, prefix)}"
        else:  # "index3"
            suffix_str = f"_{self._next_index_for_prefix(save_dir, prefix):03d}"
        return save_dir / f"{prefix}{suffix_str}{extension}"

    # ------------------------------------------------------------------
    # Slots — single-shot
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Image display
    # ------------------------------------------------------------------

    def _display_image(self, img: np.ndarray, filename: str = ""):
        self._img_arr = img
        h, w = img.shape
        lo_img, hi_img = int(img.min()), int(img.max())
        mean = float(img.mean())
        name = Path(filename).name if filename else ""
        parts = [f"{w}×{h}", f"min={lo_img}", f"max={hi_img}", f"mean={mean:.0f}"]
        if name:
            parts.insert(0, name)
        self._img_info_label.setText("  ".join(parts))
        self._render_preview()
        self._maybe_run_instant_1d(img, Path(filename) if filename else None)

    def _render_preview(self):
        if self._img_arr is None:
            return
        lo = self._min_slider.value()
        hi = self._max_slider.value()
        if hi <= lo:
            hi = lo + 1
        arr8 = ((self._img_arr.astype(np.float32) - lo) / (hi - lo) * 255)
        arr8 = arr8.clip(0, 255).astype(np.uint8)
        h, w = arr8.shape
        qimg = QtGui.QImage(
            arr8.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8
        )
        self._img_label.setSourcePixmap(QtGui.QPixmap.fromImage(qimg))

    def _auto_levels(self):
        if self._img_arr is None:
            return
        lo, hi = int(self._img_arr.min()), int(self._img_arr.max())
        self._min_slider.setValue(lo)
        self._max_slider.setValue(hi)

    # ------------------------------------------------------------------
    # Slots — directory browse
    # ------------------------------------------------------------------

    def _browse_dir(self):
        current = self._dir_edit.text() or str(Path.home())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, tr("Select save folder"), current
        )
        if chosen:
            self._dir_edit.setText(chosen)
            self._save_prefs()

    # ------------------------------------------------------------------
    # Slots — live view
    # ------------------------------------------------------------------

    def _start_live(self):
        if self._any_busy():
            return

        exposure_us = int(self._exp_spin.value() * 1_000_000)
        self._save_prefs()
        self._set_busy(True)
        self._status_label.setText(tr("Live view running... (not saved)"))

        self._live_worker = _LiveWorker(self._backend, exposure_us,
                                        _timeout_ms(self._exp_spin.value()))
        self._live_worker.frame_ready.connect(self._on_live_frame)
        self._live_worker.error.connect(self._on_live_error)
        self._live_worker.finished.connect(lambda: self._set_busy(False))
        self._live_worker.start()
        self._stop_btn.setEnabled(True)

    def _on_live_frame(self, frame: np.ndarray):
        frame = self._apply_flip(frame)
        frame, _ = self._dark_correct(frame)
        frame, _ = self._defect_correct(frame)
        self._display_image(frame)
        worker = self.sender()
        if worker is not None:
            worker.frame_displayed()

    def _on_live_error(self, msg: str):
        self._status_label.setText(tr("Error: {msg}", msg=msg))
        QtWidgets.QMessageBox.critical(self, tr("Live view error"), msg)

    def _snap(self):
        if self._any_busy():
            return

        exposure_us = int(self._exp_spin.value() * 1_000_000)
        self._save_prefs()
        self._set_busy(True)
        self._snap_stop_requested = False
        self._status_label.setText(
            tr("Capturing... (max {sec:.0f} s)", sec=self._exp_spin.value() * 2 + 10)
        )

        self._worker = _SnapWorker(self._backend, exposure_us,
                                   _timeout_ms(self._exp_spin.value()))
        self._worker.done.connect(self._on_snap_done)
        self._worker.error.connect(self._on_snap_error)
        self._worker.finished.connect(lambda: self._set_busy(False))
        self._worker.start()
        self._stop_btn.setEnabled(True)

    def _on_snap_done(self, img: np.ndarray, warn: str):
        if self._snap_stop_requested:
            self._snap_stop_requested = False
            self._status_label.setText(tr("Stopped by user"))
            return
        img = self._apply_flip(img)
        img, dark_warn = self._dark_correct(img)
        if dark_warn:
            warn = (warn + "\n" + dark_warn).strip()
        img, defect_warn = self._defect_correct(img)
        if defect_warn:
            warn = (warn + "\n" + defect_warn).strip()
        save_dir = Path(self._dir_edit.text())
        save_dir.mkdir(parents=True, exist_ok=True)
        fname = self._build_save_path(save_dir)
        meta = self._build_metadata("single", {
            "dark_corrected": self._dark_correct_chk.isChecked() and self._dark_img is not None,
            "dark_source": self._dark_path.name if self._dark_path else None,
        })
        ok = _save_tiff(fname, img, meta)
        self._display_image(img, str(fname))
        if ok:
            info = tr("Saved: {name}  ({w} × {h} px, max={max})",
                      name=fname.name, w=img.shape[1], h=img.shape[0], max=img.max())
            if warn:
                info += tr("\n[Warning] {warn}", warn=warn)
            self._status_label.setText(info)
        else:
            self._status_label.setText(tr("Save failed: {name}", name=fname))
        self._emit_done_sound()

    def _on_snap_error(self, msg: str):
        if self._snap_stop_requested:
            self._snap_stop_requested = False
            self._status_label.setText(tr("Stopped by user"))
            return
        self._status_label.setText(tr("Error: {msg}", msg=msg))
        QtWidgets.QMessageBox.critical(self, tr("Capture error"), msg)

    # ------------------------------------------------------------------
    # Slots — sequential acquisition
    # ------------------------------------------------------------------

    def _seq_start(self):
        if self._any_busy():
            return

        try:
            n = max(1, int(self._seq_n_edit.text()))
        except ValueError:
            QtWidgets.QMessageBox.warning(self, tr("Input error"), tr("Please enter an integer for the frame count"))
            return
        try:
            interval_ms = max(0, int(self._seq_interval_edit.text()))
        except ValueError:
            QtWidgets.QMessageBox.warning(self, tr("Input error"), tr("Please enter an integer [ms] for the interval"))
            return

        if not (self._seq_indiv_chk.isChecked() or
                # self._seq_batch_chk.isChecked() or
                self._seq_avg_chk.isChecked()):
            QtWidgets.QMessageBox.warning(
                self, tr("Save settings"), tr("Please select either individual save or average save")
            )
            return

        exposure_us = int(self._exp_spin.value() * 1_000_000)
        self._save_prefs()
        self._set_busy(True)
        self._seq_stop_requested = False
        self._status_label.setText(tr("Capturing sequence... (0 / {n})", n=n))
        self._seq_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        self._seq_worker = _SeqWorker(
            self._backend, exposure_us, n, interval_ms,
            _timeout_ms(self._exp_spin.value())
        )
        self._seq_worker.frame_ready.connect(self._on_seq_frame_ready)
        self._seq_worker.progress.connect(self._on_seq_progress)
        self._seq_worker.done.connect(self._on_seq_done)
        self._seq_worker.error.connect(self._on_seq_error)
        self._seq_worker.finished.connect(lambda: self._set_busy(False))
        self._seq_worker.start()
        self._stop_btn.setEnabled(True)

    def _on_seq_progress(self, current: int, total: int):
        self._status_label.setText(tr("Capturing sequence... ({current} / {total})", current=current, total=total))

    def _on_seq_frame_ready(self, idx: int, frame: np.ndarray):
        frame = self._apply_flip(frame)
        frame, _ = self._dark_correct(frame)
        frame, _ = self._defect_correct(frame)
        fname_str = ""
        if self._seq_indiv_chk.isChecked():
            save_dir = Path(self._dir_edit.text())
            save_dir.mkdir(parents=True, exist_ok=True)
            fname = self._build_save_path(save_dir)
            meta = self._build_metadata("sequence_frame", {
                "frame_index": idx + 1,
                "n_frames_total": int(self._seq_n_edit.text()),
                "interval_ms": int(self._seq_interval_edit.text()),
                "sequence_id": self._seq_ts,
                "dark_corrected": self._dark_correct_chk.isChecked() and self._dark_img is not None,
                "dark_source": self._dark_path.name if self._dark_path else None,
            })
            _save_tiff(fname, frame, meta)
            fname_str = str(fname)
        self._display_image(frame, fname_str)

    def _on_seq_done(self, frames: list[np.ndarray]):
        stopped = self._seq_stop_requested
        self._seq_stop_requested = False

        if not frames:
            self._status_label.setText(
                tr("Stopped by user (no frames captured)") if stopped
                else tr("Done (nothing saved)")
            )
            self._emit_done_sound()
            return

        frames = [self._apply_flip(f) for f in frames]
        frames, dark_warn = self._dark_correct_frames(frames)
        save_dir = Path(self._dir_edit.text())
        save_dir.mkdir(parents=True, exist_ok=True)
        messages: list[str] = []

        if self._seq_indiv_chk.isChecked():
            messages.append(tr("Individual: {n} files", n=len(frames)))

        # if self._seq_batch_chk.isChecked():
        #     fname = self._build_save_path(save_dir)
        #     _save_multiframe_tiff(fname, frames)
        #     messages.append(tr("Stack: {name}", name=fname.name))

        defect_warn = ""
        if self._seq_avg_chk.isChecked():
            avg = (np.mean(np.stack(frames, axis=0), axis=0)
                   .round().clip(0, 65535).astype(np.uint16))
            avg, defect_warn = self._defect_correct(avg)
            fname = self._build_save_path(save_dir)
            meta = self._build_metadata("sequence_average", {
                "n_frames_averaged": len(frames),
                "interval_ms": int(self._seq_interval_edit.text()),
                "sequence_id": self._seq_ts,
                "dark_corrected": self._dark_correct_chk.isChecked() and self._dark_img is not None,
                "dark_source": self._dark_path.name if self._dark_path else None,
            })
            _save_tiff(fname, avg, meta)
            self._display_image(avg, str(fname))
            messages.append(tr("Average: {name}", name=fname.name))

        prefix = tr("Stopped by user: ") if stopped else tr("Done: ")
        result = prefix + "  ".join(messages) if messages else tr("Done (nothing saved)")
        warns = [w for w in [dark_warn, defect_warn] if w]
        if warns:
            result += tr("\n[Warning] ") + " / ".join(warns)
        self._status_label.setText(result)
        self._emit_done_sound()

    def _on_seq_error(self, msg: str):
        if self._seq_stop_requested:
            self._seq_stop_requested = False
            self._status_label.setText(tr("Stopped by user"))
            return
        self._status_label.setText(tr("Error: {msg}", msg=msg))
        QtWidgets.QMessageBox.critical(self, tr("Sequential acquisition error"), msg)

    def _update_seq_est_time(self):
        try:
            n = max(1, int(self._seq_n_edit.text()))
            interval_s = max(0, int(self._seq_interval_edit.text())) / 1000.0
            total_s = n * self._exp_spin.value() + (n - 1) * interval_s
            self._seq_time_label.setText(tr("Estimated: {sec:.1f} s", sec=total_s))
        except (ValueError, AttributeError):
            self._seq_time_label.setText(tr("Estimated: --"))

    # ------------------------------------------------------------------
    # Slots — dark current
    # ------------------------------------------------------------------

    def _load_dark(self):
        start_dir = str(self._dark_path.parent) if self._dark_path else self._dir_edit.text()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select dark-current file"), start_dir,
            "TIFF images (*.tif *.tiff);;All files (*)"
        )
        if not path:
            return
        try:
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                raise ValueError(tr("Could not read the file"))
            if img.ndim != 2:
                raise ValueError(tr("A grayscale image is required (shape={shape})", shape=img.shape))
            self._apply_dark(img.astype(np.float64), Path(path))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Load error"), str(exc))

    def _acquire_dark(self):
        if self._any_busy():
            return
        try:
            n = max(1, int(self._dark_n_edit.text()))
        except ValueError:
            QtWidgets.QMessageBox.warning(self, tr("Input error"), tr("Please enter an integer for the accumulation count"))
            return

        exposure_us = int(self._exp_spin.value() * 1_000_000)
        self._save_prefs()
        self._set_busy(True)
        self._dark_stop_requested = False
        self._dark_status_label.setText(tr("Acquiring... (0 / {n})", n=n))

        self._dark_worker = _DarkWorker(
            self._backend, exposure_us, n, _timeout_ms(self._exp_spin.value())
        )
        self._dark_worker.progress.connect(self._on_dark_progress)
        self._dark_worker.done.connect(self._on_dark_done)
        self._dark_worker.error.connect(self._on_dark_error)
        self._dark_worker.finished.connect(self._on_dark_finished)
        self._dark_worker.start()
        self._stop_btn.setEnabled(True)

    def _on_dark_finished(self):
        self._set_busy(False)
        if self._dark_stop_requested:
            self._dark_stop_requested = False
            self._dark_status_label.setText(tr("Stopped by user"))

    def _on_dark_progress(self, current: int, total: int):
        self._dark_status_label.setText(tr("Acquiring... ({current} / {total})", current=current, total=total))

    def _on_dark_done(self, avg: np.ndarray):
        avg = self._apply_flip(avg)
        start_dir = str(self._dark_path.parent) if self._dark_path else self._dir_edit.text()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "2x2" if self._backend.width < 2000 else "1x1"
        n_acc = self._dark_worker._n_frames if self._dark_worker else 1
        default_name = str(Path(start_dir) / f"dark_{ts}_{n_acc}acc_{suffix}.tif")

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save dark-current image"), default_name,
            "TIFF images (*.tif *.tiff);;All files (*)"
        )
        if not path:
            self._dark_status_label.setText(tr("Save cancelled"))
            return

        meta = self._build_metadata("dark", {
            "n_accumulations": n_acc,
        })
        ok = _save_tiff(path, avg, meta)
        if not ok:
            QtWidgets.QMessageBox.critical(self, tr("Save error"), tr("Failed to save: {path}", path=path))
            self._dark_status_label.setText(tr("Save failed"))
            return

        if self._dark_apply_chk.isChecked():
            self._apply_dark(avg.astype(np.float64), Path(path))
        else:
            self._dark_status_label.setText(tr("Saved: {name}", name=Path(path).name))
        self._emit_done_sound()

    def _on_dark_error(self, msg: str):
        self._dark_status_label.setText(tr("Error: {msg}", msg=msg))
        QtWidgets.QMessageBox.critical(self, tr("Dark-current acquisition error"), msg)

    def _apply_dark(self, img: np.ndarray, path: Path):
        self._dark_img = img
        self._dark_path = path
        meta = _read_tiff_metadata(path)
        self._dark_exposure_ms = meta.get("exposure_ms")
        self._dark_flip_v = meta.get("flip_v")   # None if old file without metadata
        self._dark_flip_h = meta.get("flip_h")
        self._refresh_dark_exposure_warning()

    def _refresh_dark_exposure_warning(self):
        if self._dark_img is None:
            return
        name = self._dark_path.name if self._dark_path else tr("unknown")
        current_ms = round(self._exp_spin.value() * 1000)
        warnings: list[str] = []

        if self._dark_exposure_ms is not None and self._dark_exposure_ms != current_ms:
            warnings.append(
                tr("Exposure mismatch (dark: {dark_ms} ms / current: {cur_ms} ms)",
                   dark_ms=self._dark_exposure_ms, cur_ms=current_ms)
            )
        if self._dark_flip_v is not None and self._dark_flip_v != self._flip_v_chk.isChecked():
            warnings.append(
                tr("Vertical flip mismatch (dark: {dark_v} / current: {cur_v})",
                   dark_v=self._dark_flip_v, cur_v=self._flip_v_chk.isChecked())
            )
        if self._dark_flip_h is not None and self._dark_flip_h != self._flip_h_chk.isChecked():
            warnings.append(
                tr("Horizontal flip mismatch (dark: {dark_h} / current: {cur_h})",
                   dark_h=self._dark_flip_h, cur_h=self._flip_h_chk.isChecked())
            )

        if warnings:
            self._dark_status_label.setText(
                tr("Current dark current: {name}\n[Warning] ", name=name) + " / ".join(warnings)
            )
            self._dark_status_label.setStyleSheet("color: orange;")
        else:
            suffix = f"  ({self._dark_exposure_ms} ms)" if self._dark_exposure_ms else ""
            self._dark_status_label.setText(tr("Current dark current: {name}{suffix}", name=name, suffix=suffix))
            self._dark_status_label.setStyleSheet("")

    # ------------------------------------------------------------------
    # Image flip
    # ------------------------------------------------------------------

    def _apply_flip(self, img: np.ndarray) -> np.ndarray:
        v = self._flip_v_chk.isChecked()
        h = self._flip_h_chk.isChecked()
        if v and h:
            return img[::-1, ::-1]
        if v:
            return img[::-1, :]
        if h:
            return img[:, ::-1]
        return img

    def _on_flip_toggle(self, checked: bool):
        chk = self.sender()
        name = tr("Vertical") if chk is self._flip_v_chk else tr("Horizontal")
        state = "ON" if checked else "OFF"
        reply = QtWidgets.QMessageBox.question(
            self, tr("Confirm flip setting change"),
            tr("Change {name} flip to {state}?\n\n"
               "Note: this will break consistency with the existing dark-current image.\n"
               "Please re-acquire the dark current after changing this setting.",
               name=name, state=state),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            chk.blockSignals(True)
            chk.setChecked(not checked)
            chk.blockSignals(False)
            return
        self._save_prefs()
        self._refresh_dark_exposure_warning()

    # ------------------------------------------------------------------
    # Pixel-defect correction
    # ------------------------------------------------------------------

    def _browse_defect_file(self):
        start_dir = (str(self._defect_file_path.parent)
                     if self._defect_file_path else self._dir_edit.text())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Select pixel-defect file"), start_dir,
            "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            self._load_defect_file(Path(path))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Defect file load error"), str(exc))

    def _load_defect_file(self, path: Path) -> None:
        """Parse *path*, build the defect mask, and update the UI status."""
        binning = "2x2" if self._backend.width < 2000 else "1x1"
        defects = _parse_defect_file(
            str(path), binning, self._backend._h_blank,
            self._backend.width, self._backend.height,
        )
        self._defect_mask = _build_defect_mask(
            defects, self._backend.height, self._backend.width
        )
        self._defect_file_path = path
        self._defect_n_pixels = len(defects)
        self._defect_file_edit.setText(path.name)
        self._defect_status_label.setText(tr("Defect pixels: {n} px", n=self._defect_n_pixels))
        self._defect_status_label.setStyleSheet("color: gray; font-size: 11px;")
        self._save_prefs()

    def _current_defect_mode(self) -> str:
        if self._defect_median_radio.isChecked():
            return "median"
        if self._defect_neg1_radio.isChecked():
            return "neg1"
        return "none"

    def _defect_correct(self, img: np.ndarray) -> tuple[np.ndarray, str]:
        """Apply pixel-defect correction according to the selected mode;
        return (corrected_img, warning)."""
        mode = self._current_defect_mode()
        if mode == "none" or self._defect_mask is None:
            return img, ""
        if img.shape != self._defect_mask.shape:
            return img, tr("Defect mask size does not match ({mask_shape} vs {img_shape})",
                            mask_shape=self._defect_mask.shape, img_shape=img.shape)
        if mode == "median":
            kernel = int(self._defect_kernel_combo.currentText()[0])  # "3×3" → 3
            return _apply_defect_correction(img, self._defect_mask, kernel), ""
        return _replace_defect_pixels(img, self._defect_mask), ""

    # ------------------------------------------------------------------
    # Dark current correction
    # ------------------------------------------------------------------

    def _dark_correct(self, img: np.ndarray) -> tuple[np.ndarray, str]:
        if not self._dark_correct_chk.isChecked() or self._dark_img is None:
            return img, ""
        if img.shape != self._dark_img.shape:
            return img, tr("Dark-current image size does not match ({dark_shape} vs {img_shape})",
                            dark_shape=self._dark_img.shape, img_shape=img.shape)
        corrected = (img.astype(np.float64) - self._dark_img).clip(0, 65535).astype(np.uint16)
        return corrected, ""

    def _dark_correct_frames(self, frames: list) -> tuple[list, str]:
        if self._dark_img is None:
            return frames, ""
        corrected, warn = [], ""
        for frame in frames:
            c, w = self._dark_correct(frame)
            corrected.append(c)
            if w and not warn:
                warn = w
        return corrected, warn

    # ------------------------------------------------------------------
    # Sound notification
    # ------------------------------------------------------------------

    def _emit_done_sound(self) -> None:
        play_current_sound()

    # ------------------------------------------------------------------
    # Busy state
    # ------------------------------------------------------------------

    def _any_busy(self) -> bool:
        return ((self._worker and self._worker.isRunning()) or
                (self._seq_worker and self._seq_worker.isRunning()) or
                (self._dark_worker and self._dark_worker.isRunning()) or
                (self._live_worker and self._live_worker.isRunning()))

    def _set_busy(self, busy: bool):
        enabled = not busy
        # 検出器設定
        self._exp_spin.setEnabled(enabled)
        self._flip_v_chk.setEnabled(enabled)
        self._flip_h_chk.setEnabled(enabled)
        # 保存設定
        self._dir_edit.setEnabled(enabled)
        self._browse_btn.setEnabled(enabled)
        self._filename_edit.setEnabled(enabled)
        self._suffix_none_radio.setEnabled(enabled)
        self._suffix_datetime_radio.setEnabled(enabled)
        self._suffix_time_radio.setEnabled(enabled)
        self._suffix_index_radio.setEnabled(enabled)
        self._suffix_index3_radio.setEnabled(enabled)
        # Live表示
        self._live_btn.setEnabled(enabled)
        # 単発取込
        self._snap_btn.setEnabled(enabled)
        self._snap_btn.setText(tr("Capturing...") if busy else tr("Single shot"))
        self._snap_tiff_radio.setEnabled(enabled)
        # 連続取込
        self._seq_btn.setEnabled(enabled)
        self._seq_btn.setText(tr("Capturing...") if busy else tr("Sequential acquisition"))
        self._seq_n_edit.setEnabled(enabled)
        self._seq_interval_edit.setEnabled(enabled)
        self._seq_tiff_radio.setEnabled(enabled)
        self._seq_indiv_chk.setEnabled(enabled)
        # self._seq_batch_chk.setEnabled(enabled)
        self._seq_avg_chk.setEnabled(enabled)
        self._dark_correct_chk.setEnabled(enabled)
        # Dark
        self._dark_load_btn.setEnabled(enabled)
        self._dark_acq_btn.setEnabled(enabled)
        self._dark_acq_btn.setText(tr("Acquiring...") if busy else tr("Acquire"))
        self._dark_n_edit.setEnabled(enabled)
        self._dark_apply_chk.setEnabled(enabled)
        # 画素欠陥補正
        self._defect_none_radio.setEnabled(enabled)
        self._defect_median_radio.setEnabled(enabled)
        self._defect_neg1_radio.setEnabled(enabled)
        self._defect_kernel_combo.setEnabled(
            enabled and self._defect_median_radio.isChecked()
        )
        self._defect_file_btn.setEnabled(enabled)
        if not busy:
            self._stop_btn.setEnabled(False)

    def _stop_acquisition(self):
        """Stops whichever acquisition is currently in progress, regardless of
        whether it was started as Live view, single-shot, or sequential."""
        stopped_any = False
        if self._worker and self._worker.isRunning():
            self._snap_stop_requested = True
            self._worker.request_stop()
            stopped_any = True
        if self._seq_worker and self._seq_worker.isRunning():
            self._seq_stop_requested = True
            self._seq_worker.request_stop()
            stopped_any = True
        if self._dark_worker and self._dark_worker.isRunning():
            self._dark_stop_requested = True
            self._dark_worker.request_stop()
            stopped_any = True
        if self._live_worker and self._live_worker.isRunning():
            self._live_worker.request_stop()
            stopped_any = True
        if stopped_any:
            self._status_label.setText(tr("Stopping..."))
        self._stop_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _load_prefs(self) -> dict:
        try:
            return json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_prefs(self):
        _LOCALDATA.mkdir(exist_ok=True)
        prefs = {
            "exposure_s": self._exp_spin.value(),
            "save_dir": self._dir_edit.text(),
            "filename": self._filename_edit.text(),
            "filename_suffix_mode": self._current_suffix_mode(),
            "seq_n_frames": self._seq_n_edit.text(),
            "seq_interval_ms": self._seq_interval_edit.text(),
            "seq_indiv_save": self._seq_indiv_chk.isChecked(),
            # "seq_batch_save": self._seq_batch_chk.isChecked(),
            "seq_avg_save": self._seq_avg_chk.isChecked(),
            "dark_n_frames": self._dark_n_edit.text(),
            "dark_apply_after_save": self._dark_apply_chk.isChecked(),
            "dark_correct_enabled": self._dark_correct_chk.isChecked(),
            "defect_correct_mode": self._current_defect_mode(),
            "defect_kernel_size": self._defect_kernel_combo.currentText(),
            "defect_file_path": str(self._defect_file_path) if self._defect_file_path else "",
            "flip_v": self._flip_v_chk.isChecked(),
            "flip_h": self._flip_h_chk.isChecked(),
            "instant1d_enabled": self._instant1d_chk.isChecked(),
            "instant1d_bin_width_deg": self._instant1d_binwidth_spin.value(),
            "instant1d_autosave": self._instant1d_autosave_chk.isChecked(),
            "instant1d_fmt_csv": self._instant1d_fmt_csv_chk.isChecked(),
            "instant1d_fmt_tsv": self._instant1d_fmt_tsv_chk.isChecked(),
            "instant1d_fmt_gsas": self._instant1d_fmt_gsas_chk.isChecked(),
            "instant1d_fmt_igor": self._instant1d_fmt_igor_chk.isChecked(),
            "instant1d_units": [k for k, chk in self._instant1d_unit_chks.items() if chk.isChecked()],
        }
        _PREFS_FILE.write_text(
            json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def closeEvent(self, event):
        # Every worker must be explicitly stopped and waited on before the
        # window (and its slots) go away — live view loops indefinitely, and
        # snap/sequence/dark can each still be mid-acquisition when the user
        # closes the window.
        for worker in (self._worker, self._seq_worker,
                       self._dark_worker, self._live_worker):
            if worker and worker.isRunning():
                worker.request_stop()
                worker.wait(3000)

        for worker, signals in [
            (self._worker, [("done", self._on_snap_done),
                            ("error", self._on_snap_error)]),
            (self._seq_worker, [("progress", self._on_seq_progress),
                                ("done", self._on_seq_done),
                                ("error", self._on_seq_error)]),
            (self._dark_worker, [("progress", self._on_dark_progress),
                                 ("done", self._on_dark_done),
                                 ("error", self._on_dark_error)]),
            (self._live_worker, [("frame_ready", self._on_live_frame),
                                 ("error", self._on_live_error)]),
        ]:
            if worker:
                for sig_name, slot in signals:
                    try:
                        getattr(worker, sig_name).disconnect(slot)
                    except Exception:
                        pass
        self._save_prefs()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                         help="Launch UI only, with a simulated backend (no hardware).")
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    if args.debug:
        _backend = RadiconBackendSim()
    else:
        try:
            _backend = RadiconBackend(RADICON_SERVER, RADICON_DEVICE, RADICON_CCF["2x2"])
        except Exception as e:
            QtWidgets.QMessageBox.critical(None, tr("Connection Error"), str(e))
            sys.exit(1)
    win = RadiconWindow(backend=_backend)
    win.show()
    ret = app.exec()
    _backend.close()
    sys.exit(ret)
