"""
Pure MOVE_CONSTRAINTS evaluator — the single source of truth for the
inter-channel collision rules (currently: detector Ch9 vs. microscope arm
Ch8; commented-out Ch8 vs. rotation stage Ch11).

Before this module existed, ``PM16CController``, ``PM16CControllerSim``,
and the Experimental Scheduler ``PreValidator`` each carried their own copy
of the same rule-matching loop against three different position sources —
live wire reads (real controller), an in-memory lock-protected dict
(simulator), and a pre-collected snapshot dict (PreValidator) — so a future
schema change had to be applied in four places by hand. This module holds
the one implementation; the call sites below are thin compatibility
wrappers around it. See apps/exp_scheduler/REORGANISATION_PLAN.md §3.6 /
Phase 4, and utils/stage/IMPLEMENTATION_DETAILS.md for the rule schema and
current boundary values.

    - PM16CController._check_move_constraints_using()
    - PM16CControllerSim._check_move_constraints_locked()
    - apps.exp_scheduler.validator.pre_validator (snapshot_violations /
      move_violations, called via the ``move_constraints`` module rather
      than a local re-implementation)

This module performs no device I/O itself — every function takes position
data as plain values or through an injected ``read_pos`` callable, so it is
unit-testable without any fake device.
"""
from __future__ import annotations

from operator import eq, ge, gt, le, lt
from typing import Callable, Mapping, Optional

# ---------------------------------------------------------------------------
# Move constraints (inter-channel software limits)
#
# Each rule is evaluated before every absolute or relative move.
# If the intended target position of `target_ch` satisfies (`target_op`,
# `target_val`), then the *current* position of `required_ch` must satisfy
# (`required_op`, `required_val`) — otherwise the move is rejected.
# `target_op`/`target_val` may be omitted entirely to make a rule
# unconditional — it then applies to every move of `target_ch`, regardless
# of the requested target position (used below for Ch11, where any rotation
# is unsafe while Ch8 is extended, not just rotation past some threshold).
#
# To add a new constraint, append a dict with the keys shown above.
# ---------------------------------------------------------------------------
# Collision boundary between the Detector (Ch9) and Microscope arm (Ch8).
# Ch9 must be at or beyond this pulse position (i.e. ≤ value) before Ch8 can
# move into the beam path (positive direction), and vice versa.
# This constant is the single source of truth: MOVE_CONSTRAINTS below and all
# UI-level validation code import or reference it (re-exported from
# utils.stage.control_stage for existing importers).
CH9_CH8_SAFE_BOUNDARY = -30000

# Ch8 pulse position beyond which a rotating Ch11 (or a further-IN Ch8 move)
# risks colliding with the rotation stage. Ch8 does not conflict with Ch11
# immediately at Ch8 > 0 — there is some real mechanical margin before an
# actual collision is possible. NOT YET VERIFIED against real BL-18C
# hardware; re-check/adjust after hardware testing (see
# utils/stage/IMPLEMENTATION_DETAILS.md).
CH8_CH11_CONFLICT_BOUNDARY = 0

# Ch11 pulse range considered non-colliding while Ch8 is extended past
# CH8_CH11_CONFLICT_BOUNDARY (inclusive min, max). Not just exact θ=0° —
# real arm geometry likely tolerates some angular margin. NOT YET VERIFIED;
# re-check/adjust after hardware testing.
CH11_SAFE_RANGE_PULSES = (0, 0)

MOVE_CONSTRAINTS = [
    # Ch9 > CH9_CH8_SAFE_BOUNDARY requires Ch8 <= 0
    # Moving Ch9 TO the boundary or more negative (OUT direction) is always safe.
    # Only moving Ch9 INTO the beam path is restricted.
    {
        'target_ch': 9, 'target_op': '>', 'target_val': CH9_CH8_SAFE_BOUNDARY,
        'required': [
            {'ch': 8, 'op': '<=', 'val': 0},
        ],
    },
    # Ch8 > 0 requires Ch9 <= CH9_CH8_SAFE_BOUNDARY
    # Moving Ch8 TO 0 or more negative (OUT direction) is always safe.
    # Only moving Ch8 INTO the beam path is restricted.
    {
        'target_ch': 8, 'target_op': '>', 'target_val': 0,
        'required': [
            {'ch': 9, 'op': '<=', 'val': CH9_CH8_SAFE_BOUNDARY},
        ],
    },
    # Ch11 (rotation) may move only while Ch8 is retracted past the conflict
    # boundary. Unconditional: any rotation while Ch8 is extended is unsafe,
    # not just rotation toward a particular direction.
    # {
    #     'target_ch': 11,
    #     'required': [
    #         {'ch': 8, 'op': '<=', 'val': CH8_CH11_CONFLICT_BOUNDARY},
    #     ],
    # },
    # # Ch8 may extend past the conflict boundary only while Ch11 sits within
    # # CH11_SAFE_RANGE_PULSES of its home/zero position.
    # {
    #     'target_ch': 8, 'target_op': '>', 'target_val': CH8_CH11_CONFLICT_BOUNDARY,
    #     'required': [
    #         {'ch': 11, 'op': '>=', 'val': CH11_SAFE_RANGE_PULSES[0]},
    #         {'ch': 11, 'op': '<=', 'val': CH11_SAFE_RANGE_PULSES[1]},
    #     ],
    # },
]

