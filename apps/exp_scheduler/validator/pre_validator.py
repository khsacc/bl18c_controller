"""
Pre-execution validator for ExperimentalScheduler sequences.

Runs static analysis on a Sequence before SequenceRunner starts.
All checks run to completion (errors are accumulated, not short-circuited)
so the user sees every problem in one dialog.

REORGANISATION_PLAN.md Phase 5: the three independent ForLoopAction walkers
this file used to hand-roll (`_collect_all_actions`, `_expand_execution_order`,
`_walk_pace_actions`) are now owned by `validator/execution_trace.py`
(`ExecutionTrace.flat`/`.ordered`/`.pace_primitives()`), built once per
`validate()` call. Device-communication-free Action value checks and
Sequence structure checks live in `validator/checks/action_params.py` and
`validator/checks/sequence_structure.py` respectively.

REORGANISATION_PLAN.md Phase 6: the device-communication checks themselves
(Stage/PACE5000/LakeShore/Rad-icon/Camera) now live in
`validator/checks/{stage,pace5000,lakeshore,xrd,camera_follow}.py`. Every
physical read they depend on is collected exactly once per `validate()` call
into a `validator.snapshots.ValidationSnapshot` (see that module's
docstring for the read-sharing / Diagnostic-ownership rules) — this file is
now a facade: build the `ExecutionTrace`, decide what the snapshot needs to
contain (`snapshots.determine_requirements`), collect it
(`snapshots.collect_snapshot`), then run every checker (still via the same
`_run`/`_run_gated` exception-isolation wrappers as before) and write the
validation log.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

from ..actions import (
    AlignToReferenceAction,
    FpdOutMicroscopeInAction,
    FollowSampleAction,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    SetControlModeAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    TakeSpectrumAction,
    CompressToTargetPressureUsingRubyAction,
    WaitPressureAction,
    WaitTemperatureAction,
    AllHeatersOffAction,
)
from ..device_context import DeviceContext
from ..nearby_search import build_search_axes, effective_search_action
from .. import ruby_pressure
from .. import compression
from ..spectrum_quality import EXPOSURE_MIN_S, EXPOSURE_MAX_S, FIT_FUNCTIONS, BASELINE_MODELS
from ..scheduler_settings import (
    GlobalFollowSettings, GlobalLimits, GlobalPositions, GlobalSpectrumSettings, GlobalXrdSettings,
    RUBY_FILENAME_SUFFIXES, ruby_origin_for,
)
from ..sequence import Sequence
from settings import log_prefs

from . import snapshots
from .checks import action_params, camera_follow, lakeshore, pace5000, sequence_structure, stage, xrd
from .execution_trace import ExecutionTrace, LoopExpansionStats
from .models import ValidationPhase, emit_diagnostic, emit_preflight, emit_static

_LOG_KEY = "pre_validator"

# Owned by validator/checks/action_params.py (which itself aliases
# apps/PACE5000/pace5000_backend.py — a separate git submodule, see
# CLAUDE.md); re-exported here for
# tests/test_exp_scheduler_pre_validator.py::Phase4PaceUnitDedupTests, which
# predates the Phase 5 checks/ split.
_PACE_TO_MPA = action_params.PACE_TO_MPA
_PACE_VALID_RATE_UNITS = action_params.PACE_VALID_RATE_UNITS


@dataclass
class PreCheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # All-channel (Ch1-11) stage positions read at validation time, in pulses.
    # Populated whenever a stage controller is connected. Used by the UI to
    # detect stage moves that happen between "Validate" and "Run".
    baseline_positions: dict[int, int] = field(default_factory=dict)
    # Diagnostic objects backing the checks that have been migrated to the
    # Diagnostic model (Phase 5's action_params.py/sequence_structure.py,
    # Phase 6's device checks via emit_preflight). `errors`/`warnings` above
    # still get every check's message (mirrored in by emit_static/
    # emit_preflight) — this field is additive, not yet the UI's source of
    # truth (Phase 7).
    diagnostics: list = field(default_factory=list)
    # The full ValidationSnapshot collected this run — set unconditionally
    # (success or internal-error fallback), since Phase 8's Run-gate
    # baseline diff needs fresh device state even when this validate() call
    # otherwise found unrelated errors.
    snapshot: "snapshots.ValidationSnapshot | None" = None

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


class PreValidator:
    """Validate a Sequence against the current DeviceContext before execution."""

    def validate(
        self,
        sequence: Sequence,
        ctx: DeviceContext,
        global_limits: GlobalLimits | None = None,
        global_xrd: GlobalXrdSettings | None = None,
        global_follow: GlobalFollowSettings | None = None,
        global_spectrum: GlobalSpectrumSettings | None = None,
        global_positions: GlobalPositions | None = None,
    ) -> PreCheckResult:
        result = PreCheckResult()

        log_lines: list[str] = []

        def _log(msg: str) -> None:
            # The Windows console (cp932 etc.) cannot encode characters such
            # as "—" used in check messages; substitute them on the console
            # only — the saved log file keeps the original text.
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
            log_lines.append(msg)

        _SEP = "-" * 60
        _log(f"\n[PreValidator] {_SEP}")
        _log(f"[PreValidator] Sequence : {sequence.name!r}")

        # Built once per validate() call and shared by every check below —
        # REORGANISATION_PLAN.md Phase 5. trace.stats is computed first
        # (cheap, depth-guarded — never materialises a full per-iteration
        # unroll before deciding whether doing so is safe); trace.flat is
        # always fully populated (non-recursive, O(depth) memory,
        # width-independent); trace.ordered (true per-iteration unroll) is
        # only populated when trace.stats.within_limits.
        #
        # Wrapped the same defensive way every internal-error-prone step in
        # this facade is (see "PreValidator internal error safety net" in
        # VALIDATOR.md): every check below depends on `trace` (and, further
        # down, `snapshot`/`requirements`) existing, so an unanticipated bug
        # here must fail closed (treat the sequence as over every limit, so
        # the gated checks skip rather than raising on an undefined `trace`)
        # instead of aborting validate() entirely.
        try:
            trace = ExecutionTrace.build(sequence.actions)
        except Exception as exc:
            emit_static(
                result, "internal.execution_trace_build_error",
                f"ExecutionTrace.build: internal validation error ({exc!r}) — "
                "this indicates a bug in PreValidator itself; treat the "
                "sequence as unvalidated and report this",
            )
            # A large max_nesting_depth guarantees depth_safe/candidates_safe/
            # within_limits are all False, regardless of the real limit
            # constants (private to execution_trace.py) — every gated check
            # below is skipped, matching the old expansion_ok=False fallback.
            trace = ExecutionTrace(stats=LoopExpansionStats(0, 0, 10**6, True))
        flat_actions = [e.action for e in trace.flat]

        _log(
            f"[PreValidator] Actions  : {len(sequence.actions)} top-level / "
            f"{len(trace.flat)} flat"
        )
        n_counts = {
            "stage":     sum(1 for a in flat_actions if isinstance(a, (StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction, StartFollowingAction, FollowSampleAction, AlignToReferenceAction, TakeSpectrumAction))),
            "pace5000":  sum(1 for a in flat_actions if isinstance(a, (SetPressureAction, WaitPressureAction, SetControlModeAction, CompressToTargetPressureUsingRubyAction))),
            "lakeshore": sum(1 for a in flat_actions if isinstance(a, (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction))),
            "xrd/dark":  sum(1 for a in flat_actions if isinstance(a, (TakeXrdAction, TakeDarkAction))),
            "camera":    sum(1 for a in flat_actions if isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction, FollowSampleAction, AlignToReferenceAction))),
            "spectrum":  sum(1 for a in flat_actions if isinstance(a, TakeSpectrumAction)),
        }
        _log(f"[PreValidator] Counts   : " + "  ".join(f"{k}={v}" for k, v in n_counts.items()))
        _log(f"[PreValidator] Inputs   : global_limits={'set' if global_limits is not None else 'None'}  global_xrd={'set' if global_xrd is not None else 'None'}")
        _log(f"[PreValidator] {_SEP}")

        def _log_diff(label: str, e0: int, w0: int) -> None:
            """Log every error/warning appended to `result` since indices
            e0/w0 were captured — shared by `_run` and by the (non-`_run`)
            snapshot-collection step below, so Diagnostics raised while
            reading device state (unreadable stage position, PACE unit,
            etc.) show up in the same ``[Error]``/``[Warning]`` log lines a regular
            checker's findings would, instead of only affecting the final
            error/warning counts."""
            new_e = result.errors[e0:]
            new_w = result.warnings[w0:]
            if not new_e and not new_w:
                _log(f"[PreValidator]   {label:<38}  OK")
            else:
                status = "ERROR" if new_e else "WARN"
                _log(f"[PreValidator]   {label:<38}  {status}")
                for msg in new_e:
                    _log(f"[PreValidator]     [Error] {msg}")
                for msg in new_w:
                    _log(f"[PreValidator]     [Warning] {msg}")

        def _run(
            label: str, fn, *args,
            phase: ValidationPhase, device: str | None = None,
        ) -> None:
            e0 = len(result.errors)
            w0 = len(result.warnings)
            try:
                fn(*args)
            except Exception as exc:
                # A defensive safety net, not a substitute for fixing the
                # underlying check: without this, a single unhandled
                # exception in any one checker (e.g. a raw `None < 0`
                # comparison on a DSL field that silently defaulted to
                # None) aborts validate() entirely, and the user sees a
                # crash instead of a validation error for the rest of the
                # sequence too. `phase`/`device` are supplied by each call
                # site below (derived from which validator/checks/*.py
                # module `fn` belongs to — every checker in stage.py/
                # pace5000.py/lakeshore.py/xrd.py/camera_follow.py is
                # PREFLIGHT regardless of whether it happens to read
                # `snapshot`, matching those modules' own emit_preflight()
                # usage; action_params.py/sequence_structure.py checkers
                # and the local _check_global_limits are STATIC) so this
                # synthesized Diagnostic is tagged the same way a normal
                # Diagnostic from that checker would be.
                emit_diagnostic(
                    result, "internal.check_error",
                    f"{label}: internal validation error ({exc!r}) — "
                    "this indicates a bug in PreValidator itself; treat the "
                    "sequence as unvalidated and report this",
                    phase=phase, device=device,
                )
            _log_diff(label, e0, w0)

        def _run_gated(
            label: str, gate_ok: bool, skip_reason: str, fn, *args,
            phase: ValidationPhase, device: str | None = None,
        ) -> None:
            """Like _run, but for checks that depend on a specific
            ExecutionTrace guarantee (depth_safe / candidates_safe /
            within_limits) — skipped, with a logged reason, once
            trace.stats has already rejected the sequence on that axis, to
            avoid materialising/enumerating the very thing that guarantee
            exists to bound."""
            if gate_ok:
                _run(label, fn, *args, phase=phase, device=device)
            else:
                _log(f"[PreValidator]   {label:<38}  SKIPPED ({skip_reason})")

        def _run_structural(
            label: str, fn, *args,
            phase: ValidationPhase, device: str | None = None,
        ) -> None:
            _run_gated(
                label, trace.stats.depth_safe, "nesting too deep", fn, *args,
                phase=phase, device=device,
            )

        def _run_candidates(
            label: str, fn, *args,
            phase: ValidationPhase, device: str | None = None,
        ) -> None:
            _run_gated(
                label, trace.stats.candidates_safe,
                "a loop's iteration count is too large", fn, *args,
                phase=phase, device=device,
            )

        def _run_expanded(
            label: str, fn, *args,
            phase: ValidationPhase, device: str | None = None,
        ) -> None:
            _run_gated(
                label, trace.stats.within_limits,
                "loop expansion limit exceeded", fn, *args,
                phase=phase, device=device,
            )

        _S = ValidationPhase.STATIC
        _P = ValidationPhase.PREFLIGHT

        _run("global_limits", _check_global_limits, global_limits, result, phase=_S)

        _run("check_empty_sequence", sequence_structure.check_empty_sequence, sequence.actions, result, phase=_S)
        _run("check_loop_expansion_limits", sequence_structure.check_loop_expansion_limits, trace, result, phase=_S)

        e0 = len(result.errors)
        w0 = len(result.warnings)
        try:
            requirements = snapshots.determine_requirements(trace, global_xrd)
            snapshot = snapshots.collect_snapshot(trace, ctx, result, requirements, global_positions)
        except Exception as exc:
            # Unlike the STATIC internal-error fallbacks above, this one
            # failed while reading connected-device state (or deciding what
            # to read), so it is recorded as PREFLIGHT — with no single
            # `device` to blame (the failure could be in the trace-driven
            # requirements decision itself, not a specific device's read).
            emit_diagnostic(
                result, "internal.snapshot_collection_error",
                f"snapshot collection: internal validation error ({exc!r}) — "
                "this indicates a bug in PreValidator itself; treat the "
                "sequence as unvalidated and report this",
                phase=_P,
            )
            requirements = snapshots.SnapshotRequirements(
                stage_moving=False, pace_used=False, pace_output_state=False,
                pace_target=False, pace_max_set_pressure_mpa=None, pace_unit=False,
                lakeshore_used=False, lakeshore_heater_range=False, lakeshore_data=False,
                radicon_used=False,
            )
            snapshot = snapshots.ValidationSnapshot()
        result.snapshot = snapshot
        _log_diff("collect_snapshot", e0, w0)

        stage_mode = snapshot.stage.stage_mode if snapshot.stage is not None else "unknown"
        _log(f"[PreValidator]   {'stage snapshot':<38}  mode={stage_mode!r}")

        _run("check_stage",          stage.check_stage,          trace, ctx, snapshot, result, phase=_P, device="stage")
        _run_candidates("check_stage_schema", action_params.check_stage_schema, sequence.actions, result, phase=_S)
        _run(
            "check_xrd_oscillation_stage", stage.check_xrd_oscillation_stage,
            trace, ctx, snapshot, global_xrd, result, phase=_P, device="stage",
        )
        _run("check_stage_compound", stage.check_stage_compound, trace, result, global_positions, phase=_P, device="stage")
        _run(
            "check_global_positions_match_stage_settings",
            stage.check_global_positions_match_stage_settings,
            result, global_positions, phase=_P, device="stage",
        )
        _run(
            "check_stage_move_constraints", stage.check_stage_move_constraints,
            sequence.actions, snapshot, result, global_xrd, global_spectrum,
            global_limits, trace, global_positions,
            phase=_P, device="stage",
        )
        _run("check_pace5000",              pace5000.check_pace5000,              trace, snapshot, requirements, result, phase=_P, device="pace5000")
        _run_expanded("check_compression_sequence", pace5000.check_compression_sequence, trace, snapshot, result, phase=_P, device="pace5000")
        _run_expanded("check_pace5000_control_mode", pace5000.check_pace5000_control_mode, snapshot, result, trace, phase=_P, device="pace5000")
        _run_structural("check_pace5000_adjacency", pace5000.check_pace5000_adjacency, sequence.actions, result, phase=_P, device="pace5000")
        _run_expanded("check_pace5000_ordering",     pace5000.check_pace5000_ordering,     trace, result, phase=_P, device="pace5000")
        _run_expanded("check_pace5000_params",       action_params.check_pace5000_params, trace.pace_primitives(), result, phase=_S)
        _run_expanded("check_pace5000_wait_duration", pace5000.check_pace5000_wait_duration, trace, snapshot, result, phase=_P, device="pace5000")
        _run("check_lakeshore",      lakeshore.check_lakeshore,      trace, snapshot, result, phase=_P, device="lakeshore")
        _run_candidates("check_lakeshore_params", action_params.check_lakeshore_params, sequence.actions, result, phase=_S)
        _run_expanded("check_lakeshore_sequence", lakeshore.check_lakeshore_sequence, trace, snapshot, result, phase=_P, device="lakeshore")
        _run("check_radicon",        xrd.check_radicon,        trace, snapshot, result, phase=_P, device="radicon")
        _run("check_xrd_params",     action_params.check_xrd_params, trace.flat, global_xrd, result, ctx, phase=_S)
        _run(
            "check_spectrum", _check_spectrum,
            trace, ctx, global_spectrum, result, global_follow,
            phase=_P, device="spectrometer",
        )
        _run("check_camera",         camera_follow.check_camera, trace, result, global_follow, ctx, phase=_P, device="camera")
        _run_expanded("check_follow_pairing", sequence_structure.check_follow_pairing, trace.ordered, result, phase=_S)
        _run_expanded(
            "check_emergency_stop_confirmation", stage.check_emergency_stop_confirmation,
            trace, result, phase=_P, device="stage",
        )
        _run("check_durations",      action_params.check_durations,      trace.flat, result, phase=_S)
        _run("check_follow_params",  action_params.check_follow_params,  trace.flat, result, phase=_S)
        _run_structural("check_unused_loop_vars", sequence_structure.check_unused_loop_vars, sequence.actions, result, phase=_S)
        _run_structural("check_undefined_loop_vars", sequence_structure.check_undefined_loop_vars, sequence.actions, result, phase=_S)
        _run_structural("check_empty_loop_body", sequence_structure.check_empty_loop_body, sequence.actions, result, phase=_S)
        _run_structural("check_empty_loop_values", sequence_structure.check_empty_loop_values, sequence.actions, result, phase=_S)
        _run_structural(
            "check_duplicate_consecutive_actions",
            sequence_structure.check_duplicate_consecutive_actions, sequence.actions, result, phase=_S,
        )

        _run_expanded(
            "check_stage_mode_ordering", stage.check_stage_mode_ordering,
            trace, snapshot, result, phase=_P, device="stage",
        )
        _run("check_autofocus",           action_params.check_autofocus,           trace.flat, global_limits, result, phase=_S)

        verdict = "PASSED" if result.ok else "FAILED"
        _log(f"[PreValidator] {_SEP}")
        _log(f"[PreValidator] {verdict}  —  {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
        _log(f"[PreValidator] {_SEP}\n")

        if log_prefs.should_save(_LOG_KEY):
            self._save_log(sequence.name, log_lines)

        return result

    @staticmethod
    def _save_log(sequence_name: str, log_lines: list[str]) -> None:
        """Write the validation log to a timestamped .txt file under the
        details-log directory (only called when ``--details`` mode, or the
        per-app save checkbox, is enabled — see settings/log_prefs.py)."""
        localdata = log_prefs.get_app_dir(_LOG_KEY)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w\-]+", "_", sequence_name).strip("_") or "sequence"
        log_path = localdata / f"{ts}_{safe_name}.txt"
        try:
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"[PreValidator] Failed to save validation log: {exc}")


# ------------------------------------------------------------------ global limits (device-communication-free)

def _check_global_limits(global_limits: GlobalLimits | None, result: PreCheckResult) -> None:
    if global_limits is None:
        return
    if not global_limits.is_fully_configured():
        emit_static(
            result, "static.global_limits.not_configured",
            "Global limits are not fully configured — "
            "all six Ch3/4/5 ±mm values must be set before running",
        )
        return
    # is_fully_configured() only checks "not None" — a value loaded
    # from a hand-edited/corrupted global-limits JSON file could
    # still be NaN/Inf/negative (the UI spin boxes that normally
    # produce these values are clamped to [0.0, 9999.99], but that
    # clamp is bypassed entirely by a file load). This is not an
    # Action-level STATIC check (GlobalLimits isn't an Action), so
    # it uses the pure parser directly rather than
    # action_params.require_finite_number.
    for what, value in (
        ("Ch3 -mm", global_limits.ch3_minus_mm),
        ("Ch3 +mm", global_limits.ch3_plus_mm),
        ("Ch4 -mm", global_limits.ch4_minus_mm),
        ("Ch4 +mm", global_limits.ch4_plus_mm),
        ("Ch5 -mm", global_limits.ch5_minus_mm),
        ("Ch5 +mm", global_limits.ch5_plus_mm),
    ):
        _val, err = action_params.parse_finite_number(value, what=what, minimum=0.0)
        if err is not None:
            emit_static(result, "static.global_limits.non_finite", f"Global limits: {err}")


def _check_spectrum(
    trace: ExecutionTrace,
    ctx: DeviceContext,
    settings: GlobalSpectrumSettings | None,
    result: PreCheckResult,
    global_follow: GlobalFollowSettings | None = None,
) -> None:
    """Static/session configuration checks for take_spectrum()."""
    if not any(isinstance(e.action, TakeSpectrumAction) for e in trace.flat):
        return
    if settings is None:
        emit_static(result, "static.spectrum.settings_missing", "Spectrum settings are not configured")
        return
    if any(key not in RUBY_FILENAME_SUFFIXES for key in settings.filename_suffixes):
        emit_static(result, "static.spectrum.filename_suffix_invalid",
                    "Invalid ruby filename suffix")
    spectrum_actions = [e.action for e in trace.flat if isinstance(e.action, TakeSpectrumAction)]
    for action in spectrum_actions:
        if type(action.all_positions) is not bool:
            emit_static(
                result, "static.spectrum.positions_invalid",
                "all_positions must be a boolean",
            )
            return
    if any(action.all_positions for action in spectrum_actions) and not settings.position_names():
        emit_static(
            result, "static.spectrum.position_missing",
            "Record at least one ruby position before measuring all registered positions",
        )
    required_names = {
        name
        for action in spectrum_actions
        for name in (settings.position_names() if action.all_positions else (action.position_name,))
    }
    for name in required_names:
        if settings.offset_for(name) is None:
            emit_static(
                result, "static.spectrum.position_missing",
                f"Record both the XRD and spectrum positions before using take_spectrum ({name})",
            )
    if (global_follow is not None and required_names
            and settings.ruby_offset_origin != ruby_origin_for(global_follow.tracking_method)):
        emit_static(
            result, "static.spectrum.ruby_origin_mismatch",
            "Ruby positions were recorded for a different position-tracking method; "
            "record them again in Sample Positions",
        )
    for entry in trace.flat:
        action = entry.action
        if not isinstance(action, TakeSpectrumAction):
            continue
        if isinstance(action, CompressToTargetPressureUsingRubyAction):
            error = compression.parameters_error(action)
            if error:
                emit_static(result, "static.compression.parameters_invalid", error)
            if not settings.pressure_enabled:
                emit_static(
                    result, "static.compression.pressure_disabled",
                    "Enable Ruby Fluorescence Settings > Pressure Calculation before compression",
                )
        stop_error = ruby_pressure.stop_condition_error(action, settings)
        if stop_error:
            emit_static(result, "static.spectrum.pressure_stop_invalid", stop_error)
        exposure = (action.exposure_time_s if action.exposure_time_s is not None
                    else settings.exposure_time_s)
        if action.em_gain_max is not None and (
            type(action.em_gain_max) is not int or not 1 <= action.em_gain_max <= 1000
        ):
            emit_static(
                result, "static.spectrum.gain_invalid",
                "Maximum EM Gain override must be an integer from 1 to 1000",
            )
        _, error = action_params.parse_finite_number(
            exposure, what="Ruby exposure time (s)", minimum=EXPOSURE_MIN_S,
            maximum=EXPOSURE_MAX_S,
        )
        if error:
            emit_static(result, "static.spectrum.exposure_invalid", error)
        try:
            build_search_axes(effective_search_action(action, settings))
        except (TypeError, ValueError, OverflowError) as exc:
            emit_static(
                result, "static.spectrum.search_invalid",
                f"take_spectrum nearby search: {exc}",
            )
    try:
        from utils.stage.control_stage_sim import PM16CControllerSim
        simulated = isinstance(ctx.controller, PM16CControllerSim)
    except ImportError:
        simulated = False
    api_key_configured = isinstance(settings.api_key, str) and bool(settings.api_key.strip())
    if not simulated and not api_key_configured:
        emit_static(
            result, "static.spectrum.api_key_missing",
            "FluoRaPressee API key is required for take_spectrum",
        )
    if settings.speed not in {"H", "M", "L"}:
        emit_static(result, "static.spectrum.speed_invalid", "Spectrum stage speed must be H, M, or L")
    _, error = action_params.parse_finite_number(
        settings.r_squared_min, what="Ruby minimum R²", minimum=0.0, maximum=1.0,
    )
    if error:
        emit_static(result, "static.spectrum.quality_invalid", error)
    if (type(settings.em_gain_max) is not int
            or not 1 <= settings.em_gain_max <= 1000):
        emit_static(result, "static.spectrum.quality_invalid",
                    "Ruby maximum EM Gain must be an integer from 1 to 1000")
    if (type(settings.max_accumulations) is not int
            or not 1 <= settings.max_accumulations <= 10):
        emit_static(result, "static.spectrum.quality_invalid",
                    "Ruby maximum accumulations must be an integer from 1 to 10")
    if settings.fit_function not in FIT_FUNCTIONS:
        emit_static(result, "static.spectrum.quality_invalid",
                    "Ruby fit function must be Moffat, Pseudo Voigt, Gauss or Lorentz")
    if settings.baseline_model not in BASELINE_MODELS:
        emit_static(result, "static.spectrum.quality_invalid",
                    "Ruby fit background must be constant, linear, quadratic or auto_polynomial")
    _, settle_error = action_params.parse_finite_number(
        settings.settle_ms, what="Spectrum settle time (ms)", minimum=0.0,
    )
    timeout, timeout_error = action_params.parse_finite_number(
        settings.timeout_s, what="Spectrum timeout (s)", minimum=0.0,
        min_inclusive=False,
    )
    for error in (settle_error, timeout_error):
        if error:
            emit_static(result, "static.spectrum.timing_invalid", error)
    pressure = settings.pressure_enabled is True
    if pressure:
        # Old sequences without a λ₀ still measure spectra; only a run that
        # asks for pressure is stopped here.
        for code, message in ruby_pressure.settings_errors(settings, required_names):
            emit_static(result, f"static.spectrum.pressure_{code}", message)
        if (settings.temperature_correction_enabled is True
                and settings.temperature_source == "lakeshore_b"):
            try:
                ruby_pressure.read_lakeshore_b(
                    ctx.lakeshore, float(settings.temperature_max_age_s),
                )
            except (ruby_pressure.PressureSetupError, TypeError, ValueError) as exc:
                emit_preflight(
                    result, "preflight.spectrum.temperature_unavailable",
                    f"Ruby pressure temperature correction: {exc}", device="lakeshore",
                )
    if not simulated and api_key_configured and settle_error is None and timeout_error is None:
        try:
            from utils.fluora_pressee import FluoraPresseeReader
            reader = FluoraPresseeReader(
                settings.base_url, settings.api_key,
                timeout_s=min(timeout, 5.0),
                require_response_token=True,
            )
            reader.prepare()
        except Exception as exc:
            emit_preflight(
                result, "preflight.spectrum.not_ready",
                f"FluoRaPressee is not ready: {exc}", device="spectrometer",
            )
        else:
            first_token = (reader.last_status or {}).get("instrument_state_token")
            first_token_ready = isinstance(first_token, str) and bool(first_token.strip())
            try:
                gain_info = reader.get_em_gain_info()
            except Exception as exc:
                emit_preflight(
                    result, "preflight.spectrum.gain_unreadable",
                    f"Cannot read FluoRaPressée EM Gain: {exc}",
                    device="spectrometer",
                )
                gain_ready = False
            else:
                gain_ready = True
                # Reading the camera can initialise or refresh FRP hardware
                # state. Capture the token after that read, before any POST.
                try:
                    reader.prepare()
                except Exception as exc:
                    emit_preflight(
                        result, "preflight.spectrum.not_ready",
                        f"FluoRaPressée is not ready after EM Gain read: {exc}",
                        device="spectrometer",
                    )
                    return
            status = reader.last_status or {}
            roi = status.get("roi") if isinstance(status.get("roi"), dict) else {}
            mode_ready = roi.get("mode") in {"1d_roi", "1d_full"}
            if not mode_ready and not pressure:
                emit_preflight(
                    result, "preflight.spectrum.mode_not_ready",
                    "FluoRaPressée must be in a 1-D spectrum mode",
                    device="spectrometer",
                )
            pressure_ready = True
            if pressure:
                try:
                    ruby_pressure.check_status(status)
                except ruby_pressure.PressureSetupError as exc:
                    pressure_ready = False
                    emit_preflight(
                        result, "preflight.spectrum.pressure_not_ready",
                        str(exc), device="spectrometer",
                    )
            token = status.get("instrument_state_token")
            refreshed_token_ready = isinstance(token, str) and bool(token.strip())
            token_ready = first_token_ready and refreshed_token_ready
            if token_ready and first_token != token:
                emit_preflight(
                    result, "preflight.spectrum.state_changed_during_probe",
                    "FluoRaPressée instrument state changed while reading EM Gain; "
                    "run Validate again after the instrument state settles",
                    device="spectrometer",
                )
                gain_ready = False
            if not token_ready:
                emit_preflight(
                    result, "preflight.spectrum.state_token_missing",
                    "FluoRaPressée did not provide an instrument-state token",
                    device="spectrometer",
                )
            if gain_ready and gain_info["available"]:
                for entry in trace.flat:
                    action = entry.action
                    if not isinstance(action, TakeSpectrumAction):
                        continue
                    maximum = (action.em_gain_max if action.em_gain_max is not None
                               else settings.em_gain_max)
                    if type(maximum) is int and gain_info["gain"] > maximum:
                        emit_preflight(
                            result, "preflight.spectrum.gain_above_maximum",
                            f"Current FRP EM Gain {gain_info['gain']} exceeds "
                            f"the maximum {maximum} for this ruby step",
                            device="spectrometer", action_path=entry.action_path,
                        )
                        gain_ready = False
            # Use the first ruby step's real acquisition overrides. A POST
            # without them (as in FRP Comms) cannot expose a conflict caused
            # by applying the scheduler's exposure or accumulation settings.
            # No stage move or EM Gain write is made.
            if mode_ready and pressure_ready and token_ready and gain_ready:
                first_action = spectrum_actions[0]
                exposure = (first_action.exposure_time_s if first_action.exposure_time_s is not None
                            else settings.exposure_time_s)
                _, exposure_error = action_params.parse_finite_number(
                    exposure, what="Ruby exposure time (s)",
                    minimum=EXPOSURE_MIN_S, maximum=EXPOSURE_MAX_S,
                )
                if exposure_error is None:
                    reader.exposure_time_s = float(exposure)
                    reader.accumulations = 1
                    reader.timeout_s = max(timeout, float(exposure) + 45.0)
                    try:
                        reader.acquire_spectrum()
                    except Exception as exc:
                        emit_preflight(
                            result, "preflight.spectrum.test_acquisition_failed",
                            f"FluoRaPressée test acquisition failed: {exc}",
                            device="spectrometer",
                        )
