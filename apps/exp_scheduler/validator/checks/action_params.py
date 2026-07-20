"""
Device-communication-free Action value checks — REORGANISATION_PLAN.md
Phase 5 (§7 Phase 5 item 5).

These validate the *shape* of Action fields (finite numbers, units/enums,
pulse ranges, durations/tolerances/rates) independent of whether any
hardware is connected. They are always safe to run given a `depth_safe` (or,
for the two that resolve loop-variable candidate values, `candidates_safe`)
ExecutionTrace, and are the same regardless of whether the Action was built
by the DSL compiler, the Visual editor, or loaded from JSON — see
`tests/test_exp_scheduler_pre_validator.py`'s direct-Action-injection tests.

Two pure parsers (`parse_finite_number`, `parse_stage_position`) live here
and are reused, without any Diagnostic/PreCheckResult dependency, by
`validator/pre_validator.py`'s Global-limits and `stage_settings.json`
checks — those are not Action-level STATIC checks (they validate global
settings / a config file, not a specific Action's fields), so they keep
appending plain strings to `PreCheckResult.errors` rather than going through
`models.emit_static`.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

from apps.PACE5000.pace5000_backend import (
    MIN_SLEW_RATE_MPA_PER_SEC as _PACE_MIN_SLEW_RATE_MPA_PER_SEC,
    PRESSURE_UNIT_TO_MPA as _PACE_TO_MPA,
    RATE_UNIT_TO_MPA_PER_MIN as _PACE_RATE_TO_MPA_PER_MIN,
    rate_to_mpa_per_sec as _pace_rate_to_mpa_per_sec,
)

from ...actions import (
    Action,
    FollowSampleAction,
    ForLoopAction,
    FpdOutMicroscopeInAction,
    MicroscopeOutFpdInAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitPressureAction,
    WaitTemperatureAction,
)
from ...safety_rules import validate_ch11_oscillation_settings
from ..execution_trace import (
    LoopIteration,
    StaticTraceEntry,
    TraceEntry,
    format_loop_context,
    walk_raw,
)
from ..models import Severity, emit_static

if TYPE_CHECKING:
    from ...scheduler_settings import GlobalXrdSettings
    from ..pre_validator import PreCheckResult

# See module docstring — owned here (not pre_validator.py) because
# check_pace5000_params needs them; pre_validator.py imports these two names
# back for tests/test_exp_scheduler_pre_validator.py::Phase4PaceUnitDedupTests
# compatibility.
PACE_TO_MPA = _PACE_TO_MPA
PACE_VALID_RATE_UNITS = tuple(_PACE_RATE_TO_MPA_PER_MIN)
pace_rate_to_mpa_per_sec = _pace_rate_to_mpa_per_sec

# PM16C ASCII protocol: ABS/RELx±dddd move range (see
# utils/stage/IMPLEMENTATION_DETAILS.md).
PM16C_PULSE_MAX = 2_147_483_647
_STAGE_SPEED_LEVELS = ("H", "M", "L")


# ------------------------------------------------------------------ pure numeric parsers

def _is_strict_number(value) -> bool:
    """True for a genuine int/float — excludes bool (an int subclass in
    Python — `float(True) == 1.0` would otherwise silently accept a
    misplaced JSON `true`/`false`) and excludes str (a numeric-looking
    string like `"1.5"` converts cleanly via `float()`, but nothing
    downstream writes the converted value back onto the Action field — the
    field itself stays a `str`, and reaches a raw arithmetic expression at
    run time, e.g. `time.monotonic() + interval_s` /
    `max_ch4_um / PULSE_SCALE[4]` in `runner.py`, raising `TypeError`). Every
    current call site of `parse_finite_number`/`parse_stage_position` already
    resolves a loop-variable-name string to its numeric value (via
    `entry.variables`/`loop_values`) *before* calling this — a value that is
    still a `str` at this point is never a legitimate unresolved reference,
    only a type-confused literal."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_finite_number(
    value,
    *,
    what: str = "value",
    minimum: float | None = None,
    min_inclusive: bool = True,
    maximum: float | None = None,
    max_inclusive: bool = True,
    integer: bool = False,
) -> tuple[float | None, str | None]:
    """Pure: validate `value` is already a genuine int/float (not a
    numeric-looking string, not a bool) and a finite real number, optionally
    within [minimum, maximum] and/or integer-valued. Returns `(value, None)`
    on success or `(None, reason)` on failure, where `reason` has no label
    prefix — callers own how to present it (a `Diagnostic` message here, a
    plain `PreCheckResult.errors` string in `pre_validator.py`'s Global
    limits / `stage_settings.json` checks).

    This is a system-boundary check (DSL text, hand-edited/corrupted
    sequence JSON — Python's own `json.loads` accepts NaN/Infinity/-Infinity
    literals — or a numeric-literal overflow like `1e400` producing `inf` at
    the Python parser level): never skip silently. A silent skip just moves
    the same failure downstream to a raw comparison (crashing PreValidator —
    e.g. `None < 0`) or to a device backend call at run time.
    """
    if not _is_strict_number(value):
        return None, f"{what} is not numeric (got {value!r})"
    try:
        f = float(value)
    except OverflowError:
        # An int too large for float (e.g. from hand-edited/corrupted
        # Sequence JSON, or a DSL literal like 10**500) — a system boundary
        # value like any other non-numeric input, not a bug.
        return None, f"{what} is not numeric (got {value!r})"
    if math.isnan(f) or math.isinf(f):
        return None, f"{what} is NaN/Inf"
    if minimum is not None:
        ok = f >= minimum if min_inclusive else f > minimum
        if not ok:
            op = ">=" if min_inclusive else ">"
            return None, f"{what} must be {op} {minimum:g} (got {f:g})"
    if maximum is not None:
        ok = f <= maximum if max_inclusive else f < maximum
        if not ok:
            op = "<=" if max_inclusive else "<"
            return None, f"{what} must be {op} {maximum:g} (got {f:g})"
    if integer and f != int(f):
        return None, f"{what} must be an integer (got {f})"
    return f, None


