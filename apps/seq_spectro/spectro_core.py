"""Qt-free model of Sequential Spectroscopic Measurements.

Positions, per-marker calculation settings, input validation, the
``POST /acquire/pressure`` request fields, response evaluation and the
averaged pressures. The pressure itself is always calculated by
FluoRaPressée; this module only chooses what to ask for and judges the
answer.

The scale keys mirror ``PressureCalculator.PRESSURE_SCALES`` /
``TEMPERATURE_SCALES`` in the FluoRaPressée repository. FRP returns
``pressure_gpa: null`` for an unknown key, so keys are checked here before
any stage motion.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

MARKER_RUBY = "ruby"
MARKER_SM = "sm_srb4o7"


@dataclass(frozen=True)
class MarkerSpec:
    key: str                 # FRP sensor key
    label: str
    pressure_scales: dict[str, str]
    temperature_scales: dict[str, str]
    lambda_0_range_nm: tuple[float, float]
    fit_peak_count: int      # ruby: R1 + R2 doublet; Sm: the single 0-0 line
    # Rough λ₀ used only by the --debug simulation.
    typical_lambda_0_nm: float


MARKERS: dict[str, MarkerSpec] = {
    MARKER_RUBY: MarkerSpec(
        key=MARKER_RUBY,
        label="Ruby",
        pressure_scales={
            "ruby_shen_2020": "Shen et al. 2020",
            "ruby_kraus_2016": "Kraus et al. 2016",
            "ruby_sokolova_2013": "Sokolova et al. 2013",
            "ruby_jacobsen_2008": "Jacobsen et al. 2008",
            "ruby_dorogokupets_oganov_2007": "Dorogokupets and Oganov 2007",
            "ruby_holzapfel_2003": "Holzapfel 2003",
            "ruby_mao_1986": "Mao et al. 1986",
            "ruby_piermarini_1975": "Piermarini et al. 1975",
        },
        temperature_scales={
            "ruby_kobayashi_unpublished": "Kobayashi et al. unpublished (0–300 K)",
            "ruby_yen_nicol_1992": "Yen and Nicol 1992 (0–600 K)",
            "ruby_ragan_1992": "Ragan et al. 1992 (0–600 K)",
            "ruby_datchi_2007_ht": "Datchi et al. 2007 HT (296–900 K)",
            "ruby_datchi_2007_lt": "Datchi et al. 2007 LT (0–296 K)",
            "ruby_wei_2024": "Wei et al. 2024 R1 (296–773 K)",
        },
        lambda_0_range_nm=(690.0, 700.0),
        fit_peak_count=2,
        typical_lambda_0_nm=694.25,
    ),
    MARKER_SM: MarkerSpec(
        key=MARKER_SM,
        label="Sm2+:SrB4O7",
        pressure_scales={
            "sm_srb4o7_datchi_1997_mxb1986": "0-0 line: Datchi et al. 1997 (MXB1986)",
            "sm_srb4o7_datchi_2007_do2007": "0-0 line: Datchi et al. 2007 (DO2007)",
            "sm_srb4o7_rashchenko_2015_lam11": "0-0 line (lam1): Rashchenko et al. 2015",
            "sm_srb4o7_wei_2024": "0-0 line: Wei et al. 2024 (Ar PTM)",
        },
        temperature_scales={
            "sm_srb4o7_datchi_2007": "Datchi et al. 2007 (296–900 K)",
            "sm_srb4o7_wei_2024": "Wei et al. 2024 0-0 line (296–923 K)",
        },
        lambda_0_range_nm=(680.0, 690.0),
        fit_peak_count=1,
        typical_lambda_0_nm=685.41,
    ),
}

FIT_FUNCTIONS = ("Moffat", "Pseudo Voigt", "Lorentz", "Gauss")
BASELINE_MODELS = {
    "constant": "Constant",
    "linear": "Linear",
    "quadratic": "Quadratic",
    "auto_polynomial": "Auto polynomial (BIC)",
}

CHANNELS = (3, 4, 5)
# Move order used for every position change (same as the Experimental Scheduler).
MOVE_ORDER = (5, 4, 3)

T0_RANGE_K = (1.0, 1000.0)
EXPOSURE_RANGE_S = (0.001, 600.0)
ACCUMULATION_RANGE = (1, 1000)
# Outer bound only; the camera's own range comes from GET /hardware/camera/em-gain.
EM_GAIN_RANGE = (0, 1000)
TEMPERATURE_MAX_AGE_S = 10.0
# A temperature-corrected λ₀ further than this from the registered λ₀ means a
# wrong reference or model (ruby moves about -0.9 nm at 0 K, +4 nm at 900 K).
MAX_ZERO_PEAK_SHIFT_NM = 5.0


class SetupError(ValueError):
    """The sequence cannot be started with the current inputs."""


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# ── Settings ─────────────────────────────────────────────────────────────────

@dataclass
class MarkerSettings:
    """Calculation and acquisition settings shared by every position of one marker."""
    pressure_scale: str = ""          # deliberately no implicit default
    temperature_correction: bool = False
    temperature_scale: str = ""
    fit_function: str = "Moffat"
    baseline_model: str = "linear"
    fit_range_enabled: bool = False   # False: fit the whole spectrum
    fit_range_start_nm: float = 0.0
    fit_range_end_nm: float = 0.0
    r_squared_min: float = 0.95
    exposure_time_s: float = 0.1
    accumulations: int = 1
    # False: use FRP's EM gain as found at the start of the run (and never
    # touch it on cameras without one).
    em_gain_enabled: bool = False
    em_gain: int = 1
    # Below the R² minimum, retry with more signal (shared ES algorithm,
    # apps/exp_scheduler/spectrum_quality.next_retry): halve the exposure
    # when saturated, otherwise raise the EM gain up to em_gain_max, then add
    # accumulations up to max_accumulations.
    optimise: bool = True
    em_gain_max: int = 10
    max_accumulations: int = 4

    def fit_range(self) -> tuple[float, float] | None:
        if not self.fit_range_enabled:
            return None
        return (float(self.fit_range_start_nm), float(self.fit_range_end_nm))

    @classmethod
    def from_dict(cls, data: dict) -> "MarkerSettings":
        result = cls()
        if not isinstance(data, dict):
            return result
        for name, default in asdict(result).items():
            value = data.get(name, default)
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = default
            elif isinstance(default, float):
                value = _finite(value) if _finite(value) is not None else default
            else:
                value = str(value)
            setattr(result, name, value)
        return result


@dataclass
class Position:
    """One row of the position table.

    ``record`` holds the absolute Ch3/Ch4/Ch5 pulses captured by
    "Record current pos"; offsets are always computed from the records, never
    stored.
    """
    marker: str = MARKER_RUBY
    lambda_0_nm: float | None = None
    t0_k: float | None = None
    record: dict[int, int] | None = None

    def to_dict(self) -> dict:
        return {
            "marker": self.marker,
            "lambda_0_nm": self.lambda_0_nm,
            "t0_k": self.t0_k,
            "record": ({f"ch{ch}": int(self.record[ch]) for ch in CHANNELS}
                       if self.record else None),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        marker = data.get("marker") if data.get("marker") in MARKERS else MARKER_RUBY
        raw = data.get("record")
        record = None
        if isinstance(raw, dict):
            try:
                record = {ch: int(raw[f"ch{ch}"]) for ch in CHANNELS}
            except (KeyError, TypeError, ValueError):
                record = None
        return cls(marker, _finite(data.get("lambda_0_nm")), _finite(data.get("t0_k")), record)


def offsets(positions: list[Position]) -> list[dict[int, int] | None]:
    """Pulse offsets of every position from Pos1 (None when either is unrecorded)."""
    if not positions or positions[0].record is None:
        return [None] * len(positions)
    origin = positions[0].record
    return [None if p.record is None else {ch: p.record[ch] - origin[ch] for ch in CHANNELS}
            for p in positions]


def targets(positions: list[Position], current: dict[int, int]) -> list[dict[int, int]]:
    """Absolute targets when the stage currently sits on Pos1."""
    result = []
    for offset in offsets(positions):
        if offset is None:
            raise SetupError("Every position must be recorded")
        result.append({ch: current[ch] + offset[ch] for ch in CHANNELS})
    return result


def markers_in_use(positions: list[Position]) -> list[str]:
    """Markers present in the table, in the fixed MARKERS order."""
    used = {p.marker for p in positions}
    return [key for key in MARKERS if key in used]


def needs_temperature(positions: list[Position], settings: dict[str, MarkerSettings]) -> bool:
    return any(settings[m].temperature_correction for m in markers_in_use(positions))


def setup_errors(positions: list[Position], settings: dict[str, MarkerSettings],
                 output_dir: str) -> list[str]:
    """Every problem that blocks a run, as user-facing messages."""
    errors: list[str] = []
    if not positions:
        errors.append("Add at least one position")
    if not output_dir.strip():
        errors.append("Choose an output directory")
    for index, pos in enumerate(positions, start=1):
        spec = MARKERS[pos.marker]
        if pos.record is None:
            errors.append(f"Pos{index}: record the stage position")
        low, high = spec.lambda_0_range_nm
        if pos.lambda_0_nm is None:
            errors.append(f"Pos{index}: enter λ₀")
        elif not low <= pos.lambda_0_nm <= high:
            errors.append(f"Pos{index}: λ₀ of {spec.label} must be {low:g}–{high:g} nm")
        if settings[pos.marker].temperature_correction:
            if pos.t0_k is None:
                errors.append(f"Pos{index}: enter T₀ (temperature correction is on for {spec.label})")
            elif not T0_RANGE_K[0] <= pos.t0_k <= T0_RANGE_K[1]:
                errors.append(f"Pos{index}: T₀ must be {T0_RANGE_K[0]:g}–{T0_RANGE_K[1]:g} K")
    for marker in markers_in_use(positions):
        spec, s = MARKERS[marker], settings[marker]
        if s.pressure_scale not in spec.pressure_scales:
            errors.append(f"{spec.label}: select a pressure scale")
        if s.temperature_correction and s.temperature_scale not in spec.temperature_scales:
            errors.append(f"{spec.label}: select a temperature-correction scale")
        if s.fit_function not in FIT_FUNCTIONS:
            errors.append(f"{spec.label}: invalid fit function")
        if s.baseline_model not in BASELINE_MODELS:
            errors.append(f"{spec.label}: invalid baseline model")
        if s.fit_range_enabled and not s.fit_range_start_nm < s.fit_range_end_nm:
            errors.append(f"{spec.label}: fit range start must be below its end")
        if not 0.0 <= s.r_squared_min <= 1.0:
            errors.append(f"{spec.label}: R² minimum must be 0–1")
        if not EXPOSURE_RANGE_S[0] <= s.exposure_time_s <= EXPOSURE_RANGE_S[1]:
            errors.append(f"{spec.label}: exposure time must be "
                          f"{EXPOSURE_RANGE_S[0]:g}–{EXPOSURE_RANGE_S[1]:g} s")
        if not ACCUMULATION_RANGE[0] <= s.accumulations <= ACCUMULATION_RANGE[1]:
            errors.append(f"{spec.label}: accumulations must be "
                          f"{ACCUMULATION_RANGE[0]}–{ACCUMULATION_RANGE[1]}")
        if s.optimise:
            if not ACCUMULATION_RANGE[0] <= s.max_accumulations <= ACCUMULATION_RANGE[1]:
                errors.append(f"{spec.label}: maximum accumulations must be "
                              f"{ACCUMULATION_RANGE[0]}–{ACCUMULATION_RANGE[1]}")
            elif s.max_accumulations < s.accumulations:
                errors.append(f"{spec.label}: maximum accumulations must not be below "
                              f"the accumulations ({s.accumulations})")
            if not 1 <= s.em_gain_max <= EM_GAIN_RANGE[1]:
                errors.append(f"{spec.label}: EM gain maximum must be 1–{EM_GAIN_RANGE[1]}")
        if s.em_gain_enabled and (type(s.em_gain) is not int
                                  or not EM_GAIN_RANGE[0] <= s.em_gain <= EM_GAIN_RANGE[1]):
            errors.append(f"{spec.label}: EM gain must be an integer "
                          f"{EM_GAIN_RANGE[0]}–{EM_GAIN_RANGE[1]}")
    return errors


def em_gain_errors(info: dict, positions: list[Position],
                   settings: dict[str, MarkerSettings]) -> list[str]:
    """Check the requested EM gains against FRP's ``GET /hardware/camera/em-gain``."""
    requested = [(MARKERS[m].label, settings[m].em_gain) for m in markers_in_use(positions)
                 if settings[m].em_gain_enabled]
    if not requested:
        return []
    if info.get("exists") is not True:
        return ["The FluoRaPressée camera has no EM gain; untick “Set EM gain”"]
    if info.get("available") is not True:
        return ["The FluoRaPressée camera EM gain cannot be changed now"]
    low, high, step = info.get("minimum"), info.get("maximum"), info.get("increment")
    if not all(type(v) is int for v in (low, high, step)) or step < 1:
        return ["FluoRaPressée reported an invalid EM gain range"]
    errors = []
    for label, gain in requested:
        if not low <= gain <= high:
            errors.append(f"{label}: EM gain {gain} is outside the camera range {low}–{high}")
        elif (gain - low) % step:
            errors.append(f"{label}: EM gain {gain} is not a multiple of the step {step} from {low}")
    return errors


