"""DSL AST validator — whitelist-based safety check.

Usage:
    validator = ASTValidator()
    errors = validator.validate(dsl_text)   # accepts str or ast.AST
    if not errors:
        tree = ast.parse(dsl_text)
        seq = SequenceBuilder().build(tree)

REORGANISATION_PLAN.md Phase 3: the per-command unit/enum whitelist, numeric
lower bound, and required-keyword-argument checks below no longer read
their own hand-written tables — they read `dsl/_registry.py`'s CommandSpec
registry (populated by `dsl/api.py`'s `@dsl_command` declarations), the same
registry `dsl/parser.py` and `llm/prompt_builder.py` read.
"""
from __future__ import annotations

import ast
import math

from . import ALLOWED_FUNCTIONS
from ._registry import get_spec

_BANNED_BUILTINS: frozenset[str] = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "getattr", "setattr", "delattr", "vars", "dir",
    "input", "print",
})

#: take_xrd()'s osc_pos_a_deg/osc_pos_b_deg/osc_dwell_ms/osc_speed are only
#: carried into the Action by dsl/_factories.py::take_xrd() when the call's
#: own `oscillate` is truthy — matching ui/step_editor.py, where these 4
#: fields and `oscillate` are always set together as one group behind a
#: single "has oscillation" checkbox, and TakeXrdAction.to_dsl(), which only
#: ever emits them when self.oscillate is truthy. Passing one of these
#: without oscillate=True in the same call used to compile successfully and
#: silently discard the value — see _check_take_xrd_oscillation_group().
_TAKE_XRD_OSCILLATION_SUBFIELDS: frozenset[str] = frozenset({
    "osc_pos_a_deg", "osc_pos_b_deg", "osc_dwell_ms", "osc_speed",
})


