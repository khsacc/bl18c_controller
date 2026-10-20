"""Ruby Finder scan adapter and spectrum-cube helpers."""
from __future__ import annotations

import numpy as np

try:
    from utils.fluora_pressee import (
        DEFAULT_FRP_BASE_URL, FluoraPresseeError, FluoraPresseeReader,
    )
except ImportError:
    import os, sys
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    from utils.fluora_pressee import (
        DEFAULT_FRP_BASE_URL, FluoraPresseeError, FluoraPresseeReader,
    )

try:
    from apps.scan2d.free_2d_scan_backend import GpibReaderSim
except ImportError:
    import os, sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    from apps.scan2d.free_2d_scan_backend import GpibReaderSim


class FluoraPresseeReaderSim(GpibReaderSim):
    """2-D Gaussian map simulator that also retains a ruby-like spectrum."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.spectrum_x = np.linspace(680.0, 710.0, 512)
        profile = np.exp(-0.5 * ((self.spectrum_x - 694.2) / 0.8) ** 2)
        self._spectrum_profile = profile / np.sum(profile)
        self.retained_spectra: list[np.ndarray] = []
        self.last_acquisition = {
            "x_axis": {"label": "Wavelength", "unit": "nm"},
            "simulation": True,
        }

    def prepare(self) -> None:
        self.retained_spectra.clear()

    def read_transmitted(self) -> float:
        intensity = super().read_transmitted()
        spectrum = (intensity * self._spectrum_profile).astype(np.float32)
        self.retained_spectra.append(spectrum)
        return intensity


def build_spectrum_cube(
    spectra: list[np.ndarray],
    *,
    n_y: int,
    n_x: int,
    reads_per_point: int,
    measured_mask: np.ndarray,
) -> np.ndarray | None:
    """Average retained reads and place completed points into scan-grid order."""
    if not spectra:
        return None
    reads_per_point = max(1, int(reads_per_point))
    completed = int(np.count_nonzero(measured_mask))
    usable = min(completed, len(spectra) // reads_per_point)
    if usable == 0:
        return None

    spectral_points = spectra[0].size
    cube = np.full((n_y, n_x, spectral_points), np.nan, dtype=np.float32)
    measured_cells = np.argwhere(measured_mask)
    for point_index, (row, col) in enumerate(measured_cells[:usable]):
        start = point_index * reads_per_point
        point_reads = spectra[start:start + reads_per_point]
        if any(item.shape != (spectral_points,) for item in point_reads):
            raise ValueError("Retained spectra do not all have the same shape")
        cube[row, col] = np.mean(point_reads, axis=0, dtype=np.float64)
    return cube
