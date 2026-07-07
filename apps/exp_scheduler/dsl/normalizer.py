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
            elements = list(range(*args))
        except TypeError as exc:
            raise NormalizationError(f"Invalid range() arguments: {exc}") from exc

        if len(elements) > _MAX_RANGE_ELEMENTS:
            raise NormalizationError(
                f"range() would expand to {len(elements)} elements "
                f"(limit is {_MAX_RANGE_ELEMENTS}). "
                "Use an explicit list or reduce the range."
            )

        elts = [ast.Constant(value=float(e)) for e in elements]
        return ast.List(elts=elts, ctx=ast.Load())

    @staticmethod
    def _eval_int_args(call: ast.Call) -> list[int]:
        values: list[int] = []
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                values.append(int(arg.value))
            else:
                raise NormalizationError(
                    "range() arguments must be integer literals in DSL."
                )
        return values


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
