"""DSL AST validator — whitelist-based safety check.

Usage:
    validator = ASTValidator()
    errors = validator.validate(dsl_text)   # accepts str or ast.AST
    if not errors:
        tree = ast.parse(dsl_text)
        seq = SequenceBuilder().build(tree)
"""
from __future__ import annotations

import ast

from . import ALLOWED_FUNCTIONS

_BANNED_BUILTINS: frozenset[str] = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "getattr", "setattr", "delattr", "vars", "dir",
    "input", "print",
})

# Valid string values for specific keyword arguments.
# Key: function name → { kwarg_name: frozenset of valid string values }.
# For list-typed args (like devices=[...]) each list element is checked.
_VALID_UNITS: dict[str, dict[str, frozenset[str]]] = {
    "wait": {
        "unit": frozenset({"s", "min"}),
    },
    "set_pressure": {
        "unit": frozenset({"MPa", "Bar"}),
        "rate_unit": frozenset({"MPa/min", "Bar/min", "MPa/sec", "Bar/sec"}),
    },
    "wait_pressure": {
        "unit": frozenset({"MPa", "Bar"}),
    },
    "set_temperature": {
        "unit": frozenset({"K"}),
    },
    "wait_temperature": {
        "unit": frozenset({"K"}),
    },
    "set_speed": {
        "speed": frozenset({"H", "M", "L"}),
    },
    "start_following": {
        "interval_unit": frozenset({"s", "min"}),
    },
    "follow_sample_position": {
        "unit": frozenset({"s", "min"}),
        "interval_unit": frozenset({"s", "min"}),
    },
    "microscope_out_and_fpd_in": {
        "speed": frozenset({"H", "M", "L"}),
    },
    "fpd_out_and_microscope_in": {
        "speed": frozenset({"H", "M", "L"}),
    },
}

# Numeric keyword arguments with a lower bound.
# Key: function name → { kwarg_name: (bound, inclusive) }.
# inclusive=True means "value >= bound" is required; False means "value > bound".
# Only literal numeric arguments can be checked here — loop variables (e.g.
# `pressure=p`) are resolved and range-checked later by PreValidator, since
# their values aren't known until the sequence actually runs.
_NUMERIC_BOUNDS: dict[str, dict[str, tuple[float, bool]]] = {
    "set_pressure": {
        "pressure": (0.0, True),
        "rate": (0.0, True),
    },
    "wait_pressure": {
        "tol": (0.0, False),
    },
}


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
                self._check_unit_args(node, name)
                self._check_numeric_args(node, name)
        elif isinstance(node.func, ast.Attribute):
            self._err(node, "Method calls (obj.method()) are not allowed")
        else:
            self._err(node, "Dynamic function calls are not allowed")
        # Always recurse into arguments so nested errors are caught
        self.generic_visit(node)

    # ── Kwarg value validation ───────────────────────────────────────

    def _check_unit_args(self, node: ast.Call, fname: str) -> None:
        """Validate string keyword arguments that have a fixed set of valid values."""
        unit_specs = _VALID_UNITS.get(fname)
        if not unit_specs:
            return
        for kw in node.keywords:
            if kw.arg is None:
                continue
            valid_set = unit_specs.get(kw.arg)
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
        bounds = _NUMERIC_BOUNDS.get(fname)
        if not bounds:
            return
        for kw in node.keywords:
            if kw.arg is None:
                continue
            bound = bounds.get(kw.arg)
            if bound is None:
                continue
            value = self._literal_num(kw.value)
            if value is None:
                continue
            limit, inclusive = bound
            ok = value >= limit if inclusive else value > limit
            if not ok:
                op = ">=" if inclusive else ">"
                self._err(
                    node,
                    f"{fname}(): {kw.arg} must be {op} {limit:g} (got {value:g})",
                )