def parse_stage_position(v) -> tuple[int | None, str | None]:
    """Pure: validate an already-resolved position/delta value (not a
    loop-variable name) as a finite integer pulse count within the PM16C
    protocol's ±2,147,483,647 ABS/REL range."""
    if not _is_strict_number(v):
        return None, f"position/delta is not numeric (got {v!r})"
    try:
        f = float(v)
    except OverflowError:
        return None, f"position/delta is not numeric (got {v!r})"
    if math.isnan(f) or math.isinf(f):
        return None, "position/delta is NaN/Inf"
    if f != int(f):
        return None, f"position/delta must be an integer pulse count (got {f})"
    n = int(f)
    if not (-PM16C_PULSE_MAX <= n <= PM16C_PULSE_MAX):
        return None, (
            f"position/delta {n} is outside the PM16C protocol range "
            f"±{PM16C_PULSE_MAX}"
        )
    return n, None


def require_finite_number(
    label: str,
    value,
    r: "PreCheckResult",
    *,
    code: str,
    action_path: str | None = None,
    loop_context: str | None = None,
    what: str = "value",
    minimum: float | None = None,
    min_inclusive: bool = True,
    maximum: float | None = None,
    max_inclusive: bool = True,
    integer: bool = False,
) -> float | None:
    """STATIC-phase wrapper around `parse_finite_number`: on failure, emits a
    `Diagnostic` (via `models.emit_static`) instead of returning an error
    string. Returns the parsed value (or None) exactly like
    `parse_finite_number` so call sites that need the resolved number keep
    the same `val = require_finite_number(...)` shape they had before
    Phase 5 (when this was `pre_validator._require_finite_number`)."""
    val, err = parse_finite_number(
        value, what=what, minimum=minimum, min_inclusive=min_inclusive,
        maximum=maximum, max_inclusive=max_inclusive, integer=integer,
    )
    if err is not None:
        emit_static(r, code, f"{label}: {err}", action_path=action_path, loop_context=loop_context)
    return val


