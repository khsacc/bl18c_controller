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

from .actions import (
    Action, WaitAction, LogAction,
    StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction,
    SetPressureAction, WaitPressureAction, SetAndWaitPressureAction, SetControlModeAction,
    SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction,
    TakeXrdAction, TakeDarkAction,
    SaveReferenceImageAction, SaveSnapshotAction,
    StartFollowingAction, StopFollowingAction, FollowSampleAction,
    ForLoopAction,
)
from .device_context import DeviceContext
from .log_manager import RunLogger
from .sequence import Sequence

from apps.PACE5000.pace5000_backend import PRESSURE_UNIT_TO_MPA, RATE_UNIT_TO_MPA_PER_MIN
from apps.interactive_camera.autofocus import AutoFocus
from apps.interactive_camera.sample_tracking import compute_xy_shift, compute_similarity
from apps.stage_fpd_scope.stage_settings import load_stage_settings
from utils.stage.control_stage import PULSE_SCALE, MotionRevokedError

from .safety_rules import (
    exceeded_global_limit,
    global_limit_delta_mm,
    global_limits_for_channel,
    validate_ch11_oscillation_settings,
)
from . import scheduler_settings
from .validator.models import (
    Diagnostic,
    build_controller_diagnostic,
    build_runtime_diagnostic,
)

from dataclasses import dataclass as _dc

_PRESETS_PATH = Path(__file__).parent / "__localdata" / "scheduler_presets.json"
_CALIBRATION_PATH = (
    Path(__file__).parent.parent / "interactive_camera" / "calibration.json"
)
_DEFAULT_REF_PATH = Path(__file__).parent / "__localdata" / "reference_frame.png"

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


