"""DSL AST → Sequence parser.

Call SequenceBuilder.build() only after ASTValidator.validate() returns no errors.
(SequenceBuilder still defends itself independently against malformed input —
see module docstring below — but ASTValidator is the layer that reports
whitelist/unit/bounds violations with the richest messages.)

Loop-variable references inside a for body are stored as plain strings:
    for p in [0.5, 1.0]:
        set_pressure(pressure=p, ...)
→ SetPressureAction(pressure="p", ...) — the string "p" is the variable name.
Serialised as pressure_var="p" in JSON (handled by SetPressureAction.to_dict / from_dict).

REORGANISATION_PLAN.md Phase 2 (strict call binding / fail-closed parser):
`build()` no longer silently drops unsupported statements, unknown keyword
arguments, or unbound bare-name references — every one of those now becomes
a `Diagnostic` (see `SequenceBuildError`) instead of a missing Action or a
raw exception. Per-call keyword arguments are bound against the real
`dsl/api.py` function signature (`inspect.Signature.bind()` +
`apply_defaults()`) rather than read ad hoc with `dict.get(fallback)`, so an
argument's default value can no longer drift between `dsl/api.py` (what the
LLM prompt documents) and this module (what compiling actually does) — see
`_API_SIGNATURES`. `ASTValidator` is unchanged in this Phase (still the
"AST safety" layer); this module absorbed the additional call-binding
responsibility described in REORGANISATION_PLAN.md §6.2/§7 Phase 2 item 2 as
a "provisional call binder" ahead of the full `CommandSpec` registry
(Phase 3).
"""
from __future__ import annotations

import ast
import inspect
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
    SaveSnapshotAction,
    SetAndWaitPressureAction,
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
from ..validator.models import Diagnostic, Severity, ValidationPhase
from .api import DSL_NAMESPACE


class SequenceBuildError(Exception):
    """Raised by SequenceBuilder.build() when the AST contains one or more
    fail-closed violations (unknown function, unknown keyword argument,
    unbound bare name, unsupported statement, ...).

    Carries every Diagnostic found across the *whole* tree, not just the
    first — SequenceBuilder keeps walking after a problem so a single
    compile attempt surfaces as many independent issues as possible (see
    REORGANISATION_PLAN.md §7 Phase 2 item 11).
    """

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        super().__init__(f"{len(diagnostics)} DSL build error(s)")
        self.diagnostics = diagnostics


def _line_prefix(node: ast.AST) -> str:
    lineno = getattr(node, "lineno", None)
    return f"Line {lineno}: " if lineno is not None else ""