def _require_stage_position(
    label: str, v, r: "PreCheckResult", action_path: str | None, loop_context: str | None,
) -> int | None:
    val, err = parse_stage_position(v)
    if err is not None:
        emit_static(
            r, "static.stage.invalid_position", f"{label}: {err}",
            action_path=action_path, loop_context=loop_context,
        )
    return val


# ------------------------------------------------------------------ Stage

def check_stage_schema(actions: list[Action], r: "PreCheckResult") -> None:
    """Validate StageAction / compound-stage-action fields against the
    PM16C protocol schema (ch range, known operation, speed level, finite
    integer position/delta in-range) independent of controller connectivity
    or move constraints.

    This matters because an out-of-range or non-integer `ch` silently
    no-ops in both PM16CController and PM16CControllerSim instead of
    raising — so without this check, a bad channel would look exactly like
    a successful move to SequenceRunner. Likewise an invalid speed level is
    a silent no-op in `set_ch_speed`/`set_ch_speed_value`.

    Walks the raw (unexpanded) action tree — a ForLoopAction body is visited
    once, not once per iteration — so a schema violation that doesn't depend
    on a loop variable is reported once. Where `value` (position/delta) is
    itself a loop variable, every candidate in the enclosing loop's `values`
    is validated (once per referencing action, each tagged with its own
    candidate index/value as `loop_context`), since `_do_stage` resolves and
    int()-converts it at run time exactly like a literal.

    Gated by `PreValidator._run_candidates` (`ExecutionTrace.stats.
    candidates_safe`) — this walk enumerates every candidate value of a
    referenced loop, so a single degenerate loop with an enormous `values`
    list is capped the same way `ordered`'s per-iteration unroll is,
    independent of nesting depth or the total-expanded-steps product.
    """

    for a, path, _siblings, _i, loop_values in walk_raw(actions):
        if isinstance(a, ForLoopAction):
            continue

        if isinstance(a, StageAction):
            label = a.describe()
            if a.operation not in StageAction.OPERATIONS:
                emit_static(
                    r, "static.stage.unknown_operation",
                    f"{label}: unknown stage operation {a.operation!r}",
                    action_path=path,
                )
            elif a.operation not in ("normal_stop", "emergency_stop"):
                if (
                    isinstance(a.ch, bool)
                    or not isinstance(a.ch, int)
                    or not (1 <= a.ch <= 11)
                ):
                    emit_static(
                        r, "static.stage.invalid_channel",
                        f"{label}: ch must be an integer 1-11 (got {a.ch!r}) — "
                        "an out-of-range channel silently no-ops instead of "
                        "raising, so the sequence would proceed as if the "
                        "move had succeeded",
                        action_path=path,
                    )

            if a.speed is not None and a.speed not in _STAGE_SPEED_LEVELS:
                emit_static(
                    r, "static.stage.invalid_speed",
                    f"{label}: speed must be one of {_STAGE_SPEED_LEVELS} "
                    f"or None (got {a.speed!r})",
                    action_path=path,
                )

            if a.operation in ("move_absolute", "move_relative"):
                if isinstance(a.value, str):
                    values = loop_values.get(a.value)
                    if values is not None:
                        for idx, v in enumerate(values):
                            lc = format_loop_context((LoopIteration(a.value, v, idx),))
                            _require_stage_position(label, v, r, path, lc)
                    # else: undefined loop variable; check_undefined_loop_vars reports it
                else:
                    _require_stage_position(label, a.value, r, path, None)

        elif isinstance(a, (MicroscopeOutFpdInAction, FpdOutMicroscopeInAction)):
            label = a.describe()
            if a.speed not in _STAGE_SPEED_LEVELS:
                emit_static(
                    r, "static.stage.invalid_speed",
                    f"{label}: speed must be one of {_STAGE_SPEED_LEVELS} "
                    f"(got {a.speed!r})",
                    action_path=path,
                )
            if isinstance(a, MicroscopeOutFpdInAction):
                explicit = [
                    ("microscope_out_pos", a.microscope_out_pos),
                    ("fpd_in_pos", a.fpd_in_pos),
                ]
            else:
                explicit = [
                    ("fpd_out_pos", a.fpd_out_pos),
                    ("microscope_in_pos", a.microscope_in_pos),
                ]
            for field_name, pos in explicit:
                if pos is not None:
                    _require_stage_position(f"{label} ({field_name})", pos, r, path, None)


