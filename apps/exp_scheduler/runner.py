"""
SequenceRunner — executes a Sequence in a QThread.

All device calls happen in this thread (safe: each backend has its own Lock).
The main thread receives only Qt signals; it never touches device APIs.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    from scipy.optimize import curve_fit as _scipy_curve_fit
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


def _gaussian(x, a, mu, sigma, offset):
    return a * np.exp(-0.5 * ((x - mu) / sigma) ** 2) + offset


def _af_find_best_pos(sharpness_data: list, peak_method: str) -> int:
    """Pick the best focus position from [(pos, sharpness), ...].

    Uses Gaussian fitting when peak_method='gaussian' and scipy is available;
    falls back to the highest-sharpness position otherwise.
    """
    positions = np.array([p for p, _ in sharpness_data])
    sharpnesses = np.array([s for _, s in sharpness_data])
    idx_max = int(np.argmax(sharpnesses))
    fallback = int(positions[idx_max])

    if peak_method != "gaussian" or not _SCIPY_AVAILABLE or len(sharpness_data) < 4:
        return fallback

    try:
        a0 = float(sharpnesses[idx_max] - np.min(sharpnesses))
        mu0 = float(positions[idx_max])
        sigma0 = float((positions[-1] - positions[0]) / 4) or 1.0
        offset0 = float(np.min(sharpnesses))
        scan_span = float(positions[-1] - positions[0]) or 1.0

        popt, _ = _scipy_curve_fit(
            _gaussian, positions, sharpnesses,
            p0=[a0, mu0, sigma0, offset0],
            maxfev=10000,
        )
        a, mu, sigma, _ = popt
        if not (positions[0] <= mu <= positions[-1]):
            return fallback
        if a <= 0 or abs(sigma) < 1 or abs(sigma) > scan_span:
            return fallback
        return int(round(mu))
    except Exception:
        return fallback

from .actions import (
    Action, WaitAction, LogAction,
    StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction,
    SetPressureAction, WaitPressureAction, SetControlModeAction,
    SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction,
    TakeXrdAction, TakeDarkAction,
    SaveReferenceImageAction, StartFollowingAction, StopFollowingAction, FollowSampleAction,
    ForLoopAction,
)
from .device_context import DeviceContext
from .log_manager import RunLogger
from .sequence import Sequence

from apps.PACE5000.pace5000_backend import PRESSURE_UNIT_TO_MPA, RATE_UNIT_TO_MPA_PER_MIN


# µm per pulse for stage channels (from utils.stage.control_stage.PULSE_SCALE)
# Ch3: Focus Z=2µm/pulse  Ch4: Sample X=2µm/pulse  Ch5: Sample Y=0.11µm/pulse
_UM_PER_PULSE: dict[int, float] = {3: 2.0, 4: 2.0, 5: 0.11}

# Ch11 rotation stage: 0.004 deg/pulse (from utils.stage.control_stage.PULSE_SCALE)
_DEG_PER_PULSE_CH11 = 0.004

_PRESETS_PATH = Path(__file__).parent / "__localdata" / "scheduler_presets.json"
_CALIBRATION_PATH = (
    Path(__file__).parent.parent / "interactive_camera" / "calibration.json"
)
_DEFAULT_REF_PATH = Path(__file__).parent / "__localdata" / "reference_frame.npz"

_DEFAULT_PRESETS = {
    "follow_sample": {
        "interval_s": 300,
        "similarity_threshold": 0.95,
        "max_correction_per_step_um": 500,
    }
}


def _load_presets() -> dict:
    if _PRESETS_PATH.exists():
        return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))
    return _DEFAULT_PRESETS


class _StopRequested(Exception):
    """Internal sentinel: clean stop propagation up the call stack."""


from dataclasses import dataclass as _dc

_DEFAULT_XRD_SAVE_DIR = Path(__file__).parent / "__localdata" / "xrd"


@_dc
class GlobalXrdSettings:
    """Global defaults for TakeXrdAction.  Per-step overrides (non-None fields in
    TakeXrdAction) take precedence over these values."""
    exposure_ms: int = 1000
    save_dir: str | None = None          # None → __localdata/xrd/<run-timestamp>/
    dark_file: str | None = None
    dark_enabled: bool = False
    defect_file: str | None = None
    defect_enabled: bool = True
    defect_kernel: int = 3               # 3 / 4 / 5 / 6
    flip_v: bool = True
    flip_h: bool = False
    # Ch11 oscillation during exposure
    oscillate: bool = False
    osc_pos_a_deg: float = -5.0
    osc_pos_b_deg: float = 20.0
    osc_dwell_ms: int = 0
    osc_speed: str = "M"


@_dc
class _EffectiveXrd:
    """Resolved XRD settings after merging per-step overrides with GlobalXrdSettings."""
    exposure_ms: int
    save_dir: Path
    dark_file: str | None
    dark_enabled: bool
    defect_file: str | None
    defect_enabled: bool
    defect_kernel: int
    flip_v: bool
    flip_h: bool
    oscillate: bool
    osc_pos_a_deg: float
    osc_pos_b_deg: float
    osc_dwell_ms: int
    osc_speed: str


@_dc
class GlobalLimits:
    """Allowed travel (mm) from each channel's position at sequence-start.

    None means not configured — PreValidator blocks Run in that case.
    0.0 means that channel/direction is locked (no movement allowed).
    Positive value is the allowed displacement in mm.
    """
    ch3_minus_mm: float | None = None
    ch3_plus_mm:  float | None = None
    ch4_minus_mm: float | None = None
    ch4_plus_mm:  float | None = None
    ch5_minus_mm: float | None = None
    ch5_plus_mm:  float | None = None

    def is_fully_configured(self) -> bool:
        return all(v is not None for v in (
            self.ch3_minus_mm, self.ch3_plus_mm,
            self.ch4_minus_mm, self.ch4_plus_mm,
            self.ch5_minus_mm, self.ch5_plus_mm,
        ))


@_dc
class GlobalFollowSettings:
    """Global defaults for follow-sample actions.

    Per-step overrides in StartFollowingAction / FollowSampleAction take
    precedence for fields that are also present in the action (interval_s,
    similarity_threshold, max_correction_per_step_um, autofocus_enabled,
    autofocus_range_um, autofocus_steps).  The fields below that have no
    action-level counterpart are always taken from this object.
    """
    interval_s: float = 300.0
    similarity_threshold: float = 0.95
    max_correction_ch4_um: float = 400.0
    max_correction_ch5_um: float = 400.0
    xy_max_retries: int = 3
    autofocus_enabled: bool = False
    autofocus_range_um: float = 20.0
    autofocus_steps: int = 10
    autofocus_method: str = "laplacian"
    autofocus_n_frames: int = 1
    autofocus_speed: str = "H"
    autofocus_peak_method: str = "highest"


class SequenceRunner(QThread):
    step_started      = pyqtSignal(int, str)   # (flat_index, description)
    step_completed    = pyqtSignal(int)         # (flat_index)
    progress_updated  = pyqtSignal(str)         # informational / polling message
    sequence_completed = pyqtSignal()
    sequence_stopped   = pyqtSignal()
    error_occurred    = pyqtSignal(int, str)    # (flat_index, error_message)

    def __init__(
        self,
        sequence: Sequence,
        ctx: DeviceContext,
        global_limits: GlobalLimits | None = None,
        global_xrd: GlobalXrdSettings | None = None,
        global_follow: GlobalFollowSettings | None = None,
        log_path: str = "run",
        log_devices: list[str] | None = None,
        log_dir: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._sequence = sequence
        self._ctx = ctx
        self._global_limits = global_limits
        self._global_xrd = global_xrd or GlobalXrdSettings()
        self._global_follow = global_follow or GlobalFollowSettings()
        self._log_path = log_path
        self._log_devices = list(log_devices or [])
        self._log_dir = log_dir or None
        self._stop_event = threading.Event()

        # Baseline positions captured at run() start (pulses)
        self._baseline_pos: dict[int, int] = {}

        # Background follow thread
        self._follow_thread: threading.Thread | None = None
        self._follow_stop_event = threading.Event()
        self._current_follow_action: StartFollowingAction | None = None

        self._flat_index = 0   # monotonically-increasing execution counter
        self._current_step_idx = 0  # index of the action currently executing
        self._had_error = False
        self._run_timestamp: str = ""

        # Per-run caches: keyed by file path
        self._xrd_dark_cache: dict[str, np.ndarray] = {}
        self._xrd_defect_cache: dict[str, np.ndarray] = {}

        # File logger — auto-started in run() and stopped in the finally block.
        self._logger = RunLogger(ctx)

    # ------------------------------------------------------------------ public

    def request_stop(self) -> None:
        """Thread-safe: may be called from the main thread.

        Sends a decelerate-stop (ASSTP) to the stage unconditionally — the
        stage may be mid-move when Stop is pressed, and the execution thread
        only checks the stop flag at its next poll, so waiting for that poll
        would leave the stage moving in the meantime.
        """
        self._send_stage_stop(emergency=False)
        self._stop_event.set()
        self._follow_stop_event.set()

    def request_emergency_stop(self) -> None:
        """Thread-safe: may be called from the main thread.

        Sends an emergency-stop (AESTP) to the stage unconditionally, then
        ends the sequence the same way request_stop() does.
        """
        self._send_stage_stop(emergency=True)
        self._stop_event.set()
        self._follow_stop_event.set()

    def _send_stage_stop(self, emergency: bool) -> None:
        ctrl = self._ctx.controller
        if ctrl is None:
            return
        try:
            if emergency:
                self._logger.log_ops("[SEQ:ESTOP] emergency_stop() AESTP (emergency stop requested)")
                ctrl.emergency_stop()
            else:
                self._logger.log_ops("[SEQ:STOP] normal_stop() ASSTP (stop requested)")
                ctrl.normal_stop()
        except Exception:
            pass

    # ------------------------------------------------------------------ QThread

    def run(self) -> None:
        self._flat_index = 0
        self._current_step_idx = 0
        self._had_error = False
        self._run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._xrd_dark_cache.clear()
        self._xrd_defect_cache.clear()

        # Auto-start file logging for the full sequence run.
        self._logger.start(
            path=self._log_path,
            devices=self._log_devices,
            sequence_dict=self._sequence.to_dict(),
            global_limits_dict=_global_limits_to_dict(self._global_limits),
            log_base_dir=self._log_dir,
        )

        # Record baseline positions for Ch3/4/5 global-limit tracking
        ctrl = self._ctx.controller
        if ctrl is not None and self._global_limits is not None:
            for ch in (3, 4, 5):
                try:
                    self._baseline_pos[ch] = ctrl.get_ch_pos(ch)
                except Exception:
                    pass

        try:
            self._execute_actions(self._sequence.actions, var_context={})
        except _StopRequested:
            pass
        except Exception as exc:
            self._had_error = True
            self._logger.log_ops(f"[SEQ:ABORT] Unhandled error: {exc}")
            self._logger.log_science("error", note=str(exc))
            self.error_occurred.emit(self._flat_index, str(exc))
        finally:
            self._cleanup_follow_thread()
            # Write final outcome row to conditions.csv before closing files.
            if self._had_error:
                self._logger.log_ops("[SEQ:ABORT] Sequence aborted due to error")
            elif self._stop_event.is_set():
                self._logger.log_science("stop", note="Stopped by user")
                self._logger.log_ops("[SEQ:STOP] Stopped by user request")
            else:
                self._logger.log_science("stop", note="Completed successfully")
                self._logger.log_ops("[SEQ:DONE] Sequence completed successfully")
            self._logger.stop()

        if self._had_error:
            return
        if self._stop_event.is_set():
            self.sequence_stopped.emit()
        else:
            self.sequence_completed.emit()

    # ------------------------------------------------------------------ execution loop

    def _execute_actions(self, actions: list, var_context: dict) -> None:
        for action in actions:
            self._check_stop()

            if isinstance(action, ForLoopAction):
                for i, val in enumerate(action.values):
                    self._check_stop()
                    self._logger.log_ops(
                        f"[LOOP] {action.var} = {val!r}  "
                        f"(iteration {i + 1}/{len(action.values)})"
                    )
                    ctx = {**var_context, action.var: val}
                    self._execute_actions(action.body, ctx)
            else:
                idx = self._flat_index
                self._flat_index += 1
                self._current_step_idx = idx
                self.step_started.emit(idx, action.describe())
                self._logger.log_ops(f"[STEP #{idx:04d} START] {action.describe()}")
                t0 = time.monotonic()
                try:
                    self._execute_one(action, var_context)
                except _StopRequested:
                    raise
                except Exception as exc:
                    elapsed = time.monotonic() - t0
                    self._had_error = True
                    self._logger.log_ops(f"[STEP #{idx:04d} ERROR] {exc}  ({elapsed:.2f} s)")
                    self._logger.log_science("error", step_index=idx, note=str(exc))
                    self.error_occurred.emit(idx, str(exc))
                    raise _StopRequested()
                elapsed = time.monotonic() - t0
                self._logger.log_ops(f"[STEP #{idx:04d} DONE ] {elapsed:.2f} s")
                self.step_completed.emit(idx)

    # ------------------------------------------------------------------ dispatcher

    def _execute_one(self, action: Action, var_context: dict) -> None:  # noqa: C901
        idx = self._current_step_idx

        # ── General ────────────────────────────────────────────────
        if isinstance(action, WaitAction):
            self._do_wait(action.duration_s)

        elif isinstance(action, LogAction):
            self.progress_updated.emit(f"[LOG] {action.message}")
            self._logger.log_science("user_log", step_index=idx, note=action.message)

        # ── Stage ──────────────────────────────────────────────────
        elif isinstance(action, StageAction):
            self._do_stage(action, var_context)

        elif isinstance(action, MicroscopeOutFpdInAction):
            if self._follow_thread is not None and self._follow_thread.is_alive():
                raise RuntimeError(
                    "microscope_out_and_fpd_in: バックグラウンド追従スレッドが停止していません。"
                    " stop_following() を先に実行してください。"
                )
            stage_settings = self._load_stage_settings()
            steps = action.to_steps(stage_settings)
            self._logger.log_ops(
                f"[STAGE] microscope_out_and_fpd_in → {len(steps)} stage steps"
            )
            for step in steps:
                self._check_stop()
                self._do_stage(step, var_context)

        elif isinstance(action, FpdOutMicroscopeInAction):
            stage_settings = self._load_stage_settings()
            steps = action.to_steps(stage_settings)
            self._logger.log_ops(
                f"[STAGE] fpd_out_and_microscope_in → {len(steps)} stage steps"
            )
            for step in steps:
                self._check_stop()
                self._do_stage(step, var_context)

        # ── PACE5000 ───────────────────────────────────────────────
        elif isinstance(action, SetPressureAction):
            self._do_set_pressure(action, var_context)

        elif isinstance(action, WaitPressureAction):
            self._do_wait_pressure(action, idx)

        elif isinstance(action, SetControlModeAction):
            self._logger.log_ops(
                f"[PACE5000] set_control_mode(enabled={action.enabled})"
            )
            self._ctx.pace5000.set_control_mode(action.enabled)

        # ── LakeShore ──────────────────────────────────────────────
        elif isinstance(action, SetTemperatureAction):
            self._do_set_temperature(action, var_context)

        elif isinstance(action, WaitTemperatureAction):
            self._do_wait_temperature(action, idx)

        elif isinstance(action, SetHeaterAction):
            self._logger.log_ops(
                f"[LAKESHORE] set_heater_range({action.range_index})"
            )
            self._ctx.lakeshore.set_heater_range(action.range_index)

        elif isinstance(action, AllHeatersOffAction):
            self._logger.log_ops("[LAKESHORE] all_off()")
            self._ctx.lakeshore.all_off()

        # ── Radicon ────────────────────────────────────────────────
        elif isinstance(action, TakeXrdAction):
            self._do_take_xrd(action, idx)

        elif isinstance(action, TakeDarkAction):
            self._do_take_dark(action)

        # ── Camera ─────────────────────────────────────────────────
        elif isinstance(action, SaveReferenceImageAction):
            self._do_save_reference(action)

        elif isinstance(action, StartFollowingAction):
            self._logger.log_ops(
                f"[CAMERA] start_following(interval={action.interval_s}s, "
                f"threshold={action.similarity_threshold}, "
                f"max_corr={action.max_correction_per_step_um}µm)"
            )
            self._start_follow(action)

        elif isinstance(action, StopFollowingAction):
            self._logger.log_ops("[CAMERA] stop_following()")
            self._stop_follow()

        elif isinstance(action, FollowSampleAction):
            start_act = StartFollowingAction(
                reference_path=action.reference_path,
                interval_s=action.interval_s,
                similarity_threshold=action.similarity_threshold,
                max_correction_per_step_um=action.max_correction_per_step_um,
                camera_index=action.camera_index,
            )
            self._logger.log_ops(
                f"[CAMERA] follow_sample_position(duration={action.duration_s}s)"
            )
            self._start_follow(start_act)
            self._do_wait(action.duration_s)
            self._stop_follow()

        else:
            raise NotImplementedError(f"No executor for {type(action).__name__}")

    # ------------------------------------------------------------------ wait helpers

    def _do_wait(self, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._check_stop()
            self.progress_updated.emit(f"Waiting… {remaining:.0f} s remaining")
            time.sleep(min(0.2, remaining))

    def _check_stop(self) -> None:
        if self._stop_event.is_set():
            raise _StopRequested()

    # ------------------------------------------------------------------ stage

    def _do_stage(self, action: StageAction, var_context: dict) -> None:
        ctrl = self._ctx.controller
        op = action.operation

        if op == "emergency_stop":
            self._logger.log_ops("[STAGE] AESTP (emergency stop all)")
            ctrl.emergency_stop()
            return

        if op == "set_speed":
            speed = action.speed or "M"
            self._logger.log_ops(f"[STAGE] set_speed Ch{action.ch} → {speed}")
            ctrl.set_ch_speed(action.ch, speed)
            ctrl.switch_to_loc()
            return

        value = action.value
        if isinstance(value, str):
            value = var_context.get(value, 0)
        value = int(value)

        if op == "move_absolute":
            if action.speed:
                ctrl.set_ch_speed(action.ch, action.speed)
            self._logger.log_ops(
                f"[STAGE] ABS Ch{action.ch} → {value:+d}  speed={action.speed or 'M'}"
            )
            ctrl.move_ch_absolute(action.ch, value)
        elif op == "move_relative":
            if action.speed:
                ctrl.set_ch_speed(action.ch, action.speed)
            self._logger.log_ops(
                f"[STAGE] REL Ch{action.ch} Δ{value:+d}  speed={action.speed or 'M'}"
            )
            ctrl.move_ch_relative(action.ch, value)
        else:
            raise ValueError(f"Unknown stage operation: {op!r}")

        self._wait_stage_stop(ctrl)

        try:
            pos = ctrl.get_ch_pos(action.ch)
            self._logger.log_ops(f"[STAGE] Ch{action.ch} stopped at {pos:+d}")
        except Exception:
            pass

        # Check global limits after any Ch3/4/5 move
        if action.ch in (3, 4, 5):
            self._check_global_limits()

    def _wait_stage_stop(self, ctrl) -> None:
        """Poll get_is_moving() with stop-event check; switch to LOC when done."""
        consecutive_stopped = 0
        while True:
            self._check_stop()
            if ctrl.get_is_moving():
                consecutive_stopped = 0
            else:
                consecutive_stopped += 1
                if consecutive_stopped >= 4:
                    break
            time.sleep(0.1)
        ctrl.switch_to_loc()

    # ------------------------------------------------------------------ global limits

    def _check_global_limits(self) -> None:
        """Check Ch3/4/5 positions against GlobalLimits. Raises _StopRequested on violation."""
        gl = self._global_limits
        if gl is None or not self._baseline_pos:
            return
        ctrl = self._ctx.controller
        if ctrl is None:
            return

        limits_map = {
            3: (gl.ch3_minus_mm, gl.ch3_plus_mm),
            4: (gl.ch4_minus_mm, gl.ch4_plus_mm),
            5: (gl.ch5_minus_mm, gl.ch5_plus_mm),
        }
        for ch, (minus_mm, plus_mm) in limits_map.items():
            baseline = self._baseline_pos.get(ch)
            if baseline is None:
                continue
            try:
                current = ctrl.get_ch_pos(ch)
            except Exception:
                continue
            delta_mm = (current - baseline) * _UM_PER_PULSE[ch] / 1000.0

            if plus_mm is not None and delta_mm > plus_mm:
                self._trigger_global_limit_error(ch, delta_mm, f"+{plus_mm:.3f} mm")
            if minus_mm is not None and delta_mm < -minus_mm:
                self._trigger_global_limit_error(ch, delta_mm, f"-{minus_mm:.3f} mm")

    def _trigger_global_limit_error(
        self, ch: int, delta_mm: float, limit_str: str
    ) -> None:
        """Normal-stop all axes, signal follow thread, emit error, raise _StopRequested."""
        ctrl = self._ctx.controller
        self._logger.log_ops("[STAGE] normal_stop() ASSTP — global limit violation")
        try:
            ctrl.normal_stop()   # ASSTP — decelerate-stop all motors
        except Exception:
            pass
        self._follow_stop_event.set()
        msg = (
            f"Global limit exceeded on Ch{ch}: {delta_mm:+.3f} mm "
            f"(limit {limit_str} from sequence-start position)"
        )
        self._logger.log_ops(f"[LIMIT ERROR] {msg}")
        self._logger.log_science(
            "error", step_index=self._current_step_idx, note=msg
        )
        self._had_error = True
        self.error_occurred.emit(self._flat_index, msg)
        raise _StopRequested()

    # ------------------------------------------------------------------ pressure

    def _do_set_pressure(self, action: SetPressureAction, var_context: dict) -> None:
        # Single shared implementation: Pace5000Backend.set_pressure_with_ramp()
        # (apps/PACE5000/pace5000_backend.py) — also used by this app's own
        # Scheduled Control feature and the HTTP API. It sends the slew rate
        # and verifies the device applied it *before* sending the setpoint;
        # do not re-implement that ordering here.
        backend = self._ctx.pace5000
        pressure = action.pressure
        if isinstance(pressure, str):
            pressure = float(var_context.get(pressure, 0))

        pressure_mpa = float(pressure) * PRESSURE_UNIT_TO_MPA.get(action.unit, 1.0)
        rate_mpa_per_min = action.rate * RATE_UNIT_TO_MPA_PER_MIN.get(action.rate_unit, 1.0)

        def _on_slew_send() -> None:
            self._logger.log_ops(
                f"[PACE5000] set_slew_rate({rate_mpa_per_min:.4f} MPa/min)"
            )

        def _on_slew_verified(actual_mpa_per_sec: float) -> None:
            self._logger.log_ops(
                f"[PACE5000] slew rate verified → {actual_mpa_per_sec * 60:.4f} MPa/min"
            )
            self.progress_updated.emit(
                f"Slew rate verified → {actual_mpa_per_sec * 60:.6f} MPa/min"
            )

        backend.set_pressure_with_ramp(
            pressure_mpa, rate_mpa_per_min,
            on_slew_send=_on_slew_send, on_slew_verified=_on_slew_verified,
        )

        self._logger.log_ops(
            f"[PACE5000] set_target_pressure({pressure_mpa:.3f} MPa)"
        )
        self.progress_updated.emit(
            f"Pressure target → {pressure} {action.unit} ({pressure_mpa:.3f} MPa)"
        )

    def _do_wait_pressure(self, action: WaitPressureAction, step_index: int) -> None:
        backend = self._ctx.pace5000
        tol_mpa = action.tol * PRESSURE_UNIT_TO_MPA.get(action.unit, 1.0)

        raw = backend.get_target_pressure()
        if raw is None:
            raise RuntimeError("Cannot read PACE5000 target pressure")
        target_mpa = float(raw)

        self._logger.log_ops(
            f"[PACE5000] wait_pressure(target={target_mpa:.3f} MPa, tol={tol_mpa:.4f} MPa)"
        )
        self.progress_updated.emit(
            f"Waiting for pressure {target_mpa:.3f} MPa ±{tol_mpa:.4f} MPa"
        )

        def _on_update(current_mpa: float, target_mpa: float) -> None:
            self.progress_updated.emit(
                f"Pressure: {current_mpa:.4f} MPa "
                f"(target {target_mpa:.3f} ±{tol_mpa:.4f})"
            )

        result = backend.wait_for_pressure(
            tol_mpa, stop_event=self._stop_event, on_update=_on_update,
        )
        if result is None:
            raise _StopRequested()

        self._logger.log_ops(f"[PACE5000] pressure reached: {result:.4f} MPa")
        self._logger.log_science(
            "pressure_reached", step_index=step_index,
            note=f"P={result:.4f} MPa (target {target_mpa:.3f} ±{tol_mpa:.4f})",
        )
        self.progress_updated.emit(f"Pressure reached: {result:.4f} MPa")

    # ------------------------------------------------------------------ temperature

    def _do_set_temperature(self, action: SetTemperatureAction, var_context: dict) -> None:
        backend = self._ctx.lakeshore
        value_k = action.value_k
        if isinstance(value_k, str):
            value_k = float(var_context.get(value_k, 0))
        value_k = float(value_k)

        self._logger.log_ops(
            f"[LAKESHORE] set_ramp_parameter(rate={action.ramp_rate:.4f} K/min, enable=True)"
        )
        backend.set_ramp_parameter(rate_kpm=action.ramp_rate, enable=True)

        consecutive_failures = 0
        while True:
            enabled, actual_rate = backend.get_ramp_parameter()
            if enabled and abs(actual_rate - action.ramp_rate) <= 0.001:
                self._logger.log_ops(
                    f"[LAKESHORE] ramp rate verified → {actual_rate:.4f} K/min"
                )
                self.progress_updated.emit(
                    f"Ramp rate verified → {actual_rate:.4f} K/min"
                )
                break
            consecutive_failures += 1
            if consecutive_failures >= 3:
                raise RuntimeError(
                    f"LakeShore ramp rate verification failed (3 consecutive): "
                    f"sent {action.ramp_rate:.4f} K/min, "
                    f"device reports {actual_rate:.4f} K/min (ramp enabled={enabled})"
                )
            time.sleep(0.2)

        self._logger.log_ops(f"[LAKESHORE] set_setpoint({value_k:.2f} K)")
        backend.set_setpoint(value_k)
        self.progress_updated.emit(
            f"Temperature setpoint → {value_k:.2f} K (ramp {action.ramp_rate} K/min)"
        )

    def _do_wait_temperature(self, action: WaitTemperatureAction, step_index: int) -> None:
        backend = self._ctx.lakeshore
        setpoint_k = backend.get_setpoint()
        self._logger.log_ops(
            f"[LAKESHORE] wait_temperature(target={setpoint_k:.2f} K, tol={action.tol_k:.2f} K)"
        )
        self.progress_updated.emit(
            f"Waiting for temperature {setpoint_k:.2f} K ±{action.tol_k:.2f} K"
        )
        while True:
            self._check_stop()
            data = backend.get_data()
            if data:
                current_k = data[-1].temp_a_k
                if abs(current_k - setpoint_k) <= action.tol_k:
                    self._logger.log_ops(
                        f"[LAKESHORE] temperature reached: {current_k:.2f} K"
                    )
                    self._logger.log_science(
                        "temperature_reached", step_index=step_index,
                        note=f"T={current_k:.2f} K (target {setpoint_k:.2f} ±{action.tol_k:.2f})",
                    )
                    self.progress_updated.emit(
                        f"Temperature reached: {current_k:.2f} K"
                    )
                    break
                self.progress_updated.emit(
                    f"Temperature: {current_k:.2f} K "
                    f"(target {setpoint_k:.2f} ±{action.tol_k:.2f})"
                )
            time.sleep(0.2)

    # ------------------------------------------------------------------ radicon

    def _resolve_xrd(self, action: TakeXrdAction) -> _EffectiveXrd:
        g = self._global_xrd
        exposure_ms = action.exposure_ms if action.exposure_ms is not None else g.exposure_ms
        save_dir_str = action.save_dir if action.save_dir is not None else g.save_dir
        save_dir = Path(save_dir_str) if save_dir_str else (
            _DEFAULT_XRD_SAVE_DIR / self._run_timestamp
        )
        dark_file = action.dark_file if action.dark_file is not None else g.dark_file
        dark_enabled = action.dark_enabled if action.dark_enabled is not None else g.dark_enabled
        defect_file = action.defect_file if action.defect_file is not None else g.defect_file
        defect_enabled = (
            action.defect_enabled if action.defect_enabled is not None else g.defect_enabled
        )
        defect_kernel = (
            action.defect_kernel if action.defect_kernel is not None else g.defect_kernel
        )
        flip_v = action.flip_v if action.flip_v is not None else g.flip_v
        flip_h = action.flip_h if action.flip_h is not None else g.flip_h
        oscillate = action.oscillate if action.oscillate is not None else g.oscillate
        osc_pos_a_deg = (
            action.osc_pos_a_deg if action.osc_pos_a_deg is not None else g.osc_pos_a_deg
        )
        osc_pos_b_deg = (
            action.osc_pos_b_deg if action.osc_pos_b_deg is not None else g.osc_pos_b_deg
        )
        osc_dwell_ms = (
            action.osc_dwell_ms if action.osc_dwell_ms is not None else g.osc_dwell_ms
        )
        osc_speed = action.osc_speed if action.osc_speed is not None else g.osc_speed
        return _EffectiveXrd(
            exposure_ms=exposure_ms,
            save_dir=save_dir,
            dark_file=dark_file,
            dark_enabled=dark_enabled,
            defect_file=defect_file,
            defect_enabled=defect_enabled,
            defect_kernel=defect_kernel,
            flip_v=flip_v,
            flip_h=flip_h,
            oscillate=oscillate,
            osc_pos_a_deg=osc_pos_a_deg,
            osc_pos_b_deg=osc_pos_b_deg,
            osc_dwell_ms=osc_dwell_ms,
            osc_speed=osc_speed,
        )

    def _load_xrd_dark(self, path: str) -> np.ndarray | None:
        if path in self._xrd_dark_cache:
            return self._xrd_dark_cache[path]
        try:
            import cv2
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                self.progress_updated.emit(f"[XRD] Dark file not readable: {path}")
                return None
            dark = img.astype(np.float64)
            self._xrd_dark_cache[path] = dark
            return dark
        except Exception as exc:
            self.progress_updated.emit(f"[XRD] Dark load error: {exc}")
            return None

    def _load_xrd_defect_mask(self, path: str, backend) -> np.ndarray | None:
        cache_key = f"{path}:{backend.width}:{backend.height}"
        if cache_key in self._xrd_defect_cache:
            return self._xrd_defect_cache[cache_key]
        try:
            try:
                from ..Rad_icon_2022.image_utils import parse_defect_file, build_defect_mask
            except ImportError:
                import sys as _sys
                _pkg = str(Path(__file__).parent.parent.parent)
                if _pkg not in _sys.path:
                    _sys.path.insert(0, _pkg)
                from apps.Rad_icon_2022.image_utils import parse_defect_file, build_defect_mask
            binning = "2x2" if backend.width < 2000 else "1x1"
            defects = parse_defect_file(
                path, binning, backend._h_blank, backend.width, backend.height
            )
            mask = build_defect_mask(defects, backend.height, backend.width)
            self._xrd_defect_cache[cache_key] = mask
            return mask
        except Exception as exc:
            self.progress_updated.emit(f"[XRD] Defect mask load error: {exc}")
            return None

    def _do_take_xrd(self, action: TakeXrdAction, step_index: int) -> None:
        try:
            from ..Rad_icon_2022.image_utils import (
                save_tiff, apply_flip, apply_dark_correction, apply_defect_correction
            )
        except ImportError:
            import sys as _sys
            _pkg = str(Path(__file__).parent.parent.parent)
            if _pkg not in _sys.path:
                _sys.path.insert(0, _pkg)
            from apps.Rad_icon_2022.image_utils import (
                save_tiff, apply_flip, apply_dark_correction, apply_defect_correction
            )

        backend = self._ctx.radicon
        eff = self._resolve_xrd(action)

        # Start Ch11 oscillation in background thread if requested
        osc_stop = threading.Event()
        osc_thread: threading.Thread | None = None
        if eff.oscillate:
            self._logger.log_ops(
                f"[CH11] oscillation start: "
                f"pos_a={eff.osc_pos_a_deg}° pos_b={eff.osc_pos_b_deg}° "
                f"dwell={eff.osc_dwell_ms}ms speed={eff.osc_speed}"
            )
            self.progress_updated.emit(
                f"[CH11] Oscillation started "
                f"({eff.osc_pos_a_deg}°↔{eff.osc_pos_b_deg}°)"
            )
            osc_thread = threading.Thread(
                target=self._osc_loop,
                args=(eff, osc_stop),
                daemon=True,
            )
            osc_thread.start()

        try:
            self._logger.log_ops(
                f"[RADICON] set_exposure_ms({eff.exposure_ms}) + snap_triggered()"
            )
            backend.set_exposure_ms(eff.exposure_ms)
            frame = backend.snap_triggered(timeout_ms=eff.exposure_ms + 5000)
        finally:
            if osc_thread is not None:
                osc_stop.set()
                osc_thread.join(timeout=30)
                self._logger.log_ops("[CH11] oscillation stopped — returning to θ=0°")
                self.progress_updated.emit("[CH11] Returning to θ=0°…")
                self._return_ch11_to_zero(eff.osc_speed)

        frame = apply_flip(frame, eff.flip_v, eff.flip_h)

        if eff.dark_enabled and eff.dark_file:
            dark = self._load_xrd_dark(eff.dark_file)
            if dark is not None:
                try:
                    frame = apply_dark_correction(frame, dark)
                except ValueError as exc:
                    self.progress_updated.emit(f"[XRD] Dark correction skipped: {exc}")

        if eff.defect_enabled and eff.defect_file:
            mask = self._load_xrd_defect_mask(eff.defect_file, backend)
            if mask is not None:
                frame = apply_defect_correction(frame, mask, eff.defect_kernel)

        if action.save:
            eff.save_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            binning = "2x2" if backend.width < 2000 else "1x1"
            fname = eff.save_dir / f"{action.prefix}_{ts}_{binning}.tif"
            meta = {
                "image_type": "xrd",
                "exposure_ms": eff.exposure_ms,
                "binning": binning,
                "flip_v": eff.flip_v,
                "flip_h": eff.flip_h,
                "dark_corrected": eff.dark_enabled and bool(eff.dark_file),
                "dark_source": Path(eff.dark_file).name if eff.dark_file else None,
                "defect_corrected": eff.defect_enabled and bool(eff.defect_file),
                "defect_kernel": eff.defect_kernel if eff.defect_enabled else None,
                "oscillated": eff.oscillate,
                "detector": "Rad-icon 2022",
                "beamline": "BL-18C",
                "datetime": datetime.now().isoformat(timespec="seconds"),
            }
            save_tiff(fname, frame, meta)
            self._logger.log_ops(f"[RADICON] saved → {fname}")
            self._logger.log_science(
                "xrd_taken", step_index=step_index, xrd_file=str(fname)
            )
            self.progress_updated.emit(f"XRD saved → {fname}")
        else:
            self._logger.log_ops(
                f"[RADICON] frame captured ({eff.exposure_ms} ms, not saved)"
            )
            self._logger.log_science(
                "xrd_taken", step_index=step_index, note="not saved"
            )
            self.progress_updated.emit(
                f"XRD captured ({eff.exposure_ms} ms, not saved)"
            )

    def _do_take_dark(self, action: TakeDarkAction) -> None:
        backend = self._ctx.radicon
        self._logger.log_ops(
            f"[RADICON] take_dark: set_exposure_ms({action.exposure_ms}) + snap_triggered()"
        )
        backend.set_exposure_ms(action.exposure_ms)
        backend.snap_triggered(timeout_ms=action.exposure_ms + 5000)
        self._logger.log_ops(f"[RADICON] dark frame captured ({action.exposure_ms} ms)")
        self.progress_updated.emit(f"Dark frame captured ({action.exposure_ms} ms)")

    # ------------------------------------------------------------------ Ch11 oscillation

    def _osc_loop(self, eff: _EffectiveXrd, osc_stop: threading.Event) -> None:
        """Background Ch11 oscillation during XRD exposure.

        Runs A→B→A→... until osc_stop is set.
        Exits cleanly mid-move (does NOT stop the motor — caller does that via normal_stop).
        """
        ctrl = self._ctx.controller
        if ctrl is None:
            return

        try:
            pos_a = round(eff.osc_pos_a_deg / _DEG_PER_PULSE_CH11)
            pos_b = round(eff.osc_pos_b_deg / _DEG_PER_PULSE_CH11)

            def _move_and_wait(target: int) -> bool:
                """Issue absolute move to target; poll until stopped or osc_stop set.
                Returns True if motor reached target, False if osc_stop fired first."""
                ctrl.set_ch_speed(11, eff.osc_speed)
                ctrl.move_ch_absolute(11, target)
                while not osc_stop.is_set():
                    if not ctrl.get_is_moving():
                        return True
                    time.sleep(0.1)
                return False

            def _dwell() -> bool:
                """Wait osc_dwell_ms in small slices.
                Returns True when dwell finished, False if osc_stop fired."""
                if eff.osc_dwell_ms <= 0:
                    return True
                deadline = time.monotonic() + eff.osc_dwell_ms / 1000.0
                while time.monotonic() < deadline:
                    if osc_stop.is_set():
                        return False
                    time.sleep(0.05)
                return True

            while True:
                if not _move_and_wait(pos_a):
                    break
                if not _dwell():
                    break
                if not _move_and_wait(pos_b):
                    break
                if not _dwell():
                    break

        except Exception as exc:
            self.progress_updated.emit(f"[OSC] Error in oscillation loop: {exc}")

    def _return_ch11_to_zero(self, speed: str) -> None:
        """Stop any in-progress Ch11 move, then drive to 0° and wait for arrival.

        Called after oscillation ends (snap_triggered returned).
        Raises _StopRequested if the user requests sequence stop during the return.
        """
        ctrl = self._ctx.controller
        if ctrl is None:
            return
        try:
            ctrl.normal_stop()   # ASSTP — decelerate-stop any in-progress move
        except Exception:
            pass
        # Brief deceleration pause
        time.sleep(0.3)
        ctrl.set_ch_speed(11, speed)
        ctrl.move_ch_absolute(11, 0)
        self._wait_stage_stop(ctrl)
        self._logger.log_ops("[CH11] returned to θ=0°")
        self.progress_updated.emit("[CH11] Returned to θ=0°")

    # ------------------------------------------------------------------ camera / reference

    def _do_save_reference(self, action: SaveReferenceImageAction) -> None:
        self._logger.log_ops(
            f"[CAMERA] save_reference_image(path={action.path!r}, "
            f"camera_index={action.camera_index})"
        )
        cap = cv2.VideoCapture(action.camera_index)
        try:
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera index {action.camera_index}")
            ret, frame = cap.read()
            if not ret:
                raise RuntimeError("Camera read failed")
        finally:
            cap.release()

        out = Path(action.path) if action.path else _DEFAULT_REF_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez(str(out), frame=frame)
        self._logger.log_ops(f"[CAMERA] reference image saved → {out}")
        self.progress_updated.emit(f"Reference image saved → {out}")

    # ------------------------------------------------------------------ follow thread

    def _start_follow(self, action: StartFollowingAction) -> None:
        if self._follow_thread is not None and self._follow_thread.is_alive():
            raise RuntimeError(
                "start_following called while a follow session is already active"
            )
        self._current_follow_action = action
        self._follow_stop_event.clear()
        self._follow_thread = threading.Thread(
            target=self._follow_loop, args=(action,), daemon=True
        )
        self._follow_thread.start()
        self.progress_updated.emit("Sample following started")

    def _stop_follow(self) -> None:
        self._follow_stop_event.set()
        if self._follow_thread is not None:
            self._follow_thread.join(timeout=10)
            self._follow_thread = None
        self._current_follow_action = None
        self.progress_updated.emit("Sample following stopped")

    def _cleanup_follow_thread(self) -> None:
        self._follow_stop_event.set()
        if self._follow_thread is not None:
            self._follow_thread.join(timeout=5)
            self._follow_thread = None

    # ------------------------------------------------------------------ stage settings

    @staticmethod
    def _load_stage_settings() -> dict:
        """Load stage_settings.json shared with the Stage Controller UI."""
        path = (
            Path(__file__).parent.parent
            / "ui_stage_controller" / "__localdata" / "stage_settings.json"
        )
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        # Fallback defaults matching fpd_scope_stg_controller_ui.py _DEFAULT_SETTINGS
        return {"det_out": "-40000", "det_in": "1779", "ch8_out": "0", "ch8_in": "281092"}

    # ------------------------------------------------------------------ follow loop

    def _follow_loop(self, action: StartFollowingAction) -> None:
        """
        Background XY sample-tracking loop with optional Ch3 autofocus.
        Ported from interactive_camera._follow_task, extended with GlobalFollowSettings.
        Global limits are enforced after every correction move.
        """
        try:
            gf = self._global_follow

            # Resolve settings: per-step action values take priority over global
            interval_s = (
                action.interval_s if action.interval_s is not None else gf.interval_s
            )
            similarity_threshold = (
                action.similarity_threshold if action.similarity_threshold is not None
                else gf.similarity_threshold
            )

            # Per-channel correction limits
            if action.max_correction_per_step_um is not None:
                max_ch4_um = action.max_correction_per_step_um
                max_ch5_um = action.max_correction_per_step_um
            else:
                max_ch4_um = gf.max_correction_ch4_um
                max_ch5_um = gf.max_correction_ch5_um
            lim4 = max(0, int(max_ch4_um / _UM_PER_PULSE[4]))
            lim5 = max(0, int(max_ch5_um / _UM_PER_PULSE[5]))

            xy_max_retries = gf.xy_max_retries

            # Effective autofocus settings — always enabled; None fields fall back to global
            eff_af_enabled = True
            eff_af_range_um = (
                action.autofocus_range_um if action.autofocus_range_um is not None
                else gf.autofocus_range_um
            )
            eff_af_steps = (
                action.autofocus_steps if action.autofocus_steps is not None
                else gf.autofocus_steps
            )

            # Calibration
            cal = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
            M_inv = np.array(cal["matrix_inv"])

            # Reference frame
            ref_path = action.reference_path or str(_DEFAULT_REF_PATH)
            ref_data = np.load(ref_path)
            reference_frame = ref_data["frame"]

            cap = cv2.VideoCapture(action.camera_index)
            if not cap.isOpened():
                self.progress_updated.emit(
                    f"[follow] Cannot open camera {action.camera_index}"
                )
                return

            cumulative = {4: 0, 5: 0}

            try:
                while not (self._follow_stop_event.is_set()
                           or self._stop_event.is_set()):

                    # Interval wait in small slices to allow early exit
                    deadline = time.monotonic() + interval_s
                    while time.monotonic() < deadline:
                        if (self._follow_stop_event.is_set()
                                or self._stop_event.is_set()):
                            return
                        time.sleep(0.2)

                    if (self._follow_stop_event.is_set()
                            or self._stop_event.is_set()):
                        return

                    ret, frame = cap.read()
                    if not ret:
                        self.progress_updated.emit(
                            "[follow] Camera read failed — skipping"
                        )
                        continue

                    ctrl = self._ctx.controller

                    # Initial XY shift → motor pulses
                    dx_px, dy_px = self._compute_xy_shift(reference_frame, frame)
                    motor_disp = -(M_inv @ np.array([dx_px, dy_px]))
                    d_ch4 = int(np.clip(motor_disp[0], -lim4, lim4))
                    d_ch5 = int(np.clip(motor_disp[1], -lim5, lim5))

                    # Similarity feedback
                    sim = self._compute_similarity(reference_frame, frame)
                    msg = f"[follow] Similarity: {sim:.3f}"
                    if sim < similarity_threshold:
                        msg += f" (below threshold {similarity_threshold:.2f})"
                    self.progress_updated.emit(msg)

                    # Apply initial XY correction
                    if d_ch4 != 0:
                        ctrl.move_ch_relative(4, d_ch4)
                    if d_ch5 != 0:
                        ctrl.move_ch_relative(5, d_ch5)
                    if d_ch4 != 0 or d_ch5 != 0:
                        ctrl.wait_until_stop()
                        cumulative[4] += d_ch4
                        cumulative[5] += d_ch5
                        self._logger.log_ops(
                            f"[FOLLOW] correction: Δ Ch4={d_ch4:+d} Ch5={d_ch5:+d} | "
                            f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d} | "
                            f"similarity={sim:.3f}"
                        )
                        self.progress_updated.emit(
                            f"[follow] Δ Ch4={d_ch4:+d} Ch5={d_ch5:+d} | "
                            f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d}"
                        )
                        self._check_global_limits()

                    if self._follow_stop_event.is_set() or self._stop_event.is_set():
                        return

                    # XY re-correction retry loop (mirrors interactive_camera behaviour)
                    for _retry in range(max(0, xy_max_retries - 1)):
                        if (self._follow_stop_event.is_set()
                                or self._stop_event.is_set()):
                            break
                        ret2, chk_frame = cap.read()
                        if not ret2:
                            break
                        chk_sim = self._compute_similarity(reference_frame, chk_frame)
                        if chk_sim >= similarity_threshold:
                            break
                        self.progress_updated.emit(
                            f"[follow] Similarity {chk_sim:.3f} below threshold — "
                            f"re-correcting XY (attempt {_retry + 2}/{xy_max_retries})"
                        )
                        _dx, _dy = self._compute_xy_shift(reference_frame, chk_frame)
                        _disp = -(M_inv @ np.array([_dx, _dy]))
                        _d4 = int(np.clip(_disp[0], -lim4, lim4))
                        _d5 = int(np.clip(_disp[1], -lim5, lim5))
                        if _d4 != 0:
                            ctrl.move_ch_relative(4, _d4)
                        if _d5 != 0:
                            ctrl.move_ch_relative(5, _d5)
                        if _d4 != 0 or _d5 != 0:
                            ctrl.wait_until_stop()
                            cumulative[4] += _d4
                            cumulative[5] += _d5
                            self._check_global_limits()
                        d_ch4 += _d4
                        d_ch5 += _d5

                    if self._follow_stop_event.is_set() or self._stop_event.is_set():
                        return

                    # Optional Ch3 autofocus after XY correction
                    if eff_af_enabled:
                        self._do_follow_autofocus(
                            cap, eff_af_range_um, eff_af_steps,
                            method=gf.autofocus_method,
                            n_frames=gf.autofocus_n_frames,
                            speed=gf.autofocus_speed,
                            peak_method=gf.autofocus_peak_method,
                        )
            finally:
                cap.release()

        except _StopRequested:
            # Global limit violation propagated — follow thread exits cleanly
            pass
        except Exception as exc:
            self.progress_updated.emit(f"[follow] Error: {exc}")

    def _do_follow_autofocus(
        self,
        cap: cv2.VideoCapture,
        range_um: float,
        steps: int,
        method: str = "laplacian",
        n_frames: int = 1,
        speed: str = "H",
        peak_method: str = "highest",
    ) -> None:
        """Sharpness scan on Ch3 (focus axis). Moves to sharpest position.

        range_um   — ±half-range in µm (e.g. 20 → scan ±20 µm around current pos)
        steps      — number of scan positions (≥2)
        method     — 'laplacian' (variance of Laplacian) or 'tenengrad' (mean |∇|²)
        n_frames   — frames averaged per position (1 = no averaging)
        speed      — Ch3 speed during scan ('H' / 'M' / 'L')
        peak_method — 'highest' or 'gaussian' (Gaussian fit via scipy if available)

        Global limits are enforced after the final move to the best position.
        """
        if steps < 2 or range_um <= 0:
            return
        if self._follow_stop_event.is_set() or self._stop_event.is_set():
            return

        ctrl = self._ctx.controller
        if ctrl is None:
            return

        # Set Ch3 speed for the scan
        try:
            ctrl.set_ch_speed(3, speed)
        except Exception:
            pass

        half_pulses = int(range_um / _UM_PER_PULSE[3])
        if half_pulses == 0:
            return

        # Clamp scan range so it does not exceed global limits for Ch3
        gl = self._global_limits
        if gl is not None and 3 in self._baseline_pos:
            try:
                current_ch3 = ctrl.get_ch_pos(3)
            except Exception:
                current_ch3 = None
            if current_ch3 is not None:
                baseline = self._baseline_pos[3]
                if gl.ch3_plus_mm is not None:
                    room_plus = max(0, int(
                        (baseline + gl.ch3_plus_mm * 1000 / _UM_PER_PULSE[3]) - current_ch3
                    ))
                    half_pulses = min(half_pulses, room_plus)
                if gl.ch3_minus_mm is not None:
                    room_minus = max(0, int(
                        current_ch3 - (baseline - gl.ch3_minus_mm * 1000 / _UM_PER_PULSE[3])
                    ))
                    half_pulses = min(half_pulses, room_minus)

        if half_pulses == 0:
            self.progress_updated.emit("[AF] Ch3 range exhausted by global limit — skipping")
            return

        try:
            start_pos = ctrl.get_ch_pos(3)
        except Exception:
            return

        scan_positions = [
            start_pos - half_pulses + i * (2 * half_pulses // (steps - 1))
            for i in range(steps)
        ]

        def _measure() -> float:
            vals = []
            for _ in range(max(1, n_frames)):
                ret, frame = cap.read()
                if not ret:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if method == "tenengrad":
                    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
                    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
                    vals.append(float(np.mean(gx ** 2 + gy ** 2)))
                else:
                    vals.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
            return float(np.mean(vals)) if vals else 0.0

        sharpness_data: list[tuple[int, float]] = []

        for pos in scan_positions:
            if self._follow_stop_event.is_set() or self._stop_event.is_set():
                break
            try:
                ctrl.move_ch_absolute(3, pos)
                ctrl.wait_until_stop()
            except Exception as exc:
                self.progress_updated.emit(f"[AF] Ch3 move error: {exc}")
                break
            sharpness_data.append((pos, _measure()))

        if not sharpness_data:
            return

        best_pos = _af_find_best_pos(sharpness_data, peak_method)
        best_sharpness = max(s for _, s in sharpness_data)

        if best_pos != start_pos:
            try:
                ctrl.move_ch_absolute(3, best_pos)
                ctrl.wait_until_stop()
                self._logger.log_ops(
                    f"[AF] Ch3 → {best_pos:+d} "
                    f"(method={method}, peak={peak_method}, sharpness={best_sharpness:.1f})"
                )
                self.progress_updated.emit(
                    f"[AF] Ch3 → {best_pos} (sharpness={best_sharpness:.1f})"
                )
            except Exception as exc:
                self.progress_updated.emit(f"[AF] Ch3 final move error: {exc}")
                return

        self._check_global_limits()

    # ------------------------------------------------------------------ static helpers

    @staticmethod
    def _compute_xy_shift(ref: np.ndarray, current: np.ndarray) -> tuple[int, int]:
        """Port of interactive_camera._compute_xy_shift."""
        ref_g = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
        cur_g = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
        h, w = ref_g.shape
        my, mx = h // 5, w // 5
        template = ref_g[my:h - my, mx:w - mx]
        result = cv2.matchTemplate(cur_g, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < 0.3:
            return 0, 0
        return max_loc[0] - mx, max_loc[1] - my

    @staticmethod
    def _compute_similarity(ref: np.ndarray, current: np.ndarray) -> float:
        """Port of interactive_camera._compute_similarity."""
        ref_g = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
        cur_g = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
        if ref_g.shape != cur_g.shape:
            cur_g = cv2.resize(cur_g, (ref_g.shape[1], ref_g.shape[0]))
        result = cv2.matchTemplate(cur_g, ref_g, cv2.TM_CCOEFF_NORMED)
        return float(result[0, 0])


# ------------------------------------------------------------------ helpers

def _global_limits_to_dict(gl: GlobalLimits | None) -> dict:
    if gl is None:
        return {}
    return {
        "ch3_minus_mm": gl.ch3_minus_mm,
        "ch3_plus_mm":  gl.ch3_plus_mm,
        "ch4_minus_mm": gl.ch4_minus_mm,
        "ch4_plus_mm":  gl.ch4_plus_mm,
        "ch5_minus_mm": gl.ch5_minus_mm,
        "ch5_plus_mm":  gl.ch5_plus_mm,
    }
