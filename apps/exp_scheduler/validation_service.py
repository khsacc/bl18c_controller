"""
ValidationService — the one orchestration entry point for DSL/Visual/Run
validation. REORGANISATION_PLAN.md Phase 7 (§7 Phase 7).

Before this module existed, `ui/scheduler_window.py` had three independent
call sites (`_on_run`, `_on_validate_visual`, `_validate_sequence_from_dsl`)
that each hand-rolled the same "build the Global*Settings from the UI, call
PreValidator().validate(), then update the Validation Results panel /
Run-enabled state" sequence. This module is the one place that combines DSL
compilation (`dsl.compiler.DslCompiler`) with the device-connected preflight
(`validator.pre_validator.PreValidator`) into a single `ValidationReport`, so
every caller gets the same combined result for the same input.

`validate_dsl()`/`validate_sequence()` are also the shape §5.1's
`Diagnostic`/`ValidationReport` model was designed for from Phase 1 onward.
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime

from .device_context import DeviceContext
from .dsl.compiler import ActionSourceMap, DslCompiler
from .scheduler_settings import (
    GlobalCameraSettings,
    GlobalFollowSettings,
    GlobalLimits,
    GlobalXrdSettings,
    canonical_settings_json,
)
from .sequence import Sequence
from .validator.models import (
    Diagnostic,
    ValidationCertificate,
    ValidationPhase,
    ValidationReport,
    emit_diagnostic,
)
from .validator.pre_validator import PreValidator
from .validator.snapshots import StageSnapshot

# Matches the leading top-level statement index of an action_path produced
# by validator/execution_trace.py's child_path()/_path_str() (e.g. "[2]" or
# "[2].body[1]" for an action nested inside the 3rd top-level for loop).
_TOP_LEVEL_INDEX_RE = re.compile(r"^\[(\d+)\]")


def _with_source_lines(
    diagnostics: list[Diagnostic], source_map: ActionSourceMap,
) -> list[Diagnostic]:
    """Backfill `Diagnostic.source_line` from `source_map` for any
    Diagnostic whose `action_path` resolves to a top-level DSL statement.

    Relies on `ActionSourceMap`'s documented guarantee that, on a
    successful compile, `statement_lines[i]` lines up 1:1 with
    `sequence.actions[i]` (dsl/compiler.py). Only the leading `[i]` of
    `action_path` is used, so a Diagnostic on an action nested inside a
    `for` loop (e.g. `action_path="[0].body[1]"`) is attributed to that
    `for` statement's own line — `ActionSourceMap` only tracks top-level
    statement lines, not per-statement lines inside a loop body, so this is
    a documented approximation, not a per-statement line number.

    Diagnostics that already carry a `source_line` (e.g. a `dsl.syntax_error`
    from `DslCompiler` itself) are left untouched. `Diagnostic` is frozen,
    so backfilling produces a new Diagnostic via `dataclasses.replace()`
    rather than mutating the original.
    """
    if not source_map.statement_lines:
        return diagnostics
    out: list[Diagnostic] = []
    for d in diagnostics:
        if d.source_line is not None or d.action_path is None:
            out.append(d)
            continue
        m = _TOP_LEVEL_INDEX_RE.match(d.action_path)
        if m is None:
            out.append(d)
            continue
        idx = int(m.group(1))
        line = (
            source_map.statement_lines[idx]
            if idx < len(source_map.statement_lines) else None
        )
        out.append(dataclasses.replace(d, source_line=line) if line is not None else d)
    return out


def validate_sequence(
    sequence: Sequence,
    ctx: DeviceContext,
    global_limits: GlobalLimits | None = None,
    global_xrd: GlobalXrdSettings | None = None,
    global_follow: GlobalFollowSettings | None = None,
    global_camera: GlobalCameraSettings | None = None,
    source_map: ActionSourceMap | None = None,
) -> ValidationReport:
    """Static Action checks + live device preflight for an already-built
    Sequence — used for Visual timeline / loaded JSON, and internally by
    `validate_dsl()` once a DSL source has compiled successfully.

    `source_map`, when given, backfills DSL line numbers onto the resulting
    Diagnostics (see `_with_source_lines()`); Visual/JSON callers have no
    DSL source, so they simply omit it.

    `report.snapshot` is always set from `result.snapshot` (success or
    failure) so Phase 8's Run-gate baseline diff has fresh device state to
    compare against even when this validation itself found unrelated
    errors. `report.certificate` is set only on a clean pass with a
    snapshot to certify (REORGANISATION_PLAN.md §7 Phase 8) — never on a
    failure, so a caller can never mistake "this fresh check happened to be
    clean" for "this is a newly-validated state".
    """
    result = PreValidator().validate(sequence, ctx, global_limits, global_xrd, global_follow)
    diagnostics = list(result.diagnostics)
    if source_map is not None:
        diagnostics = _with_source_lines(diagnostics, source_map)
    report = ValidationReport(
        diagnostics=diagnostics,
        sequence=sequence,
        baseline_positions=dict(result.baseline_positions),
        snapshot=result.snapshot,
    )
    if report.ok and result.snapshot is not None:
        report.certificate = make_certificate(
            sequence, result.snapshot, ctx,
            global_limits, global_xrd, global_follow, global_camera,
        )
    return report


def validate_dsl(
    source: str,
    ctx: DeviceContext,
    global_limits: GlobalLimits | None = None,
    global_xrd: GlobalXrdSettings | None = None,
    global_follow: GlobalFollowSettings | None = None,
    global_camera: GlobalCameraSettings | None = None,
) -> ValidationReport:
    """DSL text -> ValidationReport.

    Compiles first; a compile error stops here with no Sequence to build a
    device preflight against (REORGANISATION_PLAN.md §9.2 — "compile error
    で後段の問題が見えなくなる" risk is accepted deliberately: Sequence
    construction failed, so there is nothing safe to preflight). On a
    successful compile, delegates to `validate_sequence()` so DSL and
    Visual/JSON share the exact same static + preflight checks.
    """
    compiled = DslCompiler().compile(source)
    if not compiled.ok:
        return ValidationReport(diagnostics=list(compiled.diagnostics), sequence=None)
    return validate_sequence(
        compiled.sequence, ctx, global_limits, global_xrd, global_follow, global_camera,
        source_map=compiled.source_map,
    )


# ------------------------------------------------------------------ Phase 8: ValidationCertificate / Run gate

def _sequence_fingerprint(sequence: Sequence) -> str:
    """Stable, content-based fingerprint of a Sequence — sort_keys=True JSON
    of `Sequence.to_dict()`, never repr()/id(), so two Sequences with equal
    Actions in equal order always fingerprint equal regardless of object
    identity or process."""
    return json.dumps(sequence.to_dict(), sort_keys=True)


def _device_identity(ctx: DeviceContext) -> tuple:
    """The actual backend object references held by `ctx` right now — used
    (only at Run-gate time) to detect that `ctx.controller`/`pace5000`/
    `lakeshore`/`radicon` were swapped for different instances since the
    certificate was made. See `ValidationCertificate.device_identity` for
    why these are real references, not `id()` ints."""
    return (ctx.controller, ctx.pace5000, ctx.lakeshore, ctx.radicon)


def _same_device_identity(current: tuple, validated: tuple) -> bool:
    """`is`-based comparison, deliberately not `==`/`!=` — equality would
    depend on each backend class's own `__eq__` (none currently define one,
    but a future value-equality addition, e.g. via a dataclass conversion,
    must not cause a genuinely different backend instance to be judged
    'unchanged')."""
    return all(a is b for a, b in zip(current, validated, strict=True))


def make_certificate(
    sequence: Sequence,
    snapshot,
    ctx: DeviceContext,
    global_limits: GlobalLimits | None,
    global_xrd: GlobalXrdSettings | None,
    global_follow: GlobalFollowSettings | None,
    global_camera: GlobalCameraSettings | None,
) -> ValidationCertificate:
    return ValidationCertificate(
        sequence_fingerprint=_sequence_fingerprint(sequence),
        settings_fingerprint=canonical_settings_json(
            global_limits,
            global_xrd or GlobalXrdSettings(),
            global_follow or GlobalFollowSettings(),
            global_camera or GlobalCameraSettings(),
        ),
        snapshot=snapshot,
        device_identity=_device_identity(ctx),
        validated_at=datetime.now(),
    )


def _check_stage_baseline(
    report: ValidationReport,
    cert_stage: StageSnapshot,
    fresh_stage: "StageSnapshot | None",
) -> None:
    """Compare the certificate's Ch1-11 baseline against a freshly-read
    stage snapshot, emitting a RUN_GATE Diagnostic onto `report` if the
    stage moved (or if either side's baseline is incomplete).

    Verifies BOTH sides have every one of Ch1-11 before comparing values —
    looping over only the channels common to both would silently treat a
    channel missing from either side as "nothing to compare", which could
    misreport an incomplete baseline as "no movement" (a channel that
    failed to read now could be one that moved and is jammed, exactly the
    case this check must not wave through)."""
    required = set(range(1, 12))
    cert_positions = cert_stage.positions
    fresh_positions = fresh_stage.positions if fresh_stage is not None else {}
    if not required <= set(cert_positions.keys()) or not required <= set(fresh_positions.keys()):
        emit_diagnostic(
            report, "run_gate.stage_baseline_incomplete",
            "ステージの全チャンネル (Ch1-11) の位置を確認できないため、"
            "Validate以降に移動していないことを確認できません。再度Validateしてください。",
            phase=ValidationPhase.RUN_GATE, device="stage",
        )
        return

    moved = [
        f"Ch{ch}: validation時 {cert_positions[ch]:+} → 現在 {fresh_positions[ch]:+}"
        for ch in sorted(required)
        if cert_positions[ch] != fresh_positions[ch]
    ]
    if moved:
        emit_diagnostic(
            report, "run_gate.stage_moved_since_validate",
            "最新のValidate時からステージが動いています。再度Validateしてください。\n"
            + "\n".join(moved),
            phase=ValidationPhase.RUN_GATE, device="stage",
        )


def revalidate_for_run(
    sequence: Sequence,
    ctx: DeviceContext,
    global_limits: GlobalLimits | None = None,
    global_xrd: GlobalXrdSettings | None = None,
    global_follow: GlobalFollowSettings | None = None,
    global_camera: GlobalCameraSettings | None = None,
    certificate: ValidationCertificate | None = None,
) -> ValidationReport:
    """Full live preflight re-run immediately before Run (REORGANISATION_PLAN.md
    §7 Phase 7 item 6), plus the Phase 8 (§7 Phase 8) Run gate: `certificate`
    is the `ValidationCertificate` produced by the last successful Validate
    (`ui/scheduler_window.py` tracks this as `self._certificate`), diffed
    against the current Sequence/settings/DeviceContext/stage state.

    The live preflight always runs to completion first — never skipped or
    short-circuited by a Run-gate failure — matching PreValidator's own
    "accumulate every problem" policy, so a Run attempt with a stale
    certificate AND a genuine fresh preflight error shows both at once.

    The returned report's `certificate` is always forced to None via
    `dataclasses.replace()`: `fresh_report.certificate` (from the inner
    `validate_sequence()` call) means only "this fresh preflight, taken by
    itself, was clean" — that is unrelated to whether the Run gate as a
    whole passed, and must never be mistaken by a caller for a new,
    persistable ValidationCertificate.
    """
    fresh_report = validate_sequence(
        sequence, ctx, global_limits, global_xrd, global_follow, global_camera,
    )

    if certificate is None:
        emit_diagnostic(
            fresh_report, "run_gate.not_validated",
            "シーケンスがValidateされていません。Runの前にValidateを行ってください。",
            phase=ValidationPhase.RUN_GATE,
        )
    else:
        if _sequence_fingerprint(sequence) != certificate.sequence_fingerprint:
            emit_diagnostic(
                fresh_report, "run_gate.sequence_changed",
                "Validate後にシーケンスが変更されました。再度Validateしてください。",
                phase=ValidationPhase.RUN_GATE,
            )
        settings_fingerprint = canonical_settings_json(
            global_limits,
            global_xrd or GlobalXrdSettings(),
            global_follow or GlobalFollowSettings(),
            global_camera or GlobalCameraSettings(),
        )
        if settings_fingerprint != certificate.settings_fingerprint:
            emit_diagnostic(
                fresh_report, "run_gate.settings_changed",
                "Validate後にGlobal設定が変更されました。再度Validateしてください。",
                phase=ValidationPhase.RUN_GATE,
            )
        if not _same_device_identity(_device_identity(ctx), certificate.device_identity):
            emit_diagnostic(
                fresh_report, "run_gate.device_context_changed",
                "Validate後に接続機器が変更されました。再度Validateしてください。",
                phase=ValidationPhase.RUN_GATE,
            )
        cert_stage = certificate.snapshot.stage if certificate.snapshot is not None else None
        if cert_stage is not None:
            fresh_stage = fresh_report.snapshot.stage if fresh_report.snapshot is not None else None
            _check_stage_baseline(fresh_report, cert_stage, fresh_stage)

    return dataclasses.replace(fresh_report, certificate=None)