# ------------------------------------------------------------------ PACE5000

def check_pace5000_params(entries: list["TraceEntry"], r: "PreCheckResult") -> None:
    """Validate literal/loop-resolved pressure-command parameters,
    independent of whether they came from the UI or the DSL. Consumes
    `ExecutionTrace.pace_primitives()` (SetAndWaitPressureAction already
    split into its set/wait pair) — gated by `within_limits` at the call
    site, same as the old `_walk_pace_actions`-based check."""
    for entry in entries:
        a = entry.action
        path = entry.action_path
        lc = format_loop_context(entry.loop_context)
        if isinstance(a, SetPressureAction):
            label = a.describe()
            if a.unit not in PACE_TO_MPA:
                emit_static(
                    r, "static.pace5000.invalid_unit",
                    f'{label}: unit must be "MPa" or "Bar" (got {a.unit!r})',
                    action_path=path, loop_context=lc,
                )

            pressure = a.pressure
            if isinstance(pressure, str):
                pressure = entry.variables.get(pressure)
            if pressure is not None:
                require_finite_number(
                    label, pressure, r, code="static.pace5000.non_finite_pressure",
                    action_path=path, loop_context=lc, what="pressure", minimum=0.0,
                )

            rate = require_finite_number(
                label, a.rate, r, code="static.pace5000.non_finite_rate",
                action_path=path, loop_context=lc, what="rate", minimum=0.0,
            )
            if rate is not None and a.rate_unit in PACE_VALID_RATE_UNITS:
                # rate=0 falls into this floor too (0 < 0.001) — the device's
                # own Scheduled Control feature already rejects rate<=0
                # outright, so "instantaneous" changes are not a
                # documented-safe hardware behaviour.
                rate_mpa_per_sec = _pace_rate_to_mpa_per_sec(rate, a.rate_unit)
                if rate_mpa_per_sec < _PACE_MIN_SLEW_RATE_MPA_PER_SEC:
                    emit_static(
                        r, "static.pace5000.rate_below_min_slew",
                        f"{label}: rate ({rate} {a.rate_unit} ≈ "
                        f"{rate_mpa_per_sec:.6f} MPa/sec) は PACE5000 のハードウェア"
                        f"最小 slew rate ({_PACE_MIN_SLEW_RATE_MPA_PER_SEC} MPa/sec) を"
                        "下回っています（rate=0 の瞬時変化を含む）。",
                        action_path=path, loop_context=lc,
                    )
                # else: invalid rate_unit — reported below

            if a.rate_unit not in PACE_VALID_RATE_UNITS:
                emit_static(
                    r, "static.pace5000.invalid_rate_unit",
                    f"{label}: rate_unit must be one of {PACE_VALID_RATE_UNITS} "
                    f"(got {a.rate_unit!r})",
                    action_path=path, loop_context=lc,
                )

        elif isinstance(a, WaitPressureAction):
            label = a.describe()
            if a.unit not in PACE_TO_MPA:
                emit_static(
                    r, "static.pace5000.invalid_unit",
                    f'{label}: unit must be "MPa" or "Bar" (got {a.unit!r})',
                    action_path=path, loop_context=lc,
                )

            tol = require_finite_number(
                label, a.tol, r, code="static.pace5000.non_finite_tol",
                action_path=path, loop_context=lc, what="tol",
                minimum=0.0, min_inclusive=False,
            )
            if tol is not None:
                tol_mpa = tol * PACE_TO_MPA.get(a.unit, 1.0)
                if tol_mpa < 0.0001:
                    emit_static(
                        r, "static.pace5000.tol_too_small",
                        f"{label}: tol ({tol} {a.unit}) が 0.0001 MPa 未満です — "
                        "収束に時間がかかる、または到達しない可能性があります。",
                        action_path=path, loop_context=lc, severity=Severity.WARNING,
                    )


