"""
SequenceRunner — executes a Sequence in a QThread.

All device calls happen in this thread (safe: each backend has its own Lock).
The main thread receives only Qt signals; it never touches device APIs.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from .actions import (
    Action, WaitAction, LogAction,
    StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction,
    SetPressureAction, WaitPressureAction, SetAndWaitPressureAction, SetControlModeAction,
    SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction,
    TakeXrdAction, TakeDarkAction, TakeSpectrumAction,
    CompressToTargetPressureUsingRubyAction,
    SaveReferenceImageAction, SaveSnapshotAction,
    StartFollowingAction, StopFollowingAction, FollowSampleAction,
    AlignToReferenceAction,
    ForLoopAction,
)
from .device_context import DeviceContext
from .gasket_units import (
    displacement_um, distance_um, load_stage_um_per_pixel,
    nominal_um_per_pixel, stage_um_per_pixel,
)
from .log_manager import PHOTO_SUBDIR, SPECTRUM_SUBDIR, XRD_SUBDIR, RunLogger
from .nearby_search import build_search_axes, effective_search_action, search_positions
from .spectrum_quality import (
    EXPOSURE_MIN_S, EXPOSURE_MAX_S, MAX_EXPOSURE_HALVINGS,
    assess_spectrum, next_em_gain,
)
from .ruby_master import append_ruby_record
from .run_plan import RunStep, build_run_plan
from .sequence import Sequence
from .spectrum_export import save_spectrum_csv, save_spectrum_plot
from . import ruby_pressure
from . import compression
from .xrd_master import append_xrd_record, capture_conditions

from apps.PACE5000.pace5000_backend import (
    MIN_SLEW_RATE_MPA_PER_SEC, PRESSURE_UNIT_TO_MPA, RATE_UNIT_TO_MPA_PER_MIN,
)
from apps.interactive_camera.autofocus import AutoFocus
from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole, detect_gasket_hole_near, reference_digest,
    sample_offset,
)
from apps.interactive_camera.xray_beam import (
    check_xray_beam, load_xray_beam, read_microscope_pulses,
)
from apps.interactive_camera.sample_tracking import (
    compute_xy_shift, compute_similarity, measure_xy_shift_strict,
    clamp_xy_correction,
)
from apps.stage_fpd_scope.stage_settings import load_stage_settings
from utils.stage.control_stage import PULSE_SCALE, MotionRevokedError
from apps.Rad_icon_2022.instant1d import reduce_frame, save_profile
from utils.pdindexer import PdiProfile

from .safety_rules import (
    GASKET_BEAM_CHANNELS,
    STAGE_MODE_CHANNELS,
    exceeded_global_limit,
    explain_stage_mode,
    global_limit_delta_mm,
    global_limits_for_channel,
    stage_mode_from_positions,
    validate_ch11_oscillation_settings,
)
from . import scheduler_settings
from .validator.models import (
    Diagnostic,
    build_controller_diagnostic,
    build_runtime_diagnostic,
)

from dataclasses import dataclass as _dc, replace

_PRESETS_PATH = Path(__file__).parent / "__localdata" / "scheduler_presets.json"
_CALIBRATION_PATH = (
    Path(__file__).parent.parent / "interactive_camera" / "calibration.json"
)
_DEFAULT_REF_PATH = Path(__file__).parent / "__localdata" / "reference_frame.png"

_DEFAULT_PRESETS = {
    "follow_sample": {
        "interval_s": 60,
        "similarity_threshold": 0.95,
        "max_correction_per_step_um": 400,
    }
}


def _load_presets() -> dict:
    if _PRESETS_PATH.exists():
        return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))
    return _DEFAULT_PRESETS


class _StopRequested(Exception):
    """Internal sentinel: clean stop propagation up the call stack."""


class RunnerError(RuntimeError):
    """A Runner-detected safety-rule violation, carrying a stable ``code``
    for `Diagnostic`/ops.log classification — REORGANISATION_PLAN.md Phase 9.

    Raised by MOVE_CONSTRAINTS pre-checks and Ch11 oscillation failures; the
    single terminal exception handler in `_execute_actions()` reads
    ``.code`` off any exception that has one (falling back to
    ``"runtime.unexpected_error"`` for anything else) rather than every raise
    site building its own `Diagnostic`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Two Ch3 autofocus scans with multiple frames and XY retries can exceed the
# old 10-second follow join. Keep a finite bound for stalled hardware I/O.
_FOLLOW_STOP_TIMEOUT_S = 180.0
_GASKET_COHERENCE_RADIUS_PX = 12.0
_TEMPERATURE_COMMAND_VERIFY_TIMEOUT_S = 5.0


# Cached PACE5000 / LakeShore readings older than this are not put in a
# filename (same freshness limit as build_instrument_metadata).
_XRD_SUFFIX_MAX_AGE_S = 10.0


def _format_suffix_number(value: float, decimals: int) -> str:
    """Round to *decimals* and drop trailing zeros: 1.000 → "1", 200.30 → "200.3"."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _cached_instrument_readings(backend, *, require_fresh: bool) -> dict | None:
    """Connected backend's cached readings, or None (no new device I/O)."""
    if backend is None:
        return None
    try:
        readings = backend.get_cached_readings()
    except Exception:
        return None
    if not readings.get("connected"):
        return None
    if require_fresh:
        stamp = readings.get("timestamp")
        if not (isinstance(stamp, (int, float)) and math.isfinite(stamp)
                and 0 <= time.time() - stamp <= _XRD_SUFFIX_MAX_AGE_S):
            return None
    return readings


def _lakeshore_live_setpoint(backend) -> tuple[float, bool | None]:
    """(live setpoint, ramping) from SETP?/RAMPST?; ramping is None when unknown."""
    status = getattr(backend, "get_setpoint_status", None)
    if status is None:
        return backend.get_setpoint(), None
    _target, live, ramping = status()
    return live, ramping


def _instrument_suffix(key: str, ctx) -> str | None:
    """`Pgas_<p>MPa` / `Tset_<t>K` / `TB_<t>K`, or None when unavailable."""
    if ctx is None:
        return None
    if key == "pressure_gas":
        readings = _cached_instrument_readings(ctx.pace5000, require_fresh=True)
        field, label, unit, decimals = "pressure_mpa", "Pgas", "MPa", 3
    elif key == "temperature_set":
        readings = _cached_instrument_readings(ctx.lakeshore, require_fresh=False)
        field, label, unit, decimals = "target_setpoint_k", "Tset", "K", 2
    elif key == "temperature_b":
        readings = _cached_instrument_readings(ctx.lakeshore, require_fresh=True)
        field, label, unit, decimals = "temperature_b_k", "TB", "K", 1
    else:
        return None
    value = readings.get(field) if readings is not None else None
    if not (isinstance(value, (int, float)) and math.isfinite(value)):
        return None
    return f"{label}_{_format_suffix_number(float(value), decimals)}{unit}"


_INSTRUMENT_SUFFIX_KEYS = ("pressure_gas", "temperature_set", "temperature_b")


def _xrd_filename_stem(save_dir: Path, prefix: str, suffixes, ctx=None,
                       skipped: list | None = None) -> str:
    """`<prefix>[_<suffix>...]` for a saved XRD frame.

    Suffix choices mirror the Rad-icon 2022 app (`_build_save_path`), but
    several may be combined; they are appended in XRD_FILENAME_SUFFIXES
    order. The instrument suffixes (PACE5000 gas pressure, LakeShore 335
    setpoint and Ch B temperature) come first and are taken from *ctx*'s
    cached readings; they are left out when that instrument is OFF (absent
    from *ctx*), disconnected or its reading is stale. Keys left out although
    the instrument is ON are appended to *skipped* if given. The index is the 1-based count of existing
    `<prefix>_*.tif` files in *save_dir*, zero-padded to four digits."""
    now = datetime.now()
    parts = [prefix]
    for key in scheduler_settings.XRD_FILENAME_SUFFIXES:
        if key not in suffixes:
            continue
        if key in _INSTRUMENT_SUFFIX_KEYS:
            part = _instrument_suffix(key, ctx)
            if part is not None:
                parts.append(part)
            elif skipped is not None and ctx is not None and (
                    ctx.pace5000 if key == "pressure_gas" else ctx.lakeshore) is not None:
                skipped.append(key)
        elif key == "datetime_full":
            parts.append(now.strftime("%Y-%m-%d-%H-%M-%S"))
        elif key == "datetime_time":
            parts.append(now.strftime("%H-%M-%S"))
        elif key == "index":
            count = 0
            if save_dir.is_dir():
                count = sum(
                    1 for f in save_dir.iterdir()
                    if f.is_file() and f.suffix.lower() == ".tif"
                    and f.name.startswith(f"{prefix}_")
                )
            parts.append(f"{count + 1:04d}")
    return "_".join(parts)


def _spectrum_save_path(save_dir: Path, prefix: str, suffixes, attempt: int,
                        ctx=None, skipped: list | None = None) -> Path:
    """Choose one collision-free stem shared by the NPZ, CSV and PNG exports."""
    ruby_suffixes = tuple(key for key in suffixes
                          if key in scheduler_settings.RUBY_FILENAME_SUFFIXES)
    stem = _xrd_filename_stem(save_dir, prefix, ruby_suffixes, ctx=ctx,
                              skipped=skipped)
    base = f"{stem}_attempt_{attempt:02d}"
    candidate = save_dir / f"{base}.npz"
    number = 2
    while any(candidate.with_suffix(ext).exists() for ext in (".npz", ".csv", ".png")):
        candidate = save_dir / f"{base}_{number}.npz"
        number += 1
    return candidate


@_dc
class _EffectiveXrd:
    """Resolved XRD settings after merging per-step overrides with GlobalXrdSettings."""
    exposure_ms: int
    prefix: str
    filename_suffixes: tuple[str, ...]
    dark_file: str | None
    dark_enabled: bool
    defect_file: str | None
    defect_enabled: bool
    defect_mode: str
    defect_kernel: int
    flip_v: bool
    flip_h: bool
    oscillate: bool
    osc_pos_a_deg: float
    osc_pos_b_deg: float
    osc_dwell_ms: int
    osc_speed: str