def check_frp_status(status: dict) -> None:
    """Require a 1-D, wavelength-calibrated FRP state before stage motion.

    ``POST /acquire/pressure`` only rejects a missing calibration after the
    acquisition, so this is checked from ``GET /status`` first.
    """
    roi = status.get("roi") if isinstance(status.get("roi"), dict) else {}
    if roi.get("mode") not in {"1d_roi", "1d_full"}:
        raise SetupError("FluoRaPressée is not in a 1-D spectrum mode")
    calibration = status.get("calibration") if isinstance(status.get("calibration"), dict) else {}
    configuration = (status.get("configuration")
                     if isinstance(status.get("configuration"), dict) else {})
    if calibration.get("applied") is not True or configuration.get("axis_mode") != "calibrated":
        raise SetupError("FluoRaPressée has no wavelength calibration applied")
    unit = configuration.get("unit") or calibration.get("unit")
    if not isinstance(unit, str) or unit.strip().lower() not in {"wavelength", "nm"}:
        raise SetupError(f"FluoRaPressée calibration unit is {unit!r}; a wavelength (nm) axis is needed")


# ── Request and response ─────────────────────────────────────────────────────

def build_request(pos: Position, settings: MarkerSettings,
                  temperature_k: float | None) -> dict:
    """Pressure fields added to the ``/acquire/fit`` body (peak index 1)."""
    request = {
        "sensor": pos.marker,
        "pressure_scale": settings.pressure_scale,
        "zero_pressure_peak": pos.lambda_0_nm,
        "pressure_peak_index": 1,
    }
    if settings.temperature_correction:
        if temperature_k is None or pos.t0_k is None:
            raise SetupError("Temperature correction needs the sample temperature and T₀")
        request["temperature_correction"] = {
            "enabled": True,
            "scale": settings.temperature_scale,
            "current_t": float(temperature_k),
            "t0": float(pos.t0_k),
            "zero_pressure_peak_at_t0": float(pos.lambda_0_nm),
        }
    return request


STATUS_VALID = "valid"
STATUS_LOW_R2 = "low_r2"
STATUS_SATURATED = "saturated"
STATUS_T_RANGE = "temperature_out_of_range"
STATUS_INVALID = "invalid"
STATUS_NOT_MEASURED = "not_measured"

STATUS_LABELS = {
    STATUS_VALID: "OK",
    STATUS_LOW_R2: "Low R²",
    STATUS_SATURATED: "Saturated",
    STATUS_T_RANGE: "T out of range",
    STATUS_INVALID: "Invalid",
    STATUS_NOT_MEASURED: "Not measured",
}


@dataclass
class PositionResult:
    """Outcome of one position; only ``valid`` results enter the averages."""
    index: int                         # 1-based
    marker: str
    status: str = STATUS_NOT_MEASURED
    error: str = ""
    warning: str = ""
    target: dict[int, int] | None = None
    temperature_k: float | None = None
    temperature_source: str = ""
    temperature_timestamp: str = ""
    em_gain: int | None = None         # gain in effect for the acquisition, if known
    exposure_s: float | None = None    # conditions of the final (reported) attempt
    accumulations: int | None = None
    attempts: int = 0
    saved: bool = False                # only spectra that pass the R² minimum are saved
    peak_nm: float | None = None
    peak_err_nm: float | None = None
    peak2_nm: float | None = None
    r_squared: float | None = None
    lambda_0_at_t_nm: float | None = None
    pressure_gpa: float | None = None
    pressure_err_gpa: float | None = None
    spectrum_file: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_VALID


def _peaks(response: dict) -> tuple[list[dict], dict]:
    fit = response.get("fit") if isinstance(response.get("fit"), dict) else {}
    details = fit.get("fit") if fit.get("success") is True and isinstance(fit.get("fit"), dict) else {}
    peaks = details.get("peaks") if isinstance(details.get("peaks"), list) else []
    return [p for p in peaks if isinstance(p, dict)], details


def evaluate(result: PositionResult, pos: Position, settings: MarkerSettings,
             response: dict) -> PositionResult:
    """Fill ``result`` from an ``/acquire/pressure`` response and judge it."""
    peaks, details = _peaks(response)
    peak = peaks[0] if peaks else {}
    result.peak_nm = _finite(peak.get("position"))
    result.peak_err_nm = _finite(peak.get("position_err"))
    result.peak2_nm = _finite(peaks[1].get("position")) if len(peaks) > 1 else None
    result.r_squared = _finite(details.get("R2"))
    result.pressure_gpa = _finite(response.get("pressure_gpa"))
    result.pressure_err_gpa = _finite(response.get("pressure_err_gpa"))
    result.lambda_0_at_t_nm = _finite(response.get("zero_pressure_peak_at_current_t"))
    warning = response.get("temperature_warning")
    t_warning = warning.strip() if isinstance(warning, str) else ""

    try:
        x = [float(v) for v in response.get("x") or []]
    except (TypeError, ValueError):
        x = []
    if x and pos.lambda_0_nm is not None and not min(x) <= pos.lambda_0_nm <= max(x):
        result.warning = (f"λ₀ {pos.lambda_0_nm:.2f} nm is outside the acquired range "
                          f"{min(x):.1f}–{max(x):.1f} nm")

    error = _response_error(result, pos, settings, response, x)
    if response.get("saturated") is True:
        result.status, result.error = STATUS_SATURATED, "Detector saturated"
    elif error:
        result.status, result.error = STATUS_INVALID, error
    elif result.r_squared is None or result.r_squared < settings.r_squared_min:
        r2 = "n/a" if result.r_squared is None else f"{result.r_squared:.4f}"
        result.status = STATUS_LOW_R2
        result.error = f"R² {r2} is below {settings.r_squared_min:g}"
    elif t_warning:
        # A number may come back, but outside the correction model's range.
        result.status, result.error = STATUS_T_RANGE, t_warning
    else:
        result.status, result.error = STATUS_VALID, ""
    return result


