"""
Common Diagnostic / ValidationReport model — apps/exp_scheduler
REORGANISATION_PLAN.md Phase 1 (§5.1).

These are the shared result types every validation layer (DSL compile,
Action static checks, PreValidator live preflight, Runner/controller) will
eventually report through, so the UI only ever has to render one shape.
Phase 1 only introduces the model and DslCompiler's use of it —
`validator/pre_validator.py`'s `PreCheckResult` keeps its own shape for now
(see REORGANISATION_PLAN.md §9.5); `ValidationReport.errors`/`.warnings`
below are deliberately compatible with it so callers can bridge the two
without a rewrite until the Phase 7 UI unification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationPhase(str, Enum):
    """Which layer of the multi-layer defence (§2.3) produced a Diagnostic."""

    COMPILE = "compile"          # DslCompiler: normalize / AST safety / build
    STATIC = "static"            # Action-level static checks (Phase 5)
    PREFLIGHT = "preflight"      # PreValidator, device-connected (read-only)
    RUNTIME = "runtime"          # SequenceRunner, immediately before an action
    CONTROLLER = "controller"    # controller/backend final enforcement


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    phase: ValidationPhase
    source_line: int | None = None
    action_path: str | None = None
    device: str | None = None


@dataclass
class ValidationReport:
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [d.message for d in self.diagnostics if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[str]:
        return [d.message for d in self.diagnostics if d.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors
