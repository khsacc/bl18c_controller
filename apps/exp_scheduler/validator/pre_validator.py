"""
Pre-execution validator for ExperimentalScheduler sequences.

Runs static analysis on a Sequence before SequenceRunner starts.
All checks run to completion (errors are accumulated, not short-circuited)
so the user sees every problem in one dialog.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    ReadIntensityAction,
    SaveReferenceImageAction,
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
)
from ..device_context import DeviceContext
from ..runner import GlobalLimits, GlobalXrdSettings
from ..sequence import Sequence

if TYPE_CHECKING:
    from utils.stage.control_stage_sim import PM16CControllerSim


_CALIBRATION_PATH = (
    Path(__file__).parent.parent.parent / "interactive_camera" / "calibration.json"
)
_STAGE_SETTINGS_PATH = (
    Path(__file__).parent.parent.parent / "ui_stage_controller"
    / "__localdata" / "stage_settings.json"
)
_DEFAULT_REF_PATH = Path(__file__).parent.parent / "__localdata" / "reference_frame.npz"

# Unit conversion to MPa (GPa not supported by PACE5000)
_PACE_TO_MPA: dict[str, float] = {"MPa": 1.0, "Bar": 0.1}


@dataclass
class PreCheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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

        _SEP = "─" * 60
        print(f"\n[PreValidator] {_SEP}")
        print(f"[PreValidator] Sequence : {sequence.name!r}")
        print(f"[PreValidator] Actions  : {len(sequence.actions)} top-level / {len(flat)} flat")
        n_counts = {
            "stage":     sum(1 for a in flat if isinstance(a, (StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction))),
            "pace5000":  sum(1 for a in flat if isinstance(a, (SetPressureAction, WaitPressureAction, SetControlModeAction))),
            "lakeshore": sum(1 for a in flat if isinstance(a, (SetTemperatureAction, WaitTemperatureAction, SetHeaterAction, AllHeatersOffAction))),
            "keithley":  sum(1 for a in flat if isinstance(a, ReadIntensityAction)),
            "xrd/dark":  sum(1 for a in flat if isinstance(a, (TakeXrdAction, TakeDarkAction))),
            "camera":    sum(1 for a in flat if isinstance(a, (SaveReferenceImageAction, StartFollowingAction, FollowSampleAction))),
        }
        print(f"[PreValidator] Counts   : " + "  ".join(f"{k}={v}" for k, v in n_counts.items()))
        print(f"[PreValidator] Inputs   : global_limits={'set' if global_limits is not None else 'None'}  global_xrd={'set' if global_xrd is not None else 'None'}")
        print(f"[PreValidator] {_SEP}")

        def _run(label: str, fn, *args) -> None:
            e0 = len(result.errors)
            w0 = len(result.warnings)
            fn(*args)
            new_e = result.errors[e0:]
            new_w = result.warnings[w0:]
            if not new_e and not new_w:
                print(f"[PreValidator]   {label:<38}  OK")
            else:
                status = "ERROR" if new_e else "WARN"
                print(f"[PreValidator]   {label:<38}  {status}")
                for msg in new_e:
                    print(f"[PreValidator]     ✗ {msg}")
                for msg in new_w:
                    print(f"[PreValidator]     ⚠ {msg}")

        # Safeguard: global limits configuration
        def _check_global_limits() -> None:
            if global_limits is not None and not global_limits.is_fully_configured():
                result.errors.append(
                    "Global limits are not fully configured — "
                    "all six Ch3/4/5 ±mm values must be set before running"
                )
        _run("global_limits", _check_global_limits)

        _run("_check_stage",          self._check_stage,          flat, ctx, result)
        _run("_check_stage_compound", self._check_stage_compound, flat, ctx, result)
        _run("_check_pace5000",       self._check_pace5000,       flat, ctx, result, sequence.actions)
        _run("_check_lakeshore",      self._check_lakeshore,      flat, ctx, result)
        _run("_check_keithley",       self._check_keithley,       flat, ctx, result)
        _run("_check_radicon",        self._check_radicon,        flat, ctx, result)
        _run("_check_camera",         self._check_camera,         flat, ctx, result)
        _run("_check_follow_pairing", self._check_follow_pairing, sequence.actions, result)

        e0 = len(result.errors)
        initial_mode = self._detect_stage_mode(ctx, result)
        new_e = result.errors[e0:]
        print(f"[PreValidator]   {'_detect_stage_mode':<38}  {initial_mode!r}" + (f"  ERROR" if new_e else ""))
        for msg in new_e:
            print(f"[PreValidator]     ✗ {msg}")

        _run("_check_stage_mode_ordering", self._check_stage_mode_ordering, sequence.actions, initial_mode, result)
        _run("_check_autofocus",           self._check_autofocus,           flat, global_limits, result)
        _run("_check_xrd_settings",        self._check_xrd_settings,        flat, global_xrd, result)

        verdict = "PASSED" if result.ok else "FAILED"
        print(f"[PreValidator] {_SEP}")
        print(f"[PreValidator] {verdict}  —  {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
        print(f"[PreValidator] {_SEP}\n")

        return result

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

    # ------------------------------------------------------------------ Keithley checks

    @staticmethod
    def _check_keithley(
        flat: list[Action], ctx: DeviceContext, r: PreCheckResult
    ) -> None:
        if not any(isinstance(a, ReadIntensityAction) for a in flat):
            return

        if ctx.keithley is None:
            r.errors.append(
                "Keithley 2000 is not connected (required for read_intensity)"
            )
            return

        if ctx.keithley.is_talk_only:
            r.warnings.append(
                "Keithley 2000 is in Talk-Only mode"
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
            if isinstance(a, (SaveReferenceImageAction, StartFollowingAction, FollowSampleAction))
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

                elif isinstance(a, (SaveReferenceImageAction, FollowSampleAction)):
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
    """Warn if the maximum set pressure in the sequence exceeds the current +ve source pressure."""
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
        r.warnings.append(
            f"シーケンス中の最大設定圧力 {max_mpa:.4g} MPa が、"
            f"現在の +ve source 圧力 {pos_source:.4g} MPa を超えています。\n"
            "シーケンスを開始する前にソース圧力を上げてください。"
        )


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