def _response_error(result: PositionResult, pos: Position, settings: MarkerSettings,
                    response: dict, x: list[float]) -> str:
    fit = response.get("fit") if isinstance(response.get("fit"), dict) else {}
    if fit.get("success") is not True:
        return "Peak fit failed"
    x_axis = response.get("x_axis") if isinstance(response.get("x_axis"), dict) else {}
    if x_axis.get("unit") != "nm":
        return f"FRP x axis unit is {x_axis.get('unit')!r}, not nm"
    if result.peak_nm is None:
        return "FRP fit has no finite Peak1 position"
    if result.pressure_gpa is None:
        return "FRP did not return a finite pressure"
    if x and not min(x) <= result.peak_nm <= max(x):
        return f"Peak1 ({result.peak_nm:.3f} nm) lies outside the acquired range"
    if pos.marker == MARKER_RUBY and result.peak2_nm is not None \
            and not result.peak_nm > result.peak2_nm:
        return "Peak1 is not the longer-wavelength (R1) line of the fitted doublet"
    corrected = result.lambda_0_at_t_nm
    if settings.temperature_correction and corrected is None:
        return "FRP did not return the temperature-corrected λ₀"
    if corrected is not None and pos.lambda_0_nm is not None \
            and abs(corrected - pos.lambda_0_nm) > MAX_ZERO_PEAK_SHIFT_NM:
        return (f"Corrected λ₀ {corrected:.3f} nm differs from λ₀ {pos.lambda_0_nm:.3f} nm "
                f"by more than {MAX_ZERO_PEAK_SHIFT_NM:g} nm")
    return ""


def quality_passed(quality, settings: MarkerSettings) -> bool:
    """R² criterion for a ``spectrum_quality.SpectrumQuality``: not saturated,
    fitted, and R² at or above the marker's minimum. Only such spectra are saved."""
    return (not quality.saturated and quality.fit_success
            and quality.r_squared is not None
            and quality.r_squared >= settings.r_squared_min)


# ── Averages ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MeanPressure:
    mean_gpa: float
    err_gpa: float | None   # None when any contributing point lacks an error
    count: int


def mean_pressure(results: list[PositionResult]) -> MeanPressure | None:
    """Arithmetic mean of the valid points with the fit errors propagated.

    P̄ = ΣPᵢ / n and σ(P̄) = √(Σσᵢ²) / n.
    """
    valid = [r for r in results if r.is_valid and r.pressure_gpa is not None]
    if not valid:
        return None
    n = len(valid)
    mean = sum(r.pressure_gpa for r in valid) / n
    errors = [r.pressure_err_gpa for r in valid]
    err = None if any(e is None for e in errors) else math.sqrt(sum(e * e for e in errors)) / n
    return MeanPressure(mean, err, n)


def format_mean(value: MeanPressure | None) -> str:
    if value is None:
        return "n/a"
    err = f" ± {value.err_gpa:.3f}" if value.err_gpa is not None else " (error n/a)"
    return f"{value.mean_gpa:.3f}{err} GPa  (n = {value.count})"
