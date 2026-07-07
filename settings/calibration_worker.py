"""Background worker that converts an IPA .prm file and a CeO2 TIFF into a calibrated AI."""
from __future__ import annotations

import pathlib
import traceback

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    import pyFAI.detectors as _pf_det
    from pyFAI.integrator.azimuthal import AzimuthalIntegrator
    from pyFAI.calibrant import get_calibrant
    from pyFAI.goniometer import SingleGeometry
    _PYFAI_OK = True
except ImportError:
    _PYFAI_OK = False

try:
    from apps.ipa_poni.ipa_to_poni import parse_ipa_prm, ipa_to_poni
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from apps.ipa_poni.ipa_to_poni import parse_ipa_prm, ipa_to_poni


def _build_ai_from_poni_params(poni) -> "AzimuthalIntegrator":
    det = _pf_det.Detector(pixel1=poni.pixel_size_1, pixel2=poni.pixel_size_2)
    return AzimuthalIntegrator(
        dist=poni.distance,
        poni1=poni.poni1,
        poni2=poni.poni2,
        rot1=poni.rot1,
        rot2=poni.rot2,
        rot3=poni.rot3,
        wavelength=poni.wavelength,
        detector=det,
    )


def _make_mask(img: np.ndarray, sat_percentile: float) -> np.ndarray:
    mask = np.zeros(img.shape, dtype=bool)
    mask[img >= np.percentile(img, sat_percentile)] = True
    mask[:3, :]  = True
    mask[-3:, :] = True
    mask[:, :3]  = True
    mask[:, -3:] = True
    return mask


class CalibrationWorker(QThread):
    """Runs IPA→poni conversion then pyFAI CeO2 ring refinement on a background thread.

    Signals
    -------
    progress(str)
        Status messages suitable for display in a QLabel.
    completed(ai_cal, chi2_before, chi2_after, n_pts, radial, intensity)
        Emitted on success.  All numpy arrays are safe to use in the main thread.
    failed(str)
        Emitted on any exception; payload is a human-readable error string.
    """

    progress  = pyqtSignal(str)
    completed = pyqtSignal(
        object,  # AzimuthalIntegrator
        float,   # chi2_before
        float,   # chi2_after
        int,     # n_control_pts
        object,  # radial  ndarray
        object,  # intensity  ndarray
    )
    failed = pyqtSignal(str)

    def __init__(
        self,
        prm_path:       pathlib.Path,
        ceo2_path:      pathlib.Path,
        n_bins:         int   = 2000,
        max_rings:      int   = 8,
        pts_per_deg:    float = 1.5,
        sat_percentile: float = 99.9,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._prm_path      = prm_path
        self._ceo2_path     = ceo2_path
        self._n_bins        = n_bins
        self._max_rings     = max_rings
        self._pts_per_deg   = pts_per_deg
        self._sat_pct       = sat_percentile

    def run(self) -> None:
        if not _PYFAI_OK:
            self.failed.emit("pyFAI is not installed.  Run: pip install pyFAI")
            return
        try:
            import tifffile
        except ImportError:
            self.failed.emit("tifffile is not installed.  Run: pip install tifffile")
            return

        try:
            self.progress.emit("Parsing IPAnalyzer parameter file…")
            prm  = parse_ipa_prm(self._prm_path)
            poni = ipa_to_poni(prm)
            ai_initial = _build_ai_from_poni_params(poni)

            self.progress.emit(f"Loading CeO2 image: {self._ceo2_path.name}…")
            img = tifffile.imread(str(self._ceo2_path)).astype(np.float32)

            self.progress.emit("Building saturation mask…")
            mask = _make_mask(img, self._sat_pct)

            self.progress.emit("Configuring CeO2 calibrant…")
            calibrant = get_calibrant("CeO2")
            calibrant.wavelength = poni.wavelength

            detector = ai_initial.detector
            detector.mask = mask.astype(np.int8)

            self.progress.emit(
                f"Extracting control points from CeO2 rings (max {self._max_rings})…"
            )
            sg = SingleGeometry(
                label="CeO2",
                image=img,
                calibrant=calibrant,
                detector=detector,
                geometry=ai_initial,
            )
            sg.extract_cp(
                max_rings=self._max_rings,
                pts_per_deg=self._pts_per_deg,
                Imin=0,
            )
            n_pts = len(sg.geometry_refinement.data)
            self.progress.emit(f"Found {n_pts} control points.  Refining geometry…")

            chi2_before = float(sg.geometry_refinement.chi2())
            sg.geometry_refinement.refine2()
            chi2_after = float(sg.geometry_refinement.chi2())

            ai_cal = sg.get_ai()
            ai_cal.wavelength = poni.wavelength
            ai_cal.detector   = ai_initial.detector

            self.progress.emit("Integrating calibrated 1D pattern…")
            result = ai_cal.integrate1d(
                img,
                npt=self._n_bins,
                unit="2th_deg",
                method=("no", "histogram", "cython"),
                correctSolidAngle=True,
                polarization_factor=0.95,
                mask=mask.astype(np.uint8),
            )

            self.progress.emit("Done.")
            self.completed.emit(
                ai_cal,
                chi2_before,
                chi2_after,
                n_pts,
                result.radial,
                result.intensity,
            )

        except Exception as exc:
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")