class SequenceRunner(QThread):
    step_started      = pyqtSignal(int, str)   # (flat_index, description)
    step_completed    = pyqtSignal(int)         # (flat_index)
    step_started_at   = pyqtSignal(int, str)    # (flat_index, local timestamp)
    step_finished_at  = pyqtSignal(int, str)    # also emitted on stop/error
    file_saved        = pyqtSignal(int, str)    # (flat_index, full path)
    pdi_profile_ready = pyqtSignal(object)      # PdiProfile; handled on GUI thread
    progress_updated  = pyqtSignal(str)         # informational / polling message
    sequence_completed = pyqtSignal()
    sequence_stopped   = pyqtSignal()           # stopped by the user (Stop / Emergency stop)
    # The sequence ended early because a step's own stop condition was met
    # (e.g. ruby pressure reached its threshold). This is a successful end,
    # not a user stop. Argument: human-readable reason.
    sequence_condition_stopped = pyqtSignal(str)
    error_occurred    = pyqtSignal(int, str)    # (flat_index, error_message)
    shutter_restore_available = pyqtSignal(bool)  # initial inserted state
    ops_logged        = pyqtSignal(str)         # each line written to ops.log
    xrd_image_ready   = pyqtSignal(object, str)  # (corrected frame copy, display name)
    xrd_profile_ready = pyqtSignal(object, object, str)  # (2θ deg, intensity, display name)
    sample_pressure_ready = pyqtSignal(float, float, object)  # (epoch s, GPa, Ch B K | None)

    def __init__(
        self,
        sequence: Sequence,
        ctx: DeviceContext,
        global_limits: scheduler_settings.GlobalLimits | None = None,
        global_xrd: scheduler_settings.GlobalXrdSettings | None = None,
        global_follow: scheduler_settings.GlobalFollowSettings | None = None,
        global_camera: scheduler_settings.GlobalCameraSettings | None = None,
        log_path: str = "run",
        log_devices: list[str] | None = None,
        data_dir: str | None = None,
        parent=None,
        global_spectrum: scheduler_settings.GlobalSpectrumSettings | None = None,
        global_positions: scheduler_settings.GlobalPositions | None = None,
        global_shutter: scheduler_settings.GlobalShutterSettings | None = None,
        sequence_snapshot: Sequence | None = None,
    ):
        super().__init__(parent)
        self._sequence = sequence
        # Every ForLoopAction unrolled once (built on first use of run_plan):
        # flat_index is a position in this list, and the Visual tab shows the
        # same list as its run log.
        self._run_plan: list[RunStep] | None = None
        self._sequence_snapshot = sequence_snapshot
        self._ctx = ctx
        self._global_limits = global_limits
        self._global_xrd = global_xrd or scheduler_settings.GlobalXrdSettings()
        self._poni_ai = (getattr(ctx.poni_state, "integration_ai", ctx.poni_state.ai)
                         if ctx.poni_state is not None else None)
        # Snapshot GUI-owned reduction settings before this runner enters QThread.
        self._find_spots_before_1d = getattr(ctx.poni_state, "find_spots_before_1d", False)
        self._spot_deviation_factor = getattr(ctx.poni_state, "spot_deviation_factor", 5.0)
        self._global_follow = global_follow or scheduler_settings.GlobalFollowSettings()
        self._global_camera = global_camera or scheduler_settings.GlobalCameraSettings()
        self._global_spectrum = global_spectrum or scheduler_settings.GlobalSpectrumSettings()
        self._ruby_pressure_batch: list[tuple[float, float, float | None]] | None = None
        self._global_positions = global_positions
        self._global_shutter = global_shutter or scheduler_settings.GlobalShutterSettings()
        self._shutter_initial_state: bool | None = None
        self._shutter_managed = False
        self._shutter_confirmed_in = False
        self._log_path = log_path
        self._log_devices = list(log_devices or [])
        self._data_dir = data_dir or None
        self._stop_event = threading.Event()
        self._stop_reason = ""
        # Why the stop event was set: by the user (Stop / Emergency stop), or
        # by a step's own stop condition. Decides which end signal run() emits.
        self._user_stop_requested = False
        self._condition_stop = False
        # One motion lease spans the whole sequence run (acquired in run(),
        # released in its finally). Nested threads (follow/oscillation) are
        # this same runner's own threads and reuse self._motion_lease — a
        # frozen value, safe to read from any of them.
        self._motion_lease = None

        # Baseline positions captured at run() start (pulses)
        self._baseline_pos: dict[int, int] = {}
        # The main runner and background follow/autofocus paths share one
        # controller lease. Keep a Ch3/4/5 position read, Global-limit check,
        # move, and stop confirmation together so their relative targets
        # cannot be invalidated by another scheduler move between calls.
        # Stop requests deliberately do not take this lock.
        self._limited_stage_motion_lock = threading.RLock()

        # Background follow thread
        self._follow_thread: threading.Thread | None = None
        self._follow_stop_event = threading.Event()
        self._follow_first_cycle_done = threading.Event()
        # Read by the GUI monitor only. The runner's own position measurements
        # remain independent, including ruby and one-shot alignment.
        self._gasket_preview_event = threading.Event()
        self._gasket_preview_state_lock = threading.Lock()
        self._gasket_preview_epoch = 0
        self._current_follow_action: StartFollowingAction | None = None
        # Any exception raised inside _follow_loop() (other than a clean
        # _StopRequested unwind, which is already reported by whichever
        # abort path raised it) — the thread-safe handoff back to
        # _stop_follow()/_cleanup_follow_thread(), mirroring osc_exception's
        # role for the Ch11 oscillation thread (REORGANISATION_PLAN.md
        # Phase 9).
        self._follow_exception: Exception | None = None

        # USB camera session.  When the sequence contains any Interactive
        # Camera action, the runner opens the camera once at run start and
        # keeps a latest-frame loop alive until cleanup.
        self._camera_cap: cv2.VideoCapture | None = None
        self._camera_index: int | None = None
        self._camera_thread: threading.Thread | None = None
        self._camera_stop_event = threading.Event()
        self._camera_frame_lock = threading.Lock()
        self._camera_current_frame: np.ndarray | None = None
        self._camera_last_frame_at: float = 0.0
        self._camera_error: str | None = None

        # Ch3 autofocus — shares apps.interactive_camera.autofocus.AutoFocus
        # with the Interactive Camera app. Constructed once the camera session
        # is open (frame_provider needs self._get_camera_frame to be valid)
        # and torn down alongside it.
        self._af_ch3: AutoFocus | None = None

        self._flat_index = 0   # monotonically-increasing execution counter
        self._current_step_idx = 0  # index of the action currently executing
        self._had_error = False
        # Most recent runtime/controller-layer Diagnostic, if any — a future
        # UI hook; not read anywhere yet (REORGANISATION_PLAN.md Phase 9).
        self._last_diagnostic: Diagnostic | None = None
        # Set by _abort_for_global_limit() — lets the main thread's own
        # exception handlers recognise a terminal error already reported
        # from elsewhere (the follow thread) instead of double-reporting a
        # side-effect exception as a new, unrelated failure. See
        # _abort_for_global_limit()'s docstring for the race this guards.
        self._terminal_error_reported = False
        # Failure messages accumulated by _safe_cleanup()/_release_motion_
        # lease() during run()'s finally block — see run()'s finally for why
        # a non-empty list here must still force _had_error afterwards.
        self._cleanup_failures: list[str] = []
        self._last_verified_temperature_setpoint_k: float | None = None

        # Per-run caches: keyed by file path
        self._xrd_dark_cache: dict[str, np.ndarray] = {}
        self._xrd_defect_cache: dict[str, np.ndarray] = {}

        # File logger — auto-started in run() and stopped in the finally block.
        self._logger = RunLogger(ctx, on_ops_line=self.ops_logged.emit)

    # ------------------------------------------------------------------ public

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    @property
    def user_stop_requested(self) -> bool:
        """True once the user pressed Stop or Emergency stop for this run."""
        return self._user_stop_requested

    @property
    def last_error_code(self) -> str:
        """Diagnostic code of the most recent terminal error ("" if none)."""
        diagnostic = self._last_diagnostic
        return diagnostic.code if diagnostic is not None else ""

    @property
    def run_plan(self) -> list[RunStep]:
        """The flat, loop-unrolled steps this run executes, in order."""
        if self._run_plan is None:
            self._run_plan = build_run_plan(self._sequence.actions)
        return self._run_plan

    @property
    def gasket_preview_enabled(self) -> bool:
        """Thread-safe state for the diagnostic live gasket overlay."""
        return self.gasket_preview_state[0]

    @property
    def gasket_preview_state(self) -> tuple[bool, int]:
        """Enabled flag and revision, for dropping results across AF transitions."""
        with self._gasket_preview_state_lock:
            return self._gasket_preview_event.is_set(), self._gasket_preview_epoch

    def _set_gasket_preview_enabled(self, enabled: bool) -> None:
        with self._gasket_preview_state_lock:
            if self._gasket_preview_event.is_set() == enabled:
                return
            self._gasket_preview_epoch += 1
            if enabled:
                self._gasket_preview_event.set()
            else:
                self._gasket_preview_event.clear()

    def request_stop(self) -> None:
        """Thread-safe: may be called from the main thread.

        Sends a decelerate-stop (ASSTP) to the stage unconditionally — the
        stage may be mid-move when Stop is pressed, and the execution thread
        only checks the stop flag at its next poll, so waiting for that poll
        would leave the stage moving in the meantime. Async: never blocks
        the calling (GUI) thread on socket I/O — revokes self._motion_lease
        immediately, so the execution thread's next stage call raises
        MotionRevokedError and unwinds via its normal exception handling.
        """
        self._set_gasket_preview_enabled(False)
        self._user_stop_requested = True
        if not self._condition_stop:
            self._stop_reason = "Stopped by user"
        self._send_stage_stop(emergency=False)
        self._stop_event.set()
        self._follow_stop_event.set()

    def request_emergency_stop(self) -> None:
        """Thread-safe: may be called from the main thread.

        Sends an emergency-stop (AESTP) to the stage unconditionally, then
        ends the sequence the same way request_stop() does. Async — see
        request_stop().
        """
        self._set_gasket_preview_enabled(False)
        self._user_stop_requested = True
        if not self._condition_stop:
            self._stop_reason = "Emergency stop requested by user"
        self._send_stage_stop(emergency=True)
        self._stop_event.set()
        self._follow_stop_event.set()

    def _send_stage_stop(self, emergency: bool) -> None:
        ctrl = self._ctx.controller
        if ctrl is None:
            return
        try:
            if emergency:
                self._logger.log_ops("[SEQ:ESTOP] request_emergency_stop() AESTP (emergency stop requested)")
                ctrl.request_emergency_stop(source="exp_scheduler")
            else:
                self._logger.log_ops("[SEQ:STOP] request_normal_stop() ASSTP (stop requested)")
                ctrl.request_normal_stop(source="exp_scheduler")
        except Exception:
            pass

    # ------------------------------------------------------------------ QThread

    def run(self) -> None:
        self._flat_index = 0
        self._current_step_idx = 0
        self._had_error = False
        self._stop_reason = ""
        self._condition_stop = False
        self._last_diagnostic = None
        self._terminal_error_reported = False
        self._cleanup_failures = []
        self._shutter_initial_state = None
        self._shutter_managed = False
        self._shutter_confirmed_in = False
        self._last_verified_temperature_setpoint_k = None
        self._xrd_dark_cache.clear()
        self._xrd_defect_cache.clear()

        # Auto-start file logging for the full sequence run.
        try:
            self._logger.start(
                path=self._log_path,
                devices=self._log_devices,
                sequence_dict=(self._sequence_snapshot if self._sequence_snapshot is not None
                               else self._sequence).to_dict(),
                global_limits_dict=_global_limits_to_dict(self._global_limits),
                data_dir=self._data_dir,
            )
        except Exception as exc:
            self._had_error = True
            message = f"Could not save sequence or start run logging: {exc}"
            self._last_diagnostic = build_runtime_diagnostic("runtime.log_start_failed", message)
            try:
                self._logger.stop()
            except Exception:
                pass
            self.error_occurred.emit(0, message)
            return

        # Record baseline positions for Ch3/4/5 global-limit tracking. A
        # baseline read failure here must not fail-open (an unreadable
        # channel would otherwise stay unprotected for the whole run with no
        # notification — REORGANISATION_PLAN.md Phase 9): abort the run
        # before it starts, the same way a motion-lease acquisition failure
        # does below. This is a third, independent early-abort site — it
        # sits before the try/except _StopRequested block further down, so
        # raising _StopRequested here would go uncaught and escape the
        # QThread unhandled; report and `return` instead, exactly like the
        # motion-lease failure branch.
        ctrl = self._ctx.controller
        if ctrl is not None and self._global_limits is not None:
            for ch in (3, 4, 5):
                try:
                    self._baseline_pos[ch] = int(ctrl.get_ch_pos(ch))
                except Exception as exc:
                    self._had_error = True
                    message = f"Cannot read Ch{ch} baseline position — global limits cannot be enforced: {exc}"
                    self._logger.log_ops(f"[SEQ:ABORT] {message}")
                    self._logger.log_science("error", note=message)
                    self._last_diagnostic = build_controller_diagnostic(
                        "controller.global_limit_baseline_unavailable", message, device="stage",
                    )
                    self._logger.stop()
                    self.error_occurred.emit(0, message)
                    return

        # One motion lease for the whole sequence — acquired before any
        # stage action can run, released once the run truly ends (below).
        # A sequence that never touches the stage still runs fine; it simply
        # never acquires (self._motion_lease stays None and no stage action
        # is ever dispatched, since _do_stage requires ctrl to be present).
        if ctrl is not None:
            try:
                self._motion_lease = ctrl.acquire_motion(
                    owner="Experimental Scheduler", operation="Sequence run",
                )
            except Exception as exc:
                self._had_error = True
                self._logger.log_ops(f"[SEQ:ABORT] Could not acquire stage motion: {exc}")
                self._logger.log_science("error", note=str(exc))
                self._last_diagnostic = build_controller_diagnostic(
                    "controller.motion_lease_acquire_failed", str(exc), device="stage",
                )
                self._logger.stop()
                self.error_occurred.emit(0, str(exc))
                return

        try:
            self._prepare_shutter()
            self._start_camera_session_if_needed()
            self._execute_actions(self.run_plan)
        except _StopRequested:
            pass
        except Exception as exc:
            if self._terminal_error_reported:
                # A concurrent background-thread abort (a Global-limit
                # violation on the follow thread, via _abort_for_global_limit)
                # already reported the terminal error/Diagnostic and set
                # _stop_event — this exception is that same abort's side
                # effect landing here (e.g. the follow thread's normal_stop()
                # revoking the motion lease while _start_camera_session_if_needed()
                # or _execute_actions() was independently mid-call on this
                # thread), not a second, unrelated failure. Log it for
                # investigation without double-reporting error_occurred or
                # overwriting the already-set _last_diagnostic.
                self._logger.log_ops(
                    f"[SEQ:ABORT] Exception after external abort (not re-reported): {exc}"
                )
            else:
                self._had_error = True
                code = getattr(exc, "code", "runtime.unexpected_error")
                self._logger.log_ops(f"[SEQ:ABORT] [{code}] Unhandled error: {exc}")
                self._logger.log_science("error", note=str(exc))
                self._last_diagnostic = build_runtime_diagnostic(code, str(exc))
                self._terminal_error_reported = True
                self.error_occurred.emit(self._flat_index, str(exc))
        finally:
            if self._shutter_managed:
                self._safe_cleanup("shutter insertion", self._insert_shutter)
            # Each step is run through _safe_cleanup() in its own try/except
            # (REORGANISATION_PLAN.md Phase 9 external review): these were
            # previously five back-to-back calls with no isolation between
            # them, so an exception from any one of them (e.g.
            # _cleanup_camera_session()'s VideoCapture.release() failing)
            # would propagate out of run()'s finally block and skip every
            # step after it — most importantly motion-lease release /
            # switch_to_loc, which must always be attempted regardless of
            # what else failed, or the PM16C is left held in REM
            # indefinitely.
            self._safe_cleanup("follow thread cleanup", self._cleanup_follow_thread)
            self._safe_cleanup("camera session cleanup", self._cleanup_camera_session)
            self._safe_cleanup("motion lease release", self._release_motion_lease)
            if self._global_shutter.only_open_during_xrd and self._ctx.shutter is not None:
                self._safe_cleanup("shutter connection close", self._ctx.shutter.close)
            # Round-2 external review finding: _safe_cleanup() previously
            # only logged a cleanup failure — it never fed back into
            # _had_error, so a failed camera release() or motion lease
            # release (silently leaving the PM16C in REM) still ended in
            # sequence_completed and a "Completed" status. Force the run to
            # report as failed, and — unless something else already
            # reported a terminal error, in which case a second dialog for
            # the same run would be confusing — surface it via
            # error_occurred so the user actually sees it, not just ops.log.
            if self._cleanup_failures:
                self._had_error = True
                message = "Cleanup failed after the run: " + "; ".join(self._cleanup_failures)
                self._logger.log_ops(f"[SEQ:CLEANUP] {message}")
                if (not self._terminal_error_reported
                        or any("shutter insertion" in failure for failure in self._cleanup_failures)):
                    self._terminal_error_reported = True
                    self.error_occurred.emit(self._flat_index, message)
            self._safe_cleanup("final outcome log", self._log_final_outcome)
            self._safe_cleanup("logger stop", self._logger.stop)
            if self._shutter_initial_state is not None and self._shutter_confirmed_in:
                self.shutter_restore_available.emit(self._shutter_initial_state)

        if self._had_error:
            return
        if self._stop_event.is_set():
            if self._condition_stop:
                self.sequence_condition_stopped.emit(self._stop_reason)
            else:
                self.sequence_stopped.emit()
        else:
            self.sequence_completed.emit()

    # ------------------------------------------------------------------ run() cleanup helpers

    def _prepare_shutter(self) -> None:
        if not self._global_shutter.only_open_during_xrd:
            return
        shutter = self._ctx.shutter
        if shutter is None:
            raise RunnerError("runtime.shutter_unavailable", "X-ray shutter is not available")
        self._shutter_initial_state = shutter.get_status()
        self._shutter_managed = True
        self._logger.log_ops(f"[SHUTTER] initial position: {'In' if self._shutter_initial_state else 'Out'}")
        self._insert_shutter()

    def _insert_shutter(self) -> None:
        shutter = self._ctx.shutter
        self._shutter_confirmed_in = False
        shutter.set_inserted(True)
        if shutter.get_status() is not True:
            raise RunnerError("runtime.shutter_insert_failed", "Cannot confirm shutter In")
        self._shutter_confirmed_in = True
        self._logger.log_ops("[SHUTTER] In confirmed — X-rays blocked")

    def _open_shutter_for_xrd(self) -> None:
        shutter = self._ctx.shutter
        if shutter.get_status() is not True:
            raise RunnerError("runtime.shutter_changed", "Shutter is not In before XRD")
        self._shutter_confirmed_in = False
        shutter.set_inserted(False)
        if shutter.get_status() is not False:
            raise RunnerError("runtime.shutter_open_failed", "Cannot confirm shutter Out")
        self._logger.log_ops("[SHUTTER] Out confirmed — XRD exposure permitted")

    def _safe_cleanup(self, description: str, fn) -> None:
        """Run one run()-finally cleanup step in isolation.

        See run()'s finally block for why: each step here is independent
        hardware/resource teardown, and one failing must not prevent the
        others from being attempted. The failure is also recorded into
        self._cleanup_failures — round-2 external review finding: logging
        alone let the run still end in sequence_completed even when a
        cleanup step (e.g. camera release() or motion lease release)
        failed; run()'s finally now turns a non-empty list into
        self._had_error after every cleanup step has had its chance to run.
        """
        try:
            fn()
        except Exception as exc:
            message = f"{description} failed: {exc}"
            self._cleanup_failures.append(message)
            try:
                self._logger.log_ops(f"[SEQ:CLEANUP] {message}")
            except Exception:
                pass

    def _release_motion_lease(self) -> None:
        if self._motion_lease is None:
            return
        ctrl = self._ctx.controller
        # Sequence-wide REM/LOC policy: every stage action above stays in
        # REM (stay_in_rem=True throughout) so the PM16C is switched to LOC
        # exactly once here, regardless of whether the run finished,
        # errored, or was stopped. The one exception is normal_stop()/
        # emergency_stop() (Stop button, DSL stop actions, global-limit
        # abort, the per-oscillation decelerate-stop in
        # _return_ch11_to_zero) — those send ASSTP/AESTP+LOC as one atomic
        # transaction in control_stage.py and revoke this lease as a side
        # effect, so by the time we get here it may already be invalid and
        # already in LOC; is_valid() guards against sending LOC on a
        # revoked lease in that case. A genuine failure here (is_valid()
        # was True, so the lease was live, but switch_to_loc() itself still
        # raised — e.g. a communication fault) previously vanished into a
        # bare `except: pass`, leaving the PM16C in REM with nothing to
        # show for it but a successful-looking run (round-2 external
        # review). Record it into _cleanup_failures instead — release_motion()
        # is still attempted right after regardless, same as before.
        if ctrl.coordinator.is_valid(self._motion_lease):
            try:
                ctrl.switch_to_loc(motion=self._motion_lease)
            except Exception as exc:
                message = f"switch_to_loc() failed: {exc}"
                self._cleanup_failures.append(message)
                self._logger.log_ops(f"[SEQ:CLEANUP] {message}")
        ctrl.release_motion(self._motion_lease)
        self._motion_lease = None

    def _log_final_outcome(self) -> None:
        """Write the final result row to conditions.csv before closing files."""
        if self._had_error:
            self._logger.log_ops("[SEQ:ABORT] Sequence aborted due to error")
        elif self._stop_event.is_set():
            reason = self._stop_reason or "Stopped by user"
            self._logger.log_science("stop", note=reason)
            self._logger.log_ops(f"[SEQ:STOP] {reason}")
        else:
            self._logger.log_science("stop", note="Completed successfully")
            self._logger.log_ops("[SEQ:DONE] Sequence completed successfully")

    # ------------------------------------------------------------------ execution loop

    def _execute_actions(self, plan: list[RunStep]) -> None:
        """Execute the loop-unrolled run plan (see run_plan.py). Each step
        runs with its own loop-variable binding, so loop-variable fields are
        resolved exactly as they were when loops were executed recursively."""
        previous_loops = ()
        for step in plan:
            self._check_stop()
            self._log_loop_iterations(previous_loops, step.loops)
            previous_loops = step.loops

            action = step.action
            idx = step.index
            self._flat_index = idx + 1
            self._current_step_idx = idx
            description = step.describe()
            self.step_started_at.emit(idx, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self.step_started.emit(idx, description)
            self._logger.log_ops(f"[STEP #{idx:04d} START] {description}")
            t0 = time.monotonic()
            finished_at_emitted = False
            try:
                self._execute_one(action, step.variables)
            except _StopRequested:
                raise
            except Exception as exc:
                # The one terminal handler for every synchronous step
                # exception (REORGANISATION_PLAN.md Phase 9). Classified
                # safety-rule violations arrive with their own `.code`
                # (RunnerError, raised by the MOVE_CONSTRAINTS pre-check
                # and Ch11 oscillation failures); anything else falls
                # back to a generic code — no other site in this class
                # assigns "runtime.unexpected_error" itself, so there is
                # no risk of it colliding with a real classification.
                elapsed = time.monotonic() - t0
                if self._terminal_error_reported:
                    # A concurrent background-thread abort (a Global-
                    # limit violation on the follow thread, via
                    # _abort_for_global_limit) already reported the
                    # terminal error/Diagnostic and set _stop_event —
                    # this thread's own in-flight hardware call (e.g.
                    # inside _do_stage) failing right afterwards is a
                    # side effect of that same abort (its normal_stop()
                    # revokes the motion lease this thread may be mid-
                    # call on), not a second, unrelated failure. Log it
                    # without double-reporting error_occurred or
                    # overwriting the already-set _last_diagnostic —
                    # found via external review of this Phase's plan,
                    # not exercised by the earlier direct-call test of
                    # _abort_for_global_limit() alone.
                    self._logger.log_ops(
                        f"[STEP #{idx:04d} ERROR] Exception after external "
                        f"abort (not re-reported): {exc}  ({elapsed:.2f} s)"
                    )
                    raise _StopRequested() from exc
                self._had_error = True
                code = getattr(exc, "code", "runtime.unexpected_error")
                self._logger.log_ops(f"[STEP #{idx:04d} ERROR] [{code}] {exc}  ({elapsed:.2f} s)")
                self._logger.log_science("error", step_index=idx, note=str(exc))
                self._last_diagnostic = build_runtime_diagnostic(code, str(exc))
                self.step_finished_at.emit(idx, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                finished_at_emitted = True
                self._terminal_error_reported = True
                self.error_occurred.emit(idx, str(exc))
                raise _StopRequested()
            finally:
                if not finished_at_emitted:
                    self.step_finished_at.emit(idx, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            elapsed = time.monotonic() - t0
            self._logger.log_ops(f"[STEP #{idx:04d} DONE ] {elapsed:.2f} s")
            self.step_completed.emit(idx)

    def _log_loop_iterations(self, previous: tuple, current: tuple) -> None:
        """Write the ops.log `[LOOP]` line for every loop level whose
        iteration begins at this step (same lines as before unrolling)."""
        for depth, level in enumerate(current):
            if (depth < len(previous)
                    and previous[depth].loop_path == level.loop_path
                    and previous[depth].index == level.index):
                continue
            for entered in current[depth:]:
                self._logger.log_ops(
                    f"[LOOP] {entered.var} = {entered.value!r}  "
                    f"(iteration {entered.index + 1}/{entered.count})"
                )
            return

    # ------------------------------------------------------------------ dispatcher

    def _execute_one(self, action: Action, var_context: dict) -> None:  # noqa: C901
        idx = self._current_step_idx

        # ── General ────────────────────────────────────────────────
        if isinstance(action, WaitAction):
            self._do_wait(action.duration_s)

        elif isinstance(action, LogAction):
            # action.message may contain str.format()-style "{varname}"
            # placeholders written as an f-string in the DSL (e.g.
            # f"p={p}") — dsl/parser.py::_eval_fstring() only validates
            # that varname is a bound for-loop variable at compile time and
            # preserves the placeholder verbatim; substituting the current
            # iteration's value is this layer's job. A message with no
            # placeholders (or one built via Visual/JSON, bypassing the
            # DSL compiler entirely) round-trips through .format() unchanged.
            try:
                message = action.message.format(**var_context)
            except (KeyError, IndexError, ValueError):
                message = action.message
            self.progress_updated.emit(f"[LOG] {message}")
            self._logger.log_science("user_log", step_index=idx, note=message)

        # ── Stage ──────────────────────────────────────────────────
        elif isinstance(action, StageAction):
            self._do_stage(action, var_context)

        elif isinstance(action, MicroscopeOutFpdInAction):
            if self._follow_thread is not None and self._follow_thread.is_alive():
                raise RuntimeError(
                    "microscope_out_and_fpd_in: バックグラウンド追従スレッドが停止していません。"
                    " stop_following() を先に実行してください。"
                )
            stage_settings = (self._global_positions.to_stage_settings()
                              if self._global_positions is not None else load_stage_settings())
            steps = action.to_steps(stage_settings)
            self._logger.log_ops(
                f"[STAGE] microscope_out_and_fpd_in → {len(steps)} stage steps"
            )
            for step in steps:
                self._check_stop()
                self._do_stage(step, var_context)

        elif isinstance(action, FpdOutMicroscopeInAction):
            stage_settings = (self._global_positions.to_stage_settings()
                              if self._global_positions is not None else load_stage_settings())
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

        elif isinstance(action, SetAndWaitPressureAction):
            self._do_set_pressure(action.to_set_action(), var_context)
            self._do_wait_pressure(action.to_wait_action(), idx)

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

        elif isinstance(action, CompressToTargetPressureUsingRubyAction):
            self._do_compress_to_target_pressure_using_ruby(action, idx)

        elif isinstance(action, TakeSpectrumAction):
            self._do_take_spectrum(action, idx)

        # ── Camera ─────────────────────────────────────────────────
        elif isinstance(action, SaveReferenceImageAction):
            self._do_save_reference(action)

        elif isinstance(action, SaveSnapshotAction):
            self._do_save_snapshot(action)

        elif isinstance(action, StartFollowingAction):
            self._logger.log_ops(
                f"[CAMERA] start_following(interval={action.interval_s}s, "
                f"threshold={action.similarity_threshold}, "
                f"max_corr={action.max_correction_per_step_um}µm)"
            )
            self._require_microscope_mode_for_camera("start_following")
            self._start_follow(action)

        elif isinstance(action, StopFollowingAction):
            self._logger.log_ops("[CAMERA] stop_following()")
            self._stop_follow()

        elif isinstance(action, FollowSampleAction):
            # Use to_steps() rather than reconstructing StartFollowingAction
            # by hand — a hand-written field list here previously omitted
            # autofocus_range_um/autofocus_steps, silently dropping any
            # per-step autofocus override at the exact moment it would have
            # taken effect (compiling/round-tripping the Action was fine;
            # only actually running it lost the override).
            start_act, _wait_act, _stop_act = action.to_steps()
            self._logger.log_ops(
                f"[CAMERA] follow_sample_position(duration={action.duration_s}s)"
            )
            self._require_microscope_mode_for_camera("follow_sample_position")
            self._start_follow(start_act)
            self._do_wait(action.duration_s)
            self._stop_follow()

        elif isinstance(action, AlignToReferenceAction):
            self._logger.log_ops(
                f"[CAMERA] align_to_reference(threshold={action.similarity_threshold}, "
                f"max_corr={action.max_correction_per_step_um}µm, "
                f"af={action.autofocus_enabled})"
            )
            self._require_microscope_mode_for_camera("align_to_reference")
            self._do_align_to_reference(action)

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

    def _resume_motion_after_self_stop(self) -> None:
        """Re-acquire self._motion_lease after this runner sends its own
        ASSTP/AESTP on the lease it already holds.

        MotionCoordinator.revoke_for_stop() is owner-agnostic: it invalidates
        whichever lease is currently HELD, ours included, the moment we call
        ctrl.normal_stop()/emergency_stop() — and that exact lease can never
        become valid again (see utils/stage/motion_coordinator.py). Call this
        right after any such self-triggered stop that the sequence is
        expected to continue past (e.g. the normal_stop()/emergency_stop()
        DSL primitives, or decelerating Ch11 before driving it back to zero).

        Not for request_stop()/request_emergency_stop() (the Stop button
        path): there _stop_event is already set and the resulting
        MotionRevokedError is the intended abort signal, which is why this
        checks it first rather than silently reacquiring and continuing.
        """
        self._check_stop()
        ctrl = self._ctx.controller
        if ctrl is None or self._motion_lease is None:
            return
        ctrl.release_motion(self._motion_lease)
        self._motion_lease = ctrl.acquire_motion(
            owner="Experimental Scheduler", operation="Sequence run",
        )

    # ------------------------------------------------------------------ stage

    def _do_stage(self, action: StageAction, var_context: dict) -> None:
        if action.ch in (3, 4, 5) and action.operation in (
            "move_absolute", "move_relative", "set_speed",
        ):
            with self._limited_stage_motion_lock:
                self._do_stage_impl(action, var_context)
        else:
            self._do_stage_impl(action, var_context)

    def _do_stage_impl(self, action: StageAction, var_context: dict) -> None:
        ctrl = self._ctx.controller
        op = action.operation
        motion = self._motion_lease

        if op == "emergency_stop":
            self._logger.log_ops("[STAGE] AESTP (emergency stop all)")
            ctrl.emergency_stop(source="exp_scheduler")
            self._resume_motion_after_self_stop()
            return

        if op == "normal_stop":
            self._logger.log_ops("[STAGE] ASSTP (normal stop — decelerate)")
            ctrl.normal_stop(source="exp_scheduler")
            self._resume_motion_after_self_stop()
            return

        if op == "set_speed":
            speed = action.speed or "M"
            self._logger.log_ops(f"[STAGE] set_speed Ch{action.ch} → {speed}")
            ctrl.set_ch_speed(action.ch, speed, stay_in_rem=True, motion=motion)
            return

        value = action.value
        if isinstance(value, str):
            value = var_context.get(value, 0)
        value = int(value)

        if op not in ("move_absolute", "move_relative"):
            raise ValueError(f"Unknown stage operation: {op!r}")

        # Compute the prospective target position for EVERY channel (not
        # just Ch3/4/5) so MOVE_CONSTRAINTS (Ch8/Ch9 collision, Ch11) can be
        # checked before the move is sent, mirroring the pre-check Ch11
        # oscillation already does (_do_take_xrd). Previously this was only
        # computed inside an `if action.ch in (3, 4, 5)` block for Global
        # limits, leaving check_move_constraints() never called at this
        # layer for an ordinary move — control_stage.py's
        # move_ch_absolute()/move_ch_relative() still enforced it internally
        # (one atomic wire transaction), so no collision was ever actually
        # possible, but the runtime layer itself was a no-op for this rule
        # — REORGANISATION_PLAN.md Phase 9.
        if op == "move_absolute":
            target_pos = value
        else:
            try:
                target_pos = int(ctrl.get_ch_pos(action.ch)) + value
            except Exception as exc:
                # Fail-closed: a relative move whose target can't even be
                # computed must not be sent blind (Phase 9 — this used to
                # silently skip the Global-limit pre-check via
                # target_pos=None instead of blocking the move).
                raise RunnerError(
                    "runtime.position_unreadable",
                    f"Cannot read Ch{action.ch} position — relative move "
                    f"blocked (required for safety checks): {exc}",
                ) from exc

        ok, message = ctrl.check_move_constraints(action.ch, target_pos)
        if not ok:
            raise RunnerError("runtime.move_constraint_violation", message)

        if action.ch in (3, 4, 5):
            self._check_global_limits_before_move(action.ch, target_pos)

        if op == "move_absolute":
            if action.speed:
                ctrl.set_ch_speed(action.ch, action.speed, stay_in_rem=True, motion=motion)
            self._logger.log_ops(
                f"[STAGE] ABS Ch{action.ch} → {value:+d}  speed={action.speed or 'M'}"
            )
            ctrl.move_ch_absolute(action.ch, value, motion=motion)
        else:
            if action.speed:
                ctrl.set_ch_speed(action.ch, action.speed, stay_in_rem=True, motion=motion)
            self._logger.log_ops(
                f"[STAGE] REL Ch{action.ch} Δ{value:+d}  speed={action.speed or 'M'}"
            )
            ctrl.move_ch_relative(action.ch, value, motion=motion)

        # Sequence-wide REM/LOC policy: stay in REM for the whole run — a
        # single switch_to_loc() is sent once, in run()'s finally, after the
        # sequence truly ends. See run()'s finally block for the rationale.
        ctrl.wait_until_stop(
            motion=self._motion_lease, should_stop=lambda: self._stop_event.is_set(),
            stay_in_rem=True,
        )
        self._check_stop()

        try:
            pos = int(ctrl.get_ch_pos(action.ch))
            self._logger.log_ops(f"[STAGE] Ch{action.ch} stopped at {pos:+d}")
        except Exception:
            pass

        # Check global limits after any Ch3/4/5 move
        if action.ch in (3, 4, 5):
            self._check_global_limits()

    # ------------------------------------------------------------------ global limits

    def _limits_for_ch(self, ch: int) -> tuple[float | None, float | None] | None:
        return global_limits_for_channel(self._global_limits, ch)

    def _check_global_limits_before_move(self, ch: int, target_pos: int) -> None:
        """Check a prospective Ch3/4/5 target position (pulses) against
        GlobalLimits *before* the move is sent to the controller.

        _check_global_limits() below only catches a violation after
        wait_until_stop() returns — too late for a single move whose delta
        alone overshoots the limit, since by then the stage has already
        completed it. This blocks the move outright, mirroring how
        MOVE_CONSTRAINTS is checked before Ch11 oscillation moves.
        """
        if ch not in (3, 4, 5):
            return
        limits = self._limits_for_ch(ch)
        if limits is None:
            return
        minus_mm, plus_mm = limits
        baseline = self._baseline_pos.get(ch)
        if baseline is None:
            return
        delta_mm = global_limit_delta_mm(target_pos, baseline, PULSE_SCALE[ch])
        exceeded = exceeded_global_limit(delta_mm, minus_mm, plus_mm)

        if exceeded == "plus":
            self._trigger_global_limit_exceeded(
                ch, delta_mm, f"+{plus_mm:.3f} mm", moving=False,
            )
        if exceeded == "minus":
            self._trigger_global_limit_exceeded(
                ch, delta_mm, f"-{minus_mm:.3f} mm", moving=False,
            )

    def _check_global_limits(self) -> None:
        """Check Ch3/4/5 positions against GlobalLimits. Raises _StopRequested on violation.

        Kept as a post-move safety net (e.g. for follow-sample corrections,
        which apply their own pre-move clamping instead of this per-move
        gate) — _check_global_limits_before_move() is the primary guard for
        ordinary StageAction moves.
        """
        gl = self._global_limits
        if gl is None or not self._baseline_pos:
            return
        ctrl = self._ctx.controller
        if ctrl is None:
            return

        for ch in (3, 4, 5):
            limits = self._limits_for_ch(ch)
            if limits is None:
                continue
            minus_mm, plus_mm = limits
            baseline = self._baseline_pos.get(ch)
            if baseline is None:
                continue
            try:
                current = int(ctrl.get_ch_pos(ch))
            except Exception:
                # Fail-closed (Phase 9): a position that can't be read can't
                # be judged safe, so this must not just skip this cycle's
                # check silently — abort the same way an actual violation
                # would (_trigger_global_limit_position_unreadable always
                # raises _StopRequested, so control never reaches below).
                self._trigger_global_limit_position_unreadable(ch, moving=True)
            delta_mm = global_limit_delta_mm(current, baseline, PULSE_SCALE[ch])
            exceeded = exceeded_global_limit(delta_mm, minus_mm, plus_mm)

            if exceeded == "plus":
                self._trigger_global_limit_exceeded(ch, delta_mm, f"+{plus_mm:.3f} mm")
            if exceeded == "minus":
                self._trigger_global_limit_exceeded(ch, delta_mm, f"-{minus_mm:.3f} mm")

    def _abort_for_global_limit(self, code: str, message: str, *, moving: bool) -> None:
        """The one self-contained report+stop point for any Global-limit-
        related abort — callable identically from the main run thread
        (_do_stage/_check_global_limits) and the background follow thread
        (_follow_loop); see REORGANISATION_PLAN.md Phase 9. This is a
        deliberate exception to _execute_actions()'s single terminal
        exception handler: _follow_loop() (background thread) also calls
        into this, and _execute_actions() only ever sees exceptions raised
        on the main run thread, so routing Global-limit aborts through it
        exclusively would lose the report whenever a follow-thread
        violation is what triggered it.

        moving=True (the default) also sends ASSTP to decelerate-stop all
        motors — used by the post-move safety net and the follow thread,
        where a move may already be in flight. moving=False is for the
        pre-move gate (_check_global_limits_before_move()), where the move
        was never sent — nothing is in motion, so there is nothing to stop.

        Sets both _stop_event and _follow_stop_event — the same pairing
        already used by request_stop()/request_emergency_stop() — so that a
        violation detected on EITHER thread reliably aborts the whole
        sequence, not just whichever thread happened to detect it.
        _check_stop() (used throughout _execute_actions()/_do_stage()) only
        looks at _stop_event; without also setting it here, a follow-thread
        violation would stop the follow thread but let the main run thread
        continue executing later steps — a High-severity gap found in
        external review of this Phase's plan, confirmed against
        _check_stop()'s actual implementation before being fixed here.
        run()'s finally block checks self._had_error before self._stop_event
        when deciding sequence_completed/sequence_stopped, so setting
        _stop_event here is never misclassified as a plain user stop.
        """
        if moving:
            ctrl = self._ctx.controller
            self._logger.log_ops("[STAGE] normal_stop() ASSTP — global limit violation")
            try:
                ctrl.normal_stop(source="exp_scheduler")   # ASSTP — decelerate-stop all motors
            except Exception as exc:
                # The Global-limit Diagnostic below remains the primary,
                # reported cause — but a failed stop-confirmation here is
                # itself important investigation context (the stage may
                # still be moving) and must not be silently dropped.
                self._logger.log_ops(
                    f"[STAGE] normal_stop() failed during global-limit abort: {exc}"
                )
        self._stop_event.set()
        self._follow_stop_event.set()
        self._logger.log_ops(f"[LIMIT ERROR] [{code}] {message}")
        self._logger.log_science(
            "error", step_index=self._current_step_idx, note=message
        )
        self._had_error = True
        self._terminal_error_reported = True
        self._last_diagnostic = build_runtime_diagnostic(code, message, device="stage")
        # self._current_step_idx (not self._flat_index, which is already
        # incremented past the currently-executing step — an off-by-one bug
        # for the main-thread case fixed here). When this is reached from
        # the follow thread, self._current_step_idx is whichever step the
        # MAIN thread happens to be executing at that moment — not
        # necessarily the step that started the follow session. Phase 9
        # does not implement tracking the true originating step for that
        # case (would require threading an index through the follow-session
        # start path); this is a documented limitation, not a claim that the
        # index always identifies the violating action.
        self.error_occurred.emit(self._current_step_idx, message)
        raise _StopRequested()

    def _abort_follow_thread(self, code: str, message: str) -> None:
        """Immediately abort the whole run for a follow-thread-related
        terminal failure — either an exception `_follow_loop()` itself
        raised, or `_stop_follow()` timing out waiting for it to exit
        (REORGANISATION_PLAN.md Phase 9 external review, round 2).

        Both cases mean a background thread that may still be driving
        Ch3/4/5 is no longer trustworthy, so — like
        `_abort_for_global_limit(moving=True)` — this sends ASSTP to
        physically stop the stage first, then sets both `_stop_event` and
        `_follow_stop_event` so `_check_stop()` (used throughout
        `_execute_actions()`/`_do_wait()`/etc.) aborts the main run thread
        at its very next check, instead of only failing later when an
        explicit `stop_following()` step happens to run. Without this, any
        action between a follow failure and the next `stop_following()`
        call (or, if the sequence has no matching `stop_following()` at
        all, every action to the end of the sequence) would keep executing
        as if following were still healthy.

        Unlike `_abort_for_global_limit()`, this does not raise
        `_StopRequested()` — it is called from `_follow_loop()` itself
        (a background thread with no useful caller to unwind to) and from
        `_stop_follow()` (which raises its own `RunnerError` at its own
        call site right after calling this, so the exact `_execute_actions()`
        step raising is preserved). Idempotent: if a previous call already
        reported the terminal error (`_terminal_error_reported`), this
        still repeats the physical stage stop (harmless, and a genuine
        second chance if the earlier one itself failed) but does not emit a
        second error_occurred/Diagnostic for what is fundamentally the same
        failure.
        """
        self._set_gasket_preview_enabled(False)
        ctrl = self._ctx.controller
        if ctrl is not None:
            self._logger.log_ops(f"[STAGE] normal_stop() ASSTP — {code}")
            try:
                ctrl.normal_stop(source="exp_scheduler")
            except Exception as exc:
                self._logger.log_ops(
                    f"[STAGE] normal_stop() failed during follow-thread abort: {exc}"
                )
        self._stop_event.set()
        self._follow_stop_event.set()
        if self._terminal_error_reported:
            return
        self._logger.log_ops(f"[FOLLOW ERROR] [{code}] {message}")
        self._logger.log_science(
            "error", step_index=self._current_step_idx, note=message
        )
        self._had_error = True
        self._terminal_error_reported = True
        self._last_diagnostic = build_runtime_diagnostic(code, message, device="stage")
        self.error_occurred.emit(self._current_step_idx, message)

    def _trigger_global_limit_exceeded(
        self, ch: int, delta_mm: float, limit_str: str, moving: bool = True
    ) -> None:
        message = (
            f"Global limit exceeded on Ch{ch}: {delta_mm:+.3f} mm "
            f"(limit {limit_str} from sequence-start position)"
        )
        self._abort_for_global_limit("runtime.global_limit_exceeded", message, moving=moving)

    def _trigger_global_limit_position_unreadable(self, ch: int, *, moving: bool = True) -> None:
        message = f"Cannot read Ch{ch} position — global limit cannot be enforced"
        self._abort_for_global_limit(
            "runtime.global_limit_position_unreadable", message, moving=moving,
        )

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

        def _on_target_send() -> None:
            self._logger.log_ops(
                f"[PACE5000] set_target_pressure({pressure_mpa:.3f} MPa)"
            )

        def _on_target_verified(actual_mpa: float) -> None:
            # Fires only once the device's own readback confirms the
            # setpoint — a silently-clamped/rejected target now raises
            # instead of this ever being logged.
            self._logger.log_ops(
                f"[PACE5000] target pressure verified → {actual_mpa:.3f} MPa"
            )
            self.progress_updated.emit(
                f"Pressure target verified → {pressure} {action.unit} ({actual_mpa:.3f} MPa)"
            )

        backend.set_pressure_with_ramp(
            pressure_mpa, rate_mpa_per_min,
            on_slew_send=_on_slew_send, on_slew_verified=_on_slew_verified,
            on_target_send=_on_target_send, on_target_verified=_on_target_verified,
        )

    def _do_wait_pressure(
        self, action: WaitPressureAction, step_index: int,
        *, target_mpa_override: float | None = None,
    ) -> None:
        backend = self._ctx.pace5000
        tol_mpa = action.tol * PRESSURE_UNIT_TO_MPA.get(action.unit, 1.0)

        if target_mpa_override is None:
            raw = backend.get_target_pressure()
            if raw is None:
                raise RuntimeError("Cannot read PACE5000 target pressure")
            target_mpa = float(raw)
        else:
            target_mpa = target_mpa_override

        self._logger.log_ops(
            f"[PACE5000] wait_pressure(target={target_mpa:.3f} MPa, tol={tol_mpa:.4f} MPa)"
        )
        self.progress_updated.emit(
            f"Waiting for pressure in {target_mpa - tol_mpa:.3f}–{target_mpa + tol_mpa:.3f} MPa"
        )

        # Progression here is dwell-time based, matching wait_for_pressure()'s
        # own reached/not-reached criterion: entering the arrival band isn't
        # "reached" by itself, the reading also has to hold inside it for
        # dwell_s (Pace5000Backend.DEFAULT_STABILITY_DWELL_S) continuously —
        # surface that countdown so a step that's sitting just inside the
        # band doesn't read as stalled.
        def _on_update(
            current_mpa: float, target_mpa: float, band_elapsed_s: float, dwell_s: float,
        ) -> None:
            stability_str = f"  stable {band_elapsed_s:.1f}/{dwell_s:.0f}s" if band_elapsed_s > 0 else ""
            self.progress_updated.emit(
                f"Pressure: {current_mpa:.4f} MPa "
                f"(band {target_mpa - tol_mpa:.3f}–{target_mpa + tol_mpa:.3f}){stability_str}"
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

        ramp_deadline = time.monotonic() + _TEMPERATURE_COMMAND_VERIFY_TIMEOUT_S
        while True:
            self._check_stop()
            enabled, actual_rate = backend.get_ramp_parameter()
            if enabled and abs(actual_rate - action.ramp_rate) <= 0.001:
                self._logger.log_ops(
                    f"[LAKESHORE] ramp rate verified → {actual_rate:.4f} K/min"
                )
                self.progress_updated.emit(
                    f"Ramp rate verified → {actual_rate:.4f} K/min"
                )
                break
            remaining = ramp_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"LakeShore ramp rate verification failed after "
                    f"{_TEMPERATURE_COMMAND_VERIFY_TIMEOUT_S:g} s: "
                    f"sent {action.ramp_rate:.4f} K/min, "
                    f"device reports {actual_rate:.4f} K/min (ramp enabled={enabled})"
                )
            self._do_wait(min(0.2, remaining))

        # SETP? reports the live setpoint, which only reaches value_k at the
        # end of the ramp. Accept it once it equals value_k, or while the
        # device reports ramping and it lies between the pre-write live
        # value and value_k.
        start_live, _ = _lakeshore_live_setpoint(backend)
        lo, hi = sorted((start_live, value_k))
        self._logger.log_ops(f"[LAKESHORE] set_setpoint({value_k:.2f} K)")
        backend.set_setpoint(value_k)
        setpoint_deadline = time.monotonic() + _TEMPERATURE_COMMAND_VERIFY_TIMEOUT_S
        while True:
            self._check_stop()
            live, ramping = _lakeshore_live_setpoint(backend)
            if math.isfinite(live):
                if math.isclose(live, value_k, rel_tol=0.0, abs_tol=0.01):
                    self._logger.log_ops(f"[LAKESHORE] setpoint verified → {live:.2f} K")
                    break
                if ramping is not False and lo - 0.01 <= live <= hi + 0.01:
                    self._logger.log_ops(
                        f"[LAKESHORE] setpoint verified → ramping to {value_k:.2f} K "
                        f"(live {live:.2f} K, RAMPST={'?' if ramping is None else 1})"
                    )
                    break
            remaining = setpoint_deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerError(
                    "runtime.temperature_setpoint_mismatch",
                    f"LakeShore setpoint verification failed after "
                    f"{_TEMPERATURE_COMMAND_VERIFY_TIMEOUT_S:g} s: "
                    f"requested {value_k:.4f} K, device reports live setpoint "
                    f"{live!r} K (ramping={ramping})",
                )
            self._do_wait(min(0.2, remaining))
        self._last_verified_temperature_setpoint_k = value_k
        self.progress_updated.emit(
            f"Temperature setpoint → {value_k:.2f} K (ramp {action.ramp_rate} K/min)"
        )

    def _do_wait_temperature(self, action: WaitTemperatureAction, step_index: int) -> None:
        backend = self._ctx.lakeshore
        setpoint_k = backend.get_setpoint()
        if not math.isfinite(setpoint_k):
            raise RunnerError("runtime.temperature_setpoint_invalid", "LakeShore setpoint is not finite")
        verified_target = self._last_verified_temperature_setpoint_k
        if (verified_target is not None
                and not math.isclose(setpoint_k, verified_target, rel_tol=0.0, abs_tol=0.01)):
            raise RunnerError(
                "runtime.temperature_setpoint_changed",
                f"LakeShore setpoint changed since verification: expected "
                f"{verified_target:.4f} K, device reports {setpoint_k!r} K",
            )
        if verified_target is not None:
            setpoint_k = verified_target
        self._logger.log_ops(
            f"[LAKESHORE] wait_temperature(channel=B, target={setpoint_k:.2f} K, tol={action.tol_k:.2f} K)"
        )
        self.progress_updated.emit(
            f"Waiting for temperature B {setpoint_k:.2f} K ±{action.tol_k:.2f} K"
        )
        # The backend poll re-reads the setpoint every cycle; watch its cached
        # final target (no GPIB I/O here) so a setpoint changed elsewhere
        # during the wait cannot leave this loop comparing against a stale
        # target.
        get_cached = getattr(backend, "get_cached_readings", None)
        while True:
            self._check_stop()
            if get_cached is not None:
                cached_target = get_cached().get("target_setpoint_k")
                if (cached_target is not None
                        and not math.isclose(cached_target, setpoint_k, rel_tol=0.0, abs_tol=0.01)):
                    if verified_target is not None:
                        raise RunnerError(
                            "runtime.temperature_setpoint_changed",
                            f"LakeShore setpoint changed during wait_temperature: expected "
                            f"{setpoint_k:.4f} K, device reports {cached_target!r} K",
                        )
                    self._logger.log_ops(
                        f"[LAKESHORE] setpoint changed during wait: "
                        f"{setpoint_k:.2f} K → {cached_target:.2f} K"
                    )
                    setpoint_k = cached_target
            data = backend.get_data()
            if data:
                current_k = data[-1].temp_b_k
                # The live (ramping) setpoint must also have reached the
                # target, so T_B passing through the band mid-ramp is not
                # taken as arrival.
                live_sp = getattr(data[-1], "eff_setpoint_k", None)
                ramp_done = live_sp is None or math.isclose(
                    live_sp, setpoint_k, rel_tol=0.0, abs_tol=0.01)
                if ramp_done and abs(current_k - setpoint_k) <= action.tol_k:
                    self._logger.log_ops(
                        f"[LAKESHORE] temperature B reached: {current_k:.2f} K"
                    )
                    self._logger.log_science(
                        "temperature_reached", step_index=step_index,
                        note=f"T_B={current_k:.2f} K (target {setpoint_k:.2f} ±{action.tol_k:.2f})",
                    )
                    self.progress_updated.emit(
                        f"Temperature B reached: {current_k:.2f} K"
                    )
                    break
                ramp_note = "" if ramp_done else f", ramping setpoint {live_sp:.2f} K"
                self.progress_updated.emit(
                    f"Temperature B: {current_k:.2f} K "
                    f"(target {setpoint_k:.2f} ±{action.tol_k:.2f}{ramp_note})"
                )
            time.sleep(0.2)

    # ------------------------------------------------------------------ radicon

    def _check_compression_pace_state(
        self, expected_mpa: float, tolerance_mpa: float, maximum_mpa: float,
        *, require_arrival: bool = True,
    ) -> float:
        """Fail closed if another PACE caller changed state or pressure drifted.

        Every read is non-mutating. Read the unit again at the end so a
        concurrent manual unit change cannot silently reinterpret values.
        """
        backend = self._ctx.pace5000
        try:
            unit_before = str(backend.query(":UNIT:PRES?")).strip().upper()
            state = str(backend.get_output_state()).strip().upper()
            raw_target = backend.get_target_pressure()
            raw_pressure = backend.get_pressure()
            raw_source = backend.get_positive_source_pressure()
            unit_after = str(backend.query(":UNIT:PRES?")).strip().upper()
            if unit_before not in ("MPA", "BAR") or unit_after != unit_before:
                raise ValueError("pressure unit changed or could not be read")
            factor = 1.0 if unit_before == "MPA" else 0.1
            target_mpa = float(raw_target) * factor
            pressure_mpa = float(raw_pressure) * factor
            source_mpa = float(raw_source) * factor
        except Exception as exc:
            raise RunnerError(
                "runtime.compression_pace_state_unreadable",
                f"Cannot verify PACE5000 pressure state: {exc}",
            ) from exc
        if not all(math.isfinite(value) for value in (
            target_mpa, pressure_mpa, source_mpa,
        )):
            raise RunnerError(
                "runtime.compression_pace_state_unreadable",
                "PACE5000 returned a non-finite pressure or source reading",
            )
        if state not in ("1", "ON"):
            raise RunnerError(
                "runtime.compression_control_mode_changed",
                "PACE5000 left Control mode during ruby-guided compression",
            )
        if abs(target_mpa - expected_mpa) > 1e-5:
            raise RunnerError(
                "runtime.compression_setpoint_changed",
                f"PACE5000 setpoint changed unexpectedly: expected {expected_mpa:.6f} MPa, "
                f"read {target_mpa:.6f} MPa",
            )
        if source_mpa <= maximum_mpa:
            raise RunnerError(
                "runtime.compression_source_pressure_low",
                f"PACE5000 +ve source pressure {source_mpa:.4f} MPa must exceed the "
                f"allowed maximum gas pressure {maximum_mpa:.4f} MPa",
            )
        if (pressure_mpa > maximum_mpa + 1e-5
                or (require_arrival
                    and abs(pressure_mpa - expected_mpa) > tolerance_mpa)):
            raise RunnerError(
                "runtime.compression_gas_pressure_out_of_band",
                f"PACE5000 measured gas pressure {pressure_mpa:.4f} MPa is outside "
                f"the expected band around {expected_mpa:.4f} MPa",
            )
        return pressure_mpa

    def _do_compress_to_target_pressure_using_ruby(
        self, action: CompressToTargetPressureUsingRubyAction, step_index: int,
    ) -> None:
        error = compression.parameters_error(action)
        if error:
            raise RunnerError("runtime.compression_parameters_invalid", error)
        if not self._global_spectrum.pressure_enabled:
            raise RunnerError(
                "runtime.compression_pressure_disabled",
                "Enable Ruby Fluorescence Settings > Pressure Calculation before compression",
            )
        backend = self._ctx.pace5000
        if backend is None or not backend.connected:
            raise RunnerError("runtime.compression_pace_missing", "PACE5000 is not connected")
        if str(backend.get_output_state()).strip().upper() not in ("1", "ON"):
            raise RunnerError(
                "runtime.compression_control_mode_required",
                "PACE5000 must be in Control mode before compression",
            )
        raw_unit = backend.query(":UNIT:PRES?")
        active_unit = str(raw_unit).strip().upper()
        if active_unit not in ("MPA", "BAR"):
            raise RunnerError("runtime.compression_unit_unreadable", "Cannot read PACE5000 pressure unit")
        active_factor = 1.0 if active_unit == "MPA" else 0.1
        try:
            current_mpa = float(backend.get_target_pressure()) * active_factor
            if action.gas_pressure_rate is None:
                rate_mpa_per_min = float(backend.get_slew_rate()) * active_factor * 60.0
            else:
                rate_mpa_per_min = (
                    action.gas_pressure_rate
                    * compression.RATE_TO_MPA_PER_MIN[action.gas_pressure_rate_unit]
                )
        except (TypeError, ValueError, OverflowError) as exc:
            raise RunnerError(
                "runtime.compression_pace_unreadable",
                "Cannot read PACE5000 setpoint or slew rate",
            ) from exc
        maximum_mpa = (action.max_allowed_gas_pressure
                       * compression.UNIT_TO_MPA[action.gas_pressure_unit])
        increment_mpa = (action.gas_pressure_increment
                         * compression.UNIT_TO_MPA[action.gas_pressure_unit])
        tolerance_mpa = (action.gas_pressure_tolerance
                         * compression.UNIT_TO_MPA[action.gas_pressure_unit])
        if (not all(math.isfinite(value) for value in (current_mpa, rate_mpa_per_min))
                or current_mpa < 0
                or rate_mpa_per_min / 60 < MIN_SLEW_RATE_MPA_PER_SEC):
            raise RunnerError(
                "runtime.compression_pace_unreadable",
                "PACE5000 setpoint and slew rate must be finite; slew rate must meet the device minimum",
            )
        if current_mpa > maximum_mpa + 1e-9:
            raise RunnerError(
                "runtime.compression_above_maximum",
                "Current PACE5000 setpoint already exceeds the maximum allowed gas pressure",
            )
        self._check_compression_pace_state(
            current_mpa, tolerance_mpa, maximum_mpa, require_arrival=False,
        )
        # Confirm an initially stable gas pressure before the first ruby
        # spectrum, even when no increment will ultimately be needed.
        self._do_wait_pressure(
            WaitPressureAction(tol=tolerance_mpa, unit="MPa"), step_index,
            target_mpa_override=current_mpa,
        )
        self._check_compression_pace_state(current_mpa, tolerance_mpa, maximum_mpa)

        # The normal ruby action owns image alignment, nearby search and
        # exposure/gain retries. Compression keeps the stage at ruby while
        # gas pressure changes; before each later acquisition it returns to
        # the recorded XRD departure, aligns the fresh image, then follows
        # the selected ruby offset again.
        spectrum_action = TakeSpectrumAction.from_dict(action.to_dict())
        self._last_spectrum_exposure_s = None
        self._last_spectrum_xrd_position = None
        follow_action = (
            self._current_follow_action
            if self._follow_thread is not None and self._follow_thread.is_alive()
            else None
        )
        def measure() -> float:
            nonlocal spectrum_action
            record = self._do_take_spectrum(
                spectrum_action, step_index, leave_at_ruby=True,
            )
            if (record is None or record.status != "valid"
                    or record.pressure_gpa is None
                    or not math.isfinite(record.pressure_gpa)):
                raise RunnerError(
                    "runtime.compression_pressure_invalid",
                    "Ruby measurement did not yield a valid sample pressure",
                )
            accepted_exposure = getattr(self, "_last_spectrum_exposure_s", None)
            if accepted_exposure is not None:
                spectrum_action = replace(spectrum_action, exposure_time_s=accepted_exposure)
            return record.pressure_gpa

        try:
            sample_pressure_gpa = measure()
            self._check_compression_pace_state(current_mpa, tolerance_mpa, maximum_mpa)
        except _StopRequested:
            raise
        except Exception as exc:
            xrd_on_failure = getattr(self, "_last_spectrum_xrd_position", None)
            if xrd_on_failure is not None and not self._stop_event.is_set():
                try:
                    self._return_to_xrd_position(
                        self._ctx.controller, xrd_on_failure, self._global_spectrum.speed,
                    )
                except Exception as return_exc:
                    raise RunnerError(
                        "runtime.compression_failed_and_return_failed",
                        f"Ruby measurement failed ({exc}); returning to XRD also failed ({return_exc})",
                    ) from exc
            raise
        xrd_position = self._last_spectrum_xrd_position
        self._logger.log_ops(
            f"[COMPRESSION] initial ruby pressure={sample_pressure_gpa:.4f} GPa; "
            f"PACE5000 setpoint={current_mpa:.4f} MPa"
        )
        try:
            while sample_pressure_gpa < action.target_sample_pressure_gpa:
                self._check_stop()
                self._check_compression_pace_state(
                    current_mpa, tolerance_mpa, maximum_mpa,
                )
                next_mpa = compression.next_setpoint_mpa(
                    current_mpa, increment_mpa, maximum_mpa,
                )
                if next_mpa is None:
                    raise RunnerError(
                        "runtime.compression_target_not_reached",
                        f"Ruby pressure {sample_pressure_gpa:.4f} GPa did not reach "
                        f"{action.target_sample_pressure_gpa:g} GPa at the maximum "
                        f"allowed gas pressure {maximum_mpa:g} MPa",
                    )
                self._logger.log_ops(
                    f"[COMPRESSION] PACE5000 {current_mpa:.4f} → {next_mpa:.4f} MPa"
                )
                self._do_set_pressure(SetPressureAction(
                    pressure=next_mpa, unit="MPa", rate=rate_mpa_per_min,
                    rate_unit="MPa/min",
                ), {})
                self._do_wait_pressure(WaitPressureAction(
                    tol=tolerance_mpa, unit="MPa",
                ), step_index)
                current_mpa = next_mpa
                self._check_compression_pace_state(
                    current_mpa, tolerance_mpa, maximum_mpa,
                )
                self._do_wait(action.stabilisation_duration_s)
                self._check_compression_pace_state(
                    current_mpa, tolerance_mpa, maximum_mpa,
                )
                self._return_to_xrd_position(
                    self._ctx.controller, xrd_position, self._global_spectrum.speed,
                )
                sample_pressure_gpa = measure()
                self._check_compression_pace_state(
                    current_mpa, tolerance_mpa, maximum_mpa,
                )
                self._logger.log_ops(
                    f"[COMPRESSION] ruby pressure={sample_pressure_gpa:.4f} GPa "
                    f"at PACE5000 {current_mpa:.4f} MPa"
                )
            self._return_to_xrd_position(
                self._ctx.controller, xrd_position, self._global_spectrum.speed,
            )
        except _StopRequested:
            # Operator Stop/E-stop must never initiate a new stage move.
            raise
        except Exception as exc:
            if not self._stop_event.is_set():
                try:
                    self._return_to_xrd_position(
                        self._ctx.controller, xrd_position, self._global_spectrum.speed,
                    )
                except Exception as return_exc:
                    raise RunnerError(
                        "runtime.compression_failed_and_return_failed",
                        f"Compression failed ({exc}); returning to XRD also failed ({return_exc})",
                    ) from exc
            raise
        self._logger.log_science(
            "compression_reached", step_index=step_index,
            note=(f"gas_setpoint_mpa={current_mpa:.6f}; "
                  f"sample_pressure_gpa={sample_pressure_gpa:.6f}; "
                  f"target_sample_pressure_gpa={action.target_sample_pressure_gpa:.6f}"),
        )
        if follow_action is not None:
            self._start_follow(follow_action)
        self.progress_updated.emit(
            f"Ruby pressure reached {sample_pressure_gpa:.3f} GPa; returned to XRD position"
        )

    def _do_take_spectrum(
        self, action: TakeSpectrumAction, step_index: int,
        *, leave_at_ruby: bool = False,
    ) -> ruby_pressure.PressureRecord | None:
        """Acquire spectra at one or all named Ch3/Ch4/Ch5 offsets and return.

        The live departure position is the XRD position. A running follow
        session is stopped before the excursion and restarted only after a
        successful return, so it can never interpret the deliberate offset
        as sample drift.
        """
        ctrl = self._ctx.controller
        if ctrl is None:
            raise RunnerError(
                "runtime.spectrum_stage_missing",
                "Stage controller is not connected (required for take_spectrum)",
            )
        if action.em_gain_max is not None and (
            type(action.em_gain_max) is not int or not 1 <= action.em_gain_max <= 1000
        ):
            raise RunnerError(
                "runtime.spectrum_gain_invalid", "Maximum EM Gain must be an integer from 1 to 1000",
            )
        settings = (replace(self._global_spectrum, em_gain_max=action.em_gain_max)
                    if action.em_gain_max is not None else self._global_spectrum)
        stop_error = ruby_pressure.stop_condition_error(action, settings)
        if stop_error:
            raise RunnerError("runtime.spectrum_pressure_stop_invalid", stop_error)
        if type(action.all_positions) is not bool:
            raise RunnerError(
                "runtime.spectrum_positions_invalid",
                "all_positions must be a boolean",
            )
        if action.all_positions:
            names = settings.position_names()
            if not names:
                raise RunnerError(
                    "runtime.spectrum_position_missing",
                    "No ruby positions are registered",
                )
            for name in names:
                if settings.offset_for(name) is None:
                    raise RunnerError(
                        "runtime.spectrum_position_missing",
                        f"Ruby position {name!r} is not configured",
                    )
            self._check_pressure_settings(settings, names)
            follow_action = (
                self._current_follow_action
                if self._follow_thread is not None and self._follow_thread.is_alive()
                else None
            )
            if follow_action is not None:
                self._logger.log_ops("[SPECTRUM] suspending sample following")
                self._stop_follow()
            previous_batch = self._ruby_pressure_batch
            samples: list[tuple[float, float, float | None]] = []
            self._ruby_pressure_batch = samples
            try:
                for index, name in enumerate(names, start=1):
                    self._check_stop()
                    self._logger.log_ops(f"[SPECTRUM] position {index}/{len(names)}: {name}")
                    self.progress_updated.emit(f"Acquiring ruby spectrum at {name}")
                    self._do_take_spectrum(
                        replace(action, position_name=name, all_positions=False), step_index,
                    )
            finally:
                self._ruby_pressure_batch = previous_batch
            if samples:
                temperatures = [sample[2] for sample in samples]
                mean_temperature = (sum(temperatures) / len(temperatures)
                                    if all(value is not None for value in temperatures) else None)
                self.sample_pressure_ready.emit(
                    samples[-1][0], sum(sample[1] for sample in samples) / len(samples),
                    mean_temperature,
                )
            if follow_action is not None:
                self._logger.log_ops("[SPECTRUM] resuming sample following")
                self._start_follow(follow_action)
            return
        offsets = settings.offset_for(action.position_name)
        if offsets is None:
            raise RunnerError(
                "runtime.spectrum_position_missing",
                f"Ruby position {action.position_name!r} is not configured",
            )
        exposure = (action.exposure_time_s if action.exposure_time_s is not None
                    else settings.exposure_time_s)
        if (not isinstance(exposure, (int, float)) or isinstance(exposure, bool)
                or not math.isfinite(exposure) or not EXPOSURE_MIN_S <= exposure <= EXPOSURE_MAX_S):
            raise RunnerError("runtime.spectrum_exposure_invalid", "Ruby exposure time must be 0.01–5 s")
        try:
            search_action = effective_search_action(action, settings)
            search_axes = build_search_axes(search_action)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RunnerError("runtime.spectrum_search_invalid", str(exc)) from exc
        pressure_reference = self._check_pressure_settings(settings, (action.position_name,))

        try:
            from utils.stage.control_stage_sim import PM16CControllerSim
            simulated = isinstance(ctrl, PM16CControllerSim)
        except ImportError:
            simulated = False
        if not simulated:
            self._require_microscope_mode_for_spectrum()

        reader = None
        gain_info = {"available": True, "gain": 1, "minimum": 1,
                     "maximum": 1000, "increment": 1}
        if not simulated:
            try:
                from utils.fluora_pressee import FluoraPresseeReader
                reader = FluoraPresseeReader(
                    base_url=settings.base_url,
                    api_key=settings.api_key,
                    timeout_s=max(float(settings.timeout_s),
                                  float(exposure) * settings.max_accumulations + 45.0),
                    require_response_token=True,
                )
                reader.exposure_time_s = float(exposure)
                reader.accumulations = 1
                # Authentication/readiness and state-token capture happen
                # before the first deliberate spectrum-position movement.
                reader.prepare()
                first_token = (reader.last_status or {}).get("instrument_state_token")
                if not isinstance(first_token, str) or not first_token.strip():
                    raise ValueError("FluoRaPressée did not provide an instrument-state token")
                gain_info = reader.get_em_gain_info()
                # A camera read can initialise or refresh FRP's instrument
                # state. Use a status token captured after that read.
                reader.prepare()
                token = (reader.last_status or {}).get("instrument_state_token")
                if not isinstance(token, str) or not token.strip():
                    raise ValueError("FluoRaPressée did not provide an instrument-state token")
                if first_token != token:
                    raise ValueError(
                        "FluoRaPressée instrument state changed while reading EM Gain; "
                        "validate again after the instrument state settles"
                    )
                if pressure_reference is not None:
                    # /acquire/pressure rejects an uncalibrated axis only
                    # after acquiring, so check before any stage motion.
                    ruby_pressure.check_status(reader.last_status or {})
                if (type(gain_info.get("gain")) is int
                        and gain_info["gain"] > settings.em_gain_max):
                    raise ValueError(
                        f"Current FRP EM Gain {gain_info['gain']} exceeds the "
                        f"configured maximum {settings.em_gain_max}; lower it in FRP first"
                    )
            except Exception as exc:
                raise RunnerError("runtime.spectrum_not_ready", str(exc)) from exc

        follow_action = (
            self._current_follow_action
            if self._follow_thread is not None and self._follow_thread.is_alive()
            else None
        )
        if follow_action is not None:
            self._logger.log_ops("[SPECTRUM] suspending sample following")
            self._stop_follow()

        # This is the actual XRD measurement position for this iteration.
        # Image alignment below may move Ch3/4/5 before the ruby excursion;
        # only a successful ruby measurement returns to this saved position.
        initial_xrd_position = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        self._last_spectrum_xrd_position = initial_xrd_position

        pressure = self._acquire_spectrum_at_ruby(
            search_action, step_index, settings, offsets, exposure, simulated, reader,
            search_axes, gain_info, pressure_reference, initial_xrd_position,
            **({"return_to_xrd": False} if leave_at_ruby else {}),
        )

        if follow_action is not None and not leave_at_ruby:
            self._logger.log_ops("[SPECTRUM] resuming sample following")
            self._start_follow(follow_action)
        self.progress_updated.emit(
            "Spectrum acquired at ruby" if leave_at_ruby
            else "Spectrum acquired; returned to XRD position"
        )
        return pressure

    @staticmethod
    def _ruby_target(
        departure: dict[int, int], offsets: dict[int, int],
        centre_shift: tuple[float, float] | None,
    ) -> dict[int, int]:
        """Absolute Ch3/Ch4/Ch5 of a ruby from the aligned XRD position.

        ``centre_shift`` is the image shift (px) from the aligned position
        to the one that puts the gasket centre on the X-ray beam; gasket-
        centre Ch4/Ch5 offsets start there. None means XRD-position offsets.
        """
        origin = {ch: float(departure[ch]) for ch in (3, 4, 5)}
        if centre_shift is not None:
            matrix = np.asarray(
                json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))["matrix_inv"], dtype=float)
            motion = matrix @ np.asarray(centre_shift, dtype=float)
            origin[4] += float(motion[0])
            origin[5] += float(motion[1])
        return {ch: int(round(origin[ch] + offsets[ch])) for ch in (3, 4, 5)}

    def _check_pressure_settings(
        self, settings: scheduler_settings.GlobalSpectrumSettings, names,
    ) -> ruby_pressure.RubyReference | None:
        """Validate pressure settings for ``names`` before any stage motion.

        Returns the reference of a single named ruby, or None when pressure
        calculation is off.
        """
        if not settings.pressure_enabled:
            return None
        names = tuple(names)
        errors = ruby_pressure.settings_errors(settings, names)
        if errors:
            raise RunnerError(
                "runtime.spectrum_pressure_invalid",
                "; ".join(message for _, message in errors),
            )
        if settings.temperature_correction_enabled:
            try:
                ruby_pressure.read_temperature(settings, self._ctx.lakeshore)
            except ruby_pressure.PressureSetupError as exc:
                raise RunnerError("runtime.spectrum_temperature_unavailable", str(exc)) from exc
        return ruby_pressure.reference_for(settings, names[0]) if len(names) == 1 else None

    def _acquire_spectrum_at_ruby(
        self, action: TakeSpectrumAction, step_index: int,
        settings: scheduler_settings.GlobalSpectrumSettings, offsets: dict[int, int],
        exposure: float, simulated: bool, reader,
        search_axes: tuple[tuple[int, tuple[int, ...]], ...],
        gain_info: dict,
        pressure_reference: ruby_pressure.RubyReference | None = None,
        initial_xrd_position: dict[int, int] | None = None,
        *, return_to_xrd: bool = True,
    ) -> ruby_pressure.PressureRecord | None:
        ctrl = self._ctx.controller
        if initial_xrd_position is None:
            # Callers normally capture this before any alignment motion.
            initial_xrd_position = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        expected_origin = scheduler_settings.ruby_origin_for(self._global_follow.tracking_method)
        if settings.ruby_offset_origin != expected_origin:
            raise RunnerError(
                "runtime.spectrum_ruby_origin_mismatch",
                "Ruby positions were recorded for a different position-tracking method; "
                "record them again in Sample Positions",
            )
        centre_shift = None
        if not simulated:
            centre_shift = self._align_to_xrd_reference(settings)

        departure = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        target = self._ruby_target(
            departure, offsets,
            centre_shift if expected_origin == scheduler_settings.RUBY_ORIGIN_GASKET_CENTRE
            else None,
        )
        search_targets = search_positions(target, search_axes) if search_axes else []
        # Check the entire bounded search area before the first ruby move.
        for position in [target, *search_targets]:
            for ch in (3, 4, 5):
                ok, message = ctrl.check_move_constraints(ch, position[ch])
                if not ok:
                    raise RunnerError("runtime.move_constraint_violation", message)
                ok, message = ctrl.check_soft_limits(ch, position[ch])
                if not ok:
                    raise RunnerError("runtime.move_constraint_violation", message)
                self._check_global_limits_before_move(ch, position[ch])

        save_dir: Path | None = None
        if action.save:
            save_dir = self._run_subdir(SPECTRUM_SUBDIR)
            save_dir.mkdir(parents=True, exist_ok=True)

        moved = False
        spectrum_path: Path | None = None
        final_pressure: ruby_pressure.PressureRecord | None = None
        best_target = target.copy()
        scan_points: list[list[int]] = []
        scan_intensities: list[float] = []
        search_performed = False

        def move_to(position: dict[int, int]) -> None:
            nonlocal moved
            for ch in (5, 4, 3):
                if int(ctrl.get_ch_pos(ch)) == position[ch]:
                    continue
                # The command may move the stage before a transport error.
                moved = True
                self._do_stage(StageAction(
                    operation="move_absolute", ch=ch, value=position[ch], speed=settings.speed,
                ), {})

        def acquire_at(position: dict[int, int], *, fit: bool = False,
                       exposure_s: float = exposure, accumulations: int = 1,
                       pressure_request: dict | None = None,
                       temperature: ruby_pressure.TemperatureReading | None = None) -> dict:
            if not simulated:
                reader.exposure_time_s = exposure_s
                reader.accumulations = accumulations
                reader.timeout_s = max(float(settings.timeout_s),
                                       exposure_s * accumulations + 45.0)
                if fit and pressure_request is not None:
                    return reader.acquire_spectrum_with_pressure(
                        pressure_request,
                        fit_function=settings.fit_function, fit_peak_count=2,
                        peak_sort_order="x_desc", baseline_model=settings.baseline_model,
                    )
                if fit:
                    return reader.acquire_spectrum_with_fit(
                        fit_function=settings.fit_function, fit_peak_count=2,
                        peak_sort_order="x_desc", baseline_model=settings.baseline_model,
                    )
                return reader.acquire_spectrum()
            x = np.linspace(680.0, 710.0, 1024)
            simulated_shift = sum(
                ((position[ch] - target[ch]) * PULSE_SCALE[ch]
                 - getattr(action, f"search_step_ch{ch}_um")) ** 2
                / getattr(action, f"search_step_ch{ch}_um") ** 2
                for ch, _ in search_axes
            )
            strength = math.exp(-0.5 * simulated_shift) if search_axes else 1.0
            y = 100.0 + strength * (
                5000.0 * np.exp(-0.5 * ((x - 694.2) / 0.35) ** 2)
                + 2500.0 * np.exp(-0.5 * ((x - 692.8) / 0.35) ** 2)
            )
            result = {
                "x": x.tolist(), "y_raw": y.tolist(), "y": y.tolist(),
                "mode": "1d", "timestamp": datetime.now().isoformat(),
                "simulation": True, "exposure_time_s": float(exposure_s),
                "accumulations": accumulations, "saturated": False,
            }
            if fit:
                result["fit"] = {
                    "success": True, "x_fit": x.tolist(), "y_fit": y.tolist(),
                    "fit": {"R2": 1.0,
                            "Peak1": 694.2, "Peak1_Err": 0.0, "Width1": 0.35, "Width1_Err": 0.0,
                            "Peak2": 692.8, "Peak2_Err": 0.0, "Width2": 0.35, "Width2_Err": 0.0,
                            "peaks": [
                        {"amplitude": 5000.0 * strength, "position": 694.2, "position_err": 0.0},
                        {"amplitude": 2500.0 * strength, "position": 692.8, "position_err": 0.0},
                    ]},
                }
                if pressure_request is not None:
                    result.update(ruby_pressure.simulated_fields(
                        settings, pressure_reference, temperature, 694.2,
                    ))
            return result

        def run_nearby_search(exposure_s: float) -> None:
            nonlocal best_target, search_performed
            best_intensity = -math.inf
            best_distance = math.inf
            for index, position in enumerate(search_targets, 1):
                self._check_stop()
                move_to(position)
                if settings.settle_ms:
                    self._do_wait(settings.settle_ms / 1000.0)
                response = acquire_at(position, exposure_s=exposure_s)
                self._check_stop()
                intensity_at_point = float(np.sum(np.asarray(response["y"], dtype=float)))
                if not math.isfinite(intensity_at_point):
                    raise RunnerError("runtime.spectrum_search_intensity_invalid",
                                      "Nearby search returned a non-finite intensity")
                distance = sum(((position[ch] - target[ch]) * PULSE_SCALE[ch]) ** 2
                               for ch, _ in search_axes)
                scan_points.append([position[ch] for ch in (3, 4, 5)])
                scan_intensities.append(intensity_at_point)
                if (intensity_at_point > best_intensity
                        or (intensity_at_point == best_intensity and distance < best_distance)):
                    best_intensity, best_distance = intensity_at_point, distance
                    best_target = position.copy()
                self.progress_updated.emit(
                    f"Ruby nearby search {index}/{len(search_targets)}: "
                    f"intensity={intensity_at_point:.6g}"
                )
            search_performed = True
            self._logger.log_ops(
                f"[SPECTRUM] nearby search best={best_target} intensity={best_intensity:.6g}"
            )
            move_to(best_target)
            if settings.settle_ms:
                self._do_wait(settings.settle_ms / 1000.0)

        try:
            self.progress_updated.emit("Moving to spectrum position…")
            move_to(target)
            if settings.settle_ms:
                self._do_wait(settings.settle_ms / 1000.0)

            if search_targets and action.search_when == "always":
                run_nearby_search(float(exposure))

            current_exposure = float(exposure)
            accumulations = 1
            halvings = 0
            attempt = 0
            current_gain = gain_info["gain"] if gain_info["available"] else None
            if current_gain is None:
                self._logger.log_ops("[SPECTRUM] EM Gain unavailable; using accumulations for retries")
            while True:
                self._check_stop()
                attempt += 1
                temperature = pressure_request = None
                if pressure_reference is not None:
                    # Measured just before this final acquisition, never the setpoint.
                    try:
                        temperature = ruby_pressure.read_temperature(settings, self._ctx.lakeshore)
                        pressure_request = ruby_pressure.build_request(
                            settings, pressure_reference, temperature,
                        )
                    except ruby_pressure.PressureSetupError as exc:
                        raise RunnerError(
                            "runtime.spectrum_temperature_unavailable", str(exc),
                        ) from exc
                self.progress_updated.emit(
                    f"Acquiring ruby spectrum (attempt {attempt}, "
                    f"{current_exposure:.3g} s × {accumulations}, EM Gain {current_gain})…"
                )
                start_conditions = capture_conditions(self._ctx, stage=True) if action.save else {}
                start_time = datetime.now().isoformat(timespec="milliseconds") if action.save else ""
                acquired = acquire_at(
                    best_target, fit=True, exposure_s=current_exposure,
                    accumulations=accumulations,
                    pressure_request=pressure_request, temperature=temperature,
                )
                acquired_at = time.time()
                sample_temperature = (self._cached_sample_temperature_b(acquired_at)
                                      if pressure_reference is not None else None)
                end_time = datetime.now().isoformat(timespec="milliseconds") if action.save else ""
                end_conditions = capture_conditions(self._ctx) if action.save else {}
                self._check_stop()
                quality = assess_spectrum(acquired)
                y = np.asarray(acquired["y"], dtype=float)
                intensity = float(np.sum(y, dtype=float))
                if quality.fit_error:
                    outcome = "fit_failed"
                elif quality.saturated:
                    outcome = "saturated"
                elif quality.r_squared < settings.r_squared_min:
                    outcome = "quality_low"
                else:
                    outcome = "passed"
                pressure_record = None
                if pressure_reference is not None:
                    pressure_record = ruby_pressure.evaluate(
                        ruby_pressure.new_record(
                            settings, action.position_name, pressure_reference, temperature,
                        ),
                        acquired, quality_passed=outcome == "passed",
                    )
                pressure_fields = {
                    "pressure_enabled": pressure_record is not None,
                    "pressure_request_json": json.dumps(pressure_request or {}, ensure_ascii=False),
                    "pressure_json": json.dumps(
                        pressure_record.to_dict() if pressure_record else {}, ensure_ascii=False,
                    ),
                    **(pressure_record.npz_fields() if pressure_record else {}),
                }

                if action.save:
                    prefix = Path(action.prefix).name.strip() or "spectrum"
                    skipped_suffixes: list[str] = []
                    spectrum_path = _spectrum_save_path(
                        save_dir, prefix, settings.filename_suffixes, attempt,
                        ctx=self._ctx, skipped=skipped_suffixes,
                    )
                    if skipped_suffixes:
                        self._logger.log_ops(
                            "[SPECTRUM] Filename suffix skipped (instrument disconnected "
                            "or no recent reading): " + ", ".join(skipped_suffixes)
                        )
                    np.savez_compressed(
                        spectrum_path,
                        x=np.asarray(acquired.get("x"), dtype=float),
                        y_raw=np.asarray(acquired.get("y_raw"), dtype=float),
                        y=y,
                        metadata_json=json.dumps(acquired, ensure_ascii=False),
                        xrd_ch3_pulse=initial_xrd_position[3], xrd_ch4_pulse=initial_xrd_position[4], xrd_ch5_pulse=initial_xrd_position[5],
                        aligned_xrd_ch3_pulse=departure[3], aligned_xrd_ch4_pulse=departure[4], aligned_xrd_ch5_pulse=departure[5],
                        spectrum_ch3_pulse=best_target[3], spectrum_ch4_pulse=best_target[4], spectrum_ch5_pulse=best_target[5],
                        offset_ch3_pulse=offsets[3], offset_ch4_pulse=offsets[4], offset_ch5_pulse=offsets[5],
                        ruby_position_name=action.position_name,
                        requested_exposure_time_s=current_exposure,
                        requested_accumulations=accumulations,
                        measured_em_gain=current_gain if current_gain is not None else -1,
                        quality_em_gain_max=settings.em_gain_max,
                        quality_attempt=attempt,
                        quality_exposure_halvings=halvings,
                        quality_outcome=outcome,
                        quality_error=quality.fit_error or "",
                        quality_r_squared=quality.r_squared if quality.r_squared is not None else np.nan,
                        quality_r_squared_min=settings.r_squared_min,
                        quality_fit_function=settings.fit_function,
                        quality_baseline_model=settings.baseline_model,
                        integrated_intensity=intensity,
                        search_enabled=bool(search_axes),
                        search_when=action.search_when,
                        search_performed=search_performed,
                        search_channels=np.asarray([ch for ch, _ in search_axes], dtype=int),
                        search_positions_pulse=np.asarray(scan_points, dtype=int).reshape((-1, 3)),
                        search_intensities=np.asarray(scan_intensities, dtype=float),
                        search_nominal_position_pulse=np.asarray([target[ch] for ch in (3, 4, 5)], dtype=int),
                        search_best_position_pulse=np.asarray([best_target[ch] for ch in (3, 4, 5)], dtype=int),
                        **pressure_fields,
                    )
                    csv_path = spectrum_path.with_suffix(".csv")
                    save_spectrum_csv(csv_path, acquired, {
                        "timestamp": acquired.get("timestamp", ""),
                        "ruby_position_name": action.position_name,
                        "exposure_time_s": current_exposure,
                        "accumulations": accumulations,
                        "em_gain": current_gain if current_gain is not None else "unavailable",
                        "fit_function": settings.fit_function,
                        "baseline_model": settings.baseline_model,
                        "r_squared": quality.r_squared,
                        "quality_outcome": outcome,
                        "quality_attempt": attempt,
                        "integrated_intensity": intensity,
                        "spectrum_ch3_ch4_ch5_pulse": [best_target[ch] for ch in (3, 4, 5)],
                        "xrd_ch3_ch4_ch5_pulse": [departure[ch] for ch in (3, 4, 5)],
                        "simulation": bool(acquired.get("simulation", False)),
                        **({f"pressure.{key}": value
                            for key, value in pressure_record.to_dict().items()}
                           if pressure_record else {}),
                    })
                    r2_text = f"{quality.r_squared:.4f}" if quality.r_squared is not None else "n/a"
                    try:
                        save_spectrum_plot(
                            spectrum_path.with_suffix(".png"), acquired,
                            title=spectrum_path.stem,
                            info_lines=[
                                f"Fit: {settings.fit_function}",
                                f"Background: {settings.baseline_model}",
                                f"R²: {r2_text}",
                                f"Outcome: {outcome}",
                                f"Exposure: {current_exposure:.3g} s × {accumulations}",
                                f"EM Gain: {current_gain if current_gain is not None else 'n/a'}",
                                f"Position: {action.position_name}",
                            ],
                            footer_lines=(ruby_pressure.summary_lines(pressure_record)
                                          if pressure_record else None),
                        )
                    except Exception as exc:
                        # A plotting problem must not abort the sequence; the
                        # CSV and NPZ already hold the data.
                        self._logger.log_ops(f"[SPECTRUM] PNG plot failed: {exc}")
                    master_path = append_ruby_record(
                        spectrum_path, start_time, end_time,
                        start_conditions, end_conditions,
                        ruby_name=action.position_name, attempt=attempt,
                        outcome=outcome, acquired=acquired,
                        r_squared=quality.r_squared, pressure_record=pressure_record,
                    )
                    self._logger.log_ops(f"[SPECTRUM] Ruby master updated → {master_path}")
                    self.file_saved.emit(step_index, str(csv_path))
                self._logger.log_ops(
                    f"[SPECTRUM] attempt={attempt} exposure={current_exposure:.6g}s "
                    f"accumulations={accumulations} em_gain={current_gain} outcome={outcome} "
                    f"R²={quality.r_squared} "
                    f"file={spectrum_path or '(not saved)'}"
                )
                if pressure_record is not None:
                    self._logger.log_ops(
                        f"[SPECTRUM] pressure attempt={attempt} "
                        + json.dumps(pressure_record.to_dict(), ensure_ascii=False)
                    )
                if quality.fit_error:
                    raise RunnerError("runtime.spectrum_fit_failed", quality.fit_error)
                if quality.saturated:
                    if (halvings >= MAX_EXPOSURE_HALVINGS
                            or current_exposure / 2 < EXPOSURE_MIN_S):
                        raise RunnerError(
                            "runtime.spectrum_saturated",
                            f"Ruby spectrum is still saturated after {halvings} exposure halvings "
                            f"(last exposure {current_exposure:.3g} s)",
                        )
                    current_exposure /= 2
                    accumulations = 1
                    halvings += 1
                    continue
                if outcome == "quality_low":
                    if search_targets and not search_performed and action.search_when == "on_low_quality":
                        self._logger.log_ops(
                            "[SPECTRUM] initial R² below threshold; starting nearby search"
                        )
                        run_nearby_search(current_exposure)
                        continue
                    if current_gain is not None:
                        target_gain = next_em_gain(
                            current_gain, min(settings.em_gain_max, gain_info["maximum"]),
                            gain_info["minimum"], gain_info["increment"],
                        )
                        if target_gain is not None:
                            self._check_stop()
                            if not simulated:
                                reader.set_em_gain(target_gain)
                            current_gain = target_gain
                            self._logger.log_ops(f"[SPECTRUM] EM Gain increased to {current_gain}")
                            continue
                    if accumulations >= settings.max_accumulations:
                        raise RunnerError(
                            "runtime.spectrum_quality_failed",
                            f"Ruby spectrum R² is below threshold after {attempt} attempts "
                            f"(R² {quality.r_squared:.3g} < {settings.r_squared_min:.3g}; "
                            f"maximum EM Gain {settings.em_gain_max}; "
                            f"maximum accumulations {settings.max_accumulations})",
                        )
                    accumulations += 1
                    continue
                if pressure_record is not None:
                    if pressure_record.status == "invalid":
                        raise RunnerError(
                            "runtime.spectrum_pressure_invalid",
                            f"Ruby pressure result is invalid: {pressure_record.error}",
                        )
                    if pressure_record.status == "temperature_out_of_range":
                        # The spectrum is kept; the number is not reported as a pressure.
                        self._logger.log_ops(
                            f"[SPECTRUM] WARNING pressure not valid: {pressure_record.error}"
                        )
                        self.progress_updated.emit(
                            f"Warning: ruby pressure not valid ({pressure_record.error})"
                        )
                    else:
                        self.progress_updated.emit(
                            f"Ruby pressure at {action.position_name}: "
                            f"{pressure_record.pressure_gpa:.3f} GPa"
                        )
                final_pressure = pressure_record
                if (pressure_record is not None and pressure_record.is_measured
                        and pressure_record.pressure_gpa is not None
                        and math.isfinite(pressure_record.pressure_gpa)):
                    self._report_sample_pressure(
                        pressure_record, acquired_at, sample_temperature,
                    )
                self._last_spectrum_exposure_s = current_exposure
                self._logger.log_ops(
                    f"[SPECTRUM] accepted intensity={intensity:.6g} "
                    f"file={spectrum_path or '(not saved)'}"
                )
                if ruby_pressure.stop_condition_met(action, pressure_record):
                    threshold = (action.stop_pressure_target_gpa
                                 - action.stop_pressure_tolerance_gpa)
                    self._condition_stop = True
                    self._stop_reason = (
                        f"Ruby pressure {pressure_record.pressure_gpa:.3f} GPa at "
                        f"{action.position_name} reached the {threshold:g} GPa stop threshold; "
                        "PACE5000 remains in Control mode at its last setpoint"
                    )
                    self._logger.log_science(
                        "spectrum_taken", step_index=step_index,
                        note=(f"ruby_position={action.position_name}; "
                              f"integrated_intensity={intensity:.6g}; "
                              f"{ruby_pressure.log_note(pressure_record)}; "
                              "pressure_stop_triggered=true"),
                        spectrum_file=str(spectrum_path) if spectrum_path else "",
                    )
                    self._logger.log_ops(f"[SPECTRUM:STOP] {self._stop_reason}")
                    self.progress_updated.emit(self._stop_reason)
                    self._stop_event.set()
                    self._follow_stop_event.set()
                    raise _StopRequested()
                break
        except _StopRequested:
            # Never initiate new motion after an operator Stop/E-stop.
            raise
        except Exception as exc:
            if moved and not self._stop_event.is_set():
                try:
                    self._return_to_xrd_position(ctrl, departure, settings.speed)
                except Exception as return_exc:
                    raise RunnerError(
                        "runtime.spectrum_failed_and_return_failed",
                        f"Spectrum acquisition failed ({exc}); returning to the "
                        f"XRD position also failed ({return_exc})",
                    ) from exc
            raise
        else:
            if return_to_xrd:
                try:
                    self._return_to_xrd_position(ctrl, initial_xrd_position, settings.speed)
                except Exception as exc:
                    raise RunnerError(
                        "runtime.spectrum_return_failed",
                        f"Spectrum was acquired, but returning to the XRD position failed: {exc}",
                    ) from exc

        note = f"ruby_position={action.position_name}; integrated_intensity={intensity:.6g}"
        if final_pressure is not None:
            note += "; " + ruby_pressure.log_note(final_pressure)
        self._logger.log_science(
            "spectrum_taken", step_index=step_index, note=note,
            spectrum_file=str(spectrum_path) if spectrum_path else "",
        )
        return final_pressure

    def _cached_sample_temperature_b(self, acquired_at: float) -> float | None:
        """Read the recent cached Ch B value without issuing hardware I/O."""
        lakeshore = self._ctx.lakeshore
        if lakeshore is not None:
            try:
                cached = lakeshore.get_cached_readings()
                if cached.get("connected"):
                    value = cached.get("temperature_b_k")
                    polled_at = cached.get("timestamp")
                    age = acquired_at - float(polled_at) if polled_at is not None else math.inf
                    if (value is not None and math.isfinite(float(value))
                            and -2 <= age <= self._global_spectrum.temperature_max_age_s):
                        return float(value)
            except Exception:
                # This optional display reading must never fail the spectrum step.
                pass
        return None

    def _report_sample_pressure(self, record: ruby_pressure.PressureRecord,
                                acquired_at: float, temperature: float | None) -> None:
        """Pass accepted ruby pressure and a cached Ch B reading to the GUI."""
        sample = (acquired_at, record.pressure_gpa, temperature)
        if self._ruby_pressure_batch is not None:
            self._ruby_pressure_batch.append(sample)
        else:
            self.sample_pressure_ready.emit(*sample)

    def _return_to_xrd_position(self, ctrl, departure: dict[int, int], speed: str) -> None:
        self.progress_updated.emit("Returning to XRD position…")
        for ch in (5, 4, 3):
            if int(ctrl.get_ch_pos(ch)) == departure[ch]:
                continue
            self._do_stage(StageAction(
                operation="move_absolute", ch=ch, value=departure[ch], speed=speed,
            ), {})
        actual = {ch: int(ctrl.get_ch_pos(ch)) for ch in (3, 4, 5)}
        if actual != departure:
            raise RunnerError(
                "runtime.spectrum_return_position_mismatch",
                f"XRD return position mismatch: expected {departure}, read back {actual}",
            )

    def _stage_mode_now(self) -> tuple[str, str]:
        """(mode, explanation) from the live Ch6/Ch8/Ch9, using the same rule
        as PreValidator (safety_rules.stage_mode_from_positions)."""
        ctrl = self._ctx.controller
        if ctrl is None:
            raise RunnerError("runtime.stage_not_connected", "Stage controller is not connected")
        positions: dict[int, int] = {}
        for ch in STAGE_MODE_CHANNELS:
            value = ctrl.get_ch_pos(ch)
            if value is None:
                raise RunnerError(
                    "runtime.stage_position_unreadable", f"Cannot read Ch{ch} position",
                )
            positions[ch] = int(value)
        settings = (self._global_positions.to_stage_settings()
                    if self._global_positions is not None else load_stage_settings())
        beam = load_xray_beam()
        pulses = beam.microscope_pulses if beam is not None else None
        return (stage_mode_from_positions(positions, settings, pulses),
                explain_stage_mode(positions, settings, pulses))

    def _require_microscope_mode(self, code: str, what: str) -> None:
        mode, detail = self._stage_mode_now()
        if mode != "microscope":
            raise RunnerError(
                code,
                f"{what} needs microscope mode; add fpd_out_and_microscope_in() "
                f"before this step ({detail})",
            )

    def _require_microscope_mode_for_spectrum(self) -> None:
        """Require an explicit optical-mode step before camera alignment."""
        self._require_microscope_mode("runtime.spectrum_microscope_required", "Ruby measurement")

    def _require_microscope_mode_for_camera(self, what: str) -> None:
        """Refuse image-based stage tracking unless the microscope is at the
        registered X-ray beam position. Skipped with the simulator, whose
        positions do not correspond to a real microscope."""
        try:
            from utils.stage.control_stage_sim import PM16CControllerSim
            if isinstance(self._ctx.controller, PM16CControllerSim):
                return
        except ImportError:
            pass
        self._require_microscope_mode("runtime.camera_microscope_required", what)

    def _resolve_xrd(self, action: TakeXrdAction) -> _EffectiveXrd:
        g = self._global_xrd
        exposure_ms = action.exposure_ms if action.exposure_ms is not None else g.exposure_ms
        prefix = action.prefix if action.prefix is not None else g.prefix
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
            prefix=prefix,
            filename_suffixes=tuple(g.filename_suffixes),
            dark_file=dark_file,
            dark_enabled=dark_enabled,
            defect_file=defect_file,
            defect_enabled=defect_enabled,
            defect_mode=g.defect_mode,
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
        reduction = None
        if self._shutter_managed:
            try:
                self._open_shutter_for_xrd()
                reduction = self._do_take_xrd_exposed(action, step_index)
            finally:
                # This also covers failed capture, processing and save paths.
                self._insert_shutter()
        else:
            reduction = self._do_take_xrd_exposed(action, step_index)
        if reduction is not None:
            self._run_instant1d(*reduction)

    def _do_take_xrd_exposed(
        self, action: TakeXrdAction, step_index: int,
    ) -> tuple[np.ndarray, Path | None, int, int] | None:
        try:
            from ..Rad_icon_2022.image_utils import (
                save_tiff, process_frame, build_detector_metadata, build_instrument_metadata,
            )
        except ImportError:
            import sys as _sys
            _pkg = str(Path(__file__).parent.parent.parent)
            if _pkg not in _sys.path:
                _sys.path.insert(0, _pkg)
            from apps.Rad_icon_2022.image_utils import (
                save_tiff, process_frame, build_detector_metadata, build_instrument_metadata,
            )

        backend = self._ctx.radicon
        eff = self._resolve_xrd(action)
        if self._global_xrd.instant1d_enabled and self._poni_ai is None:
            raise RunnerError("runtime.instant1d_calibration_missing",
                              "Instant 1D reduction requires a registered poni calibration")
        # Read stage positions before a possible Ch11 oscillation starts.
        start_conditions = capture_conditions(self._ctx, stage=True) if action.save else {}

        # Start Ch11 oscillation in background thread if requested
        osc_stop = threading.Event()
        osc_thread: threading.Thread | None = None
        # Single-slot exception handoff from _osc_loop() — written at most
        # once, inside that thread; read only after joining it below, so
        # there is no concurrent access.
        osc_exception: list = []
        if eff.oscillate:
            ctrl = self._ctx.controller
            if ctrl is None:
                raise RuntimeError(
                    "Stage controller is not connected (required for Ch11 oscillation)"
                )
            try:
                pos_a, pos_b = validate_ch11_oscillation_settings(
                    eff.osc_pos_a_deg,
                    eff.osc_pos_b_deg,
                    eff.osc_dwell_ms,
                    eff.osc_speed,
                )
            except (TypeError, ValueError) as exc:
                # validate_ch11_oscillation_settings() (safety_rules.py)
                # raises plain ValueError/TypeError — wrap so this reaches
                # _execute_actions()'s terminal handler with a stable code
                # rather than falling through to the generic
                # "runtime.unexpected_error" fallback.
                raise RunnerError("runtime.ch11_oscillation_invalid", str(exc)) from exc
            for target in (pos_a, pos_b):
                ok, message = ctrl.check_move_constraints(11, target)
                if not ok:
                    raise RunnerError("runtime.move_constraint_violation", message)
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
                args=(eff, osc_stop, pos_a, pos_b, osc_exception),
                daemon=True,
            )
            osc_thread.start()

        # See REORGANISATION_PLAN.md Phase 9 for the full rationale: this
        # replaces a bare try/finally that (a) silently swallowed any
        # exception from _osc_loop() — letting the sequence report success
        # even when oscillation itself failed — and (b) called
        # _return_ch11_to_zero() unconditionally even if the 30s join
        # timed out, i.e. even while the oscillation thread might still be
        # alive and driving Ch11 itself.
        osc_stop_timed_out = False
        recovery_exc: Exception | None = None
        capture_exc: Exception | None = None
        frame = None
        start_time = ""
        end_time = ""
        end_conditions = {}

        try:
            self._logger.log_ops(
                f"[RADICON] capture_triggered({eff.exposure_ms} ms)"
            )
            start_time = datetime.now().isoformat(timespec="milliseconds")
            frame = backend.capture_triggered(
                eff.exposure_ms, timeout_ms=eff.exposure_ms + 5000,
            )
            end_time = datetime.now().isoformat(timespec="milliseconds")
        except Exception as exc:
            capture_exc = exc
        finally:
            if osc_thread is not None:
                osc_stop.set()
                osc_thread.join(timeout=30)
                if osc_thread.is_alive():
                    # A genuine communication hang: _osc_loop()'s own wait
                    # loops poll osc_stop every 0.1/0.05s, so a clean 30s
                    # join timeout can only mean a wire call
                    # (move_ch_absolute/get_is_moving) never returned.
                    # Driving Ch11 to zero from this thread too while the
                    # oscillation thread might still be driving it itself
                    # is exactly the collision this guards against — the
                    # thread must be confirmed stopped (or force-stopped)
                    # before anything else touches Ch11.
                    osc_stop_timed_out = True
                    self._logger.log_ops(
                        "[CH11] oscillation thread did not stop within 30s — forcing normal_stop()"
                    )
                    try:
                        ctrl.normal_stop(source="exp_scheduler")
                    except Exception as exc:
                        # The forced stop itself failing is important
                        # investigation context regardless of whether the
                        # thread happens to exit during the grace join
                        # below — do not silently swallow it.
                        self._logger.log_ops(f"[CH11] forced normal_stop() also failed: {exc}")
                    osc_thread.join(timeout=5)
                    # Deliberately do NOT call _return_ch11_to_zero() here
                    # even if the thread ended within this grace period —
                    # the normal stop/recovery sequence has already broken
                    # down once, so this must always surface as a failure
                    # (the osc_stop_timed_out check below), never as a
                    # silent success with Ch11 left in an unknown position.
                else:
                    self._logger.log_ops("[CH11] oscillation stopped — returning to θ=0°")
                    self.progress_updated.emit("[CH11] Returning to θ=0°…")
                    try:
                        self._return_ch11_to_zero(eff.osc_speed)
                    except _StopRequested:
                        # A user Stop during the return-to-zero move — a
                        # clean unwind, not a recovery failure. Must not be
                        # caught by the generic handler below, which would
                        # otherwise report it as
                        # runtime.ch11_return_to_zero_failed (an external
                        # review finding: this except clause previously
                        # caught bare Exception, so pressing Stop here was
                        # misreported as a hardware error instead of a
                        # graceful stop).
                        raise
                    except Exception as exc:
                        recovery_exc = exc

        # Python's `raise ... from ...` can only preserve ONE __cause__ —
        # when capture_exc / osc_exception / recovery_exc coexist, whichever
        # isn't selected below would otherwise be lost entirely. Log every
        # one that's present before deciding which single exception to
        # raise.
        if osc_exception:
            self._logger.log_ops(f"[CH11] oscillation loop exception: {osc_exception[0]}")
        if capture_exc is not None:
            self._logger.log_ops(f"[CH11] frame capture exception: {capture_exc}")
        if recovery_exc is not None:
            self._logger.log_ops(f"[CH11] return-to-zero exception: {recovery_exc}")

        # Priority (most physically dangerous first): thread still alive
        # after a forced stop > initial stop timeout (even if it later
        # cleared) > θ=0° recovery failure > frame capture failure >
        # oscillation-loop execution failure.
        if osc_thread is not None and osc_thread.is_alive():
            raise RunnerError(
                "runtime.ch11_oscillation_stop_failed",
                "Ch11 oscillation thread remained alive after normal_stop()",
            ) from (osc_exception[0] if osc_exception else capture_exc)

        if osc_stop_timed_out:
            raise RunnerError(
                "runtime.ch11_oscillation_stop_timeout",
                "Ch11 oscillation did not stop within 30 seconds; normal_stop() was required",
            ) from (osc_exception[0] if osc_exception else capture_exc)

        if recovery_exc is not None:
            raise RunnerError(
                "runtime.ch11_return_to_zero_failed",
                f"Ch11 failed to return to θ=0° after oscillation: {recovery_exc}",
            ) from (capture_exc or recovery_exc)

        if capture_exc is not None:
            raise capture_exc from (osc_exception[0] if osc_exception else None)

        if osc_exception:
            raise RunnerError(
                "runtime.ch11_oscillation_execution_failed",
                f"Ch11 oscillation failed: {osc_exception[0]}",
            ) from osc_exception[0]

        saved_path: Path | None = None
        if action.save:
            # Sample after Ch11 cleanup so instrument locks cannot delay its stop.
            end_conditions = capture_conditions(self._ctx)

        dark = self._load_xrd_dark(eff.dark_file) if eff.dark_enabled and eff.dark_file else None
        mask = (
            self._load_xrd_defect_mask(eff.defect_file, backend)
            if eff.defect_enabled and eff.defect_file else None
        )
        processed = process_frame(
            frame, flip_v=eff.flip_v, flip_h=eff.flip_h,
            dark=dark, dark_enabled=eff.dark_enabled,
            defect_mask=mask,
            defect_mode=eff.defect_mode if eff.defect_enabled else "none",
            defect_kernel=eff.defect_kernel,
        )
        frame = processed.image
        if processed.dark_shape_mismatch is not None:
            self.progress_updated.emit(
                f"[XRD] Dark correction skipped: image shape {frame.shape} "
                f"does not match dark shape {processed.dark_shape_mismatch}"
            )
        if processed.defect_shape_mismatch is not None:
            self.progress_updated.emit(
                f"[XRD] Defect correction skipped: image shape {frame.shape} "
                f"does not match mask shape {processed.defect_shape_mismatch}"
            )

        if action.save:
            save_dir = self._run_subdir(XRD_SUBDIR)
            save_dir.mkdir(parents=True, exist_ok=True)
            binning = "2x2" if backend.width < 2000 else "1x1"
            skipped_suffixes: list[str] = []
            stem = _xrd_filename_stem(
                save_dir, eff.prefix, eff.filename_suffixes,
                ctx=self._ctx, skipped=skipped_suffixes,
            )
            if skipped_suffixes:
                self.progress_updated.emit(
                    "[XRD] Filename suffix skipped (instrument disconnected or "
                    f"no recent reading): {', '.join(skipped_suffixes)}"
                )
            fname = save_dir / f"{stem}_{binning}.tif"
            suffix = 2
            while fname.exists():
                fname = save_dir / f"{stem}_{binning}_{suffix}.tif"
                suffix += 1
            meta = build_detector_metadata(
                "xrd", eff.exposure_ms, backend.width, eff.flip_v, eff.flip_h,
                {
                    "dark_corrected": processed.dark_applied,
                    "dark_source": Path(eff.dark_file).name if processed.dark_applied else None,
                    "defect_corrected": processed.defect_applied,
                    "defect_mode": eff.defect_mode if processed.defect_applied else "none",
                    "defect_kernel": (
                        eff.defect_kernel
                        if processed.defect_applied and eff.defect_mode == "median" else None
                    ),
                    "oscillated": eff.oscillate,
                },
            )
            meta.update(build_instrument_metadata(
                self._ctx.controller, self._ctx.pace5000, self._ctx.lakeshore,
            ))
            if not save_tiff(fname, frame, meta) or not fname.is_file() or fname.stat().st_size == 0:
                raise RuntimeError(f"Could not save XRD image: {fname}")
            saved_path = fname
            self.file_saved.emit(step_index, str(fname))
            master_path = append_xrd_record(
                fname, start_time, end_time, start_conditions, end_conditions,
            )
            self._logger.log_ops(f"[RADICON] XRD master updated → {master_path}")
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

        display_name = (saved_path.name if saved_path is not None
                        else f"{datetime.now():%H:%M:%S} (not saved)")
        # Copy: the monitor panel keeps it after this step moves on.
        self.xrd_image_ready.emit(np.array(frame, copy=True), display_name)

        if self._global_xrd.instant1d_enabled:
            return frame, saved_path, eff.exposure_ms, backend.width

    def _run_instant1d(self, frame: np.ndarray, image_path: Path | None,
                       exposure_ms: int, detector_width: int) -> None:
        settings = self._global_xrd
        ai = self._poni_ai
        try:
            result = reduce_frame(
                ai, frame, settings.instant1d_bin_width_deg,
                find_spots=self._find_spots_before_1d,
                spot_deviation_factor=self._spot_deviation_factor,
            )
        except Exception as exc:
            raise RunnerError("runtime.instant1d_reduction_failed", str(exc)) from exc
        self._logger.log_ops(f"[RADICON] 1D reduction completed ({len(result.radial)} bins)")
        self.xrd_profile_ready.emit(
            np.array(result.radial, copy=True), np.array(result.intensity, copy=True),
            image_path.name if image_path is not None else f"{datetime.now():%H:%M:%S} (not saved)",
        )

        if image_path is not None and settings.instant1d_autosave:
            formats = tuple(key for key in ("csv", "tsv", "gsas", "igor")
                            if getattr(settings, f"instant1d_fmt_{key}"))
            paths, errors = save_profile(
                result.radial, result.intensity, ai.wavelength, ai.dist, image_path,
                formats=formats, units=settings.instant1d_units,
                exposure_ms=exposure_ms,
                binning="2x2" if detector_width < 2000 else "1x1",
                source="Experimental Scheduler",
            )
            for path in paths:
                self._logger.log_ops(f"[RADICON] 1D saved → {path}")
            if errors:
                raise RunnerError("runtime.instant1d_save_failed", "; ".join(errors))

        if settings.instant1d_send_pdi:
            name = image_path.stem if image_path is not None else datetime.now().strftime("BL18C_%Y%m%d_%H%M%S")
            try:
                profile = PdiProfile.from_pyfai(result, wavelength_m=ai.wavelength, name=name)
            except ValueError as exc:
                raise RunnerError("runtime.instant1d_profile_invalid", str(exc)) from exc
            self.pdi_profile_ready.emit(profile)

    def _do_take_dark(self, action: TakeDarkAction) -> None:
        backend = self._ctx.radicon
        self._logger.log_ops(
            f"[RADICON] take_dark: capture_triggered({action.exposure_ms} ms)"
        )
        backend.capture_triggered(
            action.exposure_ms, timeout_ms=action.exposure_ms + 5000,
        )
        self._logger.log_ops(f"[RADICON] dark frame captured ({action.exposure_ms} ms)")
        self.progress_updated.emit(f"Dark frame captured ({action.exposure_ms} ms)")

    # ------------------------------------------------------------------ Ch11 oscillation

    def _osc_loop(
        self,
        eff: _EffectiveXrd,
        osc_stop: threading.Event,
        pos_a: int,
        pos_b: int,
        osc_exception: list,
    ) -> None:
        """Background Ch11 oscillation during XRD exposure.

        Runs A→B→A→... until osc_stop is set.
        Exits cleanly mid-move (does NOT stop the motor — caller does that via normal_stop).

        Any exception here is appended to ``osc_exception`` (a single-slot
        list acting as the thread-safe handoff back to `_do_take_xrd()`, which
        reads it only after joining this thread) — REORGANISATION_PLAN.md
        Phase 9. Previously this only surfaced as a progress_updated message,
        so `_do_take_xrd()`'s finally block had no way to know oscillation had
        actually failed and would report the whole sequence step as
        successful regardless.
        """
        ctrl = self._ctx.controller
        if ctrl is None:
            return
        motion = self._motion_lease

        try:
            def _move_and_wait(target: int) -> bool:
                """Issue absolute move to target; poll until stopped or osc_stop set.
                Returns True if motor reached target, False if osc_stop fired first."""
                ctrl.set_ch_speed(11, eff.osc_speed, stay_in_rem=True, motion=motion)
                ctrl.move_ch_absolute(11, target, motion=motion)
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
            osc_exception.append(exc)

    def _return_ch11_to_zero(self, speed: str) -> None:
        """Stop any in-progress Ch11 move, then drive to 0° and wait for arrival.

        Called after oscillation ends (capture_triggered returned).
        Raises _StopRequested if the user requests sequence stop during the
        return — should_stop=... makes wait_until_stop() abandon the wait as
        soon as that happens (rather than waiting out the full stop
        confirmation), and the _check_stop() right after turns that into the
        same immediate unwind the old hand-rolled wait loop had. This also
        means the "returned to θ=0°" log below only ever fires when the move
        actually completed — never when it was cut short by a stop request.

        The normal_stop() below revokes self._motion_lease as a side effect
        (MotionCoordinator.revoke_for_stop() invalidates whichever lease is
        HELD, ours included) — _resume_motion_after_self_stop() re-acquires
        it before the lease is used again for the move back to zero.

        This is the one accepted exception to the sequence-wide "stay in
        REM" policy (see run()'s finally block): normal_stop() sends
        ASSTP+LOC as one atomic transaction (control_stage.py), so every
        oscillation cycle dips into LOC for an instant here before the
        speed-set/move below put it back into REM. Agreed with the user as
        an acceptable blip, once per oscillation cycle, rather than
        reworking the shared stop transaction in control_stage.py.
        """
        ctrl = self._ctx.controller
        if ctrl is None:
            return
        try:
            ctrl.normal_stop(source="exp_scheduler")   # ASSTP — decelerate-stop any in-progress move
        except Exception:
            pass
        self._resume_motion_after_self_stop()
        # Brief deceleration pause
        time.sleep(0.3)
        ctrl.set_ch_speed(11, speed, stay_in_rem=True, motion=self._motion_lease)
        ctrl.move_ch_absolute(11, 0, motion=self._motion_lease)
        ctrl.wait_until_stop(
            motion=self._motion_lease, should_stop=lambda: self._stop_event.is_set(),
            stay_in_rem=True,
        )
        self._check_stop()
        self._logger.log_ops("[CH11] returned to θ=0°")
        self.progress_updated.emit("[CH11] Returned to θ=0°")

    # ------------------------------------------------------------------ camera session

    def _camera_indices_for_actions(self, actions: list[Action]) -> set[int]:
        indices: set[int] = set()
        for action in actions:
            if isinstance(action, SaveSnapshotAction):
                indices.add(0)
            elif isinstance(action, (SaveReferenceImageAction, StartFollowingAction,
                                     AlignToReferenceAction)):
                indices.add(int(action.camera_index))
            elif isinstance(action, FollowSampleAction):
                indices.add(int(action.camera_index))
            elif isinstance(action, ForLoopAction):
                indices.update(self._camera_indices_for_actions(action.body))
        return indices

    def latest_camera_frame(self) -> tuple[np.ndarray, float] | None:
        """(copy of the camera session's latest frame, its monotonic capture
        time), or None when no session frame exists. Thread-safe — used by the
        scheduler window's monitor panel to show the live image."""
        with self._camera_frame_lock:
            if self._camera_current_frame is None:
                return None
            return self._camera_current_frame.copy(), self._camera_last_frame_at

    def _get_camera_frame_after(self, previous_time: float, purpose: str) -> np.ndarray:
        """Wait for a frame captured after a completed stage correction."""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self._check_stop()
            with self._camera_frame_lock:
                if (self._camera_current_frame is not None
                        and self._camera_last_frame_at > previous_time):
                    frame = self._camera_current_frame.copy()
                    if frame.size and int(frame.max()) > 0:
                        return frame
            time.sleep(0.05)
        raise RunnerError("runtime.camera_stale", f"No fresh camera frame for {purpose}")

    def _align_to_xrd_reference(
        self, settings: scheduler_settings.GlobalSpectrumSettings,
    ) -> tuple[float, float] | None:
        """Use the live microscope frame and saved reference before ruby motion.

        For gasket beta, returns the image shift (px) that would bring the
        gasket centre itself onto the X-ray beam from the aligned position —
        the origin of gasket-centre ruby offsets. Returns None otherwise.
        """
        reference_path = self._global_follow.reference_path
        if not reference_path:
            raise RunnerError("runtime.spectrum_reference_missing", "XRD reference image is not configured")
        reference = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
        if reference is None:
            raise RunnerError("runtime.spectrum_reference_missing", f"Cannot load XRD reference image: {reference_path}")
        try:
            matrix = np.asarray(json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))["matrix_inv"], dtype=float)
        except Exception as exc:
            raise RunnerError("runtime.spectrum_calibration_invalid", f"Cannot load camera calibration: {exc}") from exc
        if matrix.shape != (2, 2) or not np.isfinite(matrix).all():
            raise RunnerError("runtime.spectrum_calibration_invalid", "Camera calibration matrix is invalid")
        gasket_cfg = None
        if self._global_follow.tracking_method == "gasket_beta":
            try:
                gasket_cfg = self._resolve_gasket_reference(reference_path, reference, matrix)
            except (RuntimeError, ValueError, OSError, cv2.error) as exc:
                raise RunnerError("runtime.spectrum_image_match_failed", str(exc)) from exc
        elif self._global_follow.tracking_method != "similarity":
            raise RunnerError("runtime.spectrum_image_match_failed", "Unknown position tracking method")
        self._start_camera_session_if_needed(forced_index=0)
        self._require_camera_index(0)
        ctrl = self._ctx.controller
        if settings.xrd_reference_ch3_pulse is not None:
            target_ch3 = int(settings.xrd_reference_ch3_pulse)
            if int(ctrl.get_ch_pos(3)) != target_ch3:
                self._do_stage(StageAction(
                    operation="move_absolute", ch=3, value=target_ch3, speed=settings.speed,
                ), {})
                self._get_camera_frame_after(time.monotonic(), "Ch3 XRD restoration")
        limits = (
            int(self._global_follow.max_correction_ch4_um / PULSE_SCALE[4]),
            int(self._global_follow.max_correction_ch5_um / PULSE_SCALE[5]),
        )
        for attempt in range(6):
            self._check_stop()
            if gasket_cfg is not None:
                dx, dy, score = self._measure_gasket_average(
                    gasket_cfg, "XRD image alignment"
                )
                aligned = self._follow_frame_accepted(gasket_cfg, dx, dy, score)
            else:
                frame = self._get_camera_frame("XRD image alignment")
                try:
                    dx, dy, score = measure_xy_shift_strict(reference, frame)
                except ValueError as exc:
                    raise RunnerError("runtime.spectrum_image_match_failed", str(exc)) from exc
                aligned = max(abs(dx), abs(dy)) <= 2
            shift_text = (self._gasket_offset_text(gasket_cfg, dx, dy)
                          if gasket_cfg is not None else f"{dx:+d}, {dy:+d} px")
            self.progress_updated.emit(
                f"XRD image alignment {attempt + 1}/6: "
                f"{'gasket score' if gasket_cfg is not None else 'match'} {score:.2f}, "
                f"shift {shift_text}"
            )
            # The gasket limit is an acceptance criterion after correction,
            # not a deadband that prevents the first correction.
            if aligned and (gasket_cfg is None or attempt > 0):
                if gasket_cfg is None:
                    return None
                # Rim centre is at target + (dx, dy); the beam is at
                # target + sample offset.
                ox, oy = gasket_cfg["gasket_sample_offset"]
                return ox - dx, oy - dy
            motor_disp = -(matrix @ np.array([dx, dy]))
            corrections = (np.trunc(motor_disp).astype(int) if gasket_cfg is not None
                           else np.rint(motor_disp).astype(int))
            if any(abs(int(corrections[i])) > limits[i] for i in (0, 1)):
                raise RunnerError(
                    "runtime.spectrum_image_correction_limit",
                    "XRD image correction exceeds the configured per-step follow limit",
                )
            if not corrections.any():
                if gasket_cfg is not None and aligned:
                    ox, oy = gasket_cfg["gasket_sample_offset"]
                    return ox - dx, oy - dy
                raise RunnerError("runtime.spectrum_image_not_correctable", "XRD image shift cannot be corrected")
            for ch, delta in ((4, int(corrections[0])), (5, int(corrections[1]))):
                if delta:
                    self._do_stage(StageAction(
                        operation="move_relative", ch=ch, value=delta, speed=settings.speed,
                    ), {})
            self._do_wait(0.15)
            captured_after = time.monotonic()
            self._get_camera_frame_after(captured_after, "XRD image alignment")
        raise RunnerError("runtime.spectrum_alignment_not_converged", "XRD image alignment did not converge")

    def _start_camera_session_if_needed(self, forced_index: int | None = None) -> None:
        indices = ({forced_index} if forced_index is not None
                   else self._camera_indices_for_actions(self._sequence.actions))
        if not indices:
            return
        if len(indices) != 1:
            raise RuntimeError(
                "Interactive Camera actions in one sequence must use the same "
                f"camera index; found {sorted(indices)}"
            )

        camera_index = next(iter(indices))
        if self._camera_cap is not None:
            self._require_camera_index(camera_index)
            return
        self._logger.log_ops(f"[CAMERA] opening camera index {camera_index}")
        cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Cannot open camera index {camera_index}")

        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        self._camera_cap = cap
        self._camera_index = camera_index
        self._camera_current_frame = None
        self._camera_last_frame_at = 0.0
        self._camera_error = None
        self._camera_stop_event.clear()
        self._camera_thread = threading.Thread(
            target=self._camera_capture_loop,
            name="ExpSchedulerCamera",
            daemon=True,
        )
        self._camera_thread.start()
        self._get_camera_frame("camera initialisation", timeout_s=3.0)
        self._logger.log_ops(f"[CAMERA] camera index {camera_index} ready")

        self._af_ch3 = AutoFocus(
            self._ctx.controller, cap=None, channel=3,
            # Deliberately un-wrapped: a camera failure (or a _StopRequested
            # from a sequence stop landing mid-read) must raise and abort the
            # scan via AutoFocus's own exception handling, not be swallowed
            # into a fake zero-sharpness data point that AF would then "find
            # the best of" and move to.
            frame_provider=lambda: self._get_camera_frame_after(
                time.monotonic(), "autofocus"),
            log_callback=self.progress_updated.emit,
            # Ordinary stop_following() finishes the active correction cycle.
            should_stop=lambda: self._stop_event.is_set(),
            release_on_complete=False,
        )

    def _camera_capture_loop(self) -> None:
        cap = self._camera_cap
        
        if cap is None:
            return
        while not self._camera_stop_event.is_set():
            read_started_at = time.monotonic()
            ret, frame = cap.read()
            if ret and frame is not None:
                with self._camera_frame_lock:
                    self._camera_current_frame = frame.copy()
                    # Use read start, so a frame whose read overlapped a
                    # stage move is not mistaken for a post-move frame.
                    self._camera_last_frame_at = read_started_at
                    self._camera_error = None
            else:
                with self._camera_frame_lock:
                    self._camera_error = "Camera read failed"
                time.sleep(0.05)

    def _cleanup_camera_session(self) -> None:
        self._af_ch3 = None
        self._camera_stop_event.set()
        if self._camera_thread is not None:
            self._camera_thread.join(timeout=2.0)
            if self._camera_thread.is_alive():
                # A genuine hang: cap.read() (inside _camera_capture_loop(),
                # a background thread) never returned. Previously the
                # thread reference was cleared and self._camera_cap.release()
                # was called regardless — racing that still-running read()
                # call on the same VideoCapture object (round-2 external
                # review, same class of bug as the follow-thread join
                # timeout). Keep both references (mirrors
                # _stop_follow()/_cleanup_follow_thread()) and skip
                # release() — _safe_cleanup() records this as a cleanup
                # failure rather than silently pressing on.
                raise RuntimeError(
                    "camera capture thread did not stop within 2s — "
                    "VideoCapture.release() skipped to avoid racing its "
                    "still-running cap.read()"
                )
            self._camera_thread = None
        if self._camera_cap is not None:
            self._camera_cap.release()
            self._camera_cap = None
        self._camera_index = None
        with self._camera_frame_lock:
            self._camera_current_frame = None
            self._camera_last_frame_at = 0.0
            self._camera_error = None

    def _require_camera_index(self, camera_index: int) -> None:
        if self._camera_cap is None:
            raise RuntimeError("Interactive Camera session is not open")
        if self._camera_index != int(camera_index):
            raise RuntimeError(
                f"Camera index {camera_index} requested, but run camera session "
                f"is index {self._camera_index}"
            )

    def _get_camera_frame(self, purpose: str, timeout_s: float = 3.0) -> np.ndarray:
        deadline = time.monotonic() + timeout_s
        last_black: np.ndarray | None = None
        last_error: str | None = None
        while time.monotonic() < deadline:
            with self._camera_frame_lock:
                frame = (
                    None if self._camera_current_frame is None
                    else self._camera_current_frame.copy()
                )
                last_error = self._camera_error
            if frame is not None:
                if frame.size and int(frame.max()) > 0:
                    return frame
                last_black = frame
            if self._stop_event.is_set():
                raise _StopRequested()
            time.sleep(0.05)
        if last_black is not None:
            raise RuntimeError(
                f"Camera returned only black frames while waiting for {purpose}"
            )
        raise RuntimeError(last_error or f"No camera frame available for {purpose}")

    def _run_autofocus_sync(self, af: AutoFocus, motion) -> dict | None:
        """Run af.perform_autofocus() and block until it finishes.

        perform_autofocus() is async (spawns a thread, calls
        completion_callback only on the success path). We wait on thread
        liveness rather than only on the callback, since cancellation /
        errors / "no sharpness data" all skip the callback and would
        otherwise hang this wait forever. A non-success outcome raises so the
        follow session cannot proceed with an unknown Ch3 position.

        af is constructed with release_on_complete=False (the motion lease
        is the sequence-wide one, owned by run()'s finally — AutoFocus must
        not release it), so it never switches back to LOC itself either
        (every intermediate wait inside it uses stay_in_rem=True). We leave
        it in REM here too — the sequence-wide "stay in REM" policy means
        LOC is sent exactly once, in run()'s finally, after the whole
        sequence ends.
        """
        result: dict = {}
        done = threading.Event()
        errors: list[str] = []

        def _on_complete(sharpness_data, best_pos, best_sharpness, fit_result):
            result.update(
                sharpness_data=sharpness_data, best_pos=best_pos,
                best_sharpness=best_sharpness, fit_result=fit_result,
            )
            done.set()

        af.completion_callback = _on_complete
        af.error_callback = errors.append
        if not af.perform_autofocus(motion=motion):
            raise RunnerError("runtime.follow_autofocus_failed", "Ch3 autofocus did not start")

        thread = af.focus_thread
        warned_slow_stop = False
        while thread is not None and thread.is_alive():
            if self._stop_event.is_set() and not warned_slow_stop:
                self.progress_updated.emit(
                    "[AF] Stop requested — waiting for in-flight Ch3 move to finish")
                warned_slow_stop = True
            thread.join(timeout=0.2)

        self._check_stop()
        if errors:
            raise RunnerError("runtime.follow_autofocus_failed", f"Ch3 autofocus failed: {errors[-1]}")
        if not done.is_set():
            raise RunnerError("runtime.follow_autofocus_failed", "Ch3 autofocus did not complete")
        return result

    # ------------------------------------------------------------------ run directory

    def _run_subdir(self, name: str) -> Path:
        """<run_dir>/<name>/ for this run's data of one category (not created).

        Every file a run saves lives under the one run directory that
        RunLogger.start() created, so a run's data never scatters."""
        run_dir = self._logger.run_dir
        if run_dir is None:
            raise RunnerError(
                "runtime.run_directory_unavailable",
                "Run directory is not available; run logging has not started",
            )
        return run_dir / name

    # ------------------------------------------------------------------ camera / reference

    def _do_save_reference(self, action: SaveReferenceImageAction) -> None:
        self._logger.log_ops(
            f"[CAMERA] save_reference_image(path={action.path!r}, "
            f"camera_index={action.camera_index})"
        )
        self._require_camera_index(action.camera_index)
        frame = self._get_camera_frame_after(time.monotonic(), "save_reference_image")

        out = Path(action.path) if action.path else _DEFAULT_REF_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out), frame):
            raise RuntimeError(f"Could not save reference image: {out}")
        self.file_saved.emit(self._current_step_idx, str(out))
        self._logger.log_ops(f"[CAMERA] reference image saved → {out}")
        self.progress_updated.emit(f"Reference image saved → {out}")

    def _do_save_snapshot(self, action: SaveSnapshotAction) -> None:
        out_dir = self._run_subdir(PHOTO_SUBDIR)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out = out_dir / f"snapshot_{timestamp}.png"
        self._logger.log_ops("[CAMERA] save_snapshot()")

        camera_index = 0
        self._require_camera_index(camera_index)
        frame = self._get_camera_frame_after(time.monotonic(), "save_snapshot")

        out_dir.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out), frame):
            raise RuntimeError(f"Could not save snapshot: {out}")
        self.file_saved.emit(self._current_step_idx, str(out))
        self._logger.log_ops(f"[CAMERA] snapshot saved -> {out}")
        self.progress_updated.emit(f"Snapshot saved -> {out}")

    # ------------------------------------------------------------------ follow thread

    def _start_follow(self, action: StartFollowingAction) -> None:
        if self._follow_thread is not None and self._follow_thread.is_alive():
            raise RuntimeError(
                "start_following called while a follow session is already active"
            )
        self._current_follow_action = action
        self._follow_stop_event.clear()
        self._follow_first_cycle_done.clear()
        self._follow_exception = None
        self._follow_thread = threading.Thread(
            target=self._follow_loop, args=(action,), daemon=True
        )
        self._follow_thread.start()
        self.progress_updated.emit("Sample following started")

    def _stop_follow(self) -> None:
        """Signal the follow thread to stop and block until it fully exits.

        dsl/api.py::stop_following()'s documented contract is "Blocks until
        the following thread has fully stopped" — this must be fail-closed
        (REORGANISATION_PLAN.md Phase 9): a join() timeout used to be
        treated as success (thread reference cleared unconditionally,
        stop_following() returning normally), letting the caller proceed
        to microscope_out_and_fpd_in/stage-mode-switch steps while the
        follow thread could still be moving Ch3/4/5. On timeout this now
        raises instead of clearing `self._follow_thread`, so the terminal
        exception handler in `_execute_actions()` aborts the run and
        run()'s finally-block cleanup (`_cleanup_follow_thread()`) still has
        the live thread reference to attempt a further join against.

        Also surfaces any exception `_follow_loop()` itself raised (MOVE_
        CONSTRAINTS violation, camera/stage communication failure,
        autofocus failure, …) — previously that only reached
        progress_updated as an informational message and the sequence
        continued as if following had succeeded.

        Both failure branches below call `_abort_follow_thread()` before
        raising — a genuine timeout means the thread may still be driving
        Ch3/4/5, and it must be physically stopped (ASSTP) before cleanup
        (motion lease release, camera teardown) proceeds, not just logically
        marked as failed (REORGANISATION_PLAN.md Phase 9 external review,
        round 2). For the exc-is-not-None branch this is normally a no-op
        report-wise (`_follow_loop()`'s own exception handler already called
        `_abort_follow_thread()` for the same failure before this method
        ever sees it) but still repeats the physical stop for safety.
        """
        if self._follow_thread is not None:
            deadline = time.monotonic() + _FOLLOW_STOP_TIMEOUT_S
            # A newly started follow session must complete one verified
            # correction before a measurement may proceed, even when the
            # temperature wait finishes immediately.
            while (self._follow_thread.is_alive()
                   and not self._follow_first_cycle_done.is_set()
                   and not self._stop_event.is_set()
                   and time.monotonic() < deadline):
                self._follow_thread.join(timeout=max(0.0, min(0.2, deadline - time.monotonic())))
            self._follow_stop_event.set()
            remaining = max(0.0, deadline - time.monotonic())
            self._follow_thread.join(timeout=min(5.0, remaining) if self._stop_event.is_set() else remaining)
            if self._follow_thread.is_alive():
                self._logger.log_ops(
                    f"[CAMERA] stop_following() timed out after {_FOLLOW_STOP_TIMEOUT_S:g}s — "
                    "follow thread still alive"
                )
                message = (
                    f"stop_following() timed out after {_FOLLOW_STOP_TIMEOUT_S:g}s — the background "
                    "follow thread is still running and may still be "
                    "driving Ch3/4/5"
                )
                self._abort_follow_thread("runtime.follow_thread_stop_timeout", message)
                raise RunnerError("runtime.follow_thread_stop_timeout", message)
            self._follow_thread = None
        self._check_stop()
        if not self._follow_first_cycle_done.is_set() and self._follow_exception is None:
            message = "Sample following stopped before its first verified correction"
            self._abort_follow_thread("runtime.follow_first_cycle_missing", message)
            raise RunnerError("runtime.follow_first_cycle_missing", message)
        self._current_follow_action = None
        exc = self._follow_exception
        self._follow_exception = None
        if exc is not None:
            self._logger.log_ops(f"[CAMERA] follow thread exception: {exc}")
            message = f"Sample following failed: {exc}"
            self._abort_follow_thread("runtime.follow_thread_failed", message)
            raise RunnerError("runtime.follow_thread_failed", message) from exc
        self.progress_updated.emit("Sample following stopped")

    def _cleanup_follow_thread(self) -> None:
        """Best-effort follow-thread teardown for run()'s finally block.

        Unlike `_stop_follow()`, this must never raise — it runs in the
        unconditional cleanup path alongside camera/motion-lease teardown
        (see run()'s finally and `_safe_cleanup()`), so a still-alive
        thread or a leftover `_follow_exception` is logged, not raised.
        The thread reference is kept (not cleared) when still alive after
        the join, so it remains visible for diagnostics rather than being
        silently forgotten.
        """
        self._set_gasket_preview_enabled(False)
        self._follow_stop_event.set()
        if self._follow_thread is not None:
            self._follow_thread.join(timeout=5)
            if self._follow_thread.is_alive():
                self._logger.log_ops(
                    "[CAMERA] cleanup: follow thread still alive after 5s join"
                )
            else:
                self._follow_thread = None
        if self._follow_exception is not None:
            self._logger.log_ops(
                f"[CAMERA] cleanup: follow thread had failed: {self._follow_exception}"
            )
            self._follow_exception = None

    # ------------------------------------------------------------------ follow loop

    def _follow_should_stop(self) -> bool:
        return self._follow_stop_event.is_set() or self._stop_event.is_set()

    def _resolve_follow_config(
        self, action: StartFollowingAction | AlignToReferenceAction,
    ) -> dict:
        """Resolve per-step follow settings against GlobalFollowSettings and
        load calibration + reference frame. Shared by the background follow
        loop (start_following / follow_sample_position) and the one-shot
        align_to_reference step, so both use exactly the same parameters.
        """
        gf = self._global_follow

        # Resolve settings: per-step action values take priority over global
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

        # Effective autofocus settings — action.autofocus_enabled is the
        # per-step value (default True; DSL/Visual/JSON can set it False
        # to skip Ch3 autofocus for this step — actions.py,
        # dsl/api.py::start_following()/follow_sample_position()/
        # align_to_reference()). Other autofocus fields (range/steps) still
        # fall back to global.
        af_range_um = (
            action.autofocus_range_um if action.autofocus_range_um is not None
            else gf.autofocus_range_um
        )
        af_steps = (
            action.autofocus_steps if action.autofocus_steps is not None
            else gf.autofocus_steps
        )

        # Calibration
        cal = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))

        # Reference frame
        ref_path = action.reference_path or gf.reference_path
        if ref_path is None:
            raise RuntimeError(
                "No reference image configured — set one via "
                "Global Settings > Sample Positions > Set up sample positions "
                "(or Reference Image > Load from…), "
                "or specify reference_path on this step"
            )
        reference_frame = cv2.imread(str(ref_path), cv2.IMREAD_COLOR)
        if reference_frame is None:
            raise RuntimeError(f"Could not load reference image: {ref_path}")

        gasket_cfg = {"tracking_method": gf.tracking_method,
                      "reference_frame": reference_frame,
                      "gasket_tolerance_px": gf.gasket_tolerance_px,
                      "gasket_tolerance_um": gf.gasket_tolerance_um}
        if gf.tracking_method == "gasket_beta":
            gasket_cfg = self._resolve_gasket_reference(
                ref_path, reference_frame, cal["matrix_inv"]
            )
        elif gf.tracking_method != "similarity":
            raise RuntimeError(f"Unknown follow tracking method: {gf.tracking_method}")

        return {
            **gasket_cfg,
            "similarity_threshold": similarity_threshold,
            "lim4": max(0, int(max_ch4_um / PULSE_SCALE[4])),
            "lim5": max(0, int(max_ch5_um / PULSE_SCALE[5])),
            "xy_max_retries": gf.xy_max_retries,
            "af_enabled": action.autofocus_enabled,
            "af_range_um": af_range_um,
            "af_steps": af_steps,
            "M_inv": np.array(cal["matrix_inv"]),
        }

    def _resolve_gasket_reference(
        self, ref_path: str | Path, reference_frame: np.ndarray,
        matrix_inv=None,
    ) -> dict:
        """Validate the one annotated XRD image used by following and ruby.

        Returns the gasket part of a follow config. The X-ray beam is not
        resolved here: ``_gasket_target()`` re-reads the shared beam file at
        every measurement so the scheduler can never act on a stale copy.
        """
        gf = self._global_follow
        tolerance_um = gf.gasket_tolerance_um
        tolerance_px = gf.gasket_tolerance_px
        if tolerance_um is not None:
            try:
                tolerance_um = float(tolerance_um)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Gasket tolerance must be a positive finite µm distance") from exc
            if not math.isfinite(tolerance_um) or tolerance_um <= 0:
                raise RuntimeError("Gasket tolerance must be a positive finite µm distance")
        else:
            try:
                tolerance_px = float(tolerance_px)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Legacy gasket tolerance must be a positive finite pixel distance") from exc
            if not math.isfinite(tolerance_px) or tolerance_px <= 0:
                raise RuntimeError("Legacy gasket tolerance must be a positive finite pixel distance")
        try:
            um_matrix = stage_um_per_pixel(
                matrix_inv if matrix_inv is not None
                else json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))["matrix_inv"]
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Cannot use camera calibration for gasket tracking: {exc}") from exc
        frames = gf.gasket_average_frames
        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 10:
            raise RuntimeError("Gasket average frame count must be between 1 and 10")
        if str(ref_path) != str(gf.reference_path):
            raise RuntimeError("Gasket beta requires the annotated Global Settings reference image")
        if not gf.gasket_ellipse or not gf.gasket_reference_digest:
            raise RuntimeError("Mark the gasket outline in Sample Positions before using gasket beta")
        if reference_digest(ref_path) != gf.gasket_reference_digest:
            raise RuntimeError("Gasket outline belongs to a different reference image; mark it again")
        if not gf.gasket_sample_point:
            raise RuntimeError(
                "Mark the sample centre with Mark gasket outline before using gasket beta"
            )
        marked = GasketEllipse.from_dict(gf.gasket_ellipse)
        try:
            detection = detect_gasket_hole(reference_frame, reference_frame, marked)
            centre = (detection.ellipse.cx, detection.ellipse.cy)
            offset = sample_offset(gf.gasket_sample_point, centre)
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Gasket reference cannot be detected: {exc}") from exc
        # The beam pixel must belong to this camera geometry; the microscope
        # position is checked again at every measurement.
        try:
            check_xray_beam(load_xray_beam(), reference_frame.shape,
                            channels=GASKET_BEAM_CHANNELS)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return {
            "tracking_method": "gasket_beta",
            "reference_frame": reference_frame,
            "gasket_ellipse": gf.gasket_ellipse,
            "gasket_allow_uniform_scale": gf.gasket_allow_uniform_scale,
            "gasket_reference_centre": centre,
            "gasket_sample_offset": offset,
            "gasket_tolerance_px": tolerance_px,
            "gasket_tolerance_um": tolerance_um,
            "gasket_um_matrix": um_matrix,
            "gasket_average_frames": frames,
        }

    def _gasket_target(self, cfg: dict) -> tuple[float, float]:
        """Rim centre at which the marked sample sits on the X-ray beam.

        Reads the shared beam file and the live Ch6/Ch8 each time, so a
        beam re-registered in Interactive Camera is used at once and a moved
        microscope stops the alignment instead of steering to a stale pixel.
        """
        try:
            pulses = read_microscope_pulses(
                self._ctx.controller, require_stopped=False,
                channels=GASKET_BEAM_CHANNELS,
            )
            beam = check_xray_beam(load_xray_beam(), cfg["reference_frame"].shape,
                                   pulses, channels=GASKET_BEAM_CHANNELS)
        except (RuntimeError, ValueError) as exc:
            raise RunnerError("runtime.xray_beam_invalid", str(exc)) from exc
        ox, oy = cfg["gasket_sample_offset"]
        target = (beam.x - ox, beam.y - oy)
        if cfg.get("gasket_beam") != beam.point:
            self._logger.log_ops(
                f"[GASKET] X-ray beam=({beam.x:.1f},{beam.y:.1f}) "
                f"sample offset=({ox:+.1f},{oy:+.1f}) target rim centre="
                f"({target[0]:.1f},{target[1]:.1f})"
            )
        cfg["gasket_beam"] = beam.point
        cfg["gasket_target_centre"] = target
        return target

    @staticmethod
    def _gasket_um_matrix(cfg: dict) -> np.ndarray:
        matrix = cfg.get("gasket_um_matrix")
        if matrix is not None:
            return matrix
        # Direct callers of the measurement helpers may provide only M_inv.
        if cfg.get("M_inv") is not None:
            return stage_um_per_pixel(cfg["M_inv"])
        return load_stage_um_per_pixel(_CALIBRATION_PATH)

    @classmethod
    def _gasket_offset_text(cls, cfg: dict, dx: float, dy: float) -> str:
        ux, uy = displacement_um(cls._gasket_um_matrix(cfg), dx, dy)
        return (f"({dx:+.1f}, {dy:+.1f}) px / "
                f"({ux:+.1f}, {uy:+.1f}) µm "
                f"(|offset|={math.hypot(dx, dy):.1f} px / {math.hypot(ux, uy):.1f} µm)")

    def _measure_follow_frame(self, cfg: dict, frame: np.ndarray) -> tuple[float, float, float]:
        """Return XY error and evidence score, rejecting uncertain gasket fits."""
        reference = cfg["reference_frame"]
        if cfg["tracking_method"] == "similarity":
            dx, dy = self._compute_xy_shift(reference, frame)
            return dx, dy, self._compute_similarity(reference, frame)
        target = cfg.get("gasket_target_centre") or self._gasket_target(cfg)
        # Search at the target first; the reference location covers a stage
        # that has not yet been moved since the reference was captured.
        marked = GasketEllipse.from_dict(cfg["gasket_ellipse"])
        try:
            detection = detect_gasket_hole_near(
                reference, frame, marked,
                (target, cfg.get("gasket_last_centre"), cfg["gasket_reference_centre"]),
                allow_uniform_scale=cfg.get("gasket_allow_uniform_scale", False),
            )
        except ValueError as exc:
            raise RunnerError("runtime.gasket_detection_failed", str(exc)) from exc
        cfg["gasket_last_centre"] = (detection.ellipse.cx, detection.ellipse.cy)
        cx0, cy0 = target
        self._logger.log_ops(
            f"[GASKET] centre=({detection.ellipse.cx:.1f},{detection.ellipse.cy:.1f}) "
            f"score={detection.score:.3f} coverage={detection.coverage}/8 "
            f"residual={detection.residual_px:.1f}px "
            f"(~{detection.residual_px * nominal_um_per_pixel(self._gasket_um_matrix(cfg)):.1f}µm)"
        )
        return (detection.ellipse.cx - cx0,
                detection.ellipse.cy - cy0, detection.score)

    def _measure_gasket_average(self, cfg: dict, purpose: str) -> tuple[float, float, float]:
        """Average coherent detections from the configured fresh live frames.

        A moving rim must be detected before averaging: averaging the pixels
        would blur the edge. Require a majority of detections near their median
        so a transient bad fit cannot command a stage move or pass verification.
        """
        samples: list[tuple[float, float, float]] = []
        errors: list[str] = []
        frame_count = cfg.get("gasket_average_frames", 5)
        if (isinstance(frame_count, bool) or not isinstance(frame_count, int)
                or not 1 <= frame_count <= 10):
            raise RunnerError(
                "runtime.gasket_detection_failed",
                "Gasket average frame count must be between 1 and 10",
            )
        required = frame_count // 2 + 1
        if "gasket_sample_offset" in cfg:
            self._gasket_target(cfg)
        for _ in range(frame_count):
            frame = self._get_camera_frame_after(time.monotonic(), purpose)
            try:
                samples.append(self._measure_follow_frame(cfg, frame))
            except RunnerError as exc:
                if exc.code != "runtime.gasket_detection_failed":
                    raise
                errors.append(str(exc))
        if len(samples) < required:
            raise RunnerError(
                "runtime.gasket_detection_failed",
                f"Only {len(samples)}/{frame_count} gasket frames were detected for {purpose}: "
                f"{errors[-1] if errors else 'insufficient valid frames'}",
            )
        xy = np.asarray([(dx, dy) for dx, dy, _score in samples], dtype=float)
        median = np.median(xy, axis=0)
        deviations = xy - median
        distances = np.linalg.norm(deviations, axis=1)
        um_matrix = self._gasket_um_matrix(cfg)
        um_distances = np.linalg.norm(deviations @ um_matrix.T, axis=1)
        if cfg.get("gasket_tolerance_um") is not None:
            # Frame coherence describes detector noise, independently of the
            # final allowed positioning error set by the operator.
            inliers = um_distances <= (_GASKET_COHERENCE_RADIUS_PX
                                       * nominal_um_per_pixel(um_matrix))
        else:
            inliers = distances <= _GASKET_COHERENCE_RADIUS_PX
        if int(np.count_nonzero(inliers)) < required:
            raise RunnerError(
                "runtime.gasket_detection_failed",
                f"Gasket centres varied too much across {frame_count} frames for {purpose} "
                f"({int(np.count_nonzero(inliers))} coherent frames; "
                f"spread up to {float(np.max(distances)):.1f} px / "
                f"{float(np.max(um_distances)):.1f} µm)",
            )
        centre = np.mean(xy[inliers], axis=0)
        scores = np.asarray([score for _dx, _dy, score in samples])
        score = float(np.mean(scores[inliers]))
        spread = float(np.max(distances[inliers]))
        spread_um = float(np.max(um_distances[inliers]))
        dx, dy = float(centre[0]), float(centre[1])
        self._logger.log_ops(
            f"[GASKET AVG] {purpose}: offset={self._gasket_offset_text(cfg, *centre)} "
            f"score={score:.3f} coherent={int(np.count_nonzero(inliers))}/{frame_count} "
            f"spread={spread:.1f}px / {spread_um:.1f}µm"
        )
        return dx, dy, score

    @staticmethod
    def _follow_frame_accepted(cfg: dict, dx: float, dy: float, score: float) -> bool:
        if cfg["tracking_method"] == "gasket_beta":
            if cfg.get("gasket_tolerance_um") is not None:
                return (distance_um(SequenceRunner._gasket_um_matrix(cfg), dx, dy)
                        <= cfg["gasket_tolerance_um"])
            return math.hypot(dx, dy) <= cfg["gasket_tolerance_px"]
        return math.isfinite(score) and score >= cfg["similarity_threshold"]

    def _follow_correction_cycle(
        self, cfg: dict, cumulative: dict[int, int], *, pre_focus: bool = False,
        post_focus: bool = True,
    ) -> None:
        """One follow correction cycle: optional Ch3 pre-focus, XY correction
        from the reference image, bounded XY retries until the gasket residual
        or similarity passes, then optional Ch3 autofocus. Global limits clamp
        each correction before movement and are checked again after the move.

        Only a sequence-wide stop ends the cycle early; a graceful
        stop_following() waits for the active cycle to complete.
        """
        gf = self._global_follow
        M_inv = cfg["M_inv"]
        lim4, lim5 = cfg["lim4"], cfg["lim5"]
        similarity_threshold = cfg["similarity_threshold"]
        xy_max_retries = cfg["xy_max_retries"]

        def autofocus() -> None:
            self._do_follow_autofocus(
                cfg["af_range_um"], cfg["af_steps"],
                method=gf.autofocus_method,
                n_frames=gf.autofocus_n_frames,
                speed=gf.autofocus_speed,
                peak_method=gf.autofocus_peak_method,
            )

        # Interactive Camera focuses before the first XY measurement and
        # again after the correction attempts.
        if pre_focus and cfg["af_enabled"]:
            autofocus()

        # Initial XY shift → motor pulses
        if cfg["tracking_method"] == "gasket_beta":
            dx_px, dy_px, sim = self._measure_gasket_average(
                cfg, "follow_sample_position"
            )
        else:
            frame = self._get_camera_frame_after(time.monotonic(), "follow_sample_position")
            dx_px, dy_px, sim = self._measure_follow_frame(cfg, frame)
        # Even an offset inside the final tolerance should be corrected when
        # calibration maps it to at least one whole motor pulse.
        motor_disp = -(M_inv @ np.array([dx_px, dy_px]))

        # The beta method uses a geometric residual; a match score alone does
        # not show that the reference location has been restored.
        if cfg["tracking_method"] == "gasket_beta":
            msg = f"[follow] Gasket offset: {self._gasket_offset_text(cfg, dx_px, dy_px)}; score={sim:.3f}"
        else:
            msg = f"[follow] Similarity: {sim:.3f}"
            if sim < similarity_threshold:
                msg += f" (below threshold {similarity_threshold:.2f})"
        self.progress_updated.emit(msg)

        # Apply initial XY correction
        d_ch4, d_ch5 = self._move_follow_xy(
            int(motor_disp[0]), int(motor_disp[1]), lim4, lim5,
        )
        if d_ch4 != 0 or d_ch5 != 0:
            cumulative[4] += d_ch4
            cumulative[5] += d_ch5
            self._logger.log_ops(
                f"[FOLLOW] correction: Δ Ch4={d_ch4:+d} Ch5={d_ch5:+d} | "
                f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d} | "
                f"{'gasket score' if cfg['tracking_method'] == 'gasket_beta' else 'similarity'}={sim:.3f}"
            )
            self.progress_updated.emit(
                f"[follow] Δ Ch4={d_ch4:+d} Ch5={d_ch5:+d} | "
                f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d}"
            )
        elif cfg["tracking_method"] == "gasket_beta":
            self._logger.log_ops(
                f"[FOLLOW] no XY move: requested Ch4={int(motor_disp[0]):+d} "
                f"Ch5={int(motor_disp[1]):+d} pulses; "
                "both applied corrections are zero after pulse quantisation and limits"
            )

        if self._stop_event.is_set():
            return

        # XY re-correction retry loop (mirrors interactive_camera behaviour)
        for _retry in range(max(0, xy_max_retries)):
            if self._stop_event.is_set():
                break
            if cfg["tracking_method"] == "gasket_beta":
                _dx, _dy, chk_sim = self._measure_gasket_average(cfg, "follow XY retry")
                if self._follow_frame_accepted(cfg, _dx, _dy, chk_sim):
                    break
                self.progress_updated.emit(
                    f"[follow] Gasket offset {self._gasket_offset_text(cfg, _dx, _dy)} — "
                    f"re-correcting XY (attempt {_retry + 1}/{xy_max_retries})"
                )
            else:
                chk_frame = self._get_camera_frame_after(time.monotonic(), "follow XY retry")
                chk_sim = self._compute_similarity(cfg["reference_frame"], chk_frame)
                if chk_sim >= similarity_threshold:
                    break
                self.progress_updated.emit(
                    f"[follow] Similarity {chk_sim:.3f} below threshold — "
                    f"re-correcting XY (attempt {_retry + 1}/{xy_max_retries})"
                )
                _dx, _dy = self._compute_xy_shift(cfg["reference_frame"], chk_frame)
            _disp = -(M_inv @ np.array([_dx, _dy]))
            _d4, _d5 = self._move_follow_xy(
                int(_disp[0]), int(_disp[1]), lim4, lim5,
            )
            if _d4 != 0 or _d5 != 0:
                cumulative[4] += _d4
                cumulative[5] += _d5
                if cfg["tracking_method"] == "gasket_beta":
                    self._logger.log_ops(
                        f"[FOLLOW] retry {_retry + 1}: Δ Ch4={_d4:+d} Ch5={_d5:+d} | "
                        f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d}"
                    )

        if self._stop_event.is_set():
            return

        # Optional Ch3 autofocus after XY correction
        if post_focus and cfg["af_enabled"]:
            autofocus()

    def _do_align_to_reference(self, action: AlignToReferenceAction) -> None:
        """One-shot, synchronous version of sample following: corrects on the
        main run thread, then retries XY once after autofocus if the gasket
        residual exceeds tolerance.

        Runs on the main thread (not a follow thread), so failures simply
        raise into the normal per-action error handling, and MotionRevoked-
        Error is not swallowed — on the main thread a revoked lease can only
        mean a real stop. `_follow_stop_event` is cleared first because the
        cycle and `_do_follow_autofocus()`/AutoFocus treat it as a stop
        request, and it stays set after any earlier stop_following(); the
        Stop button still sets both events, so user stops are honoured.
        """
        if self._follow_thread is not None and self._follow_thread.is_alive():
            raise RuntimeError(
                "align_to_reference called while a follow session is active — "
                "call stop_following() first"
            )
        cfg = self._resolve_follow_config(action)
        self._require_camera_index(action.camera_index)
        self._follow_stop_event.clear()
        self.progress_updated.emit("Aligning sample to reference image")

        cumulative = {4: 0, 5: 0}
        self._follow_correction_cycle(cfg, cumulative)
        self._check_stop()

        if cfg["tracking_method"] == "gasket_beta":
            final_dx, final_dy, final_sim = self._measure_gasket_average(
                cfg, "align_to_reference"
            )
            if not self._follow_frame_accepted(cfg, final_dx, final_dy, final_sim):
                # Autofocus can change the measured centre after the first XY
                # correction. Try XY once more without another autofocus.
                self._follow_correction_cycle(cfg, cumulative, post_focus=False)
                self._check_stop()
                final_dx, final_dy, final_sim = self._measure_gasket_average(
                    cfg, "align_to_reference final verification"
                )
        else:
            final_frame = self._get_camera_frame_after(time.monotonic(), "align_to_reference")
            final_dx = final_dy = 0
            final_sim = self._compute_similarity(cfg["reference_frame"], final_frame)
        if (cfg["tracking_method"] == "gasket_beta"
                and not self._follow_frame_accepted(cfg, final_dx, final_dy, final_sim)):
            raise RunnerError(
                "runtime.gasket_alignment_failed",
                f"Gasket centre remains {self._gasket_offset_text(cfg, final_dx, final_dy)} "
                "from reference",
            )
        if cfg["tracking_method"] == "gasket_beta":
            result = (f"gasket offset={self._gasket_offset_text(cfg, final_dx, final_dy)}, "
                      f"score={final_sim:.3f}")
        else:
            threshold = cfg["similarity_threshold"]
            note = "" if final_sim >= threshold else f" (below threshold {threshold:.2f})"
            result = f"similarity={final_sim:.3f}{note}"
        self._logger.log_ops(
            f"[CAMERA] align_to_reference done: total Δ Ch4={cumulative[4]:+d} "
            f"Ch5={cumulative[5]:+d} | {result}"
        )
        self.progress_updated.emit(
            f"Alignment done — Δ Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d}, "
            f"{result}"
        )

    def _limit_follow_xy_correction(self, ch: int, proposed: int, step_limit: int) -> int:
        """Clamp one correction while _limited_stage_motion_lock is held."""
        ctrl = self._ctx.controller
        try:
            current = int(ctrl.get_ch_pos(ch))
        except Exception:
            self._trigger_global_limit_position_unreadable(ch, moving=True)
        self._check_global_limits_before_move(ch, current)
        baseline = self._baseline_pos.get(ch, current)
        limits = self._limits_for_ch(ch)
        total_min = total_max = None
        if limits is not None:
            minus_mm, plus_mm = limits
            if minus_mm is not None:
                total_min = math.ceil(-minus_mm * 1000 / PULSE_SCALE[ch])
            if plus_mm is not None:
                total_max = math.floor(plus_mm * 1000 / PULSE_SCALE[ch])
        correction = clamp_xy_correction(
            proposed, step_limit, current - baseline, total_min, total_max)
        self._check_global_limits_before_move(ch, current + correction)
        return correction

    def _move_follow_xy(
        self, proposed_ch4: int, proposed_ch5: int, limit_ch4: int, limit_ch5: int,
    ) -> tuple[int, int]:
        """Check and complete one XY correction without a competing stage move."""
        ctrl = self._ctx.controller
        with self._limited_stage_motion_lock:
            d_ch4 = self._limit_follow_xy_correction(4, proposed_ch4, limit_ch4)
            d_ch5 = self._limit_follow_xy_correction(5, proposed_ch5, limit_ch5)
            if d_ch4:
                ctrl.move_ch_relative(4, d_ch4, motion=self._motion_lease)
            if d_ch5:
                ctrl.move_ch_relative(5, d_ch5, motion=self._motion_lease)
            if d_ch4 or d_ch5:
                ctrl.wait_until_stop(motion=self._motion_lease, stay_in_rem=True)
                self._check_global_limits()
        return d_ch4, d_ch5

    def _follow_loop(self, action: StartFollowingAction) -> None:
        """
        Background XY sample-tracking loop with optional Ch3 autofocus.
        Ported from interactive_camera._follow_task, extended with GlobalFollowSettings.
        Each cycle is `_follow_correction_cycle()`, shared with align_to_reference,
        followed by a final similarity verification.
        Global limits clamp each correction before movement and are checked
        again after the move.
        """
        try:
            interval_s = (
                action.interval_s if action.interval_s is not None
                else self._global_follow.interval_s
            )
            cfg = self._resolve_follow_config(action)

            self._require_camera_index(action.camera_index)
            if cfg["tracking_method"] == "gasket_beta":
                self._set_gasket_preview_enabled(True)

            cumulative = {4: 0, 5: 0}

            first_cycle = True
            while not self._follow_should_stop():

                # Correct immediately on start; the interval applies
                # between completed attempts, not before the first one.
                if not first_cycle:
                    deadline = time.monotonic() + interval_s
                    while time.monotonic() < deadline:
                        if self._follow_should_stop():
                            return
                        time.sleep(0.2)

                if self._follow_should_stop():
                    return

                try:
                    self._follow_correction_cycle(cfg, cumulative, pre_focus=True)
                    if self._stop_event.is_set():
                        return
                    similarity_threshold = cfg["similarity_threshold"]
                    if cfg["tracking_method"] == "gasket_beta":
                        final_dx, final_dy, final_similarity = self._measure_gasket_average(
                            cfg, "follow final verification"
                        )
                        if not self._follow_frame_accepted(
                                cfg, final_dx, final_dy, final_similarity):
                            # Give a mismatch first seen after autofocus a
                            # bounded XY correction before declaring failure.
                            self._follow_correction_cycle(
                                cfg, cumulative, post_focus=False,
                            )
                            if self._stop_event.is_set():
                                return
                            final_dx, final_dy, final_similarity = self._measure_gasket_average(
                                cfg, "follow post-focus verification"
                            )
                    else:
                        verified_frame = self._get_camera_frame_after(
                            time.monotonic(), "follow final verification")
                        final_dx = final_dy = 0
                        final_similarity = self._compute_similarity(
                            cfg["reference_frame"], verified_frame
                        )
                    if not self._follow_frame_accepted(
                            cfg, final_dx, final_dy, final_similarity):
                        if cfg["tracking_method"] == "gasket_beta":
                            limit = (f"{cfg['gasket_tolerance_um']:.1f} µm"
                                     if cfg.get("gasket_tolerance_um") is not None
                                     else f"{cfg['gasket_tolerance_px']:.1f} px")
                            raise RunnerError(
                                "runtime.gasket_alignment_failed",
                                f"Gasket centre remains {self._gasket_offset_text(cfg, final_dx, final_dy)} "
                                f"from reference (limit {limit})",
                            )
                        raise RunnerError(
                            "runtime.follow_similarity_below_threshold",
                            f"Sample following failed to reach similarity threshold: "
                            f"{final_similarity:.3f} < {similarity_threshold:.3f}",
                        )
                    if cfg["tracking_method"] == "gasket_beta":
                        self.progress_updated.emit(
                            f"[follow] Verified gasket offset: "
                            f"{self._gasket_offset_text(cfg, final_dx, final_dy)}"
                        )
                    else:
                        self.progress_updated.emit(
                            f"[follow] Verified similarity: {final_similarity:.3f}"
                        )
                    if first_cycle:
                        self._follow_first_cycle_done.set()
                        first_cycle = False
                except MotionRevokedError:
                    if self._stop_event.is_set():
                        # A real abort is already in progress (Stop
                        # button, global-limit violation, or this follow
                        # thread's own prior failure) — the stop checks
                        # at the top of the next `while` pass (or the
                        # early `return`s above) already handle a clean
                        # exit; nothing more to do with this exception.
                        return
                    if self._follow_stop_event.is_set():
                        # A requested graceful end cannot treat a
                        # physically interrupted correction as complete.
                        # Fail the run rather than advancing to XRD with
                        # an unknown final sample position.
                        raise RuntimeError(
                            "Sample following was interrupted before its "
                            "active correction cycle completed"
                        )
                    # No sequence-wide stop was requested, so this can
                    # only be the main run thread's own self-triggered
                    # normal_stop()/emergency_stop() — Ch11 oscillation's
                    # return-to-zero (_return_ch11_to_zero), or a
                    # normal_stop()/emergency_stop() DSL step —
                    # transiently revoking the single shared
                    # self._motion_lease that this loop and the main
                    # thread both move under. That ASSTP/AESTP also
                    # physically decelerate-stops whatever Ch4/Ch5/Ch3
                    # move was in flight here, which is safe (not a
                    # collision), so this is expected to be transparent
                    # to follow — retry next cycle rather than treating
                    # a benign, self-inflicted lease revocation as
                    # sample-following having actually failed (external
                    # review finding, see REORGANISATION_PLAN.md §31).
                    self._logger.log_ops(
                        "[FOLLOW] motion lease revoked by a concurrent "
                        "self-stop elsewhere in the sequence — retrying "
                        "next cycle instead of aborting"
                    )
                    self.progress_updated.emit(
                        "[follow] Motion lease momentarily revoked by a "
                        "concurrent stop/resume — retrying"
                    )
                    continue

        except _StopRequested:
            # Global limit violation propagated — already fully reported by
            # _abort_for_global_limit() (error_occurred + _had_error +
            # _terminal_error_reported), so this is a clean unwind, not a
            # failure to hand back via self._follow_exception.
            pass
        except Exception as exc:
            # Handed back to _stop_follow()/_cleanup_follow_thread(), which
            # run on the main run thread after joining this one — no lock
            # needed for this single-slot handoff (REORGANISATION_PLAN.md
            # Phase 9). Without this, a MOVE_CONSTRAINTS violation, camera
            # failure, stage comms failure, or autofocus failure here only
            # ever reached the user as an informational progress_updated
            # message, and the sequence went on to report success
            # regardless.
            self.progress_updated.emit(f"[follow] Error: {exc}")
            self._follow_exception = exc
            # Abort the whole run NOW rather than waiting for the sequence
            # to eventually reach a stop_following() step — round-2 external
            # review finding: previously only self._follow_exception was
            # set here, and _check_stop() (used throughout
            # _execute_actions()) only looks at self._stop_event, so any
            # action already running or dispatched between this failure and
            # the next stop_following() call (or, with no matching
            # stop_following() at all, every remaining action) continued to
            # execute as if following were still healthy.
            self._abort_follow_thread(
                "runtime.follow_thread_failed", f"Sample following failed: {exc}",
            )
        finally:
            self._set_gasket_preview_enabled(False)

    def _do_follow_autofocus(
        self,
        range_um: float,
        steps: int,
        method: str = "laplacian",
        n_frames: int = 1,
        speed: str = "H",
        peak_method: str = "highest",
    ) -> None:
        resume_preview = self.gasket_preview_enabled
        self._set_gasket_preview_enabled(False)
        with self._limited_stage_motion_lock:
            self._do_follow_autofocus_impl(
                range_um, steps, method, n_frames, speed, peak_method,
            )
        if resume_preview and not self._follow_should_stop():
            self._set_gasket_preview_enabled(True)

    def _do_follow_autofocus_impl(
        self,
        range_um: float,
        steps: int,
        method: str,
        n_frames: int,
        speed: str,
        peak_method: str,
    ) -> None:
        """Sharpness scan on Ch3 (focus axis) via the shared AutoFocus class
        (apps.interactive_camera.autofocus). Moves to the sharpest position.

        range_um   — ±half-range in µm (e.g. 20 → scan ±20 µm around current pos)
        steps      — number of scan positions (≥2). AutoFocus walks the range
            in fixed pulse increments rather than an exact point count, so the
            number of positions actually measured may differ by one at the
            range edges — an accepted approximation (unchanged fit quality
            in practice).
        method     — 'laplacian' (variance of Laplacian) or 'tenengrad' (mean |∇|²)
        n_frames   — frames averaged per position (1 = no averaging)
        speed      — Ch3 speed during scan ('H' / 'M' / 'L')
        peak_method — 'highest' or 'gaussian' (Gaussian fit via scipy if available)

        Global limits are checked before the scan and again afterwards.
        """
        if steps < 2 or range_um <= 0:
            raise RunnerError("runtime.follow_autofocus_invalid", "Ch3 autofocus range or step count is invalid")
        if self._stop_event.is_set():
            return
        if self._af_ch3 is None:
            raise RunnerError("runtime.follow_autofocus_unavailable", "Ch3 autofocus is unavailable")

        ctrl = self._ctx.controller
        if ctrl is None:
            raise RunnerError("runtime.follow_stage_missing", "Stage controller is unavailable for Ch3 autofocus")

        # Set Ch3 speed for the scan
        ctrl.set_ch_speed(3, speed, stay_in_rem=True, motion=self._motion_lease)

        half_pulses = int(range_um / PULSE_SCALE[3])
        if half_pulses == 0:
            raise RunnerError("runtime.follow_autofocus_invalid", "Ch3 autofocus range rounds to zero pulses")

        # Clamp scan range so it does not exceed global limits for Ch3
        gl = self._global_limits
        if gl is not None and 3 in self._baseline_pos:
            try:
                raw_ch3 = ctrl.get_ch_pos(3)
            except Exception:
                self._trigger_global_limit_position_unreadable(3, moving=True)
            try:
                current_ch3 = int(raw_ch3)
            except (TypeError, ValueError) as exc:
                raise RunnerError(
                    "runtime.follow_position_unavailable",
                    f"Cannot read Ch3 position for autofocus limit check: {exc}",
                ) from exc
            self._check_global_limits_before_move(3, current_ch3)
            baseline = self._baseline_pos[3]
            if gl.ch3_plus_mm is not None:
                room_plus = max(0, math.floor(
                    baseline + gl.ch3_plus_mm * 1000 / PULSE_SCALE[3] - current_ch3
                ))
                half_pulses = min(half_pulses, room_plus)
            if gl.ch3_minus_mm is not None:
                room_minus = max(0, math.floor(
                    current_ch3 - baseline + gl.ch3_minus_mm * 1000 / PULSE_SCALE[3]
                ))
                half_pulses = min(half_pulses, room_minus)

        if half_pulses == 0:
            raise RunnerError("runtime.follow_autofocus_limit", "Ch3 autofocus range is exhausted by the global limit")

        af = self._af_ch3
        af.focus_range = half_pulses
        af.step_size = max(1, 2 * half_pulses // (steps - 1))
        af.method = method
        af.n_frames = n_frames
        af.peak_method = peak_method

        result = self._run_autofocus_sync(af, self._motion_lease)
        if result is not None:
            self._logger.log_ops(
                f"[AF] Ch3 → {result['best_pos']:+d} "
                f"(method={method}, peak={peak_method}, sharpness={result['best_sharpness']:.1f})"
            )
            self.progress_updated.emit(
                f"[AF] Ch3 → {result['best_pos']} (sharpness={result['best_sharpness']:.1f})"
            )

        self._check_global_limits()

    # ------------------------------------------------------------------ static helpers

    @staticmethod
    def _compute_xy_shift(ref: np.ndarray, current: np.ndarray) -> tuple[int, int]:
        return compute_xy_shift(ref, current)

    @staticmethod
    def _compute_similarity(ref: np.ndarray, current: np.ndarray) -> float:
        return compute_similarity(ref, current)


# ------------------------------------------------------------------ helpers

def _global_limits_to_dict(gl: scheduler_settings.GlobalLimits | None) -> dict:
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