class SequenceBuilder(ast.NodeVisitor):
    """Converts a validated DSL AST to a Sequence."""

    def __init__(self) -> None:
        self._diagnostics: list[Diagnostic] = []

    def build(self, tree: ast.AST) -> Sequence:
        """Build a Sequence from a pre-validated AST.

        Raises SequenceBuildError (carrying every Diagnostic found) if the
        tree contains anything this builder can't losslessly turn into
        Actions. Always run ASTValidator first — it rejects most malformed
        input earlier and with richer whitelist/unit/bounds diagnostics;
        this method's own checks are a fail-closed backstop for whatever
        ASTValidator doesn't cover (call binding, statement shape, unbound
        names), and fire whether or not ASTValidator ran first.
        """
        self._diagnostics = []
        stmts = tree.body if isinstance(tree, ast.Module) else [tree]
        actions = self._build_stmts(stmts, loop_vars=frozenset())
        if self._diagnostics:
            raise SequenceBuildError(self._diagnostics)
        return Sequence(actions=actions)

    # ── Diagnostic helper ────────────────────────────────────────────

    def _diag(self, node: ast.AST, code: str, message: str) -> None:
        self._diagnostics.append(Diagnostic(
            severity=Severity.ERROR,
            code=code,
            message=f"{_line_prefix(node)}{message}",
            phase=ValidationPhase.COMPILE,
            source_line=getattr(node, "lineno", None),
        ))

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
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Call):
                return self._build_call(stmt.value, loop_vars)
            self._diag(
                stmt, "dsl.unsupported_statement",
                "only function-call statements and for loops are supported",
            )
            return None
        if isinstance(stmt, ast.For):
            return self._build_for(stmt, loop_vars)
        kind = type(stmt).__name__
        self._diag(
            stmt, "dsl.unsupported_statement",
            f"{kind} statements are not supported",
        )
        return None

    def _build_for(
        self, node: ast.For, loop_vars: frozenset[str]
    ) -> ForLoopAction | None:
        if not isinstance(node.target, ast.Name):
            self._diag(
                node, "dsl.unsupported_statement",
                "for loop target must be a simple variable name",
            )
            return None
        if node.orelse:
            self._diag(node, "dsl.unsupported_statement", "for/else is not allowed")
            return None
        if not isinstance(node.iter, ast.List) or not all(
            isinstance(elt, ast.Constant) and isinstance(elt.value, (int, float))
            and not isinstance(elt.value, bool)
            for elt in node.iter.elts
        ):
            self._diag(
                node, "dsl.unsupported_statement",
                "for loop must iterate over a literal numeric list, e.g. [0.5, 1.0, 1.5]",
            )
            return None

        var = node.target.id
        values = [elt.value for elt in node.iter.elts]
        new_vars = loop_vars | {var}
        body = self._build_stmts(node.body, new_vars)
        return ForLoopAction(var=var, values=values, body=body)

    def _build_call(
        self, node: ast.Call, loop_vars: frozenset[str]
    ) -> Action | None:
        if not isinstance(node.func, ast.Name):
            self._diag(
                node, "dsl.unsupported_statement",
                "only direct function calls are supported (no method calls or "
                "dynamic calls)",
            )
            return None
        fname = node.func.id
        sig = _API_SIGNATURES.get(fname)
        builder = self._BUILDERS.get(fname)
        if sig is None or builder is None:
            self._diag(node, "dsl.unknown_function", f"Unknown function: {fname!r}")
            return None
        if node.args:
            self._diag(
                node, "dsl.positional_argument_not_supported",
                f"{fname}(): positional arguments are not supported — "
                "use keyword arguments",
            )
            return None

        evaluated: dict[str, Any] = {}
        had_error = False
        for kw in node.keywords:
            if kw.arg is None:
                self._diag(
                    node, "dsl.unsupported_statement",
                    f"{fname}(): **kwargs expansion is not supported",
                )
                had_error = True
                continue
            value, error = self._eval_arg(kw.value, loop_vars)
            if error is not None:
                self._diag(kw.value, "dsl.unbound_name", f"{fname}(): {kw.arg}: {error}")
                had_error = True
                continue
            evaluated[kw.arg] = value

        if had_error:
            return None

        try:
            bound = sig.bind(**evaluated)
        except TypeError as exc:
            self._diag(node, _classify_bind_error(exc), f"{fname}(): {exc}")
            return None

        bound.apply_defaults()
        return builder(self, dict(bound.arguments))

    # ── Argument evaluation ──────────────────────────────────────────

    def _eval_arg(
        self, node: ast.expr, loop_vars: frozenset[str]
    ) -> tuple[Any, str | None]:
        """Evaluate one DSL argument expression.

        Returns ``(value, None)`` on success or ``(None, error_message)`` on
        failure — callers attach the source line via `self._diag()`, since a
        keyword's *value* node (not the enclosing Call) is the more useful
        place to point at for an unbound-name or uneval-able-expression
        error.
        """
        if isinstance(node, ast.Constant):
            return node.value, None

        if isinstance(node, ast.Name):
            if node.id in loop_vars:
                return node.id, None
            return None, (
                f"{node.id!r} is not defined here — it is not a for-loop "
                "variable bound by an enclosing `for` (check for a typo, or "
                "move this call inside the intended loop)"
            )

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            val, err = self._eval_arg(node.operand, loop_vars)
            if err is not None:
                return None, err
            if isinstance(val, (int, float)):
                return -val, None
            return None, f"cannot evaluate DSL argument: {ast.dump(node)}"

        if isinstance(node, ast.List):
            values = []
            for elt in node.elts:
                v, err = self._eval_arg(elt, loop_vars)
                if err is not None:
                    return None, err
                values.append(v)
            return values, None

        if isinstance(node, ast.Tuple):
            values = []
            for elt in node.elts:
                v, err = self._eval_arg(elt, loop_vars)
                if err is not None:
                    return None, err
                values.append(v)
            return tuple(values), None

        if isinstance(node, ast.BinOp):
            left, lerr = self._eval_arg(node.left, loop_vars)
            if lerr is not None:
                return None, lerr
            right, rerr = self._eval_arg(node.right, loop_vars)
            if rerr is not None:
                return None, rerr
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                op = node.op
                if isinstance(op, ast.Add):
                    return left + right, None
                if isinstance(op, ast.Sub):
                    return left - right, None
                if isinstance(op, ast.Mult):
                    return left * right, None
                if isinstance(op, (ast.Div, ast.FloorDiv)):
                    return left / right, None
            return None, f"cannot evaluate DSL argument: {ast.dump(node)}"

        if isinstance(node, ast.JoinedStr):
            return self._eval_fstring(node, loop_vars), None

        return None, f"cannot evaluate DSL argument: {ast.dump(node)}"

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

    # ── Action builders ────────────────────────────────────────────
    # Every method below receives `kw`: the fully bound + defaulted argument
    # dict from `sig.bind(**evaluated).apply_defaults()` — every parameter
    # declared in the corresponding dsl/api.py function signature is always
    # present, so unlike before Phase 2, `kw.get(name, fallback)` is never
    # needed for a required field, and an *optional* field's fallback is the
    # signature's own default, not a second hardcoded one here.

    def _build_wait(self, kw: dict) -> WaitAction:
        return WaitAction(
            duration_s=self._to_seconds(kw["duration"], kw["unit"])
        )

    def _build_log_message(self, kw: dict) -> LogAction:
        return LogAction(message=str(kw["message"]))

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
            microscope_out_pos=kw["microscope_out_pos"],
            fpd_in_pos=kw["fpd_in_pos"],
            speed=str(kw["speed"]),
        )

    def _build_fpd_out_and_microscope_in(self, kw: dict) -> FpdOutMicroscopeInAction:
        return FpdOutMicroscopeInAction(
            fpd_out_pos=kw["fpd_out_pos"],
            microscope_in_pos=kw["microscope_in_pos"],
            speed=str(kw["speed"]),
        )

    def _build_set_pressure(self, kw: dict) -> SetPressureAction:
        return SetPressureAction(
            pressure=kw["pressure"],
            unit=str(kw["unit"]),
            rate=kw["rate"],
            rate_unit=kw["rate_unit"],
        )

    def _build_wait_pressure(self, kw: dict) -> WaitPressureAction:
        return WaitPressureAction(
            tol=float(kw["tol"]),
            unit=str(kw["unit"]),
        )

    def _build_set_and_wait_pressure(self, kw: dict) -> SetAndWaitPressureAction:
        return SetAndWaitPressureAction(
            pressure=kw["pressure"],
            unit=str(kw["unit"]),
            rate=kw["rate"],
            rate_unit=kw["rate_unit"],
            tol=float(kw["tol"]),
        )

    def _build_set_control_mode(self, kw: dict) -> SetControlModeAction:
        return SetControlModeAction(enabled=bool(kw["enabled"]))

    def _build_set_temperature(self, kw: dict) -> SetTemperatureAction:
        # DSL uses keyword "value"; Action stores as value_k
        return SetTemperatureAction(
            value_k=kw["value"],
            ramp_rate=kw["ramp_rate"],
        )

    def _build_wait_temperature(self, kw: dict) -> WaitTemperatureAction:
        return WaitTemperatureAction(tol_k=float(kw["tol"]))

    def _build_set_heater(self, kw: dict) -> SetHeaterAction:
        return SetHeaterAction(range_index=int(kw["range_index"]))

    def _build_all_heaters_off(self, kw: dict) -> AllHeatersOffAction:
        return AllHeatersOffAction()

    def _build_take_xrd(self, kw: dict) -> TakeXrdAction:
        exposure_ms = kw["exposure_ms"]
        defect_kernel = kw["defect_kernel"]
        # Mirrors dsl/api.py::take_xrd()'s own conditional: oscillation
        # fields only reach the Action when oscillate is truthy — otherwise
        # they stay None ("inherit from GlobalXrdSettings"), regardless of
        # whatever default/explicit value osc_pos_a_deg etc. bound to.
        oscillate = bool(kw["oscillate"]) if kw["oscillate"] else None
        return TakeXrdAction(
            exposure_ms=int(exposure_ms) if exposure_ms is not None else None,
            save=bool(kw["save"]),
            prefix=str(kw["prefix"]),
            save_dir=kw["save_dir"],
            dark_file=kw["dark_file"],
            dark_enabled=kw["dark_enabled"],
            defect_file=kw["defect_file"],
            defect_enabled=kw["defect_enabled"],
            defect_kernel=int(defect_kernel) if defect_kernel is not None else None,
            flip_v=kw["flip_v"],
            flip_h=kw["flip_h"],
            oscillate=oscillate,
            osc_pos_a_deg=float(kw["osc_pos_a_deg"]) if oscillate else None,
            osc_pos_b_deg=float(kw["osc_pos_b_deg"]) if oscillate else None,
            osc_dwell_ms=int(kw["osc_dwell_ms"]) if oscillate else None,
            osc_speed=str(kw["osc_speed"]) if oscillate else None,
        )

    def _build_take_dark(self, kw: dict) -> TakeDarkAction:
        return TakeDarkAction(exposure_ms=int(kw["exposure_ms"]))

    def _build_save_reference_image(self, kw: dict) -> SaveReferenceImageAction:
        return SaveReferenceImageAction(
            path=kw["path"],
            camera_index=int(kw["camera_index"]),
        )

    def _build_save_snapshot(self, kw: dict) -> SaveSnapshotAction:
        return SaveSnapshotAction(save_dir=kw["save_dir"])

    def _build_start_following(self, kw: dict) -> StartFollowingAction:
        interval = kw["interval"]
        interval_s = (
            self._to_seconds(interval, kw["interval_unit"])
            if interval is not None
            else None
        )
        autofocus_range_um = kw["autofocus_range_um"]
        autofocus_steps = kw["autofocus_steps"]
        return StartFollowingAction(
            reference_path=kw["reference_path"],
            interval_s=interval_s,
            similarity_threshold=kw["similarity_threshold"],
            max_correction_per_step_um=kw["max_correction_per_step_um"],
            camera_index=int(kw["camera_index"]),
            autofocus_range_um=(
                float(autofocus_range_um) if autofocus_range_um is not None else None
            ),
            autofocus_steps=(
                int(autofocus_steps) if autofocus_steps is not None else None
            ),
        )

    def _build_stop_following(self, kw: dict) -> StopFollowingAction:
        return StopFollowingAction()

    def _build_follow_sample_position(self, kw: dict) -> FollowSampleAction:
        duration_s = self._to_seconds(kw["duration"], kw["unit"])
        interval = kw["interval"]
        interval_s = (
            self._to_seconds(interval, kw["interval_unit"])
            if interval is not None
            else None
        )
        autofocus_range_um = kw["autofocus_range_um"]
        autofocus_steps = kw["autofocus_steps"]
        return FollowSampleAction(
            duration_s=duration_s,
            reference_path=kw["reference_path"],
            interval_s=interval_s,
            similarity_threshold=kw["similarity_threshold"],
            max_correction_per_step_um=kw["max_correction_per_step_um"],
            camera_index=int(kw["camera_index"]),
            autofocus_range_um=(
                float(autofocus_range_um) if autofocus_range_um is not None else None
            ),
            autofocus_steps=(
                int(autofocus_steps) if autofocus_steps is not None else None
            ),
        )

    # ── Dispatch table (must come after all _build_* definitions) ────

    _BUILDERS: dict[str, Any] = {
        "wait":                       _build_wait,
        "log_message":                _build_log_message,
        "move_absolute":              _build_move_absolute,
        "move_relative":              _build_move_relative,
        "set_speed":                  _build_set_speed,
        "normal_stop":                _build_normal_stop,
        "emergency_stop":             _build_emergency_stop,
        "microscope_out_and_fpd_in":  _build_microscope_out_and_fpd_in,
        "fpd_out_and_microscope_in":  _build_fpd_out_and_microscope_in,
        "set_pressure":               _build_set_pressure,
        "wait_pressure":              _build_wait_pressure,
        "set_and_wait_pressure":      _build_set_and_wait_pressure,
        "set_control_mode":           _build_set_control_mode,
        "set_temperature":            _build_set_temperature,
        "wait_temperature":           _build_wait_temperature,
        "set_heater":                 _build_set_heater,
        "all_heaters_off":            _build_all_heaters_off,
        "take_xrd":                   _build_take_xrd,
        "take_dark":                  _build_take_dark,
        "save_snapshot":              _build_save_snapshot,
        "save_reference_image":       _build_save_reference_image,
        "start_following":            _build_start_following,
        "stop_following":             _build_stop_following,
        "follow_sample_position":     _build_follow_sample_position,
    }


#: name -> inspect.Signature, computed once from the real dsl/api.py
#: functions — the "provisional call binder" reference signature described
#: in REORGANISATION_PLAN.md §6.2/§7 Phase 2 item 2. Phase 3's CommandSpec
#: registry supersedes this with a signature that isn't just borrowed from
#: api.py's exec()-oriented functions.
_API_SIGNATURES: dict[str, inspect.Signature] = {
    name: inspect.signature(fn) for name, fn in DSL_NAMESPACE.items()
}


def _classify_bind_error(exc: TypeError) -> str:
    """Map an inspect.Signature.bind() TypeError to a stable Diagnostic
    code. In the normal DslCompiler pipeline ASTValidator's own required-
    kwarg check already runs first and short-circuits before SequenceBuilder
    is reached, so "missing a required argument" practically only fires here
    when SequenceBuilder is invoked directly (bypassing ASTValidator)."""
    msg = str(exc)
    if "unexpected keyword argument" in msg:
        return "dsl.unknown_argument"
    if "missing a required argument" in msg:
        return "dsl.required_argument_missing"
    return "dsl.call_binding_error"