# ------------------------------------------------------------------ LakeShore 335

def check_lakeshore_params(actions: list[Action], r: "PreCheckResult") -> None:
    """Validate LakeShore-335-related literal/loop-resolved Action fields:
    `SetTemperatureAction.ramp_rate` (finite, >=0), `.value_k` (finite,
    <=300K — a loop-variable-capable field, so every candidate value of the
    referenced loop is checked, same pattern as `check_stage_schema`),
    `WaitTemperatureAction.tol_k` (finite, >0), and
    `SetHeaterAction.range_index` (one of 0/1/2/3).

    These were previously validated inline inside the live/ordered
    `_check_lakeshore_sequence` state-machine check in pre_validator.py;
    moved here so the two checks don't independently re-validate the same
    fields (§7 Phase 5 item 7). `_check_lakeshore_sequence` still needs the
    *resolved numeric value* of `ramp_rate`/`value_k` for its own
    cooling/heating-rate heuristics — it gets that via the
    non-error-emitting `pre_validator._try_resolve_float()` helper, not by
    re-validating.

    Gated by `PreValidator._run_candidates` for the same reason as
    `check_stage_schema` (the `value_k` loop-candidate enumeration).
    """

    for a, path, _siblings, _i, loop_values in walk_raw(actions):
        if isinstance(a, ForLoopAction):
            continue

        if isinstance(a, SetTemperatureAction):
            label = a.describe()
            require_finite_number(
                label, a.ramp_rate, r, code="static.lakeshore.non_finite_ramp_rate",
                action_path=path, what="ramp_rate", minimum=0.0,
            )
            if isinstance(a.value_k, str):
                values = loop_values.get(a.value_k)
                if values is not None:
                    for idx, v in enumerate(values):
                        lc = format_loop_context((LoopIteration(a.value_k, v, idx),))
                        _check_value_k(label, v, r, path, lc)
                    # else: undefined loop variable; check_undefined_loop_vars reports it
            else:
                _check_value_k(label, a.value_k, r, path, None)

        elif isinstance(a, WaitTemperatureAction):
            label = a.describe()
            require_finite_number(
                label, a.tol_k, r, code="static.lakeshore.non_finite_tol_k",
                action_path=path, what="tol_k", minimum=0.0, min_inclusive=False,
            )

        elif isinstance(a, SetHeaterAction):
            if a.range_index not in (0, 1, 2, 3):
                emit_static(
                    r, "static.lakeshore.invalid_range_index",
                    f"{a.describe()}: range_index must be one of 0/1/2/3 "
                    f"(got {a.range_index!r})",
                    action_path=path,
                )


def _check_value_k(
    label: str, v, r: "PreCheckResult", action_path: str, loop_context: str | None,
) -> None:
    val = require_finite_number(
        label, v, r, code="static.lakeshore.non_finite_value_k",
        action_path=action_path, loop_context=loop_context, what="value_k",
    )
    if val is not None and val > 300.0:
        emit_static(
            r, "static.lakeshore.value_k_too_high",
            f"{label}: setpoint {val} K が上限の 300 K を超えています",
            action_path=action_path, loop_context=loop_context,
        )


