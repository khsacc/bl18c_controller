"""Worker-thread side of Sequential Spectroscopic Measurements (Qt-free).

``SequenceRun.run()`` executes the whole sequence on the calling thread:
FRP and temperature checks, the bounded target check, one move + acquisition
per position, the per-position files and the master files, and finally the
return to Pos1. Progress is reported through plain callbacks so the window
can forward them as Qt signals.
"""
from __future__ import annotations

import csv
import json
import math
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

try:
    from apps.seq_spectro import spectro_core as core
    from apps.exp_scheduler.ruby_pressure import PressureSetupError, read_lakeshore_b
    from apps.exp_scheduler.spectrum_export import save_spectrum_csv, save_spectrum_plot
    from apps.exp_scheduler.spectrum_quality import (
        RETRY_HALVE_EXPOSURE, RETRY_RAISE_EM_GAIN, RetryState, assess_spectrum, next_retry,
    )
    from utils.stage.control_stage import PULSE_SCALE
except ImportError:  # standalone
    import os as _os
    import sys as _sys
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from apps.seq_spectro import spectro_core as core
    from apps.exp_scheduler.ruby_pressure import PressureSetupError, read_lakeshore_b
    from apps.exp_scheduler.spectrum_export import save_spectrum_csv, save_spectrum_plot
    from apps.exp_scheduler.spectrum_quality import (
        RETRY_HALVE_EXPOSURE, RETRY_RAISE_EM_GAIN, RetryState, assess_spectrum, next_retry,
    )
    from utils.stage.control_stage import PULSE_SCALE

OWNER = "Sequential Spectroscopic Measurements"
MASTER_CSV = "master.csv"
SUMMARY_JSON = "summary.json"

MASTER_FIELDS = [
    "pos", "marker", "status", "error", "warning",
    "record_ch3", "record_ch4", "record_ch5",
    "target_ch3", "target_ch4", "target_ch5",
    "offset_ch3_um", "offset_ch4_um", "offset_ch5_um",
    "lambda_0_nm", "t0_K", "temperature_K", "temperature_source", "temperature_time",
    "pressure_scale", "temperature_correction", "temperature_scale",
    "fit_function", "baseline_model", "fit_range_nm",
    "attempts", "exposure_s", "accumulations", "em_gain",
    "peak1_nm", "peak1_err_nm", "peak2_nm", "R2", "lambda_0_at_T_nm",
    "pressure_GPa", "pressure_err_GPa", "spectrum_file",
]


class RunAborted(Exception):
    """Emergency stop (or window close) during the run."""


def temperature_reading(lakeshore, required: bool):
    """Latest LakeShore Ch B reading; raises PressureSetupError only if required."""
    try:
        return read_lakeshore_b(lakeshore, core.TEMPERATURE_MAX_AGE_S)
    except PressureSetupError:
        if required:
            raise
        return None


def _fmt(value, digits=4) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}" if math.isfinite(value) else ""
    return str(value)


