"""
AST-level normalisation applied after ast.parse() and before ASTValidator.validate().

Transformations applied
-----------------------
1. ``range(start, stop[, step])`` → explicit list of float literals.
   A NormalizationError is raised if the expansion would exceed
   _MAX_RANGE_ELEMENTS elements (prevents accidental 10,000-element lists).
2. Plain ``int`` constants → ``float`` constants.
   The DSL treats all numbers as floats; normalising early avoids type
   surprises in SequenceBuilder.

The normaliser never adds DSL functions or removes AST nodes; it only
rewrites constants and the special ``range()`` call form.
"""
from __future__ import annotations

import ast

_MAX_RANGE_ELEMENTS: int = 200


class NormalizationError(ValueError):
    """Raised when a normalisation rule cannot be applied."""


class DslNormalizer(ast.NodeTransformer):
    """AST transformer that applies pre-validation normalisations."""

    def visit_For(self, node: ast.For) -> ast.For:
        if (
            isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Name)
            and node.iter.func.id == "range"
        ):
            node.iter = self._expand_range(node.iter)
        self.generic_visit(node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            node.value = float(node.value)
        return node

    # ------------------------------------------------------------------

    def _expand_range(self, call: ast.Call) -> ast.List:
        args = self._eval_int_args(call)
        try:
            range_obj = range(*args)
            element_count = len(range_obj)
        except (TypeError, ValueError, OverflowError) as exc:
            # TypeError: wrong number of arguments.
            # ValueError: e.g. range(0, 3, 0) — step must not be zero.
            # OverflowError: len() on a range whose length doesn't fit a
            # C ssize_t — certainly far larger than _MAX_RANGE_ELEMENTS.
            raise NormalizationError(f"Invalid range() arguments: {exc}") from exc

        if element_count > _MAX_RANGE_ELEMENTS:
            raise NormalizationError(
                f"range() would expand to {element_count} elements "
                f"(limit is {_MAX_RANGE_ELEMENTS}). "
                "Use an explicit list or reduce the range."
            )

        # range_obj is only materialised into a list once it's known to be
        # within _MAX_RANGE_ELEMENTS — len(range) is O(1) regardless of
        # size, so a call like range(0, 10**9) is rejected without ever
        # allocating a 10^9-element list.
        elts = [ast.Constant(value=float(e)) for e in range_obj]
        return ast.List(elts=elts, ctx=ast.Load())

    @staticmethod
    def _eval_int_args(call: ast.Call) -> list[int]:
        values: list[int] = []
        for arg in call.args:
            value = DslNormalizer._literal_number(arg)
            if value is None:
                raise NormalizationError(
                    "range() arguments must be integer literals in DSL."
                )
            if isinstance(value, float) and not value.is_integer():
                raise NormalizationError(
                    f"range() arguments must be whole numbers in DSL — "
                    f"got {value!r}."
                )
            values.append(int(value))
        return values

    @staticmethod
    def _literal_number(node: ast.expr) -> int | float | None:
        """Return the literal numeric value of *node*, or None if it isn't
        one.

        A negative (or explicitly positive) literal like ``-5`` parses as
        ``ast.UnaryOp(USub(), Constant(5))``, not ``ast.Constant(-5)`` — a
        plain ``isinstance(node, ast.Constant)`` check alone rejects it, so
        this unwraps a leading unary +/- sign first.
        """
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = DslNormalizer._literal_number(node.operand)
            if inner is None:
                return None
            return -inner if isinstance(node.op, ast.USub) else inner
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        return None


def normalize(source: str) -> tuple[str, ast.AST]:
    """Parse *source* and normalise the AST.

    Parameters
    ----------
    source : str
        Raw DSL text.

    Returns
    -------
    normalised_source : str
        Pretty-printed source after normalisation (for display/storage).
    tree : ast.AST
        Normalised AST ready for ASTValidator.validate() and SequenceBuilder.build().

    Raises
    ------
    SyntaxError
        If *source* is not valid Python syntax.
    NormalizationError
        If a normalisation rule cannot be applied (e.g., oversized range).
    """
    tree = ast.parse(source)
    normaliser = DslNormalizer()
    new_tree = normaliser.visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree), new_tree
