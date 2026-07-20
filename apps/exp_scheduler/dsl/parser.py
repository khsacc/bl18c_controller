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
raw exception. Per-call keyword arguments are bound against the command's
real signature (`inspect.Signature.bind()` + `apply_defaults()`) rather than
read ad hoc with `dict.get(fallback)`, so an argument's default value can no
longer drift between what the LLM prompt documents and what compiling
actually does.

REORGANISATION_PLAN.md Phase 3 (CommandSpec and Action factory
unification): the per-command signature, loop-variable-eligible argument
set, and Action-construction logic this module binds against are no longer
this module's own tables — they are read from `dsl/_registry.py`'s
`CommandSpec` registry (populated by `dsl/api.py`'s `@dsl_command`
declarations), the same registry `ASTValidator` and `llm/prompt_builder.py`
read. `ASTValidator` is unchanged in this Phase (still the "AST safety"
layer).
"""
from __future__ import annotations

import ast
import inspect
import typing
from typing import Any

from ..actions import Action, ForLoopAction
from ..sequence import Sequence
from ..validator.models import Diagnostic, Severity, ValidationPhase
from ._registry import get_spec


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
        spec = get_spec(fname)
        if spec is None:
            self._diag(node, "dsl.unknown_function", f"Unknown function: {fname!r}")
            return None
        sig = spec.signature
        if node.args:
            self._diag(
                node, "dsl.positional_argument_not_supported",
                f"{fname}(): positional arguments are not supported — "
                "use keyword arguments",
            )
            return None

        evaluated: dict[str, Any] = {}
        loop_var_keywords: set[str] = set()
        had_error = False
        for kw in node.keywords:
            if kw.arg is None:
                self._diag(
                    node, "dsl.unsupported_statement",
                    f"{fname}(): **kwargs expansion is not supported",
                )
                had_error = True
                continue
            if kw.arg in evaluated:
                self._diag(
                    kw.value, "dsl.duplicate_keyword_argument",
                    f"{fname}(): duplicate keyword argument {kw.arg!r}",
                )
                had_error = True
                continue
            value, error = self._eval_arg(kw.value, loop_vars)
            if error is not None:
                self._diag(kw.value, "dsl.unbound_name", f"{fname}(): {kw.arg}: {error}")
                had_error = True
                continue
            if isinstance(kw.value, ast.Name):
                loop_var_keywords.add(kw.arg)
            elif isinstance(kw.value, ast.JoinedStr) and any(
                isinstance(part, ast.FormattedValue) for part in kw.value.values
            ):
                # An f-string with at least one `{loopvar}` placeholder is a
                # loop-variable reference just as much as a bare name is —
                # _eval_fstring() already guarantees every placeholder here
                # resolves to a bound for-loop variable, so this must go
                # through the same allowed_loop_var_args gate below, or a
                # command whose runner never resolves that placeholder at
                # execution time (anything but log_message's `message`
                # today) would silently keep the literal "{var}" text
                # forever (external review finding, see
                # REORGANISATION_PLAN.md §31).
                loop_var_keywords.add(kw.arg)
            evaluated[kw.arg] = value

        if had_error:
            return None

        try:
            bound = sig.bind(**evaluated)
        except TypeError as exc:
            self._diag(node, _classify_bind_error(exc), f"{fname}(): {exc}")
            return None
        bound.apply_defaults()

        # A for-loop variable reference is only meaningful for the handful
        # of arguments actions.py's LOOP_VAR_FIELDS actually resolves at
        # run time (Runner._do_stage/_do_set_pressure/_do_set_temperature),
        # recorded per-command in the CommandSpec's argument_rules
        # (dsl/_registry.py, populated by dsl/api.py's @dsl_command).
        # Everywhere else it's a plain string masquerading as whatever type
        # that argument expects — e.g. set_speed(ch=4, speed=p) used to
        # compile a StageAction(speed="p") that would only fail once the
        # Runner tried to send it to hardware.
        allowed_loop_var_args = frozenset(
            name for name, rule in spec.argument_rules.items() if rule.loop_var_allowed
        )
        for name in loop_var_keywords:
            if name not in allowed_loop_var_args:
                self._diag(
                    node, "dsl.loop_variable_not_supported_here",
                    f"{fname}(): {name} does not accept a for-loop variable "
                    "reference — pass a literal value instead (only "
                    f"{sorted(allowed_loop_var_args) or ['(none)']} accept "
                    f"one for {fname}())",
                )
                had_error = True

        # Literal-typed argument sanity check against the real dsl/api.py
        # annotation. Only explicitly-provided, non-loop-var arguments are
        # checked — apply_defaults()-filled values are always the
        # signature's own (trusted) default, and a loop-var reference is
        # legitimately a str standing in for a future numeric value.
        for name in evaluated:
            if name in loop_var_keywords:
                continue
            param = sig.parameters.get(name)
            if param is None:
                continue
            if not _annotation_accepts(param.annotation, bound.arguments[name]):
                self._diag(
                    node, "dsl.argument_type_mismatch",
                    f"{fname}(): {name} expects {_annotation_str(param.annotation)}, "
                    f"got {bound.arguments[name]!r}",
                )
                had_error = True

        if had_error:
            return None

        return spec.factory(dict(bound.arguments))

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
            return self._eval_fstring(node, loop_vars)

        return None, f"cannot evaluate DSL argument: {ast.dump(node)}"

    def _eval_fstring(
        self, node: ast.JoinedStr, loop_vars: frozenset[str]
    ) -> tuple[str | None, str | None]:
        """Evaluate an f-string into a runner.py str.format()-compatible
        template — literal text stays as-is, `{varname}` placeholders are
        preserved verbatim for Runner to resolve against the loop's
        var_context at execution time (see runner.py::_do_log()).

        Every `{...}` part must be a bare for-loop variable name bound in
        `loop_vars`; anything else (an unbound name, or a non-trivial
        expression like `{p + 1}`) is rejected here rather than silently
        rendered as a literal "?", which used to compile cleanly but never
        matched what the text actually claimed to show.
        """
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
                continue
            if isinstance(part, ast.FormattedValue):
                if isinstance(part.value, ast.Name):
                    if part.value.id in loop_vars:
                        parts.append(f"{{{part.value.id}}}")
                        continue
                    return None, (
                        f"{part.value.id!r} is not defined here — it is not "
                        "a for-loop variable bound by an enclosing `for`"
                    )
                return None, (
                    "f-string placeholders must be a plain for-loop "
                    f"variable name, e.g. f\"{{p}}\" — got an expression: "
                    f"{ast.dump(part.value)}"
                )
            return None, f"cannot evaluate DSL argument: {ast.dump(node)}"
        return "".join(parts), None


def _annotation_str(annotation: Any) -> str:
    return getattr(annotation, "__name__", str(annotation))


def _annotation_accepts(annotation: Any, value: Any) -> bool:
    """Best-effort literal-type check for a bound DSL argument against its
    dsl/api.py parameter annotation.

    Every annotation in dsl/api.py today is a scalar (bool/int/float/str)
    optionally unioned with None (`X | None`) — this does not attempt to
    handle generic containers, since none are currently used.
    """
    if annotation is inspect.Parameter.empty:
        return True
    union_members = typing.get_args(annotation)
    if union_members:
        return any(_annotation_accepts(member, value) for member in union_members)
    if annotation is type(None):
        return value is None
    if annotation is bool:
        return isinstance(value, bool)
    if annotation is int:
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        # The normalizer converts every literal int constant to float
        # before SequenceBuilder ever runs, so a plain `ch=4` arrives here
        # as `4.0` — only a genuinely fractional float (e.g. `ch=4.9`,
        # which would otherwise silently truncate via int()) is rejected.
        return isinstance(value, float) and value.is_integer()
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is str:
        return isinstance(value, str)
    return True


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