class ASTValidator(ast.NodeVisitor):
    """
    Validates a DSL AST against the whitelist of allowed constructs.

    Banned: import / class / def / lambda / while / try / with / async variants /
            raise / del / global / nonlocal / yield / dunder names / method calls /
            non-whitelist function calls.
    """

    def __init__(self) -> None:
        self._errors: list[str] = []

    def validate(self, source: str | ast.AST) -> list[str]:
        """Return list of error messages (empty = OK).

        Accepts either raw DSL text (str) or a pre-parsed AST.
        SyntaxErrors from ast.parse() are caught and returned as error messages.
        """
        if isinstance(source, str):
            try:
                tree = ast.parse(source)
            except SyntaxError as e:
                return [f"Line {e.lineno}: SyntaxError: {e.msg}"]
        else:
            tree = source
        self._errors = []
        self.visit(tree)
        return self._errors

    # ── Internal helper ──────────────────────────────────────────────

    def _err(self, node: ast.AST, msg: str) -> None:
        ln = getattr(node, "lineno", "?")
        self._errors.append(f"Line {ln}: {msg}")

    # ── Explicitly banned statement nodes ────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:
        self._err(node, "import is not allowed")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._err(node, "from ... import is not allowed")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._err(node, "class definition is not allowed")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._err(node, "function definition (def) is not allowed")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._err(node, "async def is not allowed")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._err(node, "lambda is not allowed")

    def visit_While(self, node: ast.While) -> None:
        self._err(node, "while is not allowed; use a for loop instead")

    def visit_Assign(self, node: ast.Assign) -> None:
        # REORGANISATION_PLAN.md §12.5 decision #1 (Phase 2): SequenceBuilder
        # never built anything from `var = value` — it was silently ignored,
        # not evaluated, despite SPEC.md previously listing it as usable
        # syntax. Reject explicitly instead of leaving that documentation/
        # implementation contradiction in place.
        self._err(node, "assignment (var = value) is not allowed")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._err(node, "assignment (var = value) is not allowed")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._err(node, "assignment (var = value) is not allowed")
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        # Same rationale as visit_Assign — SPEC.md listed `if`/`else` as
        # usable syntax, but SequenceBuilder never recursed into an ast.If's
        # body at all, silently dropping the whole branch including any
        # whitelisted calls inside it.
        self._err(node, "if statement is not allowed")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._err(node, "try/except is not allowed")

    def visit_With(self, node: ast.With) -> None:
        self._err(node, "with statement is not allowed")

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._err(node, "async with is not allowed")

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._err(node, "async for is not allowed")

    def visit_Raise(self, node: ast.Raise) -> None:
        self._err(node, "raise is not allowed")

    def visit_Delete(self, node: ast.Delete) -> None:
        self._err(node, "del is not allowed")

    def visit_Global(self, node: ast.Global) -> None:
        self._err(node, "global is not allowed")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._err(node, "nonlocal is not allowed")

    def visit_Yield(self, node: ast.Yield) -> None:
        self._err(node, "yield is not allowed")

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self._err(node, "yield from is not allowed")

    def visit_Await(self, node: ast.Await) -> None:
        self._err(node, "await is not allowed")

    # Python 3.11+ exception groups
    def visit_TryStar(self, node: ast.AST) -> None:
        self._err(node, "try*/except* is not allowed")

    # ── Special-case checks ──────────────────────────────────────────

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if "__" in node.attr:
            self._err(node, f"Dunder attribute access is not allowed: {node.attr!r}")
        else:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__"):
            self._err(node, f"Dunder name is not allowed: {node.id!r}")
        # ast.Name has no meaningful AST children to recurse into

    def visit_For(self, node: ast.For) -> None:
        if not isinstance(node.iter, ast.List):
            self._err(
                node,
                "for loop must iterate over a literal numeric list, e.g. [0.5, 1.0, 1.5]",
            )
        else:
            for elt in node.iter.elts:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, (int, float))):
                    self._err(
                        node,
                        "for loop list elements must be numeric literals (int or float)",
                    )
                    break
                if math.isnan(elt.value) or math.isinf(elt.value):
                    self._err(
                        node,
                        "for loop list elements must be finite numbers",
                    )
                    break
        if node.orelse:
            self._err(node, "for/else is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name in _BANNED_BUILTINS:
                self._err(node, f"Forbidden built-in function: {name!r}")
            elif name not in ALLOWED_FUNCTIONS:
                self._err(node, f"Unknown function: {name!r} (not in the DSL function list)")
            else:
                if node.args:
                    self._err(
                        node,
                        f"{name}(): positional arguments are not supported — "
                        "use keyword arguments",
                    )
                self._check_required_kwargs(node, name)
                self._check_unit_args(node, name)
                self._check_numeric_args(node, name)
                self._check_finite_args(node, name)
                if name == "take_xrd":
                    self._check_take_xrd_oscillation_group(node)
        elif isinstance(node.func, ast.Attribute):
            self._err(node, "Method calls (obj.method()) are not allowed")
        else:
            self._err(node, "Dynamic function calls are not allowed")
        # Always recurse into arguments so nested errors are caught
        self.generic_visit(node)

    # ── Kwarg value validation ───────────────────────────────────────

    def _check_unit_args(self, node: ast.Call, fname: str) -> None:
        """Validate string keyword arguments that have a fixed set of valid values."""
        spec = get_spec(fname)
        if spec is None:
            return
        for kw in node.keywords:
            if kw.arg is None:
                continue
            rule = spec.argument_rules.get(kw.arg)
            valid_set = rule.valid_values if rule is not None else None
            if valid_set is None:
                continue
            # List argument (e.g. devices=[...])
            if isinstance(kw.value, ast.List):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value not in valid_set:
                            self._err(
                                node,
                                f"{fname}(): invalid {kw.arg!r} value {elt.value!r}."
                                f" Valid values: {sorted(valid_set)}",
                            )
            # Scalar string argument
            elif isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                if kw.value.value not in valid_set:
                    self._err(
                        node,
                        f"{fname}(): invalid {kw.arg!r} value {kw.value.value!r}."
                        f" Valid values: {sorted(valid_set)}",
                    )

    def _check_take_xrd_oscillation_group(self, node: ast.Call) -> None:
        """Reject osc_pos_a_deg/osc_pos_b_deg/osc_dwell_ms/osc_speed given
        without oscillate=True in the same take_xrd() call, rather than
        silently compiling and discarding them (see
        _TAKE_XRD_OSCILLATION_SUBFIELDS). `oscillate` isn't a loop-var
        argument (dsl/api.py has no ArgumentRule(loop_var_allowed=True) for
        it), so a literal ast.Constant check for `is True` is exhaustive —
        it correctly also flags oscillate=False paired with a subfield,
        which to_dsl() never emits (dead value, since resolved oscillate
        would be False) but which is still worth rejecting rather than
        silently dropping.
        """
        given_subfields = sorted(
            kw.arg for kw in node.keywords
            if kw.arg in _TAKE_XRD_OSCILLATION_SUBFIELDS
        )
        if not given_subfields:
            return
        oscillate_kw = next(
            (kw for kw in node.keywords if kw.arg == "oscillate"), None
        )
        oscillate_is_true = (
            oscillate_kw is not None
            and isinstance(oscillate_kw.value, ast.Constant)
            and oscillate_kw.value.value is True
        )
        if not oscillate_is_true:
            self._err(
                node,
                f"take_xrd(): {', '.join(given_subfields)} only take effect "
                "when oscillate=True is also given in this call — pass "
                "oscillate=True, or omit these to inherit the global XRD "
                "oscillation settings",
            )

    def _check_required_kwargs(self, node: ast.Call, fname: str) -> None:
        """Error when a required keyword argument (per the CommandSpec's
        signature — every parameter with no default) is missing — whether
        omitted entirely or passed positionally (the latter is also
        independently flagged in visit_Call)."""
        spec = get_spec(fname)
        if spec is None:
            return
        required = spec.required_kwargs
        if not required:
            return
        provided = {kw.arg for kw in node.keywords if kw.arg is not None}
        missing = required - provided
        if missing:
            self._err(
                node,
                f"{fname}(): missing required argument(s): {', '.join(sorted(missing))}",
            )

    def _check_finite_args(self, node: ast.Call, fname: str) -> None:
        """Reject a numeric-literal keyword argument that is NaN/Inf, for
        every whitelisted function — not just the ones with a configured
        lower bound (ArgumentRule.lower_bound). Python's own literal grammar
        can produce `inf` from an ordinary-looking overflow (e.g. `1e400`),
        with no function call involved, so this must run unconditionally.
        """
        for kw in node.keywords:
            if kw.arg is None:
                continue
            value = self._literal_num(kw.value)
            if value is None:
                continue
            if math.isnan(value) or math.isinf(value):
                self._err(
                    node, f"{fname}(): {kw.arg} must be a finite number (got {value})"
                )

    @staticmethod
    def _literal_num(node: ast.expr) -> float | None:
        """Return the numeric value of a literal int/float, unwrapping a
        leading unary +/- (e.g. -1.0), or None if not a numeric literal."""
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = ASTValidator._literal_num(node.operand)
            if inner is None:
                return None
            return -inner if isinstance(node.op, ast.USub) else inner
        return None

    def _check_numeric_args(self, node: ast.Call, fname: str) -> None:
        """Validate literal numeric keyword arguments against a lower bound.

        Only literal values are checked; loop-variable arguments (e.g.
        `pressure=p`) are left to PreValidator, which resolves them per
        iteration at validate time.
        """
        spec = get_spec(fname)
        if spec is None:
            return
        for kw in node.keywords:
            if kw.arg is None:
                continue
            rule = spec.argument_rules.get(kw.arg)
            if rule is None or rule.lower_bound is None:
                continue
            value = self._literal_num(kw.value)
            if value is None:
                continue
            limit, inclusive = rule.lower_bound, rule.lower_bound_inclusive
            ok = value >= limit if inclusive else value > limit
            if not ok:
                op = ">=" if inclusive else ">"
                self._err(
                    node,
                    f"{fname}(): {kw.arg} must be {op} {limit:g} (got {value:g})",
                )
