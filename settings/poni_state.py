"""Shared calibration state consumed by any window that needs a pyFAI AzimuthalIntegrator."""
from __future__ import annotations

import pathlib

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

try:
    from pyFAI.integrator.azimuthal import AzimuthalIntegrator
except ImportError:
    AzimuthalIntegrator = object  # type: ignore[misc,assignment]


class PoniState(QObject):
    """Observable container for the current calibrated AzimuthalIntegrator and its provenance.

    Created once in ModeSelectorLauncher and shared with every sub-window that
    needs detector geometry (XrdScanWindow, future SingleCrystalWindow, …).
    Update via :meth:`update`; listen to :attr:`poni_changed` to react to changes.
    """

    poni_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.ai:             AzimuthalIntegrator | None = None
        self.prm_path:       pathlib.Path | None = None
        self.ceo2_path:      pathlib.Path | None = None
        self.poni_path:      pathlib.Path | None = None   # set when saved to disk
        self.chi2_before:    float | None        = None
        self.chi2_after:     float | None        = None
        self.n_control_pts:  int | None          = None
        self.radial:         np.ndarray | None   = None
        self.intensity:      np.ndarray | None   = None

    def update(
        self,
        ai,
        prm_path:      pathlib.Path | None = None,
        ceo2_path:     pathlib.Path | None = None,
        chi2_before:   float | None        = None,
        chi2_after:    float | None        = None,
        n_control_pts: int | None          = None,
        radial:        np.ndarray | None   = None,
        intensity:     np.ndarray | None   = None,
        poni_path:     pathlib.Path | None = None,
    ) -> None:
        """Replace calibration state and emit poni_changed.

        `poni_path` should be passed whenever `ai` corresponds to a file
        already on disk (e.g. just loaded from, or just saved to, a .poni
        file) so listeners see the association atomically with `ai` itself,
        rather than in a separate step after the signal has already fired.
        Omit it for an in-session calibration result with no backing file.
        """
        self.ai            = ai
        self.prm_path      = prm_path
        self.ceo2_path     = ceo2_path
        self.chi2_before   = chi2_before
        self.chi2_after    = chi2_after
        self.n_control_pts = n_control_pts
        self.radial        = radial
        self.intensity     = intensity
        self.poni_path     = poni_path
        self.poni_changed.emit()

    def is_calibrated(self) -> bool:
        return self.ai is not None
