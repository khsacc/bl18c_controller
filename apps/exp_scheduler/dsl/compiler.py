"""
DslCompiler — the single DSL text -> Sequence entry point.

REORGANISATION_PLAN.md Phase 1: before this module existed, DSL text reached
`SequenceBuilder` through three independent paths (`ui/dsl_editor.py` twice,
`ui/llm_panel.py::_on_apply()` once) that didn't agree on whether
`normalize()` ran first, and `llm/session.py` validated a *different* parse
of the same text than the one `ui/llm_panel.py` later built into a Sequence.
`DslCompiler.compile()` is now the only place that runs
normalize -> AST safety validation -> SequenceBuilder.build(), so all three
call sites get identical results for identical input.

This Phase deliberately does NOT change what is accepted or rejected — it
only unifies *how* the existing normalize/validate/build steps are invoked
and converts their outcomes to `Diagnostic`s. `ASTValidator` and
`SequenceBuilder` are unchanged and still importable directly (existing
tests keep working); `_classify_legacy_message()` below is a transitional
shim that gives `ASTValidator`'s existing free-text error strings a stable
`Diagnostic.code`, without rewriting `ASTValidator` itself into a
Diagnostic-native validator — that rewrite belongs to Phase 3 (`CommandSpec`).

REORGANISATION_PLAN.md Phase 2 changed what SequenceBuilder.build() does on
failure: instead of either silently dropping the offending statement/argument
or raising a bare exception, it now raises `SequenceBuildError` carrying a
full list of `Diagnostic`s (unknown keyword argument, unbound bare name,
unsupported statement, ...). `compile()` below unpacks that list directly —
each Diagnostic already has its own stable `code` and `source_line`, so no
`_classify_legacy_message()`-style text sniffing is needed for it.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from ..sequence import Sequence
from .normalizer import NormalizationError, normalize
from .parser import SequenceBuilder, SequenceBuildError
from .validator import ASTValidator
from ..validator.models import Diagnostic, Severity, ValidationPhase

# (substring, code) pairs, checked in order — see _classify_legacy_message().
_LEGACY_MESSAGE_CODES: tuple[tuple[str, str], ...] = (
    ("missing required argument", "dsl.required_argument_missing"),
    ("positional arguments are not supported", "dsl.positional_argument_not_supported"),
    ("must be a finite number", "dsl.non_finite_literal"),
    ("finite numbers", "dsl.non_finite_literal"),
    ("Unknown function", "dsl.unknown_function"),
    ("Forbidden built-in function", "dsl.forbidden_builtin"),
    ("Method calls", "dsl.method_call_not_supported"),
    ("Dynamic function calls", "dsl.dynamic_call_not_supported"),
    ("Dunder", "dsl.dunder_not_supported"),
    ("invalid ", "dsl.invalid_unit_value"),
    ("must be >=", "dsl.numeric_bound_violation"),
    ("must be >", "dsl.numeric_bound_violation"),
    ("for loop must iterate", "dsl.invalid_for_loop_iterable"),
    ("for loop list elements", "dsl.invalid_for_loop_iterable"),
    ("for/else", "dsl.invalid_for_loop_iterable"),
    ("only take effect when oscillate=True", "dsl.oscillation_subfield_without_oscillate"),
    ("is not allowed", "dsl.construct_not_allowed"),
)

_LEGACY_LINE_PREFIX = "Line "


def _classify_legacy_message(message: str) -> tuple[str, int | None]:
    """Derive a stable Diagnostic.code and source_line from one of
    ASTValidator's "Line N: ..." free-text error strings.

    This exists so REORGANISATION_PLAN.md §7 Phase 1 item 8 (moving
    test_exp_scheduler_dsl_validator.py off message substring assertions)
    has something stable to assert against without first doing the full
    Phase 3 CommandSpec rewrite of ASTValidator.
    """
    source_line: int | None = None
    body = message
    if body.startswith(_LEGACY_LINE_PREFIX):
        head, _, rest = body.partition(":")
        line_text = head[len(_LEGACY_LINE_PREFIX):].strip()
        if line_text.isdigit():
            source_line = int(line_text)
        body = rest.strip() or body

    for substring, code in _LEGACY_MESSAGE_CODES:
        if substring in body:
            return code, source_line
    return "dsl.validation_error", source_line


@dataclass(frozen=True)
class ActionSourceMap:
    """Line number of each top-level DSL statement, in source order.

    Since REORGANISATION_PLAN.md Phase 2, `SequenceBuilder` no longer
    silently drops unsupported top-level statements — every statement
    either produces exactly one Action (a call or a for loop) or the whole
    compile fails with a Diagnostic, so on a successful compile
    `statement_lines[i]` lines up 1:1 with `sequence.actions[i]`.
    """

    statement_lines: tuple[int | None, ...] = ()


@dataclass
class CompileResult:
    sequence: Sequence | None
    diagnostics: list[Diagnostic]
    normalised_source: str | None
    source_map: ActionSourceMap

    @property
    def ok(self) -> bool:
        return self.sequence is not None and not any(
            d.severity is Severity.ERROR for d in self.diagnostics
        )


def _source_map_from(tree: ast.AST) -> ActionSourceMap:
    body = tree.body if isinstance(tree, ast.Module) else [tree]
    return ActionSourceMap(
        statement_lines=tuple(getattr(stmt, "lineno", None) for stmt in body)
    )


class DslCompiler:
    """DSL text -> CompileResult. The only supported way to turn DSL text
    into a Sequence — see module docstring."""

    def compile(self, source: str) -> CompileResult:
        try:
            normalised, tree = normalize(source)
        except SyntaxError as exc:
            diagnostic = Diagnostic(
                severity=Severity.ERROR,
                code="dsl.syntax_error",
                message=f"Line {exc.lineno}: SyntaxError: {exc.msg}",
                phase=ValidationPhase.COMPILE,
                source_line=exc.lineno,
            )
            return CompileResult(
                sequence=None, diagnostics=[diagnostic],
                normalised_source=None, source_map=ActionSourceMap(),
            )
        except NormalizationError as exc:
            diagnostic = Diagnostic(
                severity=Severity.ERROR,
                code="dsl.normalization_error",
                message=str(exc),
                phase=ValidationPhase.COMPILE,
            )
            return CompileResult(
                sequence=None, diagnostics=[diagnostic],
                normalised_source=None, source_map=ActionSourceMap(),
            )

        source_map = _source_map_from(tree)

        legacy_errors = ASTValidator().validate(tree)
        if legacy_errors:
            diagnostics = []
            for msg in legacy_errors:
                code, source_line = _classify_legacy_message(msg)
                diagnostics.append(Diagnostic(
                    severity=Severity.ERROR,
                    code=code,
                    message=msg,
                    phase=ValidationPhase.COMPILE,
                    source_line=source_line,
                ))
            return CompileResult(
                sequence=None, diagnostics=diagnostics,
                normalised_source=normalised, source_map=source_map,
            )

        try:
            sequence = SequenceBuilder().build(tree)
        except SequenceBuildError as exc:
            return CompileResult(
                sequence=None, diagnostics=exc.diagnostics,
                normalised_source=normalised, source_map=source_map,
            )
        except Exception as exc:  # noqa: BLE001 - defensive fallback for unforeseen bugs
            diagnostic = Diagnostic(
                severity=Severity.ERROR,
                code="dsl.build_error",
                message=str(exc),
                phase=ValidationPhase.COMPILE,
            )
            return CompileResult(
                sequence=None, diagnostics=[diagnostic],
                normalised_source=normalised, source_map=source_map,
            )

        return CompileResult(
            sequence=sequence, diagnostics=[],
            normalised_source=normalised, source_map=source_map,
        )