class SequenceRun:
    def __init__(self, *, controller, motion, reader, lakeshore_getter: Callable,
                 positions: list[core.Position], settings: dict[str, core.MarkerSettings],
                 output_dir: str, abort_event: threading.Event,
                 on_progress: Callable[[str], None] = lambda _m: None,
                 on_position_started: Callable[[int], None] = lambda _i: None,
                 on_position_done: Callable[[core.PositionResult], None] = lambda _r: None) -> None:
        self.controller = controller
        self.motion = motion
        self.reader = reader
        self.lakeshore_getter = lakeshore_getter
        self.positions = list(positions)
        self.settings = dict(settings)
        self.output_dir = Path(output_dir)
        self.abort_event = abort_event
        self.on_progress = on_progress
        self.on_position_started = on_position_started
        self.on_position_done = on_position_done

        self.origin: dict[int, int] | None = None
        self.targets: list[dict[int, int]] = []
        self.results: list[core.PositionResult] = [
            core.PositionResult(i, p.marker) for i, p in enumerate(self.positions, start=1)
        ]
        self.run_dir: Path | None = None
        self.started_at = ""
        self.finished_at = ""
        self.outcome = ""
        self.message = ""
        self.current_em_gain: int | None = None
        # EM gain found at the start of the run (base for markers without "Set").
        self.initial_em_gain: int | None = None
        # (minimum, maximum, increment) when the gain can be changed, else None.
        self.em_gain_range: tuple[int, int, int] | None = None
        # Last master-file write failure (empty when every write succeeded).
        self.save_error = ""

    # ── Main entry ──────────────────────────────────────────────────────────

    def run(self) -> str:
        """Run every position; returns ``completed`` / ``aborted`` / ``error``.

        On ``completed`` the stage is already back on Pos1. On the other
        outcomes it is left where it stopped and the caller decides.
        """
        self.started_at = datetime.now().isoformat(timespec="seconds")
        try:
            self._prepare()
            for index, pos in enumerate(self.positions):
                self._check_abort()
                self._measure(index, pos)
            self._check_abort()
            self.on_progress("All positions measured. Returning to Pos1…")
            self.return_to_origin()
            self.outcome, self.message = "completed", "Sequence completed; returned to Pos1."
        except RunAborted:
            self.outcome, self.message = "aborted", "Emergency stop — sequence halted."
        except Exception as exc:  # noqa: BLE001 — reported to the operator
            if self.abort_event.is_set():
                self.outcome, self.message = "aborted", "Emergency stop — sequence halted."
            else:
                self.outcome, self.message = "error", str(exc)
        finally:
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            self._safe_write_master_files()
        return self.outcome

    def _safe_write_master_files(self) -> None:
        """Write master.csv / summary.json; a failure is recorded, never raised.

        A full disk or a vanished network share must not leave the window
        believing the run is still going.
        """
        try:
            self._write_master_files()
        except Exception as exc:  # noqa: BLE001
            self.save_error = f"Could not write the master files in {self.run_dir}: {exc}"
            self.on_progress(self.save_error)

    def _check_abort(self) -> None:
        if self.abort_event.is_set():
            raise RunAborted()

    # ── Preparation ─────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        errors = core.setup_errors(self.positions, self.settings, str(self.output_dir))
        if errors:
            raise core.SetupError("; ".join(errors))

        self.on_progress("Checking FluoRaPressée…")
        self.reader.prepare()
        core.check_frp_status(self.reader.last_status or {})
        self._check_em_gain()

        if core.needs_temperature(self.positions, self.settings):
            temperature_reading(self.lakeshore_getter(), required=True)

        current = {}
        for ch in core.CHANNELS:
            value = self.controller.get_ch_pos(ch)
            if value is None:
                raise RuntimeError(f"Could not read the current position of Ch{ch}")
            current[ch] = int(value)
        self.origin = current
        self.targets = core.targets(self.positions, current)
        for result, target in zip(self.results, self.targets):
            result.target = dict(target)
        self._check_targets()

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = self.output_dir / stamp
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = self.output_dir / f"{stamp}_{suffix}"
        run_dir.mkdir(parents=True)
        self.run_dir = run_dir
        self.on_progress(f"Saving to {run_dir}")

    def _check_em_gain(self) -> None:
        """Read FRP's EM gain range and check the requested gains before any motion."""
        used = core.markers_in_use(self.positions)
        required = any(self.settings[m].em_gain_enabled for m in used)
        try:
            info = self.reader.get_em_gain_info()
        except Exception:
            if required:
                raise
            # Optimisation then falls back to accumulations only.
            self.on_progress("EM gain could not be read; retries use accumulations only")
            return
        errors = core.em_gain_errors(info, self.positions, self.settings)
        if errors:
            raise core.SetupError("; ".join(errors))
        gain = info.get("gain") if type(info.get("gain")) is int else None
        self.current_em_gain = self.initial_em_gain = gain
        limits = tuple(info.get(k) for k in ("minimum", "maximum", "increment"))
        if (info.get("exists") is True and info.get("available") is True and gain is not None
                and all(type(v) is int for v in limits) and limits[2] >= 1):
            self.em_gain_range = limits
        will_change = required or (self.em_gain_range is not None
                                   and any(self.settings[m].optimise for m in used))
        if will_change:
            # The camera read can refresh FRP's instrument state: take the
            # token that set_em_gain() must present after it.
            self.reader.prepare()
            token = (self.reader.last_status or {}).get("instrument_state_token")
            if not isinstance(token, str) or not token.strip():
                raise core.SetupError("FluoRaPressée did not provide an instrument-state token, "
                                      "which is needed to change the EM gain")

    def _check_targets(self) -> None:
        """Check every target (and every hop between them) before the first move."""
        path = [self.origin, *self.targets, self.origin]
        for previous, target in zip(path, path[1:]):
            for ch in core.CHANNELS:
                ok, message = self.controller.check_move_constraints(ch, target[ch])
                if ok:
                    ok, message = self.controller.check_soft_limits(ch, target[ch])
                if ok and hasattr(self.controller, "check_max_move"):
                    ok, message = self.controller.check_max_move(ch, target[ch] - previous[ch])
                if not ok:
                    raise core.SetupError(message)

    # ── Motion ──────────────────────────────────────────────────────────────

    def move_to(self, target: dict[int, int]) -> None:
        for ch in core.MOVE_ORDER:
            self._check_abort()
            if int(self.controller.get_ch_pos(ch)) == target[ch]:
                continue
            self.controller.move_ch_absolute(ch, target[ch], motion=self.motion)
            self.controller.wait_until_stop(motion=self.motion,
                                            should_stop=self.abort_event.is_set)
            self._check_abort()

    def return_to_origin(self) -> None:
        if self.origin is None:
            return
        self.move_to(self.origin)

    # ── One position ────────────────────────────────────────────────────────

    def _measure(self, index: int, pos: core.Position) -> None:
        number = index + 1
        settings = self.settings[pos.marker]
        spec = core.MARKERS[pos.marker]
        result = self.results[index]
        self.on_position_started(number)
        self.on_progress(f"Pos{number}/{len(self.positions)}: moving…")
        self.move_to(self.targets[index])

        prefix = f"Pos{number}/{len(self.positions)}"
        # Every position starts from its marker's own conditions.
        if settings.em_gain_enabled:
            base_gain = settings.em_gain
        else:
            base_gain = self.initial_em_gain if self.em_gain_range is not None else None
        state = RetryState(settings.exposure_time_s, settings.accumulations, base_gain)
        while True:
            self._check_abort()
            result.attempts += 1
            self._apply_em_gain(state.em_gain, prefix)
            self.on_progress(
                f"{prefix}: acquiring {spec.label} spectrum (attempt {result.attempts}, "
                f"{state.exposure_s:.3g} s × {state.accumulations}, "
                f"EM gain {self.current_em_gain if self.current_em_gain is not None else 'n/a'})…")
            response = self._acquire(pos, settings, spec, result, state)
            self._check_abort()
            quality = assess_spectrum(response)
            passed = core.quality_passed(quality, settings)
            if passed or not settings.optimise:
                break
            ceiling = None
            if self.em_gain_range is not None and state.em_gain is not None:
                ceiling = min(settings.em_gain_max, self.em_gain_range[1])
            decision = next_retry(
                state, saturated=quality.saturated, max_accumulations=settings.max_accumulations,
                **({"em_gain_ceiling": ceiling, "em_gain_minimum": self.em_gain_range[0],
                    "em_gain_increment": self.em_gain_range[2]} if ceiling is not None else {}),
            )
            if decision.gave_up:
                self.on_progress(f"{prefix}: R² criterion not met after {result.attempts} attempts")
                break
            what = {RETRY_HALVE_EXPOSURE: "saturated — halving the exposure",
                    RETRY_RAISE_EM_GAIN: f"R² low — raising EM gain to {decision.state.em_gain}"
                    }.get(decision.kind, "R² low — adding an accumulation")
            self.on_progress(f"{prefix}: {what}")
            state = decision.state

        if passed:
            try:
                result.spectrum_file = self._save_spectrum(number, pos, settings, result, response)
                result.saved = True
            except Exception as exc:  # noqa: BLE001 — the measurement itself stands
                result.warning = ((result.warning + "; ") if result.warning else "") \
                    + f"Save failed: {exc}"
        self.on_position_done(result)
        self._safe_write_master_files()

    def _apply_em_gain(self, gain: int | None, prefix: str) -> None:
        if gain is None or gain == self.current_em_gain:
            return
        self.on_progress(f"{prefix}: setting EM gain {gain}…")
        self.current_em_gain = None          # unknown if the PUT fails part-way
        self.current_em_gain = self.reader.set_em_gain(gain)

    def _acquire(self, pos: core.Position, settings: core.MarkerSettings,
                 spec: core.MarkerSpec, result: core.PositionResult, state: RetryState) -> dict:
        """One acquisition; ``result`` is refilled from scratch for this attempt."""
        for name in ("status", "error", "warning", "temperature_k", "temperature_source",
                     "temperature_timestamp", "peak_nm", "peak_err_nm", "peak2_nm", "r_squared",
                     "lambda_0_at_t_nm", "pressure_gpa", "pressure_err_gpa"):
            setattr(result, name, getattr(core.PositionResult(0, ""), name))
        # Measured just before each acquisition, never a setpoint.
        reading = temperature_reading(self.lakeshore_getter(), settings.temperature_correction)
        if reading is not None:
            result.temperature_k = reading.value_k
            result.temperature_source = reading.source
            result.temperature_timestamp = reading.timestamp_iso()
        request = core.build_request(pos, settings, result.temperature_k)
        result.em_gain = self.current_em_gain
        result.exposure_s = state.exposure_s
        result.accumulations = state.accumulations

        self.reader.exposure_time_s = state.exposure_s
        self.reader.accumulations = state.accumulations
        self.reader.timeout_s = max(120.0, state.exposure_s * state.accumulations + 45.0)
        response = self.reader.acquire_spectrum_with_pressure(
            request,
            fit_function=settings.fit_function,
            fit_peak_count=spec.fit_peak_count,
            peak_sort_order="x_desc",
            baseline_model=settings.baseline_model,
            fit_range=settings.fit_range(),
        )
        core.evaluate(result, pos, settings, response)
        return response

    # ── Files ───────────────────────────────────────────────────────────────

    def _offset_um(self, index: int) -> dict[int, float] | None:
        offset = core.offsets(self.positions)[index]
        if offset is None:
            return None
        return {ch: offset[ch] * PULSE_SCALE[ch] for ch in core.CHANNELS}

    def _row(self, index: int) -> dict:
        pos, result = self.positions[index], self.results[index]
        s = self.settings[pos.marker]
        offset = self._offset_um(index) or {}
        record = pos.record or {}
        target = result.target or {}
        fit_range = s.fit_range()
        return {
            "pos": index + 1,
            "marker": core.MARKERS[pos.marker].label,
            "status": result.status,
            "error": result.error,
            "warning": result.warning,
            **{f"record_ch{ch}": record.get(ch, "") for ch in core.CHANNELS},
            **{f"target_ch{ch}": target.get(ch, "") for ch in core.CHANNELS},
            **{f"offset_ch{ch}_um": _fmt(offset.get(ch), 2) for ch in core.CHANNELS},
            "lambda_0_nm": _fmt(pos.lambda_0_nm),
            "t0_K": _fmt(pos.t0_k, 2),
            "temperature_K": _fmt(result.temperature_k, 3),
            "temperature_source": result.temperature_source,
            "temperature_time": result.temperature_timestamp,
            "pressure_scale": s.pressure_scale,
            "temperature_correction": "on" if s.temperature_correction else "off",
            "temperature_scale": s.temperature_scale if s.temperature_correction else "",
            "fit_function": s.fit_function,
            "baseline_model": s.baseline_model,
            "fit_range_nm": f"{fit_range[0]:g}-{fit_range[1]:g}" if fit_range else "full",
            "attempts": result.attempts or "",
            "exposure_s": "" if result.exposure_s is None else f"{result.exposure_s:g}",
            "accumulations": "" if result.accumulations is None else result.accumulations,
            "em_gain": "" if result.em_gain is None else result.em_gain,
            "peak1_nm": _fmt(result.peak_nm),
            "peak1_err_nm": _fmt(result.peak_err_nm),
            "peak2_nm": _fmt(result.peak2_nm),
            "R2": _fmt(result.r_squared, 5),
            "lambda_0_at_T_nm": _fmt(result.lambda_0_at_t_nm),
            "pressure_GPa": _fmt(result.pressure_gpa),
            "pressure_err_GPa": _fmt(result.pressure_err_gpa),
            "spectrum_file": result.spectrum_file,
        }

    def averages(self) -> dict:
        """Overall and per-marker means (per-marker only when markers are mixed)."""
        overall = core.mean_pressure(self.results)
        per_marker = {}
        used = core.markers_in_use(self.positions)
        if len(used) > 1:
            for marker in used:
                per_marker[marker] = core.mean_pressure(
                    [r for r in self.results if r.marker == marker])
        return {"overall": overall, "per_marker": per_marker}

    def _write_master_files(self) -> None:
        if self.run_dir is None:
            return
        with open(self.run_dir / MASTER_CSV, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=MASTER_FIELDS)
            writer.writeheader()
            for index in range(len(self.positions)):
                writer.writerow(self._row(index))

        def mean_dict(value):
            return None if value is None else asdict(value)

        averages = self.averages()
        summary = {
            "app": OWNER,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome or "running",
            "message": self.message,
            "pos1_stage_position": ({f"ch{ch}": v for ch, v in self.origin.items()}
                                    if self.origin else None),
            "mean_pressure_method": "arithmetic mean of valid points; "
                                    "error = sqrt(sum(err_i^2)) / n",
            "mean_pressure": mean_dict(averages["overall"]),
            "mean_pressure_per_marker": {
                core.MARKERS[m].label: mean_dict(v) for m, v in averages["per_marker"].items()
            },
            "marker_settings": {
                core.MARKERS[m].label: asdict(self.settings[m])
                for m in core.markers_in_use(self.positions)
            },
            "positions": [p.to_dict() for p in self.positions],
            "results": [self._row(i) for i in range(len(self.positions))],
        }
        with open(self.run_dir / SUMMARY_JSON, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False)

    def _save_spectrum(self, number: int, pos: core.Position, settings: core.MarkerSettings,
                       result: core.PositionResult, response: dict) -> str:
        if self.run_dir is None:
            return ""
        spec = core.MARKERS[pos.marker]
        stem = f"pos{number:02d}_{pos.marker}_{datetime.now().strftime('%H%M%S')}"
        row = self._row(number - 1)
        metadata = {k: row[k] for k in (
            "pos", "marker", "lambda_0_nm", "t0_K", "temperature_K", "temperature_source",
            "pressure_scale", "temperature_correction", "temperature_scale", "fit_function",
            "baseline_model", "fit_range_nm", "attempts", "exposure_s", "accumulations", "em_gain",
            "R2",
            "pressure_GPa", "pressure_err_GPa", "status")}

        fit = response.get("fit") if isinstance(response.get("fit"), dict) else {}
        np.savez_compressed(
            self.run_dir / f"{stem}.npz",
            x=np.asarray(response.get("x") or [], dtype=float),
            y=np.asarray(response.get("y") or [], dtype=float),
            y_raw=np.asarray(response.get("y_raw") or [], dtype=float),
            x_fit=np.asarray(fit.get("x_fit") or [], dtype=float),
            y_fit=np.asarray(fit.get("y_fit") or [], dtype=float),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            response_json=json.dumps(response, ensure_ascii=False, default=str),
        )
        save_spectrum_csv(self.run_dir / f"{stem}.csv", response, metadata)

        pressure = (f"{result.pressure_gpa:.3f}" if result.pressure_gpa is not None else "n/a")
        if result.pressure_err_gpa is not None:
            pressure += f" ± {result.pressure_err_gpa:.3f}"
        footer = [
            f"P: {pressure} GPa ({core.STATUS_LABELS.get(result.status, result.status)})",
            f"P scale: {spec.pressure_scales.get(settings.pressure_scale, settings.pressure_scale)}",
            f"λ0 = {pos.lambda_0_nm:.3f} nm",
        ]
        if settings.temperature_correction:
            corrected = (f"{result.lambda_0_at_t_nm:.3f} nm"
                         if result.lambda_0_at_t_nm is not None else "n/a")
            footer.append(f"T corr.: {spec.temperature_scales.get(settings.temperature_scale)}"
                          f"; T0 = {pos.t0_k:.2f} K → λ0(T) = {corrected}")
        else:
            footer.append("T corr.: off")
        if result.temperature_k is not None:
            footer.append(f"T: {result.temperature_k:.2f} K")
        if result.error:
            footer.append("Error: " + result.error)
        if result.warning:
            footer.append("Warning: " + result.warning)
        r2 = f"R² = {result.r_squared:.4f}" if result.r_squared is not None else "R² = n/a"
        save_spectrum_plot(self.run_dir / f"{stem}.png", response,
                           f"Pos{number} — {spec.label}", [r2], footer)
        return f"{stem}.npz"


