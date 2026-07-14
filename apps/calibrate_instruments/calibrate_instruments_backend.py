"""Calibrate Detector Geometry backend — dataclasses and the multi-position pyFAI
GoniometerRefinement worker (QThread) that fits distance/poni/rot (and
optionally wavelength) from calibrant rings acquired at several detector
positions.

See SPEC.md (in this directory) for the full design rationale — in
particular why the magnescale (mgs) reading, not the Ch9 pulse count, is
used as the multi-position `pos` label fed into pyFAI's GoniometerRefinement.
"""
from __future__ import annotations

import math
import pathlib
import traceback
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    import pyFAI.detectors as pf_detectors
    from pyFAI.calibrant import get_calibrant, ALL_CALIBRANTS
    from pyFAI.goniometer import ExtendedTransformation, GoniometerRefinement, SingleGeometry
    from pyFAI.integrator.azimuthal import AzimuthalIntegrator
    PYFAI_AVAILABLE = True
except ImportError:
    PYFAI_AVAILABLE = False
    AzimuthalIntegrator = object  # type: ignore[misc,assignment]

try:
    from utils.poni_io import build_ai, parse_poni
    from apps.ipa_poni.ipa_to_poni import parse_ipa_prm, ipa_to_poni
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    from utils.poni_io import build_ai, parse_poni
    from apps.ipa_poni.ipa_to_poni import parse_ipa_prm, ipa_to_poni


# mgs [mm] -> distance [m] unit conversion. The magnescale is a direct absolute
# linear encoder, so this is treated as a fixed, known unit conversion — never
# fit — rather than a parameter to discover.
NOMINAL_MGS_SCALE_M_PER_MM = 1e-3

# hc in keV*Angstrom (matches pyFAI's internal constant), used for the
# wavelength <-> energy conversion in both manual entry and the goniometer
# transformation's optional energy fit.
HC_KEV_ANG = 12.398419843320026

# Rad-icon 2022 (SB1533) physical pixel pitch at 1x1 (unbinned), per the
# manufacturer's datasheet: 204x221 mm active area / 2064x2236 px = 99 um.
# 2x2 binning doubles the effective pixel pitch. Pixel size is always taken
# from this constant + the detected binning of the actual position images —
# never from an IPA prm file or existing poni file — since those may have
# been calibrated at a different binning than what is currently in use.
RAD_ICON_PIXEL_SIZE_1X1_UM = 99.0

# Cropped image width (after the 4px/side blanking crop in RadiconBackend)
# is ~2056 px at 1x1 and ~1024 px at 2x2 — comfortably separated by this
# threshold regardless of small variations from the h_blank crop or ROI.
_BINNING_WIDTH_THRESHOLD_PX = 1500


def detect_binning(image_width_px: int) -> str:
    """Infer Rad-icon 2022 binning ("1x1" or "2x2") from a captured/loaded
    image's pixel width."""
    return "1x1" if image_width_px > _BINNING_WIDTH_THRESHOLD_PX else "2x2"


def pixel_size_um_for_binning(binning: str) -> float:
    return RAD_ICON_PIXEL_SIZE_1X1_UM * (2 if binning == "2x2" else 1)


def detect_beam_center(image: np.ndarray) -> tuple[float, float, float]:
    """Estimate the beam center (x_px, y_px) from a single Debye-Scherrer ring
    image, using the point symmetry of concentric (or tilted-elliptical)
    rings about the true center — no calibrant/distance/wavelength needed.

    Method: a point-symmetric image satisfies I(x,y) ~ I(2*cx-x, 2*cy-y).
    Correlating the image against its own 180-degree rotation with FFT phase
    correlation gives the translation between them; half that translation,
    added to the image's own geometric center, is the beam center. Returns
    (x_px, y_px, confidence) where confidence is phaseCorrelate's response
    (roughly 0-1; low values mean the result is unreliable — e.g. too few
    rings, very asymmetric masking/shadowing, or a very noisy image).
    """
    import cv2

    height, width = image.shape[:2]
    proc = np.log1p(np.clip(image.astype(np.float32), 0, None))
    flipped = proc[::-1, ::-1].copy()

    window = cv2.createHanningWindow((width, height), cv2.CV_32F)
    shift, response = cv2.phaseCorrelate(proc * window, flipped * window)
    shift_x, shift_y = shift

    cx = (width - 1) / 2.0 + shift_x / 2.0
    cy = (height - 1) / 2.0 + shift_y / 2.0
    return cx, cy, float(response)


