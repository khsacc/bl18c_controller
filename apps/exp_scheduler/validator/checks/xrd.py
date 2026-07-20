"""
Rad-icon 2022 connectivity check — REORGANISATION_PLAN.md Phase 6
(§7 Phase 6).

Moved from validator/pre_validator.py's `_check_radicon`. Field-level
validation of TakeXrdAction/TakeDarkAction (exposure_ms, oscillation
settings, file overrides) already lives in
`validator/checks/action_params.py::check_xrd_params` (Phase 5) — this file
only checks device presence, via `snapshot.radicon` (there is no liveness
getter on RadiconBackend, so "available" is exactly `ctx.radicon is not
None`, same as before Phase 6).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...actions import TakeDarkAction, TakeXrdAction
from ..execution_trace import ExecutionTrace
from ..models import emit_preflight
from ..snapshots import ValidationSnapshot

if TYPE_CHECKING:
    from ..pre_validator import PreCheckResult

_DEVICE = "radicon"


def check_radicon(
    trace: ExecutionTrace, snapshot: ValidationSnapshot, r: "PreCheckResult"
) -> None:
    flat_actions = [e.action for e in trace.flat]
    if not any(isinstance(a, (TakeXrdAction, TakeDarkAction)) for a in flat_actions):
        return

    if snapshot.radicon is None or not snapshot.radicon.available:
        emit_preflight(
            r, "preflight.radicon.controller_not_connected",
            "Rad-icon 2022 is not connected (required for take_xrd / take_dark)",
            device=_DEVICE,
        )