# ── --debug simulation ───────────────────────────────────────────────────────

class SimSpectroReader:
    """Fabricates FRP ``/acquire/pressure`` responses for --debug runs.

    Pressures are synthetic (about 5 GPa with a small spread); the
    calculation uses rough Mao-type (ruby) and linear (Sm) scales.
    """

    def __init__(self) -> None:
        self.exposure_time_s: float | None = None
        self.accumulations: int | None = None
        self.timeout_s = 120.0
        self.last_status: dict | None = None
        self.em_gain = 1
        # Optional per-call overrides for tests, e.g. [{"R2": 0.5}, {"saturated": True}].
        self.script: list[dict] = []
        self._count = 0
        self._rng = np.random.default_rng()

    def get_status(self) -> dict:
        return {
            "busy": False, "camera_connected": True,
            "roi": {"mode": "1d_roi"},
            "calibration": {"applied": True, "unit": "nm"},
            "configuration": {"axis_mode": "calibrated", "unit": "nm"},
        }

    def prepare(self) -> None:
        self.last_status = self.get_status()
        self.last_status["instrument_state_token"] = "sim:1"

    def get_em_gain_info(self) -> dict:
        return {"exists": True, "available": True, "gain": self.em_gain,
                "minimum": 0, "maximum": 300, "increment": 1}

    def set_em_gain(self, gain: int) -> int:
        self.em_gain = int(gain)
        return self.em_gain

    def acquire_spectrum_with_pressure(self, pressure: dict, *, fit_function: str,
                                       fit_peak_count: int, peak_sort_order: str,
                                       baseline_model: str,
                                       fit_range: tuple[float, float] | None = None) -> dict:
        time.sleep(min(2.0, (self.exposure_time_s or 0.1) * (self.accumulations or 1)))
        self._count += 1
        override = self.script.pop(0) if self.script else {}
        p_true = 5.0 + 0.15 * self._count + float(self._rng.normal(0.0, 0.05))
        sensor = pressure["sensor"]
        lam0 = float(pressure["zero_pressure_peak"])
        correction = pressure.get("temperature_correction")
        lam0_t = None
        warning = None
        if correction:
            dt = float(correction["current_t"]) - float(correction["t0"])
            lam0_t = lam0 + (0.0068 if sensor == core.MARKER_RUBY else -0.0001) * dt
            if sensor == core.MARKER_SM and float(correction["current_t"]) < 296.0:
                warning = "Temperature is outside the valid range of the Sm2+:SrB4O7 scale."
        zero = lam0_t if lam0_t is not None else lam0
        if sensor == core.MARKER_RUBY:
            peak1 = zero * (1.0 + 7.665 * p_true / 1904.0) ** (1.0 / 7.665)
            centres = [(peak1, 1.0), (peak1 - 1.5, 0.6)]
        else:
            peak1 = zero + 0.255 * p_true
            centres = [(peak1, 1.0)]

        x = np.linspace(675.0, 715.0, 1024)
        y = np.full_like(x, 200.0)
        for centre, amp in centres:
            y += 5000.0 * amp / (1.0 + ((x - centre) / 0.4) ** 2)
        y += self._rng.normal(0.0, 20.0, x.size)
        mask = np.ones_like(x, dtype=bool) if fit_range is None else \
            (x >= fit_range[0]) & (x <= fit_range[1])
        x_fit = x[mask]
        y_fit = np.full_like(x_fit, 200.0)
        for centre, amp in centres:
            y_fit += 5000.0 * amp / (1.0 + ((x_fit - centre) / 0.4) ** 2)
        peaks = [{"index": i, "position": c, "position_err": 0.002}
                 for i, (c, _a) in enumerate(centres[:fit_peak_count], start=1)]
        return {
            "mode": "1d",
            "x": x.tolist(), "y": y.tolist(), "y_raw": y.tolist(),
            "x_axis": {"source": "calibrated", "unit": "nm", "calibrated": True},
            "fit": {"success": True, "x_fit": x_fit.tolist(), "y_fit": y_fit.tolist(),
                    "fit": {"peaks": peaks, "R2": float(override.get("R2", 0.995)),
                            **{f"Peak{p['index']}": p["position"] for p in peaks},
                            **{f"Peak{p['index']}_Err": p["position_err"] for p in peaks}}},
            "saturated": bool(override.get("saturated", False)),
            "pressure_gpa": p_true,
            "pressure_err_gpa": 0.02 if sensor == core.MARKER_RUBY else 0.03,
            "zero_pressure_peak_at_current_t": lam0_t,
            "temperature_warning": warning,
        }