def calibrant_names() -> list[str]:
    """Sorted list of pyFAI's built-in calibrant names."""
    if not PYFAI_AVAILABLE:
        return []
    return sorted(ALL_CALIBRANTS.keys())


@dataclass
class CalibrationPosition:
    label: str
    is_primary: bool = False
    mgs_mm: float | None = None
    ch9_pulse: int | None = None          # recorded for reference only — not used in the fit
    image: np.ndarray | None = None
    control_points: object | None = None  # pyFAI ControlPoints, set by extract_cp()
    n_control_points: int | None = None


@dataclass
class ManualInitialParams:
    distance_mm: float
    beam_center_x_px: float
    beam_center_y_px: float
    rot1_deg: float = 0.0
    rot2_deg: float = 0.0
    wavelength_ang: float | None = None
    energy_kev: float | None = None


@dataclass
class FreeParamStages:
    """Which parameters (besides dist0, which is always fit) the user allows
    to move. Each maps to its own checkbox in the UI, default checked."""
    fit_poni1:      bool = True   # Stage 2: beam center Y
    fit_poni2:      bool = True   # Stage 2: beam center X
    fit_rot1:       bool = True   # Stage 2: tilt
    fit_rot2:       bool = True   # Stage 2: tilt
    fit_wavelength: bool = True   # Stage 3: wavelength (via energy)


def build_ai_from_manual(
    params: ManualInitialParams, pixel_size_um: float,
) -> "AzimuthalIntegrator":
    if params.wavelength_ang is not None:
        wavelength_m = params.wavelength_ang * 1e-10
    elif params.energy_kev is not None:
        wavelength_m = (HC_KEV_ANG / params.energy_kev) * 1e-10
    else:
        raise ValueError("Either wavelength_ang or energy_kev must be given")

    pixel_size_m = pixel_size_um * 1e-6
    detector = pf_detectors.Detector(pixel1=pixel_size_m, pixel2=pixel_size_m)
    return AzimuthalIntegrator(
        dist=params.distance_mm * 1e-3,
        poni1=params.beam_center_y_px * pixel_size_m,
        poni2=params.beam_center_x_px * pixel_size_m,
        rot1=math.radians(params.rot1_deg),
        rot2=math.radians(params.rot2_deg),
        rot3=0.0,
        wavelength=wavelength_m,
        detector=detector,
    )


def build_initial_ai(
    mode: str,
    pixel_size_um: float,
    prm_path: pathlib.Path | None = None,
    poni_path: pathlib.Path | None = None,
    manual: ManualInitialParams | None = None,
) -> "AzimuthalIntegrator":
    """Build the initial-guess AzimuthalIntegrator from one of the three sources.

    `pixel_size_um` (detected from the primary position's image — see
    `detect_binning`/`pixel_size_um_for_binning`) always overrides whatever
    pixel size the prm/poni source declares, since those may have been
    calibrated at a different Rad-icon binning than what is currently in use.
    """
    pixel_size_m = pixel_size_um * 1e-6
    if mode == "prm":
        if prm_path is None:
            raise ValueError("prm_path is required for mode='prm'")
        prm  = parse_ipa_prm(prm_path)
        poni = ipa_to_poni(prm)
        detector = pf_detectors.Detector(pixel1=pixel_size_m, pixel2=pixel_size_m)
        return AzimuthalIntegrator(
            dist=poni.distance, poni1=poni.poni1, poni2=poni.poni2,
            rot1=poni.rot1, rot2=poni.rot2, rot3=poni.rot3,
            wavelength=poni.wavelength, detector=detector,
        )
    if mode == "poni":
        if poni_path is None:
            raise ValueError("poni_path is required for mode='poni'")
        ai = build_ai(parse_poni(poni_path))
        ai.detector = pf_detectors.Detector(pixel1=pixel_size_m, pixel2=pixel_size_m)
        return ai
    if mode == "manual":
        if manual is None:
            raise ValueError("manual params are required for mode='manual'")
        return build_ai_from_manual(manual, pixel_size_um)
    raise ValueError(f"Unknown initial-geometry mode: {mode!r}")


def _mgs_pos_function(metadata) -> float:
    """pos_function for GoniometerRefinement: metadata *is* the mgs value (mm)."""
    return float(metadata)


def make_goniometer_transformation() -> "ExtendedTransformation":
    return ExtendedTransformation(
        param_names=["dist0", "poni1", "poni2", "rot1", "rot2", "scale0", "energy"],
        pos_names=["mgs"],
        dist_expr="dist0 + mgs*scale0",
        poni1_expr="poni1",
        poni2_expr="poni2",
        rot1_expr="rot1",
        rot2_expr="rot2",
        rot3_expr="0",
        wavelength_expr="hc/energy*1e-10",
        constants={"hc": HC_KEV_ANG},
    )


