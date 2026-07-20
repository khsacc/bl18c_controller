"""
Device-communication-free Sequence structure checks — REORGANISATION_PLAN.md
Phase 5 (§7 Phase 5 item 6): start/stop pairing, undefined/unused loop
variables, empty loop body/values, duplicate consecutive actions, and the
loop-expansion-limit report.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...actions import (
    Action,
    ForLoopAction,
    FollowSampleAction,
    StartFollowingAction,
    StopFollowingAction,
    action_loop_var_ref,
)
from ..execution_trace import (
    ExecutionTrace,
    TraceEntry,
    format_loop_context,
    loop_limit_messages,
    walk_raw,
)
from ..models import Severity, emit_static

if TYPE_CHECKING:
    from ..pre_validator import PreCheckResult


def check_empty_sequence(actions: list[Action], r: "PreCheckResult") -> None:
    """Error when the sequence has no top-level actions at all — running it
    would do nothing, which is almost always an accidental empty save
    rather than an intentional no-op sequence."""
    if not actions:
        emit_static(r, "static.sequence.empty", "シーケンスにアクションが一つもありません")


def check_unused_loop_vars(actions: list[Action], r: "PreCheckResult") -> None:
    """Warn when a ForLoopAction variable is never referenced in its body."""
    for a, path, _siblings, _i, _loop_values in walk_raw(actions):
        if not isinstance(a, ForLoopAction):
            continue
        if not _loop_body_uses_var(a.body, a.var):
            emit_static(
                r, "static.sequence.unused_loop_var",
                f"for ループ変数 {a.var!r} がループ本体内で一度も使用されていません。"
                "各反復で同じ処理が繰り返されます。",
                action_path=path, severity=Severity.WARNING,
            )


def check_undefined_loop_vars(actions: list[Action], r: "PreCheckResult") -> None:
    """Error when an action references a loop variable that is not defined
    at that point in the sequence — e.g. a stale reference left after a loop
    was deleted or renamed by hand, or a Copy/Paste that moved an action out
    of its original loop's scope."""
    for a, path, _siblings, _i, defined in walk_raw(actions):
        if isinstance(a, ForLoopAction):
            continue
        for name in _action_loop_var_names(a):
            if name not in defined:
                emit_static(
                    r, "static.sequence.undefined_loop_var",
                    f"{a.describe()}: ループ変数 {name!r} はこの位置では未定義です",
                    action_path=path,
                )


def check_empty_loop_body(actions: list[Action], r: "PreCheckResult") -> None:
    """Error when a ForLoopAction has no body — e.g. a loop created via
    "+ Add Loop" in the Visual editor that never got any steps added."""
    for a, path, _siblings, _i, _loop_values in walk_raw(actions):
        if isinstance(a, ForLoopAction) and not a.body:
            emit_static(
                r, "static.sequence.empty_loop_body",
                f"{a.describe()}: ループ本体が空です", action_path=path,
            )


def check_empty_loop_values(actions: list[Action], r: "PreCheckResult") -> None:
    """Error when a ForLoopAction has an empty `values` list — the body
    would run zero times, silently skipping everything written inside it."""
    for a, path, _siblings, _i, _loop_values in walk_raw(actions):
        if isinstance(a, ForLoopAction) and not a.values:
            emit_static(
                r, "static.sequence.empty_loop_values",
                f"{a.describe()}: ループの values が空です"
                "（本体が一度も実行されません）",
                action_path=path,
            )


def check_duplicate_consecutive_actions(actions: list[Action], r: "PreCheckResult") -> None:
    """Warn when the exact same action (identical type and every parameter)
    appears twice in a row. Actions are plain dataclasses, so `==` already
    compares class + all fields — including a ForLoopAction's var/values/
    body, recursively."""
    for a, path, siblings, i, _loop_values in walk_raw(actions):
        if i > 0 and a == siblings[i - 1]:
            emit_static(
                r, "static.sequence.duplicate_consecutive_action",
                f"{a.describe()}: 直前と全く同一のアクションが連続しています。"
                "誤って重複していないか確認してください。",
                action_path=path, severity=Severity.WARNING,
            )


