"""
Common Diagnostic / ValidationReport model — apps/exp_scheduler
REORGANISATION_PLAN.md Phase 1 (§5.1).

These are the shared result types every validation layer (DSL compile,
Action static checks, PreValidator live preflight, Runner/controller)
reports through, so the UI only ever has to render one shape.
`apps/exp_scheduler/validation_service.py` (Phase 7, §7 Phase 7) is what
actually bridges `validator/pre_validator.py`'s `PreCheckResult` into a
`ValidationReport` for UI callers — see that module for the
`validate_dsl()`/`validate_sequence()`/`revalidate_for_run()` entry points.
This module stays a leaf module (no imports from other `exp_scheduler`
submodules) so it can be imported from anywhere without a cycle risk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..sequence import Sequence
    from .snapshots import ValidationSnapshot


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
    RUN_GATE = "run_gate"        # Phase 8: ValidationCertificate re-check at Run time


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    phase: ValidationPhase
    source_line: int | None = None
    action_path: str | None = None
    device: str | None = None
    # Human-readable rendering of the enclosing for-loop iteration chain
    # (see execution_trace.format_loop_context) — None when the Diagnostic
    # is not tied to a specific loop iteration (Phase 5, REORGANISATION_PLAN.md).
    loop_context: str | None = None


@dataclass(frozen=True)
class ValidationCertificate:
    """What was validated, in what state — REORGANISATION_PLAN.md Phase 8
    (§7 Phase 8). Produced by `validation_service.make_certificate()` only
    on a clean (`report.ok`) validation pass, and referenced by
    `validation_service.revalidate_for_run()`'s Run gate to decide whether a
    Run request may proceed without a fresh Validate. Not consumed/cleared
    on use — `ui/scheduler_window.py` keeps the same certificate across
    repeated Run attempts (e.g. re-pressing Run after declining the
    warning-continue dialog, or a Run that doesn't move the stage) until
    something invalidates it (`_reset_validation()` — a Sequence/settings
    edit, a Sequence load, or a Run-gate rejection).

    `device_identity` holds actual object references (not `id()` ints) to
    `ctx.controller`/`pace5000`/`lakeshore`/`radicon` at validation time —
    holding the reference keeps the object alive so a later `id()` cannot
    be reused by an unrelated object, and comparison is done with `is` (see
    `validation_service._same_device_identity()`), never `==`, so a future
    value-equality override on any of those classes can't cause a swapped
    backend to be misjudged as unchanged. Not persisted or logged — only
    meaningful for the lifetime of the window that produced it.
    """
    sequence_fingerprint: str
    settings_fingerprint: str
    snapshot: "ValidationSnapshot"
    device_identity: tuple
    validated_at: datetime


@dataclass
class ValidationReport:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # The Sequence this report was produced for — None when compilation
    # itself failed (validate_dsl() with no Sequence to build a preflight
    # against, see REORGANISATION_PLAN.md §9.2).
    sequence: "Sequence | None" = None
    # All-channel (Ch1-11) stage positions read at validation time, in
    # pulses — mirrors PreCheckResult.baseline_positions (used by
    # ui/scheduler_window.py to detect stage moves between Validate/Run).
    baseline_positions: dict[int, int] = field(default_factory=dict)
    # The full device snapshot this report was produced from — set
    # regardless of report.ok (Phase 8 baseline diffing needs fresh device
    # state even when the fresh preflight independently found unrelated
    # errors elsewhere). None only when PreValidator itself never ran
    # (e.g. a DSL compile error).
    snapshot: "ValidationSnapshot | None" = None
    # Set only when this report represents a clean, certifiable validation
    # pass (report.ok and snapshot is not None) — see
    # validation_service.validate_sequence()/make_certificate().
    certificate: "ValidationCertificate | None" = None

    @property
    def errors(self) -> list[str]:
        return [d.message for d in self.diagnostics if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[str]:
        return [d.message for d in self.diagnostics if d.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors


class _DiagnosticSink(Protocol):
    """Structural type for anything that can absorb a Diagnostic while
    keeping the legacy string-list shape populated too — satisfied by both
    `ValidationReport` and `validator.pre_validator.PreCheckResult` without
    either module importing the other (avoids a circular import between
    `pre_validator.py` and `validator/checks/*.py`, both of which call
    `emit_static`)."""

    diagnostics: list[Diagnostic]
    errors: list[str]
    warnings: list[str]


def emit_diagnostic(
    sink: _DiagnosticSink,
    code: str,
    message: str,
    *,
    phase: ValidationPhase,
    device: str | None = None,
    action_path: str | None = None,
    loop_context: str | None = None,
    severity: Severity = Severity.ERROR,
) -> Diagnostic:
    """Build a Diagnostic for an arbitrary `phase`, append it to
    `sink.diagnostics`, and mirror its message into `sink.errors`/
    `sink.warnings` (the bridge that lets `PreCheckResult` keep its
    string-list shape while `ValidationReport`/`validation_service.py`,
    Phase 7, treat `diagnostics` as authoritative). `emit_static`/
    `emit_preflight` below are the phase-fixed callers most checkers use;
    this general form exists for call sites whose phase depends on which
    checker they wrap — REORGANISATION_PLAN.md Phase 7 §7 item 2, e.g.
    `validator/pre_validator.py`'s `_run()` safety net, which wraps checker
    functions from both STATIC (`action_params.py`/`sequence_structure.py`)
    and PREFLIGHT (`validator/checks/{stage,pace5000,lakeshore,xrd,
    camera_follow}.py`) modules and must tag its own synthesized Diagnostic
    to match whichever one raised."""
    d = Diagnostic(
        severity, code, message, phase,
        action_path=action_path, loop_context=loop_context, device=device,
    )
    sink.diagnostics.append(d)
    (sink.errors if severity is Severity.ERROR else sink.warnings).append(d.message)
    return d


def emit_static(
    sink: _DiagnosticSink,
    code: str,
    message: str,
    *,
    action_path: str | None = None,
    loop_context: str | None = None,
    severity: Severity = Severity.ERROR,
) -> Diagnostic:
    """Build a `ValidationPhase.STATIC` Diagnostic, append it to
    `sink.diagnostics`, and mirror its message into `sink.errors`/
    `sink.warnings` — the bridge that lets `validator/checks/action_params.py`
    and `sequence_structure.py` (Phase 5) report through the new Diagnostic
    model while `PreCheckResult` keeps its string-list shape too."""
    return emit_diagnostic(
        sink, code, message, phase=ValidationPhase.STATIC,
        action_path=action_path, loop_context=loop_context, severity=severity,
    )


def emit_preflight(
    sink: _DiagnosticSink,
    code: str,
    message: str,
    *,
    device: str,
    action_path: str | None = None,
    loop_context: str | None = None,
    severity: Severity = Severity.ERROR,
) -> Diagnostic:
    """Like `emit_static`, but for a `ValidationPhase.PREFLIGHT` Diagnostic —
    one produced by reading a connected device's live state during
    PreValidator's snapshot collection (`validator/snapshots.py`, Phase 6)
    rather than by a device-communication-free Action/Sequence check.
    `device` is required (unlike `Diagnostic.device`, which stays optional
    for the STATIC-phase Diagnostics `emit_static` produces) so every
    preflight Diagnostic is traceable to the physical read that failed."""
    return emit_diagnostic(
        sink, code, message, phase=ValidationPhase.PREFLIGHT, device=device,
        action_path=action_path, loop_context=loop_context, severity=severity,
    )


def build_runtime_diagnostic(
    code: str,
    message: str,
    *,
    device: str | None = None,
    action_path: str | None = None,
) -> Diagnostic:
    """Build a `ValidationPhase.RUNTIME` Diagnostic — `runner.py`'s own
    device-action-immediately-before-dispatch safety checks (MOVE_CONSTRAINTS
    pre-check, Global limits, Ch11 oscillation) — REORGANISATION_PLAN.md
    Phase 9. Unlike `emit_static`/`emit_preflight`, this does not append to a
    sink: `SequenceRunner` has no `ValidationReport` to accumulate into, only
    a single `_last_diagnostic` slot, so this is a plain constructor."""
    return Diagnostic(
        Severity.ERROR, code, message, ValidationPhase.RUNTIME,
        device=device, action_path=action_path,
    )


def build_controller_diagnostic(
    code: str,
    message: str,
    *,
    device: str | None = None,
    action_path: str | None = None,
) -> Diagnostic:
    """Like `build_runtime_diagnostic`, but for a `ValidationPhase.CONTROLLER`
    Diagnostic — a startup-gate/final-enforcement failure reported by
    `runner.py` (motion lease acquisition, Global-limit baseline read) rather
    than an individual action's own safety check — REORGANISATION_PLAN.md
    Phase 9."""
    return Diagnostic(
        Severity.ERROR, code, message, ValidationPhase.CONTROLLER,
        device=device, action_path=action_path,
    )