class XrdSnapWorker(QThread):
    """Runs a single Rad-icon snap() on a background thread.

    backend.snap() blocks for the full exposure (plus DMA/readout), which can
    be many seconds — calling it directly from a UI slot freezes the window
    for that whole time, so "Take XRD" runs it here instead.
    """

    done  = pyqtSignal(object)  # np.ndarray
    error = pyqtSignal(str)

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self._backend = backend

    def run(self) -> None:
        try:
            img = self._backend.snap()
            self.done.emit(img)
        except Exception as exc:
            self.error.emit(str(exc))


class MultiPositionCalibrationWorker(QThread):
    """Runs SingleGeometry ring extraction + staged GoniometerRefinement across
    all positions on a background thread. See SPEC.md for the staged-refine
    rationale (dist0 -> +beam geometry -> +wavelength -> simplex).
    """

    progress        = pyqtSignal(str)
    ring_extracted  = pyqtSignal(str, object, int)      # (label, SingleGeometry, n_points)
    stage_completed = pyqtSignal(str, float)             # (stage_name, chi2)
    completed       = pyqtSignal(object, dict)           # (ai_primary, results)
    failed          = pyqtSignal(str)

    def __init__(
        self,
        positions: list[CalibrationPosition],
        calibrant_name: str,
        ai_initial: "AzimuthalIntegrator",
        stages: FreeParamStages,
        max_rings: int = 8,
        pts_per_deg: float = 1.5,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._positions      = positions
        self._calibrant_name = calibrant_name
        self._ai_initial     = ai_initial
        self._stages         = stages
        self._max_rings      = max_rings
        self._pts_per_deg    = pts_per_deg

    def run(self) -> None:
        if not PYFAI_AVAILABLE:
            self.failed.emit("pyFAI is not installed.  Run: pip install pyFAI")
            return
        try:
            self._run()
        except Exception as exc:
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _run(self) -> None:
        positions = self._positions
        primary = next((p for p in positions if p.is_primary), positions[0])

        calibrant = get_calibrant(self._calibrant_name)
        calibrant.wavelength = self._ai_initial.wavelength
        detector = self._ai_initial.detector

        ai0 = self._ai_initial
        # ai0.dist is the initial guess for the *distance at the primary
        # position*, not dist0 itself — dist0 is the y-intercept at mgs=0
        # (dist = dist0 + mgs*scale0), so it must be back-solved from the
        # primary position's mgs value before use as a starting guess.
        dist0_init = ai0.dist - primary.mgs_mm * NOMINAL_MGS_SCALE_M_PER_MM

        # ── Ring extraction per position ──────────────────────────────────
        # Each position's true distance can differ a lot from ai0.dist (e.g.
        # 100mm vs 200mm apart) — extracting control points against a single
        # shared initial geometry makes ring-index assignment unreliable the
        # farther a position's real distance is from ai0.dist, which was
        # observed to silently corrupt the whole multi-position fit in
        # testing. Build a per-position initial geometry instead, using the
        # dist0/scale0 model to estimate that position's expected distance.
        for pos in positions:
            if pos.image is None or pos.mgs_mm is None:
                raise ValueError(f"Position '{pos.label}' has no image/mgs value")
            self.progress.emit(f"Extracting control points: {pos.label} (mgs={pos.mgs_mm} mm)…")
            dist_at_pos = dist0_init + pos.mgs_mm * NOMINAL_MGS_SCALE_M_PER_MM
            ai_pos = AzimuthalIntegrator(
                dist=dist_at_pos, poni1=ai0.poni1, poni2=ai0.poni2,
                rot1=ai0.rot1, rot2=ai0.rot2, rot3=ai0.rot3,
                wavelength=ai0.wavelength, detector=detector,
            )
            sg = SingleGeometry(
                pos.label, image=pos.image, calibrant=calibrant,
                detector=detector, geometry=ai_pos,
            )
            pos.control_points = sg.extract_cp(
                max_rings=self._max_rings, pts_per_deg=self._pts_per_deg, Imin=0,
            )
            pos.n_control_points = len(sg.geometry_refinement.data)
            self.ring_extracted.emit(pos.label, sg, pos.n_control_points)

        # ── Build the GoniometerRefinement ────────────────────────────────
        param = {
            "dist0":  dist0_init,
            "poni1":  ai0.poni1,
            "poni2":  ai0.poni2,
            "rot1":   ai0.rot1,
            "rot2":   ai0.rot2,
            "scale0": NOMINAL_MGS_SCALE_M_PER_MM,
            "energy": HC_KEV_ANG / (ai0.wavelength * 1e10),
        }
        # Start every parameter locked to its initial value (bounds min==max is
        # the safest, version-independent way to "fix" a parameter); each
        # stage below widens the bounds of the parameters it wants to free.
        bounds = {name: (val, val) for name, val in param.items()}
        bounds["dist0"] = (dist0_init - 0.1, dist0_init + 0.1)

        trans_function = make_goniometer_transformation()
        gonioref = GoniometerRefinement(
            param, bounds=bounds, pos_function=_mgs_pos_function,
            trans_function=trans_function, detector=detector,
            wavelength=ai0.wavelength,
        )
        for pos in positions:
            gonioref.new_geometry(
                pos.label, image=pos.image, metadata=pos.mgs_mm,
                control_points=pos.control_points, calibrant=calibrant,
            )

        # NB: refine2()/refine3() return the fitted parameter array, not chi2 —
        # chi2 must be read back separately via gonioref.chi2(). Also, once
        # constructed, gonioref.bounds is a plain *list* ordered like
        # param_names (not the dict it was constructed from) — index into it
        # by position, looked up by name.
        param_names = trans_function.param_names

        def _set_bound(name: str, lo: float, hi: float) -> None:
            gonioref.bounds[param_names.index(name)] = (lo, hi)

        # ── Stage 1: distance offset only ─────────────────────────────────
        self.progress.emit("Stage 1/3: refining distance offset (dist0)…")
        gonioref.refine2()
        self.stage_completed.emit("dist0", float(gonioref.chi2()))

        # ── Stage 2: + beam geometry (poni1, poni2, rot1, rot2) ───────────
        if any([self._stages.fit_poni1, self._stages.fit_poni2,
                self._stages.fit_rot1, self._stages.fit_rot2]):
            self.progress.emit("Stage 2/3: refining beam center + tilt…")
            if self._stages.fit_poni1:
                _set_bound("poni1", max(0.0, param["poni1"] - 0.05), param["poni1"] + 0.05)
            if self._stages.fit_poni2:
                _set_bound("poni2", max(0.0, param["poni2"] - 0.05), param["poni2"] + 0.05)
            if self._stages.fit_rot1:
                _set_bound("rot1", -0.2, 0.2)
            if self._stages.fit_rot2:
                _set_bound("rot2", -0.2, 0.2)
            gonioref.refine2()
            self.stage_completed.emit("beam_geometry", float(gonioref.chi2()))

        # ── Stage 3: + wavelength/energy (optional) ───────────────────────
        if self._stages.fit_wavelength:
            self.progress.emit("Stage 3/3: refining energy/wavelength…")
            e0 = param["energy"]
            _set_bound("energy", e0 - 1.0, e0 + 1.0)
            gonioref.refine2()
            self.stage_completed.emit("wavelength", float(gonioref.chi2()))

        # ── Final polish (unbounded simplex) ──────────────────────────────
        # refine3()'s simplex ignores self.bounds entirely (Nelder-Mead has no
        # bounds support in pyFAI) and frees every parameter not explicitly
        # named in fix= — so any parameter the user left unchecked (plus
        # scale0, always) must be pinned here explicitly, or it would drift
        # freely and corrupt the rest of the fit.
        fix_params = ["scale0"]
        if not self._stages.fit_poni1:
            fix_params.append("poni1")
        if not self._stages.fit_poni2:
            fix_params.append("poni2")
        if not self._stages.fit_rot1:
            fix_params.append("rot1")
        if not self._stages.fit_rot2:
            fix_params.append("rot2")
        if not self._stages.fit_wavelength:
            fix_params.append("energy")
        self.progress.emit("Final polish (simplex, no bounds)…")
        gonioref.refine3(fix=fix_params, method="simplex")
        chi2 = float(gonioref.chi2())
        self.stage_completed.emit("simplex", chi2)

        # ── Evaluate the fitted geometry at the primary position ─────────
        # (the non-primary positions only exist to constrain the fit — the
        # geometry actually used for real measurements is the primary one)
        ai_primary = gonioref.get_ai(primary.mgs_mm)
        self.progress.emit("Done.")
        self.completed.emit(ai_primary, {
            "chi2":          float(chi2),
            "params":        dict(zip(trans_function.param_names, gonioref.param)),
            "primary_label": primary.label,
            "primary_mgs":   primary.mgs_mm,
        })