_OPS = {'>=': ge, '<=': le, '>': gt, '<': lt, '==': eq}

# read_pos(ch) -> the current position of `ch` as a decimal string (matching
# the PM16C wire format), or None if unreadable. None is fail-closed: it is
# always treated as a violation, exactly like the real controller refusing
# to move blind when a required companion channel can't be read.
ReadPos = Callable[[int], Optional[str]]


def _rule_violations(rule: dict, target_pos: int, read_pos: ReadPos):
    """Yield (req_ch, req_op, req_val, req_pos) for every broken entry of
    ``rule['required']``, given ``target_pos`` already satisfies (or the
    rule is unconditional on) ``rule``'s target_op/target_val gate.
    ``req_pos`` is None when ``read_pos`` could not read that channel."""
    target_op = rule.get('target_op')
    if target_op is not None and not _OPS[target_op](target_pos, rule['target_val']):
        return
    for req in rule['required']:
        req_str = read_pos(req['ch'])
        if req_str is None:
            yield req['ch'], req['op'], req['val'], None
            continue
        req_pos = int(req_str)
        if not _OPS[req['op']](req_pos, req['val']):
            yield req['ch'], req['op'], req['val'], req_pos


def _move_message(ch: int, target_pos: int, req_ch: int, req_op: str, req_val: int,
                   req_pos: Optional[int]) -> str:
    if req_pos is None:
        return (
            f"Cannot read Ch{req_ch} position "
            f"(required for limit check on Ch{ch})"
        )
    return (
        f"Move blocked: Ch{ch} → {target_pos:+} requires "
        f"Ch{req_ch} {req_op} {req_val:+}, but current position is {req_pos:+}"
    )


def _snapshot_message(ch: int, target_pos: int, req_ch: int, req_op: str, req_val: int,
                       req_pos: Optional[int]) -> str:
    if req_pos is None:
        return (
            f"Cannot read Ch{req_ch} position "
            f"(required for limit check on Ch{ch})"
        )
    return (
        f"Ch{ch}={target_pos:+} requires Ch{req_ch} {req_op} {req_val:+}, "
        f"but Ch{req_ch}={req_pos:+}"
    )


def check_move(ch: int, target_pos: int, read_pos: ReadPos) -> "tuple[bool, str]":
    """First-violation-stops MOVE_CONSTRAINTS check, run synchronously
    before sending a real move. Used by both PM16CController (live wire
    reads) and PM16CControllerSim (locked in-memory reads).

    Returns (True, "") when safe, (False, reason) on the first violation
    found (rule order, then required-entry order — matching the pre-Phase-4
    behaviour of both controllers)."""
    for rule in MOVE_CONSTRAINTS:
        if rule['target_ch'] != ch:
            continue
        for violation in _rule_violations(rule, target_pos, read_pos):
            return False, _move_message(ch, target_pos, *violation)
    return True, ""


def list_move_violations(positions: Mapping[int, int], ch: int, target_pos: int) -> "list[str]":
    """All MOVE_CONSTRAINTS violations for a prospective move of `ch` to
    `target_pos`, evaluated against an already-collected position snapshot.

    Unlike check_move(), this does not stop at the first violation — used by
    PreValidator's step-by-step sequence simulation, which wants to report
    every problem at a given step, not just the first."""
    read_pos: ReadPos = lambda rch: (str(positions[rch]) if rch in positions else None)
    out: "list[str]" = []
    for rule in MOVE_CONSTRAINTS:
        if rule['target_ch'] != ch:
            continue
        for violation in _rule_violations(rule, target_pos, read_pos):
            out.append(_move_message(ch, target_pos, *violation))
    return out


def list_snapshot_violations(positions: Mapping[int, int]) -> "list[str]":
    """All MOVE_CONSTRAINTS violations already present in a full position
    snapshot — i.e. "is this snapshot self-consistent", not "would a
    prospective move be safe". Each rule's target_ch is evaluated against
    its own recorded position in `positions` rather than a hypothetical
    move target. Used by PreValidator to check the current stage state
    before simulating any moves."""
    read_pos: ReadPos = lambda rch: (str(positions[rch]) if rch in positions else None)
    out: "list[str]" = []
    for rule in MOVE_CONSTRAINTS:
        target_pos = positions.get(rule['target_ch'])
        if target_pos is None:
            continue
        for violation in _rule_violations(rule, target_pos, read_pos):
            out.append(_snapshot_message(rule['target_ch'], target_pos, *violation))
    return out