_DEFAULT_XRD_SAVE_DIR = Path(__file__).parent / "__localdata" / "xrd"
_DEFAULT_SNAPSHOT_SAVE_DIR = Path(__file__).parent / "__localdata" / "snapshots"


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
        global_limits: scheduler_settings.GlobalLimits | None = None,
        global_xrd: scheduler_settings.GlobalXrdSettings | None = None,
        global_follow: scheduler_settings.GlobalFollowSettings | None = None,
        global_camera: scheduler_settings.GlobalCameraSettings | None = None,
        log_path: str = "run",
        log_devices: list[str] | None = None,
        log_dir: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._sequence = sequence
        self._ctx = ctx
        self._global_limits = global_limits
        self._global_xrd = global_xrd or scheduler_settings.GlobalXrdSettings()
        self._global_follow = global_follow or scheduler_settings.GlobalFollowSettings()
        self._global_camera = global_camera or scheduler_settings.GlobalCameraSettings()
        self._log_path = log_path
        self._log_devices = list(log_devices or [])
        self._log_dir = log_dir or None
        self._stop_event = threading.Event()
        # One motion lease spans the whole sequence run (acquired in run(),
        # released in its finally). Nested threads (follow/oscillation) are
        # this same runner's own threads and reuse self._motion_lease — a
        # frozen value, safe to read from any of them.
        self._motion_lease = None

        # Baseline positions captured at run() start (pulses)
        self._baseline_pos: dict[int, int] = {}

        # Background follow thread
        self._follow_thread: threading.Thread | None = None
        self._follow_stop_event = threading.Event()
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
        self._run_timestamp: str = ""
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
        would leave the stage moving in the meantime. Async: never blocks
        the calling (GUI) thread on socket I/O — revokes self._motion_lease
        immediately, so the execution thread's next stage call raises
        MotionRevokedError and unwinds via its normal exception handling.
        """
        self._send_stage_stop(emergency=False)
        self._stop_event.set()
        self._follow_stop_event.set()

    def request_emergency_stop(self) -> None:
        """Thread-safe: may be called from the main thread.

        Sends an emergency-stop (AESTP) to the stage unconditionally, then
        ends the sequence the same way request_stop() does. Async — see
        request_stop().
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
        self._last_diagnostic = None
        self._terminal_error_reported = False
        self._cleanup_failures = []
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
            self._start_camera_session_if_needed()
            self._execute_actions(self._sequence.actions, var_context={})
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
                self.error_occurred.emit(self._flat_index, str(exc))
        finally:
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
                if not self._terminal_error_reported:
                    self._terminal_error_reported = True
                    self.error_occurred.emit(self._flat_index, message)
            self._safe_cleanup("final outcome log", self._log_final_outcome)
            self._safe_cleanup("logger stop", self._logger.stop)

        if self._had_error:
            return
        if self._stop_event.is_set():
            self.sequence_stopped.emit()
        else:
            self.sequence_completed.emit()

    # ------------------------------------------------------------------ run() cleanup helpers

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
            self._logger.log_science("stop", note="Stopped by user")
            self._logger.log_ops("[SEQ:STOP] Stopped by user request")
        else:
            self._logger.log_science("stop", note="Completed successfully")
            self._logger.log_ops("[SEQ:DONE] Sequence completed successfully")

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
            stage_settings = load_stage_settings()
            steps = action.to_steps(stage_settings)
            self._logger.log_ops(
                f"[STAGE] microscope_out_and_fpd_in → {len(steps)} stage steps"
            )
            for step in steps:
                self._check_stop()
                self._do_stage(step, var_context)

        elif isinstance(action, FpdOutMicroscopeInAction):
            stage_settings = load_stage_settings()
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

        try:
            self._logger.log_ops(
                f"[RADICON] set_exposure_ms({eff.exposure_ms}) + snap_triggered()"
            )
            backend.set_exposure_ms(eff.exposure_ms)
            frame = backend.snap_triggered(timeout_ms=eff.exposure_ms + 5000)
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

        Called after oscillation ends (snap_triggered returned).
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
            elif isinstance(action, (SaveReferenceImageAction, StartFollowingAction)):
                indices.add(int(action.camera_index))
            elif isinstance(action, FollowSampleAction):
                indices.add(int(action.camera_index))
            elif isinstance(action, ForLoopAction):
                indices.update(self._camera_indices_for_actions(action.body))
        return indices

    def _start_camera_session_if_needed(self) -> None:
        indices = self._camera_indices_for_actions(self._sequence.actions)
        if not indices:
            return
        if len(indices) != 1:
            raise RuntimeError(
                "Interactive Camera actions in one sequence must use the same "
                f"camera index; found {sorted(indices)}"
            )

        camera_index = next(iter(indices))
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
            frame_provider=lambda: self._get_camera_frame("autofocus"),
            log_callback=self.progress_updated.emit,
            should_stop=lambda: self._stop_event.is_set() or self._follow_stop_event.is_set(),
            release_on_complete=False,
        )

    def _camera_capture_loop(self) -> None:
        cap = self._camera_cap
        
        if cap is None:
            return
        while not self._camera_stop_event.is_set():
            ret, frame = cap.read()
            if ret and frame is not None:
                with self._camera_frame_lock:
                    self._camera_current_frame = frame.copy()
                    self._camera_last_frame_at = time.monotonic()
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
        otherwise hang this wait forever. Returns None on any non-success
        outcome (not started, cancelled, error, no data).

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

        def _on_complete(sharpness_data, best_pos, best_sharpness, fit_result):
            result.update(
                sharpness_data=sharpness_data, best_pos=best_pos,
                best_sharpness=best_sharpness, fit_result=fit_result,
            )
            done.set()

        af.completion_callback = _on_complete
        if not af.perform_autofocus(motion=motion):
            return None  # already focusing, or no motion lease — thread never started

        thread = af.focus_thread
        warned_slow_stop = False
        while thread is not None and thread.is_alive():
            if (self._stop_event.is_set() or self._follow_stop_event.is_set()) and not warned_slow_stop:
                self.progress_updated.emit(
                    "[AF] Stop requested — waiting for in-flight Ch3 move to finish")
                warned_slow_stop = True
            thread.join(timeout=0.2)

        return result if done.is_set() else None

    # ------------------------------------------------------------------ camera / reference

    def _do_save_reference(self, action: SaveReferenceImageAction) -> None:
        self._logger.log_ops(
            f"[CAMERA] save_reference_image(path={action.path!r}, "
            f"camera_index={action.camera_index})"
        )
        self._require_camera_index(action.camera_index)
        frame = self._get_camera_frame("save_reference_image")

        out = Path(action.path) if action.path else _DEFAULT_REF_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out), frame):
            raise RuntimeError(f"Could not save reference image: {out}")
        self._logger.log_ops(f"[CAMERA] reference image saved → {out}")
        self.progress_updated.emit(f"Reference image saved → {out}")

    def _do_save_snapshot(self, action: SaveSnapshotAction) -> None:
        save_dir = (
            action.save_dir
            or self._global_camera.snapshot_save_dir
            or str(_DEFAULT_SNAPSHOT_SAVE_DIR)
        )
        out_dir = Path(save_dir)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out = out_dir / f"snapshot_{timestamp}.png"
        self._logger.log_ops(
            f"[CAMERA] save_snapshot(save_dir={save_dir!r})"
        )

        camera_index = 0
        self._require_camera_index(camera_index)
        frame = self._get_camera_frame("save_snapshot")

        out_dir.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out), frame):
            raise RuntimeError(f"Could not save snapshot: {out}")
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
        self._follow_stop_event.set()
        if self._follow_thread is not None:
            self._follow_thread.join(timeout=10)
            if self._follow_thread.is_alive():
                self._logger.log_ops(
                    "[CAMERA] stop_following() timed out after 10s — "
                    "follow thread still alive"
                )
                message = (
                    "stop_following() timed out after 10s — the background "
                    "follow thread is still running and may still be "
                    "driving Ch3/4/5"
                )
                self._abort_follow_thread("runtime.follow_thread_stop_timeout", message)
                raise RunnerError("runtime.follow_thread_stop_timeout", message)
            self._follow_thread = None
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
            lim4 = max(0, int(max_ch4_um / PULSE_SCALE[4]))
            lim5 = max(0, int(max_ch5_um / PULSE_SCALE[5]))

            xy_max_retries = gf.xy_max_retries

            # Effective autofocus settings — action.autofocus_enabled is the
            # per-step value (default True; DSL/Visual/JSON can set it False
            # to skip Ch3 autofocus for this follow session — actions.py,
            # dsl/api.py::start_following()/follow_sample_position()).
            # Other autofocus fields (range/steps) still fall back to global.
            eff_af_enabled = action.autofocus_enabled
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
            ref_path = action.reference_path or gf.reference_path
            if ref_path is None:
                raise RuntimeError(
                    "No reference image configured — set one via "
                    "Global Settings > Follow Settings > Reference Image, "
                    "or specify reference_path on this step"
                )
            reference_frame = cv2.imread(str(ref_path), cv2.IMREAD_COLOR)
            if reference_frame is None:
                raise RuntimeError(f"Could not load reference image: {ref_path}")

            self._require_camera_index(action.camera_index)

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

                    try:
                        frame = self._get_camera_frame("follow_sample_position")

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
                            ctrl.move_ch_relative(4, d_ch4, motion=self._motion_lease)
                        if d_ch5 != 0:
                            ctrl.move_ch_relative(5, d_ch5, motion=self._motion_lease)
                        if d_ch4 != 0 or d_ch5 != 0:
                            ctrl.wait_until_stop(motion=self._motion_lease, stay_in_rem=True)
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
                            chk_frame = self._get_camera_frame("follow XY retry")
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
                                ctrl.move_ch_relative(4, _d4, motion=self._motion_lease)
                            if _d5 != 0:
                                ctrl.move_ch_relative(5, _d5, motion=self._motion_lease)
                            if _d4 != 0 or _d5 != 0:
                                ctrl.wait_until_stop(motion=self._motion_lease, stay_in_rem=True)
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
                                eff_af_range_um, eff_af_steps,
                                method=gf.autofocus_method,
                                n_frames=gf.autofocus_n_frames,
                                speed=gf.autofocus_speed,
                                peak_method=gf.autofocus_peak_method,
                            )
                    except MotionRevokedError:
                        if self._stop_event.is_set() or self._follow_stop_event.is_set():
                            # A real abort is already in progress (Stop
                            # button, global-limit violation, or this follow
                            # thread's own prior failure) — the stop checks
                            # at the top of the next `while` pass (or the
                            # early `return`s above) already handle a clean
                            # exit; nothing more to do with this exception.
                            return
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
            finally:
                pass

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

    def _do_follow_autofocus(
        self,
        range_um: float,
        steps: int,
        method: str = "laplacian",
        n_frames: int = 1,
        speed: str = "H",
        peak_method: str = "highest",
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

        Global limits are enforced after the scan (self._check_global_limits()).
        """
        if steps < 2 or range_um <= 0:
            return
        if self._follow_stop_event.is_set() or self._stop_event.is_set():
            return
        if self._af_ch3 is None:
            return

        ctrl = self._ctx.controller
        if ctrl is None:
            return

        # Set Ch3 speed for the scan
        try:
            ctrl.set_ch_speed(3, speed, stay_in_rem=True, motion=self._motion_lease)
        except Exception:
            pass

        half_pulses = int(range_um / PULSE_SCALE[3])
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
                        (baseline + gl.ch3_plus_mm * 1000 / PULSE_SCALE[3]) - current_ch3
                    ))
                    half_pulses = min(half_pulses, room_plus)
                if gl.ch3_minus_mm is not None:
                    room_minus = max(0, int(
                        current_ch3 - (baseline - gl.ch3_minus_mm * 1000 / PULSE_SCALE[3])
                    ))
                    half_pulses = min(half_pulses, room_minus)

        if half_pulses == 0:
            self.progress_updated.emit("[AF] Ch3 range exhausted by global limit — skipping")
            return

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
