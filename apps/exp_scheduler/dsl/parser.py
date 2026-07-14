"""DSL AST → Sequence parser.

Call SequenceBuilder.build() only after ASTValidator.validate() returns no errors.

Loop-variable references inside a for body are stored as plain strings:
    for p in [0.5, 1.0]:
        set_pressure(pressure=p, ...)
→ SetPressureAction(pressure="p", ...) — the string "p" is the variable name.
Serialised as pressure_var="p" in JSON (handled by SetPressureAction.to_dict / from_dict).
"""
from __future__ import annotations

import ast
from typing import Any

from ..actions import (
    Action,
    AllHeatersOffAction,
    FpdOutMicroscopeInAction,
    FollowSampleAction,
    ForLoopAction,
    LogAction,
    MicroscopeOutFpdInAction,
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
from ..sequence import Sequence


class SequenceBuilder(ast.NodeVisitor):
    """Converts a validated DSL AST to a Sequence."""

    def build(self, tree: ast.AST) -> Sequence:
        """Build a Sequence from a pre-validated AST.

        Raises ValueError if an unexpected structure is encountered.
        Always run ASTValidator first.
        """
        stmts = tree.body if isinstance(tree, ast.Module) else [tree]
        actions = self._build_stmts(stmts, loop_vars=frozenset())
        return Sequence(actions=actions)

    # ── Statement processing ─────────────────────────────────────────

    def _build_stmts(
        self, stmts: list, loop_vars: frozenset[str]
    ) -> list[Action]:
        actions: list[Action] = []
        for stmt in stmts:
            action = self._build_stmt(stmt, loop_vars)
            if action is not None:
                actions.append(action)
        return actions

    def _build_stmt(
        self, stmt: ast.stmt, loop_vars: frozenset[str]
    ) -> Action | None:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            return self._build_call(stmt.value, loop_vars)
        if isinstance(stmt, ast.For):
            return self._build_for(stmt, loop_vars)
        # ast.Assign, ast.If, ast.Pass etc. — no Action produced
        return None

    def _build_for(
        self, node: ast.For, loop_vars: frozenset[str]
    ) -> ForLoopAction:
        var = node.target.id  # validated by ASTValidator to be ast.Name
        values = [elt.value for elt in node.iter.elts]  # validated to be numeric constants
        new_vars = loop_vars | {var}
        body = self._build_stmts(node.body, new_vars)
        return ForLoopAction(var=var, values=values, body=body)

    def _build_call(
        self, node: ast.Call, loop_vars: frozenset[str]
    ) -> Action | None:
        if not isinstance(node.func, ast.Name):
            return None
        fname = node.func.id
        kwargs: dict[str, Any] = {
            kw.arg: self._eval_arg(kw.value, loop_vars)
            for kw in node.keywords
            if kw.arg is not None
        }
        builder = self._BUILDERS.get(fname)
        if builder is None:
            return None
        return builder(self, kwargs)

    # ── Argument evaluation ──────────────────────────────────────────

    def _eval_arg(self, node: ast.expr, loop_vars: frozenset[str]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Name):
            if node.id in loop_vars:
                return node.id  # loop-variable reference: store name as string
            return node.id  # other names (True/False handled as Constant in 3.8+)

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            val = self._eval_arg(node.operand, loop_vars)
            if isinstance(val, (int, float)):
                return -val

        if isinstance(node, ast.List):
            return [self._eval_arg(elt, loop_vars) for elt in node.elts]

        if isinstance(node, ast.Tuple):
            return tuple(self._eval_arg(elt, loop_vars) for elt in node.elts)

        if isinstance(node, ast.BinOp):
            left = self._eval_arg(node.left, loop_vars)
            right = self._eval_arg(node.right, loop_vars)
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                op = node.op
                if isinstance(op, ast.Add):
                    return left + right
                if isinstance(op, ast.Sub):
                    return left - right
                if isinstance(op, ast.Mult):
                    return left * right
                if isinstance(op, (ast.Div, ast.FloorDiv)):
                    return left / right

        if isinstance(node, ast.JoinedStr):
            return self._eval_fstring(node, loop_vars)

        raise ValueError(f"Cannot evaluate DSL argument: {ast.dump(node)}")

    def _eval_fstring(
        self, node: ast.JoinedStr, loop_vars: frozenset[str]
    ) -> str:
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
            elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
                parts.append(f"{{{part.value.id}}}")
            else:
                parts.append("?")
        return "".join(parts)

    # ── Duration / interval helpers ──────────────────────────────────

    @staticmethod
    def _to_seconds(value: float, unit: str) -> float:
        return float(value) * 60 if unit == "min" else float(value)

    # ── Action builders ──────────────────────────────────────────────

    def _build_wait(self, kw: dict) -> WaitAction:
        return WaitAction(
            duration_s=self._to_seconds(kw.get("duration", 0), kw.get("unit", "s"))
        )

    def _build_log_message(self, kw: dict) -> LogAction:
        return LogAction(message=str(kw.get("message", "")))

    def _build_move_absolute(self, kw: dict) -> StageAction:
        return StageAction(
            operation="move_absolute",
            ch=int(kw["ch"]),
            value=kw["position"],
        )

    def _build_move_relative(self, kw: dict) -> StageAction:
        return StageAction(
            operation="move_relative",
            ch=int(kw["ch"]),
            value=kw["delta"],
        )

    def _build_set_speed(self, kw: dict) -> StageAction:
        return StageAction(
            operation="set_speed",
            ch=int(kw["ch"]),
            speed=str(kw["speed"]),
        )

    def _build_normal_stop(self, kw: dict) -> StageAction:
        return StageAction(operation="normal_stop")

    def _build_emergency_stop(self, kw: dict) -> StageAction:
        return StageAction(operation="emergency_stop")

    def _build_microscope_out_and_fpd_in(self, kw: dict) -> MicroscopeOutFpdInAction:
        return MicroscopeOutFpdInAction(
            microscope_out_pos=kw.get("microscope_out_pos"),
            fpd_in_pos=kw.get("fpd_in_pos"),
            speed=str(kw.get("speed", "H")),
        )

    def _build_fpd_out_and_microscope_in(self, kw: dict) -> FpdOutMicroscopeInAction:
        return FpdOutMicroscopeInAction(
            fpd_out_pos=kw.get("fpd_out_pos"),
            microscope_in_pos=kw.get("microscope_in_pos"),
            speed=str(kw.get("speed", "H")),
        )

    def _build_set_pressure(self, kw: dict) -> SetPressureAction:
        return SetPressureAction(
            pressure=kw["pressure"],
            unit=str(kw["unit"]),
            rate=kw.get("rate"),
            rate_unit=kw.get("rate_unit"),
        )

    def _build_wait_pressure(self, kw: dict) -> WaitPressureAction:
        return WaitPressureAction(
            tol=float(kw["tol"]),
            unit=str(kw["unit"]),
        )

    def _build_set_control_mode(self, kw: dict) -> SetControlModeAction:
        return SetControlModeAction(enabled=bool(kw["enabled"]))

    def _build_set_temperature(self, kw: dict) -> SetTemperatureAction:
        # DSL uses keyword "value"; Action stores as value_k
        return SetTemperatureAction(
            value_k=kw["value"],
            ramp_rate=kw.get("ramp_rate"),
        )

    def _build_wait_temperature(self, kw: dict) -> WaitTemperatureAction:
        return WaitTemperatureAction(tol_k=float(kw["tol"]))

    def _build_set_heater(self, kw: dict) -> SetHeaterAction:
        return SetHeaterAction(range_index=int(kw["range_index"]))

    def _build_all_heaters_off(self, kw: dict) -> AllHeatersOffAction:
        return AllHeatersOffAction()

    def _build_take_xrd(self, kw: dict) -> TakeXrdAction:
        raw_exp = kw.get("exposure_ms")
        return TakeXrdAction(
            exposure_ms=int(raw_exp) if raw_exp is not None else None,
            save=bool(kw.get("save", True)),
            prefix=str(kw.get("prefix", "scan")),
        )

    def _build_take_dark(self, kw: dict) -> TakeDarkAction:
        return TakeDarkAction(exposure_ms=int(kw["exposure_ms"]))

    def _build_save_reference_image(self, kw: dict) -> SaveReferenceImageAction:
        return SaveReferenceImageAction(
            path=kw.get("path"),
            camera_index=int(kw.get("camera_index", 0)),
        )

    def _build_start_following(self, kw: dict) -> StartFollowingAction:
        interval = kw.get("interval")
        interval_s = (
            self._to_seconds(interval, kw.get("interval_unit", "s"))
            if interval is not None
            else None
        )
        return StartFollowingAction(
            reference_path=kw.get("reference_path"),
            interval_s=interval_s,
            similarity_threshold=kw.get("similarity_threshold"),
            max_correction_per_step_um=kw.get("max_correction_per_step_um"),
            camera_index=int(kw.get("camera_index", 0)),
        )

    def _build_stop_following(self, kw: dict) -> StopFollowingAction:
        return StopFollowingAction()

    def _build_follow_sample_position(self, kw: dict) -> FollowSampleAction:
        duration_s = self._to_seconds(kw.get("duration", 0), kw.get("unit", "s"))
        interval = kw.get("interval")
        interval_s = (
            self._to_seconds(interval, kw.get("interval_unit", "s"))
            if interval is not None
            else None
        )
        return FollowSampleAction(
            duration_s=duration_s,
            reference_path=kw.get("reference_path"),
            interval_s=interval_s,
            similarity_threshold=kw.get("similarity_threshold"),
            max_correction_per_step_um=kw.get("max_correction_per_step_um"),
            camera_index=int(kw.get("camera_index", 0)),
        )

    # ── Dispatch table (must come after all _build_* definitions) ────

    _BUILDERS: dict[str, Any] = {
        "wait":                       _build_wait,
        "log_message":                _build_log_message,
        "move_absolute":              _build_move_absolute,
        "move_relative":              _build_move_relative,
        "set_speed":                  _build_set_speed,
        "emergency_stop":             _build_emergency_stop,
        "microscope_out_and_fpd_in":  _build_microscope_out_and_fpd_in,
        "fpd_out_and_microscope_in":  _build_fpd_out_and_microscope_in,
        "set_pressure":               _build_set_pressure,
        "wait_pressure":              _build_wait_pressure,
        "set_control_mode":           _build_set_control_mode,
        "set_temperature":            _build_set_temperature,
        "wait_temperature":           _build_wait_temperature,
        "set_heater":                 _build_set_heater,
        "all_heaters_off":            _build_all_heaters_off,
        "take_xrd":                   _build_take_xrd,
        "take_dark":                  _build_take_dark,
        "save_reference_image":       _build_save_reference_image,
        "start_following":            _build_start_following,
        "stop_following":             _build_stop_following,
        "follow_sample_position":     _build_follow_sample_position,
    }