# ------------------------------------------------------------------ Rad-icon / XRD

def check_xrd_params(
    flat: list["StaticTraceEntry"], global_xrd: "GlobalXrdSettings | None", r: "PreCheckResult",
) -> None:
    """Validate `TakeXrdAction` (exposure_ms, oscillation settings, file
    overrides) and `TakeDarkAction.exposure_ms` (moved here from
    `pre_validator._check_radicon`, which now only checks Rad-icon
    connectivity — §7 Phase 5 item 7)."""
    xrd_entries = [e for e in flat if isinstance(e.action, TakeXrdAction)]
    dark_entries = [e for e in flat if isinstance(e.action, TakeDarkAction)]
    if not xrd_entries and not dark_entries:
        return

    g = global_xrd  # may be None; runner will use GlobalXrdSettings() defaults
    if g is not None:
        if g.dark_enabled and g.dark_file:
            if not Path(g.dark_file).exists():
                emit_static(
                    r, "static.xrd.dark_file_missing",
                    f"Global XRD dark file not found: {g.dark_file}",
                    severity=Severity.WARNING,
                )
        if g.defect_enabled and g.defect_file:
            if not Path(g.defect_file).exists():
                emit_static(
                    r, "static.xrd.defect_file_missing",
                    f"Global XRD defect file not found: {g.defect_file}",
                    severity=Severity.WARNING,
                )

    for e in dark_entries:
        a = e.action
        require_finite_number(
            a.describe(), a.exposure_ms, r, code="static.xrd.non_finite_exposure",
            action_path=e.action_path, what="exposure_ms", minimum=0.0, min_inclusive=False,
        )

    for e in xrd_entries:
        a = e.action
        path = e.action_path
        label = a.describe()
        if a.exposure_ms is not None:
            require_finite_number(
                label, a.exposure_ms, r, code="static.xrd.non_finite_exposure",
                action_path=path, what="exposure_ms", minimum=0.0, min_inclusive=False,
            )

        oscillate = a.oscillate if a.oscillate is not None else (
            g.oscillate if g is not None else False
        )
        if oscillate:
            pos_a_deg = a.osc_pos_a_deg if a.osc_pos_a_deg is not None else (
                g.osc_pos_a_deg if g is not None else -5.0
            )
            pos_b_deg = a.osc_pos_b_deg if a.osc_pos_b_deg is not None else (
                g.osc_pos_b_deg if g is not None else 20.0
            )
            dwell_ms = a.osc_dwell_ms if a.osc_dwell_ms is not None else (
                g.osc_dwell_ms if g is not None else 0
            )
            speed = a.osc_speed if a.osc_speed is not None else (
                g.osc_speed if g is not None else "M"
            )
            try:
                validate_ch11_oscillation_settings(pos_a_deg, pos_b_deg, dwell_ms, speed)
            except ValueError as exc:
                emit_static(
                    r, "static.xrd.invalid_oscillation", f"{label}: {exc}", action_path=path,
                )

        if a.dark_enabled is True and a.dark_file is not None:
            if not Path(a.dark_file).exists():
                emit_static(
                    r, "static.xrd.dark_file_not_found",
                    f"{label}: dark file not found: {a.dark_file}",
                    action_path=path, severity=Severity.WARNING,
                )
        if a.defect_enabled is True and a.defect_file is not None:
            if not Path(a.defect_file).exists():
                emit_static(
                    r, "static.xrd.defect_file_not_found",
                    f"{label}: defect file not found: {a.defect_file}",
                    action_path=path, severity=Severity.WARNING,
                )
        if a.save_dir is not None:
            p = Path(a.save_dir)
            if not p.exists():
                emit_static(
                    r, "static.xrd.save_dir_will_be_created",
                    f"{label}: save_dir does not exist and will be created: {a.save_dir}",
                    action_path=path, severity=Severity.WARNING,
                )
            elif not p.is_dir():
                emit_static(
                    r, "static.xrd.save_dir_not_a_directory",
                    f"{label}: save_dir is not a directory: {a.save_dir}",
                    action_path=path,
                )


