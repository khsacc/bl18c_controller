"""
Pre-execution validator for ExperimentalScheduler sequences.

Runs static analysis on a Sequence before SequenceRunner starts.
All checks run to completion (errors are accumulated, not short-circuited)
so the user sees every problem in one dialog.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..actions import (
    Action,
    AllHeatersOffAction,
    FollowSampleAction,
    ForLoopAction,
    FpdOutMicroscopeInAction,
    LogAction,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    SetControlModeAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    StageAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitAction,
    WaitPressureAction,
    WaitTemperatureAction,
    action_loop_var_ref,
)
from ..device_context import DeviceContext
from ..runner import (
    GlobalLimits,
    GlobalXrdSettings,
    _validate_ch11_oscillation_settings,
)
from ..sequence import Sequence
from settings import log_prefs
from utils.stage.control_stage import MOVE_CONSTRAINTS, _OPS
from apps.stage_fpd_scope.stage_settings import SETTINGS_FILE as _STAGE_SETTINGS_PATH

if TYPE_CHECKING:
    from utils.stage.control_stage_sim import PM16CControllerSim


_CALIBRATION_PATH = (
    Path(__file__).parent.parent.parent / "interactive_camera" / "calibration.json"
)
_DEFAULT_REF_PATH = Path(__file__).parent.parent / "__localdata" / "reference_frame.png"

# settings/log_prefs.py app key — validation logs save under __localdata/pre_validator/
_LOG_KEY = "pre_validator"

# Unit conversion to MPa (GPa not supported by PACE5000)
_PACE_TO_MPA: dict[str, float] = {"MPa": 1.0, "Bar": 0.1}
_PACE_VALID_UNITS = ("MPa", "Bar")
_PACE_VALID_RATE_UNITS = ("MPa/min", "Bar/min", "MPa/sec", "Bar/sec")


def _walk_pace_actions(actions: list, var_context: dict, visitor) -> None:
    """Depth-first walk in execution order, expanding ForLoopAction bodies
    once per loop value so per-iteration ordering checks see the real
    sequence of pressure commands."""
    for a in actions:
        if isinstance(a, ForLoopAction):
            for val in a.values:
                _walk_pace_actions(a.body, {**var_context, a.var: val}, visitor)
        else:
            visitor(a, var_context)


def _expand_execution_order(actions: list, var_context: dict) -> list[tuple[Action, dict]]:
    """Flatten the action tree into a single list of (action, var_context)
    pairs in true execution order, expanding ForLoopAction bodies once per
    loop value. Unlike `_collect_all_actions`, the per-iteration variable
    context travels with each action so downstream checks can resolve
    loop-variable references (e.g. SetTemperatureAction.value_k == "t")."""
    out: list[tuple[Action, dict]] = []
    for a in actions:
        if isinstance(a, ForLoopAction):
            for val in a.values:
                out.extend(_expand_execution_order(a.body, {**var_context, a.var: val}))
        else:
            out.append((a, var_context))
    return out


@dataclass
class PreCheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # All-channel (Ch1-11) stage positions read at validation time, in pulses.
    # Populated whenever a stage controller is connected. Used by the UI to
    # detect stage moves that happen between "Validate" and "Run".
    baseline_positions: dict[int, int] = field(default_factory=dict)

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
    ) -> PreCheckResult:
        result = PreCheckResult()
        flat = self._collect_all_actions(sequence.actions)

        log_lines: list[str] = []

        def _log(msg: str) -> None:
            print(msg)
            log_lines.append(msg)

        _SEP = "─" * 60
        _log(f"\n[PreValidator] {_SEP}")
        _log(f"[PreValidator] Sequence : {sequence.name!r}")
        _log(f"[PreValidator] Actions  : {len(sequence.actions)} top-level / {len(flat)} flat")
        n_counts = {
            "stage":     sum(1 for a in flat if isinstance(a, (StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction))),
            "pace5000":  sum(1 for a in flat if isinstance(a, (SetPressureAction, WaitPressureAction, SetControlModeAction))),
            "lakeshore": sum(1 for a in flat if isinstance(a, (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction))),
            "xrd/dark":  sum(1 for a in flat if isinstance(a, (TakeXrdAction, TakeDarkAction))),
            "camera":    sum(1 for a in flat if isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction, FollowSampleAction))),
        }
        _log(f"[PreValidator] Counts   : " + "  ".join(f"{k}={v}" for k, v in n_counts.items()))
        _log(f"[PreValidator] Inputs   : global_limits={'set' if global_limits is not None else 'None'}  global_xrd={'set' if global_xrd is not None else 'None'}")
        _log(f"[PreValidator] {_SEP}")

        def _run(label: str, fn, *args) -> None:
            e0 = len(result.errors)
            w0 = len(result.warnings)
            fn(*args)
            new_e = result.errors[e0:]
            new_w = result.warnings[w0:]
            if not new_e and not new_w:
                _log(f"[PreValidator]   {label:<38}  OK")
            else:
                status = "ERROR" if new_e else "WARN"
                _log(f"[PreValidator]   {label:<38}  {status}")
                for msg in new_e:
                    _log(f"[PreValidator]     ✗ {msg}")
                for msg in new_w:
                    _log(f"[PreValidator]     ⚠ {msg}")

        # Safeguard: global limits configuration
        def _check_global_limits() -> None:
            if global_limits is not None and not global_limits.is_fully_configured():
                result.errors.append(
                    "Global limits are not fully configured — "
                    "all six Ch3/4/5 ±mm values must be set before running"
                )
        _run("global_limits", _check_global_limits)

        _run("_check_stage",          self._check_stage,          flat, ctx, result)
        _run(
            "_check_xrd_oscillation_stage", self._check_xrd_oscillation_stage,
            flat, ctx, global_xrd, result,
        )
        _run("_check_stage_compound", self._check_stage_compound, flat, ctx, result)
        _run(
            "_check_stage_move_constraints", self._check_stage_move_constraints,
            sequence.actions, ctx, result, global_xrd,
        )
        _run("_check_pace5000",              self._check_pace5000,              flat, ctx, result, sequence.actions)
        _run("_check_pace5000_control_mode", self._check_pace5000_control_mode, ctx, result, sequence.actions)
        _run("_check_pace5000_adjacency",    self._check_pace5000_adjacency,    sequence.actions, result)
        _run("_check_pace5000_ordering",     self._check_pace5000_ordering,     sequence.actions, result)
        _run("_check_pace5000_params",       self._check_pace5000_params,       sequence.actions, result)
        _run("_check_lakeshore",      self._check_lakeshore,      flat, ctx, result)
        _run("_check_lakeshore_sequence", self._check_lakeshore_sequence, sequence.actions, ctx, result)
        _run("_check_radicon",        self._check_radicon,        flat, ctx, result)
        _run("_check_camera",         self._check_camera,         flat, ctx, result)
        _run("_check_follow_pairing", self._check_follow_pairing, sequence.actions, result)
        _run("_check_unused_loop_vars", self._check_unused_loop_vars, sequence.actions, result)
        _run("_check_undefined_loop_vars", self._check_undefined_loop_vars, sequence.actions, result)
        _run("_check_empty_loop_body", self._check_empty_loop_body, sequence.actions, result)

        e0 = len(result.errors)
        initial_mode = self._detect_stage_mode(ctx, result)
        new_e = result.errors[e0:]
        _log(f"[PreValidator]   {'_detect_stage_mode':<38}  {initial_mode!r}" + (f"  ERROR" if new_e else ""))
        for msg in new_e:
            _log(f"[PreValidator]     ✗ {msg}")

        _run("_check_stage_mode_ordering", self._check_stage_mode_ordering, sequence.actions, initial_mode, result)
        _run("_check_autofocus",           self._check_autofocus,           flat, global_limits, result)
        _run("_check_xrd_settings",        self._check_xrd_settings,        flat, global_xrd, result)

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

    # ------------------------------------------------------------------ collection

    @staticmethod
    def _collect_all_actions(actions: list) -> list[Action]:
        """Recursively flatten ForLoopAction bodies into a single action list."""
        result: list[Action] = []
        for a in actions:
            if isinstance(a, ForLoopAction):
                result.extend(PreValidator._collect_all_actions(a.body))
            else:
                result.append(a)
        return result

    # ------------------------------------------------------------------ stage checks

    @staticmethod
    def _check_stage(flat: list[Action], ctx: DeviceContext, r: PreCheckResult) -> None:
        stage_actions = [
            a for a in flat
            if isinstance(a, (StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction))
        ]
        if not stage_actions:
            return

        if ctx.controller is None:
            r.errors.append("Stage controller is not connected (required for stage operations)")
            return

        try:
            from utils.stage.control_stage_sim import PM16CControllerSim
            if isinstance(ctx.controller, PM16CControllerSim):
                r.warnings.append("Stage is running in simulation mode (PM16CControllerSim)")
        except ImportError:
            pass

        if ctx.controller.get_is_moving():
            r.errors.append(
                "Stage is currently moving — wait until all axes stop before starting a sequence"
            )

    @staticmethod
    def _check_xrd_oscillation_stage(
        flat: list[Action],
        ctx: DeviceContext,
        global_xrd: GlobalXrdSettings | None,
        r: PreCheckResult,
    ) -> None:
        """Ch11 oscillation makes an XRD action a stage operation too."""
        oscillating_actions = [
            a for a in flat
            if isinstance(a, TakeXrdAction) and (
                a.oscillate if a.oscillate is not None
                else (global_xrd.oscillate if global_xrd is not None else False)
            )
        ]
        if not oscillating_actions:
            return
        if ctx.controller is None:
            r.errors.append(
                "Stage controller is not connected (required for Ch11 oscillation)"
            )
            return
        if ctx.controller.get_is_moving():
            r.errors.append(
                "Stage is currently moving — wait until all axes stop before starting Ch11 oscillation"
            )

    @staticmethod
    def _check_stage_compound(
        flat: list[Action], ctx: DeviceContext, r: PreCheckResult
    ) -> None:
        for a in flat:
            if isinstance(a, MicroscopeOutFpdInAction):
                if a.microscope_out_pos is None or a.fpd_in_pos is None:
                    _check_stage_settings(
                        r,
                        required_keys=["ch8_out", "det_in"],
                        action_name="microscope_out_and_fpd_in",
                    )
                    break  # one check is enough even if action appears multiple times

        for a in flat:
            if isinstance(a, FpdOutMicroscopeInAction):
                if a.fpd_out_pos is None or a.microscope_in_pos is None:
                    _check_stage_settings(
                        r,
                        required_keys=["det_out", "ch8_in"],
                        action_name="fpd_out_and_microscope_in",
                    )
                    break

    @staticmethod
    def _check_stage_move_constraints(
        actions: list,
        ctx: DeviceContext,
        r: PreCheckResult,
        global_xrd: GlobalXrdSettings | None,
    ) -> None:
        """Simulate every stage move in the sequence (including for-loop
        iterations and microscope/FPD compound-action expansions) starting
        from the current stage position, verifying MOVE_CONSTRAINTS
        (Ch8/Ch9 interlock) is never violated at any point.

        Also records the current all-11-channel position onto `r` — the UI
        uses this as the baseline to detect stage moves between Validate and
        Run.
        """
        if ctx.controller is None:
            return  # already reported by _check_stage

        positions: dict[int, int] = {}
        for ch in range(1, 12):
            try:
                positions[ch] = int(ctx.controller.get_ch_pos(ch))
            except Exception:
                r.errors.append(
                    f"Cannot read Ch{ch} position (required for move-constraint validation)"
                )
                return
        r.baseline_positions = dict(positions)

        for msg in _violates_move_constraints(positions):
            r.errors.append(f"現在位置: {msg}")

        stage_settings = _load_stage_settings_dict()
        # Step numbers mirror SequenceRunner._flat_index (1-based here to match
        # the "Step N" label shown during an actual run): every leaf action
        # (i.e. everything except ForLoopAction itself) advances the counter
        # once, regardless of action type, so numbers line up with the run log
        # even when non-stage actions are interleaved.
        step_counter = [0]

        def _apply(step: StageAction, var_context: dict, step_no: int, label: str) -> None:
            if step.operation not in ("move_absolute", "move_relative"):
                return
            value = step.value
            if isinstance(value, str):
                value = var_context.get(value)
                if value is None:
                    return  # unresolved loop variable; already flagged elsewhere
            value = int(value)
            target = value if step.operation == "move_absolute" else positions[step.ch] + value
            for msg in _violates_move_constraints_for_move(positions, step.ch, target):
                r.errors.append(f"Step{step_no}: {label}: {msg}")
            positions[step.ch] = target

        def _walk(acts: list, var_context: dict) -> None:
            for a in acts:
                if isinstance(a, ForLoopAction):
                    for val in a.values:
                        _walk(a.body, {**var_context, a.var: val})
                    continue
                step_counter[0] += 1
                step_no = step_counter[0]
                if isinstance(a, (MicroscopeOutFpdInAction, FpdOutMicroscopeInAction)):
                    if stage_settings is None:
                        continue  # already reported by _check_stage_compound
                    for step in a.to_steps(stage_settings):
                        _apply(step, var_context, step_no, a.describe())
                elif isinstance(a, StageAction):
                    _apply(a, var_context, step_no, a.describe())
                elif isinstance(a, TakeXrdAction):
                    oscillate = (
                        a.oscillate if a.oscillate is not None
                        else (global_xrd.oscillate if global_xrd is not None else False)
                    )
                    if not oscillate:
                        continue
                    pos_a_deg = (
                        a.osc_pos_a_deg if a.osc_pos_a_deg is not None
                        else (global_xrd.osc_pos_a_deg if global_xrd is not None else -5.0)
                    )
                    pos_b_deg = (
                        a.osc_pos_b_deg if a.osc_pos_b_deg is not None
                        else (global_xrd.osc_pos_b_deg if global_xrd is not None else 20.0)
                    )
                    dwell_ms = (
                        a.osc_dwell_ms if a.osc_dwell_ms is not None
                        else (global_xrd.osc_dwell_ms if global_xrd is not None else 0)
                    )
                    speed = (
                        a.osc_speed if a.osc_speed is not None
                        else (global_xrd.osc_speed if global_xrd is not None else "M")
                    )
                    try:
                        targets = _validate_ch11_oscillation_settings(
                            pos_a_deg, pos_b_deg, dwell_ms, speed
                        )
                    except ValueError:
                        continue  # _check_xrd_settings reports the configuration error.
                    for target in targets:
                        for msg in _violates_move_constraints_for_move(positions, 11, target):
                            r.errors.append(f"Step{step_no}: {a.describe()}: {msg}")

        _walk(actions, {})

    # ------------------------------------------------------------------ PACE5000 checks

    @staticmethod
    def _check_pace5000(
        flat: list[Action],
        ctx: DeviceContext,
        r: PreCheckResult,
        original_actions: list | None = None,
    ) -> None:
        pace_actions = [
            a for a in flat
            if isinstance(a, (SetPressureAction, WaitPressureAction, SetControlModeAction))
        ]
        if not pace_actions:
            return

        if ctx.pace5000 is None or not ctx.pace5000._is_connected:
            r.errors.append(
                "PACE5000 is not connected (required for pressure operations)"
            )
            return

        # Validation: find max set pressure across the whole sequence and compare
        # against the current +ve source pressure.
        if original_actions is not None:
            _check_pace5000_source_pressure(original_actions, ctx, r)

    @staticmethod
    def _check_pace5000_control_mode(
        ctx: DeviceContext, r: PreCheckResult, original_actions: list
    ) -> None:
        """Detect sequences that set/wait on pressure while the PACE5000 is
        still in Measure mode (Pressure Control : OFF), so the commands
        would silently have no effect.

        Step 1: pressure ops exist but set_control_mode is never called.
        Step 2: set_control_mode is called, but more than one set_pressure
        happens before the first enabling call — likely a user who forgot
        the mode was still Measure while iterating.
        """
        pace_related: list[Action] = []
        _walk_pace_actions(
            original_actions, {},
            lambda a, vc: pace_related.append(a)
            if isinstance(a, (SetPressureAction, WaitPressureAction, SetControlModeAction))
            else None,
        )
        if not any(isinstance(a, (SetPressureAction, WaitPressureAction)) for a in pace_related):
            return

        if ctx.pace5000 is None or not ctx.pace5000._is_connected:
            return  # already reported by _check_pace5000

        try:
            output_state = ctx.pace5000.get_output_state()
        except Exception:
            return
        if output_state is None:
            return
        if output_state.strip() in ("1", "ON"):
            return  # already in Control mode

        msg = (
            "圧力を変更するコマンドが送信されますが、Control ModeがMeasureのままのため、"
            "実際には圧力が変化しません。"
        )

        if not any(isinstance(a, SetControlModeAction) for a in pace_related):
            r.errors.append(msg)
            return

        state = {"count": 0, "controlled": False, "violation": False}

        def _check2(a: Action, vc: dict) -> None:
            if state["controlled"] or state["violation"]:
                return
            if isinstance(a, SetPressureAction):
                state["count"] += 1
                if state["count"] > 1:
                    state["violation"] = True
            elif isinstance(a, SetControlModeAction) and a.enabled:
                state["controlled"] = True

        _walk_pace_actions(original_actions, {}, _check2)
        if state["violation"]:
            r.errors.append(msg)

    @staticmethod
    def _check_pace5000_adjacency(actions: list, r: PreCheckResult) -> None:
        """Warn when a set_pressure is not immediately followed by a wait,
        since the sequence will keep going before the setpoint is reached."""

        def _scan(acts: list) -> None:
            for i, a in enumerate(acts):
                if isinstance(a, ForLoopAction):
                    _scan(a.body)
                    continue
                if isinstance(a, SetPressureAction):
                    nxt = acts[i + 1] if i + 1 < len(acts) else None
                    if not isinstance(nxt, (WaitAction, WaitPressureAction)):
                        r.warnings.append(
                            f"{a.describe()}: 圧力変更後、設定圧力に到達するのを待たずに"
                            "次の動作が始まります。問題ないか確認してください。"
                        )

        _scan(actions)

    @staticmethod
    def _check_pace5000_ordering(actions: list, r: PreCheckResult) -> None:
        """Error when wait_pressure appears with no preceding set_pressure;
        warn when consecutive set_pressure calls have no wait_pressure
        between them."""
        state = {"seen_set_pressure": False, "wait_since_last_set": True}

        def _visit(a: Action, vc: dict) -> None:
            if isinstance(a, SetPressureAction):
                if state["seen_set_pressure"] and not state["wait_since_last_set"]:
                    r.warnings.append(
                        f"{a.describe()}: 直前の set_pressure との間に wait_pressure が"
                        "ないまま、続けて set_pressure が実行されています。"
                    )
                state["seen_set_pressure"] = True
                state["wait_since_last_set"] = False
            elif isinstance(a, WaitPressureAction):
                if not state["seen_set_pressure"]:
                    r.errors.append(
                        f"{a.describe()}: 直前に set_pressure が実行されていません。"
                    )
                state["wait_since_last_set"] = True

        _walk_pace_actions(actions, {}, _visit)

    @staticmethod
    def _check_pace5000_params(actions: list, r: PreCheckResult) -> None:
        """Validate literal/loop-resolved pressure-command parameters,
        independent of whether they came from the UI or the DSL."""

        def _visit(a: Action, vc: dict) -> None:
            if isinstance(a, SetPressureAction):
                label = a.describe()
                if a.unit not in _PACE_VALID_UNITS:
                    r.errors.append(f"{label}: unit must be \"MPa\" or \"Bar\" (got {a.unit!r})")

                pressure = a.pressure
                if isinstance(pressure, str):
                    pressure = vc.get(pressure)
                if pressure is not None:
                    try:
                        p = float(pressure)
                    except (TypeError, ValueError):
                        p = None
                    if p is not None:
                        if math.isnan(p) or math.isinf(p):
                            r.errors.append(f"{label}: pressure is NaN/Inf")
                        elif p < 0:
                            r.errors.append(f"{label}: pressure must be >= 0 (got {p})")

                try:
                    rate = float(a.rate)
                except (TypeError, ValueError):
                    rate = None
                if rate is not None:
                    if math.isnan(rate) or math.isinf(rate):
                        r.errors.append(f"{label}: rate is NaN/Inf")
                    elif rate < 0:
                        r.errors.append(f"{label}: rate must be >= 0 (got {rate})")
                    elif rate == 0:
                        r.warnings.append(
                            f"{label}: rate=0 は瞬時に設定値を変更します（推奨されません）"
                        )

                if a.rate_unit not in _PACE_VALID_RATE_UNITS:
                    r.errors.append(
                        f"{label}: rate_unit must be one of {_PACE_VALID_RATE_UNITS} "
                        f"(got {a.rate_unit!r})"
                    )

            elif isinstance(a, WaitPressureAction):
                label = a.describe()
                if a.unit not in _PACE_VALID_UNITS:
                    r.errors.append(f"{label}: unit must be \"MPa\" or \"Bar\" (got {a.unit!r})")

                try:
                    tol = float(a.tol)
                except (TypeError, ValueError):
                    tol = None
                if tol is not None:
                    if math.isnan(tol) or math.isinf(tol):
                        r.errors.append(f"{label}: tol is NaN/Inf")
                    elif tol <= 0:
                        r.errors.append(f"{label}: tol must be > 0 (got {tol})")
                    else:
                        tol_mpa = tol * _PACE_TO_MPA.get(a.unit, 1.0)
                        if tol_mpa < 0.0001:
                            r.warnings.append(
                                f"{label}: tol ({tol} {a.unit}) が 0.0001 MPa 未満です — "
                                "収束に時間がかかる、または到達しない可能性があります。"
                            )

        _walk_pace_actions(actions, {}, _visit)

    # ------------------------------------------------------------------ LakeShore checks

    @staticmethod
    def _check_lakeshore(
        flat: list[Action], ctx: DeviceContext, r: PreCheckResult
    ) -> None:
        ls_actions = [
            a for a in flat
            if isinstance(
                a,
                (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction),
            )
        ]
        if not ls_actions:
            return

        if ctx.lakeshore is None or not ctx.lakeshore.is_connected:
            r.errors.append(
                "LakeShore 335 is not connected (required for temperature operations)"
            )
            return

        try:
            ctx.lakeshore.get_setpoint()
        except Exception:
            r.errors.append(
                "LakeShore 335 の現在の設定値を読み出せませんでした — "
                "通信に問題がある可能性があります"
            )

        if any(isinstance(a, WaitTemperatureAction) for a in ls_actions):
            try:
                data = ctx.lakeshore.get_data()
                if not data:
                    r.warnings.append(
                        "LakeShore has not produced any readings yet — "
                        "wait_temperature may hang until the first reading arrives"
                    )
            except Exception:
                pass

    @staticmethod
    def _check_lakeshore_sequence(
        actions: list, ctx: DeviceContext, r: PreCheckResult
    ) -> None:
        """Single forward pass over the LakeShore-335-related command stream
        in execution order (ForLoopAction bodies expanded per iteration),
        tracking the running setpoint / heater state at each step so that
        ordering, parameter, and ramp-rate checks can all be evaluated
        together — analogous to how stage positions are simulated across
        every step in `_check_stage_move_constraints`."""
        flat = PreValidator._collect_all_actions(actions)
        if not any(
            isinstance(
                a,
                (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction),
            )
            for a in flat
        ):
            return

        initial_setpoint: float | None = None
        initial_heater_on: bool | None = None
        if ctx.lakeshore is not None and ctx.lakeshore.is_connected:
            try:
                initial_setpoint = ctx.lakeshore.get_setpoint()
            except Exception:
                pass
            try:
                initial_heater_on = ctx.lakeshore.get_heater_range() != 0
            except Exception:
                pass

        ordered = _expand_execution_order(actions, {})

        current_setpoint = initial_setpoint
        heater_on = initial_heater_on
        seen_set_temp_ever = False
        heater_turned_on_before_first_set = False
        all_heaters_off_pending = False
        wait_temp_since_last_set = True
        since_set_has_wait_temp = False
        since_set_has_follow = False
        follow_open = False
        prev_was_wait_temp = False

        for i, (a, vc) in enumerate(ordered):
            label = f"Step{i + 1}: {a.describe()}"

            if prev_was_wait_temp and isinstance(a, (FollowSampleAction, StartFollowingAction)):
                r.errors.append(
                    f"{label}: 直前の wait_temperature の直後に追従を開始しようとしています。"
                    "wait_temperature の間に温度が変化しているため試料位置がずれている可能性が"
                    "あります。set_temperature → start_following → wait_temperature の順に"
                    "してください。"
                )
            prev_was_wait_temp = isinstance(a, WaitTemperatureAction)

            if isinstance(a, SetTemperatureAction):
                if a.ramp_rate < 0:
                    r.errors.append(f"{label}: ramp_rate must be >= 0 (got {a.ramp_rate})")

                val = _validate_ls_temp_value(label, a.value_k, vc, r)
                if val is not None and val > 300.0:
                    r.errors.append(
                        f"{label}: setpoint {val} K が上限の 300 K を超えています"
                    )

                if all_heaters_off_pending:
                    r.errors.append(
                        f"{label}: 直前に all_heaters_off が実行されており、ヒーターOFFの状態の"
                        "まま温度設定を変更しようとしています。"
                    )

                if not seen_set_temp_ever:
                    if initial_heater_on is False and not heater_turned_on_before_first_set:
                        r.warnings.append(
                            f"{label}: 現在ヒーター出力がOFFです。最初の set_temperature より"
                            "前に set_heater でヒーター出力を入れていないため、温度制御が"
                            "できない可能性があります。"
                        )
                elif not wait_temp_since_last_set:
                    r.warnings.append(
                        f"{label}: 直前の set_temperature との間に wait_temperature がないまま、"
                        "続けて set_temperature が実行されています。"
                    )

                if val is not None and current_setpoint is not None:
                    diff = val - current_setpoint
                    if diff == 0:
                        r.warnings.append(
                            f"{label}: 設定値が直前の setpoint ({current_setpoint} K) から"
                            "変化していません。意味のない温度設定コマンドです。"
                        )
                    elif diff < 0 and a.ramp_rate >= 5:
                        r.warnings.append(
                            f"{label}: 冷却方向 ({current_setpoint} → {val} K) で "
                            f"rate={a.ramp_rate} K/min（5 K/min以上）のため、実際の冷却速度が"
                            "設定より遅くなる可能性があります。"
                        )
                    elif diff > 0 and a.ramp_rate >= 10:
                        r.warnings.append(
                            f"{label}: 加熱方向 ({current_setpoint} → {val} K) で "
                            f"rate={a.ramp_rate} K/min（10 K/min以上）のため、実際の加熱速度が"
                            "設定より遅くなる可能性があります。"
                        )

                # SetTemperature -> wait() [not wait_temperature] -> ... (until next set_temperature)
                if i + 1 < len(ordered) and isinstance(ordered[i + 1][0], WaitAction):
                    wait_action = ordered[i + 1][0]
                    has_wait_temp = False
                    for j in range(i + 2, len(ordered)):
                        nxt = ordered[j][0]
                        if isinstance(nxt, SetTemperatureAction):
                            break
                        if isinstance(nxt, WaitTemperatureAction):
                            has_wait_temp = True
                            break
                    if (
                        not has_wait_temp
                        and val is not None
                        and current_setpoint is not None
                        and a.ramp_rate > 0
                    ):
                        estimate_s = abs(val - current_setpoint) / a.ramp_rate * 60.0
                        if wait_action.duration_s < estimate_s:
                            r.warnings.append(
                                f"{label}: 直後の wait() の待機時間 "
                                f"({wait_action.duration_s:.0f} s) が、rate={a.ramp_rate} K/min "
                                f"での概算所要時間（約{estimate_s:.0f} s）より短く、"
                                "wait_temperature もないため、設定温度への到達前に次の動作へ"
                                "進む可能性があります。"
                            )

                if val is not None:
                    current_setpoint = val
                seen_set_temp_ever = True
                wait_temp_since_last_set = False
                since_set_has_wait_temp = False
                since_set_has_follow = follow_open
                continue

            if isinstance(a, WaitTemperatureAction):
                if a.tol_k <= 0:
                    r.errors.append(f"{label}: tol_k must be > 0 (got {a.tol_k})")
                elif a.tol_k < 0.01:
                    r.warnings.append(
                        f"{label}: tol ({a.tol_k} K) が小さすぎます — "
                        "収束に時間がかかる、または到達しない可能性があります。"
                    )

                if not seen_set_temp_ever:
                    r.warnings.append(
                        f"{label}: これより前に set_temperature が実行されていません。"
                    )
                if heater_on is False:
                    r.warnings.append(
                        f"{label}: ヒーターがOFFのまま wait_temperature を実行しています。"
                        "設定温度に到達しない可能性が高いです。"
                    )
                wait_temp_since_last_set = True
                since_set_has_wait_temp = True
                continue

            if isinstance(a, SetHeaterAction):
                if a.range_index not in (0, 1, 2, 3):
                    r.errors.append(
                        f"{label}: range_index must be one of 0/1/2/3 (got {a.range_index!r})"
                    )
                else:
                    is_on = a.range_index != 0
                    if is_on:
                        if not seen_set_temp_ever:
                            heater_turned_on_before_first_set = True
                        all_heaters_off_pending = False
                    heater_on = is_on
                continue

            if isinstance(a, AllHeatersOffAction):
                heater_on = False
                all_heaters_off_pending = True
                continue

            if isinstance(a, FollowSampleAction):
                since_set_has_follow = True
                continue

            if isinstance(a, StartFollowingAction):
                follow_open = True
                continue

            if isinstance(a, StopFollowingAction):
                if follow_open:
                    since_set_has_follow = True
                follow_open = False
                continue

            if isinstance(a, TakeXrdAction) and seen_set_temp_ever:
                if not since_set_has_wait_temp:
                    r.warnings.append(
                        f"{label}: 直前の set_temperature の後に wait_temperature がないため、"
                        "試料の温度が安定化していない可能性があります。"
                    )
                if not (since_set_has_follow or follow_open):
                    r.warnings.append(
                        f"{label}: 直前の set_temperature の後に follow_sample_position、"
                        "または start_following + stop_following のペアがないため、"
                        "試料位置がずれている可能性があります。"
                    )

    # ------------------------------------------------------------------ Radicon checks

    @staticmethod
    def _check_radicon(
        flat: list[Action], ctx: DeviceContext, r: PreCheckResult
    ) -> None:
        if not any(isinstance(a, (TakeXrdAction, TakeDarkAction)) for a in flat):
            return

        if ctx.radicon is None:
            r.errors.append(
                "Rad-icon 2022 is not connected (required for take_xrd / take_dark)"
            )

    # ------------------------------------------------------------------ Camera / Follow checks

    @staticmethod
    def _check_camera(
        flat: list[Action], ctx: DeviceContext, r: PreCheckResult
    ) -> None:
        camera_actions = [
            a for a in flat
            if isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction, FollowSampleAction))
        ]
        if not camera_actions:
            return

        # Check camera availability (open and immediately release)
        camera_indices: set[int] = set()
        for a in camera_actions:
            camera_indices.add(getattr(a, "camera_index", 0))

        try:
            import cv2
            for idx in camera_indices:
                cap = cv2.VideoCapture(idx)
                opened = cap.isOpened()
                cap.release()
                if not opened:
                    r.errors.append(f"Camera index {idx} could not be opened")
        except ImportError:
            r.warnings.append("opencv-python not installed — camera checks skipped")

        # For following actions check calibration and reference image
        follow_actions = [
            a for a in flat
            if isinstance(a, (StartFollowingAction, FollowSampleAction))
        ]
        if follow_actions:
            _check_calibration(r)

            for a in follow_actions:
                ref_path_str = getattr(a, "reference_path", None)
                if ref_path_str is not None:
                    ref = Path(ref_path_str)
                else:
                    ref = _DEFAULT_REF_PATH
                if not ref.exists():
                    r.errors.append(
                        f"Reference image not found: {ref} "
                        f"(run save_reference_image() first or specify reference_path)"
                    )

    # ------------------------------------------------------------------ Structural checks

    @staticmethod
    def _check_follow_pairing(actions: list, r: PreCheckResult) -> None:
        """Scan the action tree (including ForLoopAction bodies) for start/stop follow pairing."""
        errors: list[str] = []
        depth = 0

        def _scan(acts: list) -> None:
            nonlocal depth
            for a in acts:
                if isinstance(a, ForLoopAction):
                    _scan(a.body)
                elif isinstance(a, StartFollowingAction):
                    if depth > 0:
                        errors.append(
                            "start_following called while a follow session is already active "
                            "(nested start_following is not allowed)"
                        )
                    depth += 1
                elif isinstance(a, FollowSampleAction):
                    if depth > 0:
                        errors.append(
                            "follow_sample_position called while a follow session is already active"
                        )
                    # depth は変更しない — start と stop が内部で完結するため
                elif isinstance(a, StopFollowingAction):
                    if depth == 0:
                        errors.append(
                            "stop_following appears before any start_following in the sequence"
                        )
                    else:
                        depth -= 1

        _scan(actions)
        r.errors.extend(errors)

        if depth > 0:
            r.warnings.append(
                "start_following has no matching stop_following — "
                "following will continue until the sequence ends"
            )

    @staticmethod
    def _check_unused_loop_vars(actions: list, r: PreCheckResult) -> None:
        """Warn when a ForLoopAction variable is never referenced in its body."""

        def _scan(acts: list) -> None:
            for a in acts:
                if not isinstance(a, ForLoopAction):
                    continue
                if not _loop_body_uses_var(a.body, a.var):
                    r.warnings.append(
                        f"for ループ変数 {a.var!r} がループ本体内で一度も使用されていません。"
                        "各反復で同じ処理が繰り返されます。"
                    )
                _scan(a.body)

        _scan(actions)

    @staticmethod
    def _check_undefined_loop_vars(actions: list, r: PreCheckResult) -> None:
        """Error when an action references a loop variable that is not
        defined at that point in the sequence — e.g. a stale reference left
        after a loop was deleted or renamed by hand, or a Copy/Paste that
        moved an action out of its original loop's scope.

        `_check_stage_move_constraints._apply` silently skips a stage move
        whose loop-variable value can't be resolved, with the comment
        "unresolved loop variable; already flagged elsewhere" — this check
        is what makes that actually true.
        """

        def _walk(acts: list, defined: frozenset[str]) -> None:
            for a in acts:
                if isinstance(a, ForLoopAction):
                    _walk(a.body, defined | {a.var})
                    continue
                for name in _action_loop_var_names(a):
                    if name not in defined:
                        r.errors.append(
                            f"{a.describe()}: ループ変数 {name!r} はこの位置では未定義です"
                        )

        _walk(actions, frozenset())

    @staticmethod
    def _check_empty_loop_body(actions: list, r: PreCheckResult) -> None:
        """Error when a ForLoopAction has no body — e.g. a loop created via
        "+ Add Loop" in the Visual editor that never got any steps added."""

        def _scan(acts: list) -> None:
            for a in acts:
                if isinstance(a, ForLoopAction):
                    if not a.body:
                        r.errors.append(f"{a.describe()}: ループ本体が空です")
                    _scan(a.body)

        _scan(actions)

    # ------------------------------------------------------------------ stage mode ordering

    @staticmethod
    def _detect_stage_mode(ctx: DeviceContext, result: PreCheckResult) -> str:
        """Read Ch8/Ch9 positions and return 'microscope' | 'xrd' | 'unknown'."""
        if ctx.controller is None:
            return "unknown"
        try:
            pos8_raw = ctx.controller.get_ch_pos(8)
            pos9_raw = ctx.controller.get_ch_pos(9)
        except Exception:
            return "unknown"

        if pos8_raw is None or pos9_raw is None:
            result.errors.append(
                "ステージ (Ch8/Ch9) の位置を取得できませんでした — "
                "ハードウェアとの通信に問題がある可能性があります"
            )
            return "unknown"

        try:
            pos8 = int(pos8_raw)
            pos9 = int(pos9_raw)
        except (ValueError, TypeError):
            return "unknown"

        settings = _load_stage_settings_dict()
        if settings is None:
            return "unknown"
        try:
            ch8_in  = int(settings["ch8_in"])
            ch8_out = int(settings["ch8_out"])
            det_in  = int(settings["det_in"])
            det_out = int(settings["det_out"])
        except (KeyError, ValueError):
            return "unknown"

        T = 2000  # position tolerance in pulses
        near_ch8_in  = abs(pos8 - ch8_in)  < T
        near_ch8_out = abs(pos8 - ch8_out) < T
        near_det_in  = abs(pos9 - det_in)  < T
        near_det_out = abs(pos9 - det_out) < T

        if near_ch8_in and near_det_out:
            return "microscope"
        if near_ch8_out and near_det_in:
            return "xrd"
        return "unknown"

    @staticmethod
    def _check_stage_mode_ordering(
        actions: list, initial_mode: str, r: PreCheckResult
    ) -> None:
        """State-machine scan to detect camera / XRD ordering violations.

        Tracks two flags through the sequence:
        - stage_mode: 'microscope' | 'xrd' | 'unknown'
        - follow_active: True between start_following and stop_following

        Errors:
          - camera op while stage_mode == 'xrd'
          - XRD op while stage_mode == 'microscope'
          - microscope_out_and_fpd_in while follow_active

        Warnings:
          - XRD op while stage_mode == 'unknown' (FPD position unverified)
          - ForLoopAction body changes stage_mode (non-idempotent loop)
        """
        errors: list[str] = []
        warnings: list[str] = []
        # Use a mutable dict so the nested _scan closure can modify state
        state: dict = {"stage_mode": initial_mode, "follow_active": False}

        def _scan(acts: list) -> None:
            for a in acts:
                if isinstance(a, ForLoopAction):
                    mode_before = state["stage_mode"]
                    _scan(a.body)
                    if state["stage_mode"] != mode_before:
                        warnings.append(
                            f"for ループのボディ内で stage_mode が {mode_before!r} から "
                            f"{state['stage_mode']!r} に変化します。"
                            "次の反復の開始状態が変わるため、意図した動作か確認してください。"
                        )
                    continue

                if isinstance(a, MicroscopeOutFpdInAction):
                    if state["follow_active"]:
                        errors.append(
                            "microscope_out_and_fpd_in: バックグラウンド追従スレッド "
                            "(start_following) が停止していません。"
                            "microscope_out_and_fpd_in の前に stop_following() を呼んでください。"
                        )
                    state["stage_mode"] = "xrd"

                elif isinstance(a, FpdOutMicroscopeInAction):
                    state["stage_mode"] = "microscope"

                elif isinstance(a, StartFollowingAction):
                    if state["stage_mode"] == "xrd":
                        errors.append(
                            f"{a.describe()}: microscope_out_and_fpd_in の後はカメラ操作を"
                            "実行できません（顕微鏡がサンプル軸上にない）。"
                        )
                    state["follow_active"] = True

                elif isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, FollowSampleAction)):
                    if state["stage_mode"] == "xrd":
                        errors.append(
                            f"{a.describe()}: microscope_out_and_fpd_in の後はカメラ操作を"
                            "実行できません（顕微鏡がサンプル軸上にない）。"
                        )
                    # FollowSampleAction: follow_active unchanged (internally paired)

                elif isinstance(a, StopFollowingAction):
                    state["follow_active"] = False

                elif isinstance(a, (TakeXrdAction, TakeDarkAction)):
                    if state["stage_mode"] == "microscope":
                        errors.append(
                            f"{a.describe()}: FPD がサンプル軸上にないため XRD 測定は"
                            "実行できません。先に microscope_out_and_fpd_in() を呼んでください。"
                        )
                    elif state["stage_mode"] == "unknown":
                        warnings.append(
                            f"{a.describe()}: 事前に microscope_out_and_fpd_in() が"
                            "呼ばれていません。FPD がすでに軸上にある場合は問題ありませんが、"
                            "確認してください。"
                        )

        _scan(actions)
        r.errors.extend(errors)
        r.warnings.extend(warnings)

    # ------------------------------------------------------------------ autofocus checks

    @staticmethod
    def _check_autofocus(
        flat: list[Action],
        global_limits: GlobalLimits | None,
        r: PreCheckResult,
    ) -> None:
        af_actions = [
            a for a in flat
            if isinstance(a, (StartFollowingAction, FollowSampleAction))
        ]
        if not af_actions:
            return

        for a in af_actions:
            range_um = getattr(a, "autofocus_range_um", None)
            steps = getattr(a, "autofocus_steps", None)
            if range_um is not None and range_um <= 0:
                r.errors.append(
                    f"{a.describe()}: autofocus_range_um must be > 0 when autofocus is enabled"
                )
            if steps is not None and steps < 2:
                r.errors.append(
                    f"{a.describe()}: autofocus_steps must be >= 2 when autofocus is enabled"
                )

        # Warn if Ch3 global limits are absent (autofocus could move Ch3 unboundedly)
        if global_limits is None or (
            global_limits.ch3_minus_mm is None or global_limits.ch3_plus_mm is None
        ):
            r.warnings.append(
                "Autofocus (Ch3) is enabled but Ch3 global limits are not set — "
                "Ch3 may move without bound during autofocus"
            )


    # ------------------------------------------------------------------ XRD settings checks

    @staticmethod
    def _check_xrd_settings(
        flat: list[Action],
        global_xrd: GlobalXrdSettings | None,
        r: PreCheckResult,
    ) -> None:
        xrd_actions = [a for a in flat if isinstance(a, TakeXrdAction)]
        if not xrd_actions:
            return

        g = global_xrd  # may be None; runner will use GlobalXrdSettings() defaults

        # ── Global settings checks ────────────────────────────────────────
        if g is not None:
            if g.dark_enabled and g.dark_file:
                if not Path(g.dark_file).exists():
                    r.warnings.append(
                        f"Global XRD dark file not found: {g.dark_file}"
                    )
            if g.defect_enabled and g.defect_file:
                if not Path(g.defect_file).exists():
                    r.warnings.append(
                        f"Global XRD defect file not found: {g.defect_file}"
                    )

        # ── Per-step override checks ──────────────────────────────────────
        for a in xrd_actions:
            label = a.describe()
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
                    _validate_ch11_oscillation_settings(
                        pos_a_deg, pos_b_deg, dwell_ms, speed
                    )
                except ValueError as exc:
                    r.errors.append(f"{label}: {exc}")
            # dark file override
            if a.dark_enabled is True and a.dark_file is not None:
                if not Path(a.dark_file).exists():
                    r.warnings.append(
                        f"{label}: dark file not found: {a.dark_file}"
                    )
            # defect file override
            if a.defect_enabled is True and a.defect_file is not None:
                if not Path(a.defect_file).exists():
                    r.warnings.append(
                        f"{label}: defect file not found: {a.defect_file}"
                    )
            # save_dir override: must be an existing directory
            if a.save_dir is not None:
                p = Path(a.save_dir)
                if not p.exists():
                    r.warnings.append(
                        f"{label}: save_dir does not exist and will be created: {a.save_dir}"
                    )
                elif not p.is_dir():
                    r.errors.append(
                        f"{label}: save_dir is not a directory: {a.save_dir}"
                    )


# ------------------------------------------------------------------ loop-variable helpers

def _loop_body_uses_var(actions: list, var: str) -> bool:
    """Return True when `var` is referenced anywhere in a loop body.

    Direct loop-variable references are stored in specific action fields as a
    plain string (for example, SetPressureAction.pressure == "p").  f-string
    references are stored as strings containing "{p}" by SequenceBuilder.
    """
    for action in actions:
        if isinstance(action, ForLoopAction):
            # A nested loop with the same variable name shadows this loop var.
            if action.var == var:
                continue
            if _loop_body_uses_var(action.body, var):
                return True
            continue
        if _action_uses_loop_var(action, var):
            return True
    return False


_PLACEHOLDER_VAR_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


def _action_uses_loop_var(action: Action, var: str) -> bool:
    return var in _action_loop_var_names(action)


def _action_loop_var_names(action: Action) -> set[str]:
    """Every loop-variable name `action` references: either via its direct
    loop-var field (see actions.LOOP_VAR_FIELDS / action_loop_var_ref) or an
    f-string placeholder such as "{p}" embedded in another string field
    (e.g. a LogAction message written by the DSL parser)."""
    names: set[str] = set()
    ref = action_loop_var_ref(action)
    if ref is not None:
        names.add(ref)
    for value in vars(action).values():
        if isinstance(value, str):
            names.update(_PLACEHOLDER_VAR_RE.findall(value))
    return names


# ------------------------------------------------------------------ stage move-constraint helpers

def _violates_move_constraints(positions: dict[int, int]) -> list[str]:
    """Evaluate MOVE_CONSTRAINTS against a full position snapshot.

    Unlike PM16CController.check_move_constraints() (which validates a single
    proposed move against live hardware), this checks whether the *given*
    snapshot is self-consistent — i.e. any channel already at/beyond its
    target_op boundary has its required companion channel(s) in range.
    """
    violations: list[str] = []
    for rule in MOVE_CONSTRAINTS:
        target_pos = positions.get(rule['target_ch'])
        if target_pos is None:
            continue
        target_op = rule.get('target_op')
        if target_op is not None and not _OPS[target_op](target_pos, rule['target_val']):
            continue
        for req in rule['required']:
            req_pos = positions.get(req['ch'])
            if req_pos is None or _OPS[req['op']](req_pos, req['val']):
                continue
            violations.append(
                f"Ch{rule['target_ch']}={target_pos:+} requires "
                f"Ch{req['ch']} {req['op']} {req['val']:+}, but Ch{req['ch']}={req_pos:+}"
            )
    return violations


def _violates_move_constraints_for_move(
    positions: dict[int, int], ch: int, target_pos: int
) -> list[str]:
    """Evaluate MOVE_CONSTRAINTS exactly as PM16CController does before a move."""
    violations: list[str] = []
    for rule in MOVE_CONSTRAINTS:
        if rule['target_ch'] != ch:
            continue
        target_op = rule.get('target_op')
        if target_op is not None and not _OPS[target_op](target_pos, rule['target_val']):
            continue
        for req in rule['required']:
            req_pos = positions.get(req['ch'])
            if req_pos is None or _OPS[req['op']](req_pos, req['val']):
                continue
            violations.append(
                f"Move blocked: Ch{ch} → {target_pos:+} requires "
                f"Ch{req['ch']} {req['op']} {req['val']:+}, "
                f"but current position is {req_pos:+}"
            )
    return violations


# ------------------------------------------------------------------ PACE5000 source-pressure helpers

def _find_max_set_pressure_mpa(actions: list, var_context: dict) -> float | None:
    """Recursively walk the action tree and return the maximum SetPressureAction
    target in MPa, accounting for ForLoopAction variable substitution.
    Returns None if the sequence contains no SetPressureAction."""
    max_mpa: float | None = None
    for a in actions:
        if isinstance(a, ForLoopAction):
            for val in a.values:
                ctx = {**var_context, a.var: val}
                child = _find_max_set_pressure_mpa(a.body, ctx)
                if child is not None:
                    max_mpa = child if max_mpa is None else max(max_mpa, child)
        elif isinstance(a, SetPressureAction):
            pressure = a.pressure
            if isinstance(pressure, str):
                pressure = var_context.get(pressure)
                if pressure is None:
                    continue
            p_mpa = float(pressure) * _PACE_TO_MPA.get(a.unit, 1.0)
            max_mpa = p_mpa if max_mpa is None else max(max_mpa, p_mpa)
    return max_mpa


def _check_pace5000_source_pressure(
    actions: list, ctx: DeviceContext, r: PreCheckResult
) -> None:
    """Error if the maximum set pressure in the sequence exceeds the current +ve source pressure."""
    max_mpa = _find_max_set_pressure_mpa(actions, {})
    if max_mpa is None:
        return
    try:
        ctx.pace5000.write(":UNIT:PRES MPA")
        pos_source = ctx.pace5000.get_positive_source_pressure()
    except Exception:
        return
    if pos_source is None:
        return
    if max_mpa > pos_source:
        r.errors.append(
            f"現状のSource Pressure ({pos_source:.4g} MPa) が"
            f"シーケンス中の最大設定圧力 ({max_mpa:.4g} MPa) を下回っているため、"
            "Source Pressureを上げてから再度validateしてください。"
        )


# ------------------------------------------------------------------ LakeShore helpers

def _validate_ls_temp_value(
    label: str, value_k: float | str, var_context: dict, r: PreCheckResult
) -> float | None:
    """Resolve SetTemperatureAction.value_k (literal or loop-variable
    reference) and validate it. Returns the resolved float, or None if the
    variable is not yet resolvable (already flagged elsewhere) or invalid
    (an error has been appended)."""
    v = value_k
    if isinstance(v, str):
        v = var_context.get(v)
        if v is None:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        r.errors.append(f"{label}: value_k is not numeric (got {v!r})")
        return None
    if math.isnan(f) or math.isinf(f):
        r.errors.append(f"{label}: value_k is NaN/Inf")
        return None
    return f


# ------------------------------------------------------------------ file-check helpers

def _load_stage_settings_dict() -> dict | None:
    if not _STAGE_SETTINGS_PATH.exists():
        return None
    try:
        return json.loads(_STAGE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _check_stage_settings(
    r: PreCheckResult, required_keys: list[str], action_name: str
) -> None:
    if not _STAGE_SETTINGS_PATH.exists():
        r.errors.append(
            f"{action_name}: stage_settings.json not found at {_STAGE_SETTINGS_PATH}"
        )
        return
    try:
        settings = json.loads(_STAGE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        r.errors.append(
            f"{action_name}: failed to parse stage_settings.json — {exc}"
        )
        return
    for key in required_keys:
        if key not in settings:
            r.errors.append(
                f"{action_name}: stage_settings.json is missing key {key!r} "
                f"(required when position is not specified explicitly)"
            )


def _check_calibration(r: PreCheckResult) -> None:
    if not _CALIBRATION_PATH.exists():
        r.errors.append(
            f"calibration.json not found at {_CALIBRATION_PATH} "
            "(run the calibration procedure in the Interactive Camera app first)"
        )
        return
    try:
        data = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        r.errors.append(f"calibration.json could not be parsed — {exc}")
        return
    if "matrix_inv" not in data:
        r.errors.append(
            "calibration.json has no 'matrix_inv' key — "
            "please re-run the calibration procedure in the Interactive Camera app"
        )
