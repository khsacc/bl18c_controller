"""Ruby pressure calculation through FluoRaPressée ``POST /acquire/pressure``.

Qt-free helpers shared by SequenceRunner and PreValidator. The pressure
itself is always calculated by FluoRaPressée; this module only

* lists the backend keys the scheduler may send (mirrors
  ``PressureCalculator.PRESSURE_SCALES["ruby"]`` / ``TEMPERATURE_SCALES["ruby"]``
  in the FluoRaPressée repository — FRP silently returns ``pressure_gpa: null``
  for an unknown key, so keys are checked here before any stage motion),
* checks the per-ruby zero-pressure references and the calculation settings,
* reads the measured sample temperature (never a setpoint) right before the
  final acquisition,
* builds the request fields, and
* validates the response before a pressure is reported as measured.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime

SENSOR = "ruby"
# FRP sorts the fitted peaks with peak_sort_order="x_desc", so Peak1 is the
# longer-wavelength R1 line.
PRESSURE_PEAK_INDEX = 1

PRESSURE_SCALES: dict[str, str] = {
    "ruby_shen_2020": "Shen et al. 2020",
    "ruby_kraus_2016": "Kraus et al. 2016",
    "ruby_sokolova_2013": "Sokolova et al. 2013",
    "ruby_jacobsen_2008": "Jacobsen et al. 2008",
    "ruby_dorogokupets_oganov_2007": "Dorogokupets and Oganov 2007",
    "ruby_holzapfel_2003": "Holzapfel 2003",
    "ruby_mao_1986": "Mao et al. 1986",
    "ruby_piermarini_1975": "Piermarini et al. 1975",
}

# key -> (label, valid temperature range in K). The range is only used by the
# debug simulation; the real warning comes from FRP's temperature_warning.
TEMPERATURE_SCALES: dict[str, tuple[str, tuple[float, float]]] = {
    "ruby_kobayashi_unpublished": ("Kobayashi et al. unpublished", (0.0, 300.0)),
    "ruby_yen_nicol_1992": ("Yen and Nicol 1992", (0.0, 600.0)),
    "ruby_ragan_1992": ("Ragan et al. 1992", (0.0, 600.0)),
    "ruby_datchi_2007_ht": ("Datchi et al. 2007 HT", (296.0, 900.0)),
    "ruby_datchi_2007_lt": ("Datchi et al. 2007 LT", (0.0, 296.0)),
    "ruby_wei_2024": ("Wei et al. 2024 (R1)", (296.0, 773.0)),
}

TEMPERATURE_SOURCES: dict[str, str] = {
    "lakeshore_b": "LakeShore 335 Ch B (measured)",
    "manual": "Manual value",
}

LAMBDA_0_RANGE_NM = (690.0, 700.0)
T0_RANGE_K = (1.0, 1000.0)
TEMPERATURE_RANGE_K = (0.1, 1000.0)
TEMPERATURE_MAX_AGE_RANGE_S = (1.0, 600.0)
# Ruby R1 moves by roughly -0.9 nm (0 K) to +4 nm (900 K) from room
# temperature; a larger corrected-λ₀ shift means a wrong reference or model.
MAX_ZERO_PEAK_SHIFT_NM = 5.0
# Allowed clock skew between the LakeShore poll timestamp and this process.
_FUTURE_TOLERANCE_S = 1.0


class PressureSetupError(ValueError):
    """Pressure calculation cannot be requested with the current settings."""


def pressure_scale_label(key: str) -> str:
    return PRESSURE_SCALES.get(key, key)


def temperature_scale_label(key: str) -> str:
    entry = TEMPERATURE_SCALES.get(key)
    return entry[0] if entry else key


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def stop_condition_error(action, settings) -> str | None:
    """Validate the optional rising-pressure sequence stop condition."""
    target = action.stop_pressure_target_gpa
    tolerance = action.stop_pressure_tolerance_gpa
    if target is None:
        if _finite(tolerance) != 0.0:
            return "Ruby stop tolerance requires a target pressure"
        return None
    if not settings.pressure_enabled:
        return "Enable ruby pressure calculation before using a pressure stop condition"
    target_value = _finite(target)
    tolerance_value = _finite(tolerance)
    if target_value is None or target_value <= 0.0:
        return "Ruby stop target must be a finite positive GPa value"
    if tolerance_value is None or not 0.0 <= tolerance_value < target_value:
        return "Ruby stop tolerance must be finite, non-negative and below the target"
    return None


def stop_condition_met(action, record: "PressureRecord | None") -> bool:
    """An upper-band overshoot also stops a rising-pressure sequence."""
    return (action.stop_pressure_target_gpa is not None
            and record is not None and record.is_measured
            and record.pressure_gpa >= (action.stop_pressure_target_gpa
                                        - action.stop_pressure_tolerance_gpa))


@dataclass(frozen=True)
class RubyReference:
    """Zero-pressure R1 position measured for one named ruby."""
    lambda_0_nm: float
    t0_k: float | None = None


def reference_for(settings, name: str) -> RubyReference | None:
    """Return the reference registered for exactly this ruby name.

    There is deliberately no fallback to another ruby or a shared default:
    every ruby in the cell has its own zero-pressure position.
    """
    values = (settings.ruby_references or {}).get(name)
    if not isinstance(values, dict):
        return None
    lambda_0 = _finite(values.get("lambda_0_nm"))
    if lambda_0 is None:
        return None
    return RubyReference(lambda_0, _finite(values.get("t0_k")))


def settings_errors(settings, names) -> list[tuple[str, str]]:
    """``[(code_suffix, message), ...]`` for a pressure-enabled measurement."""
    if not settings.pressure_enabled:
        return []
    errors: list[tuple[str, str]] = []
    if settings.pressure_scale not in PRESSURE_SCALES:
        errors.append(("scale_missing",
                       "Select a ruby pressure scale for the pressure calculation"))
    correction = settings.temperature_correction_enabled
    if type(correction) is not bool:
        errors.append(("temperature_invalid", "Temperature correction must be on or off"))
        correction = False
    if correction:
        if settings.temperature_scale not in TEMPERATURE_SCALES:
            errors.append(("temperature_scale_missing",
                           "Select a ruby temperature-correction scale"))
        if settings.temperature_source not in TEMPERATURE_SOURCES:
            errors.append(("temperature_source_invalid",
                           "Temperature source must be LakeShore 335 Ch B or a manual value"))
        elif settings.temperature_source == "manual":
            value = _finite(settings.manual_temperature_k)
            if value is None or not TEMPERATURE_RANGE_K[0] <= value <= TEMPERATURE_RANGE_K[1]:
                errors.append(("temperature_invalid",
                               "Manual sample temperature must be 0.1–1000 K"))
        max_age = _finite(settings.temperature_max_age_s)
        if (settings.temperature_source == "lakeshore_b"
                and (max_age is None or not TEMPERATURE_MAX_AGE_RANGE_S[0] <= max_age
                     <= TEMPERATURE_MAX_AGE_RANGE_S[1])):
            errors.append(("temperature_invalid",
                           "Maximum temperature-reading age must be 1–600 s"))
    for name in sorted(set(names)):
        reference = reference_for(settings, name)
        if reference is None:
            errors.append(("reference_missing",
                           f"Register the zero-pressure λ₀ of ruby {name!r} before calculating pressure"))
            continue
        low, high = LAMBDA_0_RANGE_NM
        if not low <= reference.lambda_0_nm <= high:
            errors.append(("reference_invalid",
                           f"λ₀ of ruby {name!r} must be {low:g}–{high:g} nm"))
        if correction:
            if reference.t0_k is None:
                errors.append(("reference_missing",
                               f"Register the λ₀ measurement temperature T₀ of ruby {name!r}"))
            elif not T0_RANGE_K[0] <= reference.t0_k <= T0_RANGE_K[1]:
                errors.append(("reference_invalid",
                               f"T₀ of ruby {name!r} must be {T0_RANGE_K[0]:g}–{T0_RANGE_K[1]:g} K"))
    return errors


@dataclass(frozen=True)
class TemperatureReading:
    value_k: float
    source: str
    timestamp: float | None = None   # epoch seconds of the LakeShore poll
    age_s: float | None = None

    def timestamp_iso(self) -> str:
        if self.timestamp is None:
            return ""
        return datetime.fromtimestamp(self.timestamp).isoformat(timespec="milliseconds")


def read_lakeshore_b(lakeshore, max_age_s: float, now: float | None = None) -> TemperatureReading:
    """Return the latest polled Ch B reading, or raise PressureSetupError."""
    if lakeshore is None:
        raise PressureSetupError("LakeShore 335 is not connected (Ch B temperature required)")
    cached = lakeshore.get_cached_readings()
    if cached.get("connected") is not True:
        raise PressureSetupError("LakeShore 335 is not connected (Ch B temperature required)")
    value = _finite(cached.get("temperature_b_k"))
    if value is None or value <= 0.0:
        raise PressureSetupError("LakeShore 335 Ch B has no valid temperature reading")
    timestamp = _finite(cached.get("timestamp"))
    if timestamp is None:
        raise PressureSetupError("LakeShore 335 Ch B reading has no timestamp")
    now = time.time() if now is None else now
    age = now - timestamp
    if age < -_FUTURE_TOLERANCE_S or age > max_age_s:
        raise PressureSetupError(
            f"LakeShore 335 Ch B reading is stale ({age:.1f} s old; limit {max_age_s:g} s)"
        )
    return TemperatureReading(value, "lakeshore_b", timestamp, max(age, 0.0))


def read_temperature(settings, lakeshore, now: float | None = None) -> TemperatureReading | None:
    """Temperature to send with the request.

    Required (raises PressureSetupError) when temperature correction is on.
    With correction off the measured Ch B value is still recorded when one is
    available, but its absence does not block the measurement.
    """
    if settings.temperature_correction_enabled:
        if settings.temperature_source == "manual":
            value = _finite(settings.manual_temperature_k)
            if value is None or value <= 0.0:
                raise PressureSetupError("Manual sample temperature is not set")
            return TemperatureReading(value, "manual")
        return read_lakeshore_b(lakeshore, float(settings.temperature_max_age_s), now)
    try:
        return read_lakeshore_b(lakeshore, float(settings.temperature_max_age_s), now)
    except Exception:
        return None


def check_status(status: dict) -> None:
    """Require a 1-D, wavelength-calibrated FRP state before stage motion.

    ``POST /acquire/pressure`` only rejects a missing calibration after the
    acquisition, so this is checked from ``GET /status`` first.
    """
    roi = status.get("roi") if isinstance(status.get("roi"), dict) else {}
    if roi.get("mode") not in {"1d_roi", "1d_full"}:
        raise PressureSetupError(
            "FluoRaPressée is not in a 1-D spectrum mode; pressure calculation needs 1-D data"
        )
    calibration = status.get("calibration") if isinstance(status.get("calibration"), dict) else {}
    configuration = (status.get("configuration")
                     if isinstance(status.get("configuration"), dict) else {})
    if calibration.get("applied") is not True or configuration.get("axis_mode", "calibrated") != "calibrated":
        raise PressureSetupError(
            "FluoRaPressée has no wavelength calibration applied; pressure calculation needs a calibrated axis"
        )
    unit = configuration.get("unit") or calibration.get("unit")
    if isinstance(unit, str) and unit.strip().lower() not in {"wavelength", "nm"}:
        raise PressureSetupError(
            f"FluoRaPressée calibration unit is {unit!r}; ruby pressure needs a wavelength (nm) axis"
        )


def build_request(settings, reference: RubyReference,
                  temperature: TemperatureReading | None) -> dict:
    """Pressure fields added to the ``/acquire/fit`` body."""
    request = {
        "sensor": SENSOR,
        "pressure_scale": settings.pressure_scale,
        "zero_pressure_peak": reference.lambda_0_nm,
        "pressure_peak_index": PRESSURE_PEAK_INDEX,
    }
    if settings.temperature_correction_enabled:
        if temperature is None or reference.t0_k is None:
            raise PressureSetupError("Temperature correction needs the sample temperature and T₀")
        request["temperature_correction"] = {
            "enabled": True,
            "scale": settings.temperature_scale,
            "current_t": temperature.value_k,
            "t0": reference.t0_k,
            "zero_pressure_peak_at_t0": reference.lambda_0_nm,
        }
    return request


@dataclass
class PressureRecord:
    """Everything needed to re-analyse one final acquisition attempt."""
    status: str = "not_evaluated"   # valid | temperature_out_of_range | invalid | not_evaluated
    error: str = ""
    ruby_position_name: str = ""
    sensor: str = SENSOR
    pressure_scale: str = ""
    pressure_scale_label: str = ""
    temperature_correction_enabled: bool = False
    temperature_scale: str = ""
    temperature_scale_label: str = ""
    temperature_source: str = ""
    lambda_0_nm: float | None = None
    t0_k: float | None = None
    measured_temperature_k: float | None = None
    measured_temperature_timestamp: str = ""
    measured_temperature_age_s: float | None = None
    pressure_peak_index: int = PRESSURE_PEAK_INDEX
    peak_nm: float | None = None
    peak_err_nm: float | None = None
    pressure_gpa: float | None = None
    pressure_err_gpa: float | None = None
    zero_pressure_peak_at_current_t_nm: float | None = None
    temperature_warning: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def npz_fields(self) -> dict:
        """Flat ``pressure_*`` NPZ fields; ``None`` becomes NaN or ``""``."""
        fields = {}
        for key, value in self.to_dict().items():
            name = key if key.startswith(("pressure_", "temperature_")) else f"pressure_{key}"
            if value is None:
                value = float("nan")
            fields[name] = value
        return fields

    @property
    def is_measured(self) -> bool:
        return self.status == "valid"


def new_record(settings, name: str, reference: RubyReference | None,
               temperature: TemperatureReading | None) -> PressureRecord:
    correction = bool(settings.temperature_correction_enabled)
    return PressureRecord(
        ruby_position_name=name,
        pressure_scale=settings.pressure_scale,
        pressure_scale_label=pressure_scale_label(settings.pressure_scale),
        temperature_correction_enabled=correction,
        temperature_scale=settings.temperature_scale if correction else "",
        temperature_scale_label=temperature_scale_label(settings.temperature_scale) if correction else "",
        temperature_source=(temperature.source if temperature is not None
                            else (settings.temperature_source if correction else "")),
        lambda_0_nm=reference.lambda_0_nm if reference else None,
        t0_k=reference.t0_k if reference else None,
        measured_temperature_k=temperature.value_k if temperature else None,
        measured_temperature_timestamp=temperature.timestamp_iso() if temperature else "",
        measured_temperature_age_s=temperature.age_s if temperature else None,
    )


def evaluate(record: PressureRecord, response: dict, *, quality_passed: bool) -> PressureRecord:
    """Fill ``record`` from an ``/acquire/pressure`` response and judge it.

    Only a spectrum that already passed the saturation/fit/R² checks is
    judged; earlier attempts keep ``not_evaluated`` with the raw values.
    """
    record.pressure_gpa = _finite(response.get("pressure_gpa"))
    record.pressure_err_gpa = _finite(response.get("pressure_err_gpa"))
    record.zero_pressure_peak_at_current_t_nm = _finite(response.get("zero_pressure_peak_at_current_t"))
    warning = response.get("temperature_warning")
    record.temperature_warning = warning.strip() if isinstance(warning, str) else ""
    fit = response.get("fit") if isinstance(response.get("fit"), dict) else {}
    details = fit.get("fit") if isinstance(fit.get("fit"), dict) else {}
    peaks = details.get("peaks") if isinstance(details.get("peaks"), list) else []
    index = record.pressure_peak_index - 1
    peak = peaks[index] if 0 <= index < len(peaks) and isinstance(peaks[index], dict) else {}
    record.peak_nm = _finite(peak.get("position"))
    record.peak_err_nm = _finite(peak.get("position_err"))
    if not quality_passed:
        record.status = "not_evaluated"
        return record

    error = _response_error(record, response, peaks)
    if error:
        record.status, record.error = "invalid", error
    elif record.temperature_warning:
        # A number may be returned, but it is outside the correction model's
        # validated range: keep the spectrum, never report it as measured.
        record.status = "temperature_out_of_range"
        record.error = record.temperature_warning
    else:
        record.status, record.error = "valid", ""
    return record


def _response_error(record: PressureRecord, response: dict, peaks: list) -> str:
    x_axis = response.get("x_axis") if isinstance(response.get("x_axis"), dict) else {}
    if x_axis.get("unit") != "nm":
        return f"FRP x axis unit is {x_axis.get('unit')!r}, not nm"
    if record.pressure_gpa is None:
        return "FRP did not return a finite pressure"
    if record.peak_nm is None:
        return f"FRP fit has no finite Peak{record.pressure_peak_index} position"
    try:
        x = [float(v) for v in response.get("x") or []]
    except (TypeError, ValueError):
        x = []
    if x and not min(x) <= record.peak_nm <= max(x):
        return f"Peak{record.pressure_peak_index} ({record.peak_nm:.3f} nm) lies outside the acquired range"
    if len(peaks) >= 2:
        other = _finite(peaks[1].get("position")) if isinstance(peaks[1], dict) else None
        if other is not None and not record.peak_nm > other:
            return "Peak1 is not the longer-wavelength (R1) line of the fitted doublet"
    corrected = record.zero_pressure_peak_at_current_t_nm
    if record.lambda_0_nm is not None:
        if corrected is None:
            if record.temperature_correction_enabled:
                return "FRP did not return the temperature-corrected λ₀"
        elif abs(corrected - record.lambda_0_nm) > MAX_ZERO_PEAK_SHIFT_NM:
            return (f"Corrected λ₀ {corrected:.3f} nm differs from the registered λ₀ "
                    f"{record.lambda_0_nm:.3f} nm by more than {MAX_ZERO_PEAK_SHIFT_NM:g} nm")
    return ""


def simulated_fields(settings, reference: RubyReference,
                     temperature: TemperatureReading | None, peak_nm: float) -> dict:
    """Pressure response fields for --debug runs (rough Mao-type scale)."""
    zero = reference.lambda_0_nm
    warning = None
    if settings.temperature_correction_enabled and temperature is not None and reference.t0_k is not None:
        zero += 0.0068 * (temperature.value_k - reference.t0_k)
        low, high = TEMPERATURE_SCALES.get(settings.temperature_scale, ("", (0.0, 1e9)))[1]
        if not low <= temperature.value_k <= high:
            warning = (f"Temperature {temperature.value_k} K is outside the valid range "
                       f"({low}-{high} K) for ruby / {settings.temperature_scale}.")
    pressure = 1904.0 / 7.665 * ((peak_nm / zero) ** 7.665 - 1.0)
    return {
        "x_axis": {"source": "calibrated", "unit": "nm", "calibrated": True},
        "pressure_gpa": pressure,
        "pressure_err_gpa": 0.01,
        "zero_pressure_peak_at_current_t": zero,
        "temperature_warning": warning,
    }


def summary_lines(record: PressureRecord) -> list[str]:
    """Plot annotation: temperature, scales, correction and the pressure."""
    if record.pressure_gpa is not None:
        err = f" ± {record.pressure_err_gpa:.3f}" if record.pressure_err_gpa is not None else ""
        pressure = f"{record.pressure_gpa:.3f}{err} GPa"
    else:
        pressure = "n/a"
    status = {"valid": "", "not_evaluated": " (not evaluated)",
              "temperature_out_of_range": " (NOT VALID: T out of range)",
              "invalid": " (INVALID)"}.get(record.status, f" ({record.status})")
    lines = [f"P: {pressure}{status}", f"P scale: {record.pressure_scale_label}"]
    if record.measured_temperature_k is not None:
        source = TEMPERATURE_SOURCES.get(record.temperature_source, record.temperature_source)
        when = f" @ {record.measured_temperature_timestamp[11:19]}" if record.measured_temperature_timestamp else ""
        lines.append(f"T: {record.measured_temperature_k:.2f} K ({source}{when})")
    else:
        lines.append("T: n/a")
    lambda_0 = f"{record.lambda_0_nm:.3f} nm" if record.lambda_0_nm is not None else "n/a"
    if record.temperature_correction_enabled:
        t0 = f"{record.t0_k:.2f} K" if record.t0_k is not None else "n/a"
        corrected = (f"{record.zero_pressure_peak_at_current_t_nm:.3f} nm"
                     if record.zero_pressure_peak_at_current_t_nm is not None else "n/a")
        lines.append(f"T corr.: {record.temperature_scale_label}")
        lines.append(f"λ0(T0={t0}) = {lambda_0} → λ0(T) = {corrected}")
    else:
        lines.append("T corr.: off")
        lines.append(f"λ0 = {lambda_0}")
    if record.peak_nm is not None:
        lines.append(f"Peak{record.pressure_peak_index} used: {record.peak_nm:.3f} nm")
    if record.temperature_warning:
        lines.append("Warning: " + record.temperature_warning)
    elif record.error:
        lines.append("Error: " + record.error)
    return lines


def log_note(record: PressureRecord) -> str:
    """``key=value`` pairs appended to the conditions.csv note."""
    def fmt(value, digits):
        return "" if value is None else f"{value:.{digits}f}"
    return "; ".join([
        f"pressure_status={record.status}",
        f"pressure_GPa={fmt(record.pressure_gpa, 4)}",
        f"pressure_err_GPa={fmt(record.pressure_err_gpa, 4)}",
        f"peak_nm={fmt(record.peak_nm, 4)}",
        f"lambda_0_nm={fmt(record.lambda_0_nm, 4)}",
        f"t0_K={fmt(record.t0_k, 3)}",
        f"T_used_K={fmt(record.measured_temperature_k, 3)}",
        f"T_time={record.measured_temperature_timestamp}",
        f"pressure_scale={record.pressure_scale}",
        f"temperature_scale={record.temperature_scale or 'off'}",
        f"lambda_0_at_T_nm={fmt(record.zero_pressure_peak_at_current_t_nm, 4)}",
        f"temperature_warning={record.temperature_warning}",
    ])