def check_follow_pairing(ordered: list["TraceEntry"], r: "PreCheckResult") -> None:
    """Scan the sequence in true execution order (`ExecutionTrace.ordered`)
    for start/stop follow pairing.

    Unrolling matters: a start_following left open at the end of a loop body
    becomes a *nested* start_following the moment the next iteration begins
    — a scan that only visits the body once cannot see that. Gated by
    `within_limits` at the call site (`ordered` is empty otherwise).

    `open_stack` tracks every still-unclosed `StartFollowingAction` entry
    (not just a bare depth count) so the final "no matching stop_following"
    Diagnostic can carry the action_path/loop_context of the specific call
    that was left open, rather than none at all — `len(open_stack)` is
    exactly the old `depth` counter."""
    open_stack: list["TraceEntry"] = []
    for entry in ordered:
        a = entry.action
        label = entry.label
        lc = format_loop_context(entry.loop_context)
        if isinstance(a, StartFollowingAction):
            if open_stack:
                emit_static(
                    r, "static.sequence.follow_pairing_violation",
                    f"{label}: start_following called while a follow session is "
                    "already active (nested start_following is not allowed)",
                    action_path=entry.action_path, loop_context=lc,
                )
            open_stack.append(entry)
        elif isinstance(a, FollowSampleAction):
            if open_stack:
                emit_static(
                    r, "static.sequence.follow_pairing_violation",
                    f"{label}: follow_sample_position called while a follow "
                    "session is already active",
                    action_path=entry.action_path, loop_context=lc,
                )
            # open_stack は変更しない — start と stop が内部で完結するため
        elif isinstance(a, StopFollowingAction):
            if not open_stack:
                emit_static(
                    r, "static.sequence.follow_pairing_violation",
                    f"{label}: stop_following appears before any start_following "
                    "in the sequence",
                    action_path=entry.action_path, loop_context=lc,
                )
            else:
                open_stack.pop()

    if open_stack:
        last_open = open_stack[-1]
        emit_static(
            r, "static.sequence.follow_not_closed",
            "start_following has no matching stop_following — "
            "following will continue until the sequence ends",
            action_path=last_open.action_path,
            loop_context=format_loop_context(last_open.loop_context),
            severity=Severity.WARNING,
        )


def check_loop_expansion_limits(trace: "ExecutionTrace", r: "PreCheckResult") -> None:
    """Reports `trace.stats`'s exceeded-limit messages, if any (see
    `execution_trace.loop_limit_messages`). Callers use
    `trace.stats.{depth_safe,candidates_safe,within_limits}` to decide which
    other checks to skip — this function only reports."""
    for msg in loop_limit_messages(trace.stats):
        emit_static(r, "static.sequence.loop_limit_exceeded", msg)


# ------------------------------------------------------------------ loop-variable helpers

_PLACEHOLDER_VAR_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


def _loop_body_uses_var(actions: list, var: str) -> bool:
    """Return True when `var` is referenced anywhere in a loop body.

    Direct loop-variable references are stored in specific action fields as a
    plain string (for example, SetPressureAction.pressure == "p"). f-string
    references are stored as strings containing "{p}" by SequenceBuilder.
    """
    for action in actions:
        if isinstance(action, ForLoopAction):
            # A nested loop with the same variable name shadows this loop var.
            if action.var == var:
                continue
            if _loop_body_uses_var(action.body, var):
                return True
            continue
        if _action_uses_loop_var(action, var):
            return True
    return False


def _action_uses_loop_var(action: Action, var: str) -> bool:
    return var in _action_loop_var_names(action)


def _action_loop_var_names(action: Action) -> set[str]:
    """Every loop-variable name `action` references: either via its direct
    loop-var field (see actions.LOOP_VAR_FIELDS / action_loop_var_ref) or an
    f-string placeholder such as "{p}" embedded in another string field
    (e.g. a LogAction message written by the DSL parser)."""
    names: set[str] = set()
    ref = action_loop_var_ref(action)
    if ref is not None:
        names.add(ref)
    for value in vars(action).values():
        if isinstance(value, str):
            names.update(_PLACEHOLDER_VAR_RE.findall(value))
    return names
