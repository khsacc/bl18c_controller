"""Detector Calibration settings page.

Embedded as a QWidget inside SettingsWindow's QStackedWidget.

This page no longer runs any calibration itself — all calibration logic
(single- or multi-detector-position) lives in apps/calibrate_instruments/.
This page only lets the user point at an existing .poni file on disk, shows
the AzimuthalIntegrator geometry it decodes to, and offers a shortcut to
open the Calibrate Detector Geometry wizard when no calibration data exists
yet. PoniState is always updated with the resulting AzimuthalIntegrator
(never a file path), so every other window (XrdScanWindow, RadiconWindow, …)
keeps consuming calibration data exactly as before, via PoniState.ai /
poni_changed.
"""
from __future__ import annotations

import json
import math
import pathlib
from typing import Callable

from PyQt6.QtWidgets import (
    QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

try:
    from settings.poni_state import PoniState
    from settings.i18n import tr
    from utils.poni_io import parse_poni, build_ai
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from settings.poni_state import PoniState
    from settings.i18n import tr
    from utils.poni_io import parse_poni, build_ai


# __localdata lives one level up from pages/ (i.e. settings/__localdata/)
_LOCALDATA = pathlib.Path(__file__).parent.parent / "__localdata"
_PREFS_FILE = _LOCALDATA / "detector_calibration.json"

_PARAMS_LINE = (
    "Distance = {dist_mm:.4f} mm    Poni1 = {poni1_mm:.4f} mm    Poni2 = {poni2_mm:.4f} mm\n"
    "Rot1 = {rot1_deg:.4f}°    Rot2 = {rot2_deg:.4f}°    Rot3 = {rot3_deg:.4f}°\n"
    "Wavelength = {wavelength_ang:.6f} Å    Pixel size = {px1_um:.1f} × {px2_um:.1f} µm"
)


def remember_poni_path(path: pathlib.Path) -> None:
    """Persist *path* as this page's remembered poni file.

    Callers outside this page (e.g. CalibrateInstrumentsWindow, after
    writing a fresh .poni to disk) should call this alongside
    ``PoniState.update(ai=..., poni_path=path)`` so that Settings ->
    Detector Calibration shows "Loaded from: <name>" the next time it is
    opened, instead of unassociated in-session data — even if this page
    was never opened during the session that produced the file.
    """
    _LOCALDATA.mkdir(parents=True, exist_ok=True)
    _PREFS_FILE.write_text(
        json.dumps({"last_poni_path": str(path)}, indent=2), encoding="utf-8"
    )


class DetectorCalibrationPage(QWidget):
    """Settings page showing the currently loaded poni geometry.

    Calibration itself is done in the "Calibrate Detector Geometry" wizard
    (apps/calibrate_instruments/); this page only loads an existing .poni
    file (or displays whatever the wizard last produced in this session)
    and forwards the resulting AI to PoniState.
    """

    def __init__(
        self,
        poni_state: PoniState,
        open_calibrate_instruments: Callable[[], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self._poni_state = poni_state
        self._open_calibrate_instruments = open_calibrate_instruments
        self._poni_path: pathlib.Path | None = None

        self._setup_ui()
        self._poni_state.poni_changed.connect(self._refresh_display)
        self._restore_prefs()
        self._refresh_display()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Poni file ────────────────────────────────────────────────────────
        file_grp = QGroupBox(tr("Poni File"))
        file_lay = QHBoxLayout(file_grp)
        file_lay.addWidget(QLabel(tr("Poni file (.poni):")))
        self._path_label = QLabel(tr("Not loaded"))
        self._path_label.setWordWrap(True)
        self._path_label.setStyleSheet("font-size: 10px; color: #888;")
        browse_btn = QPushButton(tr("Browse…"))
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._on_browse_poni)
        file_lay.addWidget(self._path_label, 1)
        file_lay.addWidget(browse_btn, 0)
        root.addWidget(file_grp)

        # ── Calibration data ─────────────────────────────────────────────────
        data_grp = QGroupBox(tr("Calibration Data"))
        data_lay = QVBoxLayout(data_grp)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        data_lay.addWidget(self._status_label)

        self._params_label = QLabel("")
        self._params_label.setWordWrap(True)
        self._params_label.setStyleSheet("font-size: 11px; color: #333;")
        data_lay.addWidget(self._params_label)

        root.addWidget(data_grp)

        # ── Calibrate Detector Geometry shortcut ────────────────────────────
        btn_row = QHBoxLayout()
        self._calibrate_btn = QPushButton("")
        self._calibrate_btn.clicked.connect(self._on_open_calibrate_instruments)
        btn_row.addWidget(self._calibrate_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        root.addStretch()

    # ── Prefs persistence ─────────────────────────────────────────────────────

    def _save_prefs(self) -> None:
        _LOCALDATA.mkdir(parents=True, exist_ok=True)
        data = {"last_poni_path": str(self._poni_path) if self._poni_path else None}
        _PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _restore_prefs(self) -> None:
        if self._poni_state.poni_path is not None:
            # Live state already has an authoritative path (e.g. a poni
            # just saved by the Calibrate Detector Geometry wizard, in this
            # same run, before this page was ever constructed) — prefer it
            # over whatever was last remembered on disk.
            self._poni_path = self._poni_state.poni_path
            self._update_path_label()
            self._save_prefs()
            return

        if not _PREFS_FILE.exists():
            return
        try:
            data = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
            last_path = data.get("last_poni_path")
        except Exception:
            return
        if not last_path or not pathlib.Path(last_path).exists():
            return
        # Don't clobber a fresher in-session calibration (e.g. produced by
        # the wizard while this page didn't exist yet) with the
        # last-remembered file path — only auto-load if nothing is
        # calibrated yet.
        if self._poni_state.is_calibrated():
            self._poni_path = pathlib.Path(last_path)
            self._update_path_label()
        else:
            self._load_poni(pathlib.Path(last_path))

    # ── File browsing / loading ─────────────────────────────────────────────

    def _update_path_label(self) -> None:
        if self._poni_path is not None:
            self._path_label.setText(str(self._poni_path))
            self._path_label.setStyleSheet("font-size: 10px; color: #080;")
        else:
            self._path_label.setText(tr("Not loaded"))
            self._path_label.setStyleSheet("font-size: 10px; color: #888;")

    def _on_browse_poni(self) -> None:
        start = str(self._poni_path.parent) if self._poni_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Select poni file"), start,
            "poni files (*.poni);;All files (*)",
        )
        if not path:
            return
        self._load_poni(pathlib.Path(path))

    def _load_poni(self, path: pathlib.Path) -> None:
        try:
            ai = build_ai(parse_poni(path))
        except Exception as exc:
            QMessageBox.critical(self, tr("Load Error"), str(exc))
            return
        self._poni_path = path
        self._update_path_label()
        self._save_prefs()
        self._poni_state.update(ai=ai, poni_path=path)

    # ── Display ──────────────────────────────────────────────────────────────

    def _refresh_display(self) -> None:
        calibrated = self._poni_state.is_calibrated()

        if not calibrated:
            self._status_label.setText(tr("⚠ No calibration data loaded."))
            self._status_label.setStyleSheet("color: #a05a00; font-weight: bold;")
            self._params_label.setText("")
        else:
            ai = self._poni_state.ai
            if self._poni_state.poni_path is not None:
                self._status_label.setText(
                    tr("● Loaded from: {name}", name=self._poni_state.poni_path.name)
                )
            else:
                self._status_label.setText(
                    tr("● In-session calibration data (not loaded from a file)")
                )
            self._status_label.setStyleSheet("color: green; font-weight: bold;")
            self._params_label.setText(tr(
                _PARAMS_LINE,
                dist_mm=ai.dist * 1e3,
                poni1_mm=ai.poni1 * 1e3,
                poni2_mm=ai.poni2 * 1e3,
                rot1_deg=math.degrees(ai.rot1),
                rot2_deg=math.degrees(ai.rot2),
                rot3_deg=math.degrees(ai.rot3),
                wavelength_ang=ai.wavelength * 1e10,
                px1_um=ai.detector.pixel1 * 1e6,
                px2_um=ai.detector.pixel2 * 1e6,
            ))

        if calibrated:
            self._calibrate_btn.setText(tr("Recalibrate…"))
            self._calibrate_btn.setStyleSheet("")
        else:
            self._calibrate_btn.setText(tr("Open Calibrate Detector Geometry…"))
            self._calibrate_btn.setStyleSheet(
                "font-weight: bold; background-color: #4CAF50; color: white; padding: 8px 14px;"
            )
        self._calibrate_btn.setEnabled(self._open_calibrate_instruments is not None)
        self._calibrate_btn.setToolTip(
            "" if self._open_calibrate_instruments is not None
            else tr("Not available when this page is opened standalone.")
        )

    def _on_open_calibrate_instruments(self) -> None:
        if self._open_calibrate_instruments is not None:
            self._open_calibrate_instruments()