# ------------------------------------------------------------------ Wait / duration

def check_durations(flat: list["StaticTraceEntry"], r: "PreCheckResult") -> None:
    """WaitAction/FollowSampleAction durations must be finite and > 0. A
    non-finite duration (e.g. from a DSL numeric-literal overflow like
    `wait(duration=1e400)`) makes `SequenceRunner._do_wait()`'s
    `deadline = now + duration_s` an unreachable point in the future — the
    sequence hangs until someone notices and presses Stop."""
    for e in flat:
        a = e.action
        if isinstance(a, (WaitAction, FollowSampleAction)):
            require_finite_number(
                a.describe(), a.duration_s, r, code="static.duration.non_finite",
                action_path=e.action_path, what="duration_s", minimum=0.0, min_inclusive=False,
            )


# ------------------------------------------------------------------ Camera / Follow

def check_follow_params(flat: list["StaticTraceEntry"], r: "PreCheckResult") -> None:
    """Per-step follow-action overrides — interval_s, similarity_threshold,
    max_correction_per_step_um — are optional (None -> GlobalFollowSettings
    default) but must be sane finite numbers when given explicitly. These
    reach the background follow thread uncaught otherwise."""
    for e in flat:
        a = e.action
        if not isinstance(a, (StartFollowingAction, FollowSampleAction)):
            continue
        label = a.describe()
        if a.interval_s is not None:
            require_finite_number(
                label, a.interval_s, r, code="static.follow.invalid_interval",
                action_path=e.action_path, what="interval_s", minimum=0.0, min_inclusive=False,
            )
        if a.similarity_threshold is not None:
            require_finite_number(
                label, a.similarity_threshold, r,
                code="static.follow.invalid_similarity_threshold",
                action_path=e.action_path, what="similarity_threshold",
                minimum=0.0, maximum=1.0,
            )
        if a.max_correction_per_step_um is not None:
            require_finite_number(
                label, a.max_correction_per_step_um, r,
                code="static.follow.invalid_max_correction",
                action_path=e.action_path, what="max_correction_per_step_um", minimum=0.0,
            )


def check_autofocus(
    flat: list["StaticTraceEntry"], global_limits, r: "PreCheckResult",
) -> None:
    af_entries = [e for e in flat if isinstance(e.action, (StartFollowingAction, FollowSampleAction))]
    if not af_entries:
        return

    for e in af_entries:
        a = e.action
        label = a.describe()
        range_um = getattr(a, "autofocus_range_um", None)
        steps = getattr(a, "autofocus_steps", None)
        # Comparing directly (range_um <= 0 / steps < 2) would silently pass
        # NaN — every comparison against NaN is False — so a NaN/Inf
        # autofocus field would reach SequenceRunner's int() conversion
        # uncaught. require_finite_number rejects NaN/Inf/non-numeric first.
        if range_um is not None:
            require_finite_number(
                label, range_um, r, code="static.follow.invalid_autofocus_range",
                action_path=e.action_path, what="autofocus_range_um",
                minimum=0.0, min_inclusive=False,
            )
        if steps is not None:
            require_finite_number(
                label, steps, r, code="static.follow.invalid_autofocus_steps",
                action_path=e.action_path, what="autofocus_steps",
                minimum=2.0, integer=True,
            )

    # Warn if Ch3 global limits are absent (autofocus could move Ch3 unboundedly)
    if global_limits is None or (
        global_limits.ch3_minus_mm is None or global_limits.ch3_plus_mm is None
    ):
        emit_static(
            r, "static.follow.autofocus_ch3_limits_unset",
            "Autofocus (Ch3) is enabled but Ch3 global limits are not set — "
            "Ch3 may move without bound during autofocus",
            severity=Severity.WARNING,
        )
