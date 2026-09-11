"""FluoRaPressée HTTP reader used by Ruby Finder."""
from __future__ import annotations

import json
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np

try:
    from apps.scan2d.free_2d_scan_backend import GpibReaderSim
except ImportError:
    import os, sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    from apps.scan2d.free_2d_scan_backend import GpibReaderSim

DEFAULT_FRP_BASE_URL = "http://192.168.1.102:8765"


class FluoraPresseeError(RuntimeError):
    """Raised when the remote acquisition service cannot supply a spectrum."""


class FluoraPresseeReader:
    """Adapt ``POST /acquire`` to the scalar reader interface used by scan2d.

    The scalar is the sum of every finite value in the background-corrected
    ``y`` spectrum returned by FluoRaPressée. Detector ROI and calibration
    settings are always left unchanged. Exposure time and accumulation count
    follow the server's current setting unless ``exposure_time_s`` /
    ``accumulations`` are set on this reader, in which case that value is
    sent as a per-acquisition override (FluoRaPressée's ``AcquireRequest``
    supports both; it has no EM-gain override, so gain must still be set in
    the FluoRaPressée GUI).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_FRP_BASE_URL,
        api_key: str = "",
        timeout_s: float = 120.0,
        opener: Callable = urlopen,
        retain_spectra: bool = False,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("FluoRaPressée URL must be an http:// or https:// URL")
        if not api_key.strip():
            raise ValueError("FluoRaPressée API key is required")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        self.base_url = base_url
        self._api_key = api_key.strip()
        self.timeout_s = float(timeout_s)
        self._opener = opener
        self.last_acquisition: dict | None = None
        self._expected_state_token: str | None = None
        self.exposure_time_s: float | None = None
        self.accumulations: int | None = None
        self.retain_spectra = bool(retain_spectra)
        self.spectrum_x: np.ndarray | None = None
        self.retained_spectra: list[np.ndarray] = []

    def set_current_position(self, x_pulse: int, y_pulse: int) -> None:
        """The real reader does not need simulated stage coordinates."""

    def get_status(self) -> dict:
        """Return the raw ``GET /status`` response (connectivity/diagnostics)."""
        return self._request_json("GET", "/status")

    def prepare(self) -> None:
        """Check authentication and detector readiness before stage motion."""
        self.spectrum_x = None
        self.retained_spectra.clear()
        status = self.get_status()
        if status.get("busy") is True:
            raise FluoraPresseeError("FluoRaPressée is busy")
        if status.get("camera_connected") is not True:
            raise FluoraPresseeError("FluoRaPressée detector is not connected")
        token = status.get("instrument_state_token")
        self._expected_state_token = token if isinstance(token, str) and token else None

    def read_transmitted(self) -> float:
        result = self.acquire_spectrum()
        spectrum = np.asarray(result["y"], dtype=float)
        if self.retain_spectra:
            self._retain_spectrum(result, spectrum)
        return float(np.sum(spectrum, dtype=float))

    def _retain_spectrum(self, result: dict, spectrum: np.ndarray) -> None:
        """Retain one acquisition while enforcing a stable spectral axis."""
        try:
            x = np.asarray(result["x"], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise FluoraPresseeError(
                "FluoRaPressée response does not contain a numeric x axis"
            ) from exc
        if x.ndim != 1 or x.shape != spectrum.shape or not np.all(np.isfinite(x)):
            raise FluoraPresseeError(
                "FluoRaPressée returned an invalid spectral x axis"
            )
        if self.spectrum_x is None:
            self.spectrum_x = x.copy()
        elif not np.array_equal(self.spectrum_x, x):
            raise FluoraPresseeError(
                "FluoRaPressée spectral x axis changed during the scan"
            )
        self.retained_spectra.append(spectrum.astype(np.float32, copy=True))

    def acquire_spectrum(self) -> dict:
        """Acquire and return one complete 1-D spectrum response.

        ``prepare()`` should be called first so the acquisition is guarded by
        the instrument-state token captured before any stage movement.
        """
        payload = self._acquisition_payload()
        result = self._request_json("POST", "/acquire", payload)
        self._validate_spectrum_response(result)
        self.last_acquisition = result
        return result

    def acquire_spectrum_with_fit(
        self,
        *,
        fit_function: str = "Moffat",
        fit_peak_count: int = 2,
        peak_sort_order: str = "x_desc",
        baseline_model: str = "constant",
    ) -> dict:
        """Acquire a spectrum and request a server-side peak fit.

        FluoRaPressée returns the acquired spectrum even when the numerical
        fit reports ``success: false``. This method therefore validates only
        the acquisition here and leaves fit-result handling to the caller.
        """
        payload = self._acquisition_payload()
        payload.update({
            "fit_function": fit_function,
            "fit_peak_count": int(fit_peak_count),
            "peak_sort_order": peak_sort_order,
            "baseline_model": baseline_model,
        })
        result = self._request_json("POST", "/acquire/fit", payload)
        self._validate_spectrum_response(result)
        self.last_acquisition = result
        return result

    def _acquisition_payload(self) -> dict:
        payload = {"dark": {"mode": "none"}}
        if self._expected_state_token is not None:
            payload["expected_state_token"] = self._expected_state_token
        if self.exposure_time_s is not None:
            payload["exposure_time_s"] = self.exposure_time_s
        if self.accumulations is not None:
            payload["accumulations"] = self.accumulations
        return payload

    @staticmethod
    def _validate_spectrum_response(result: dict) -> None:
        if result.get("mode") != "1d":
            raise FluoraPresseeError(
                "FluoRaPressée returned a 2-D acquisition; select a 1-D spectrum mode"
            )

        try:
            spectrum = np.asarray(result["y"], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise FluoraPresseeError(
                "FluoRaPressée response does not contain a numeric y spectrum"
            ) from exc
        if spectrum.ndim != 1 or spectrum.size == 0:
            raise FluoraPresseeError("FluoRaPressée returned an empty or invalid spectrum")
        if not np.all(np.isfinite(spectrum)):
            raise FluoraPresseeError("FluoRaPressée spectrum contains non-finite values")

    def public_metadata(self) -> dict:
        """Return log-safe acquisition metadata (never includes the API key)."""
        metadata = {
            "source": "FluoRaPressée POST /acquire",
            "base_url": self.base_url,
            "intensity_reduction": "sum(y)",
            "wavelength_region": "all acquired spectral points",
            "dark_mode": "none",
            "instrument_state_locked": self._expected_state_token is not None,
            "exposure_time_s_override": self.exposure_time_s,
            "accumulations_override": self.accumulations,
        }
        if self.last_acquisition is not None:
            metadata.update(
                exposure_time_s=self.last_acquisition.get("exposure_time_s"),
                frp_accumulations=self.last_acquisition.get("accumulations"),
                configuration=self.last_acquisition.get("configuration"),
                hardware_state=self.last_acquisition.get("hardware_state"),
                x_axis=self.last_acquisition.get("x_axis"),
            )
        return metadata

    def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-API-Key": self._api_key,
            },
        )
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = self._http_error_detail(exc)
            raise FluoraPresseeError(
                f"FluoRaPressée API returned HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FluoraPresseeError(
                f"Cannot connect to FluoRaPressée at {self.base_url}: {reason}"
            ) from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FluoraPresseeError("FluoRaPressée returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise FluoraPresseeError("FluoRaPressée returned an unexpected JSON value")
        return decoded

    @staticmethod
    def _http_error_detail(exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("detail") if isinstance(payload, dict) else None
            return str(detail) if detail is not None else str(payload)
        except Exception:
            return str(exc.reason)


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
