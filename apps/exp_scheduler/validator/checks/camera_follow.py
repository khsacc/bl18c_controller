"""
Camera / Follow checks — REORGANISATION_PLAN.md Phase 6 (§7 Phase 6).

Moved from validator/pre_validator.py's `_check_camera`, `_check_calibration`.
Camera is deliberately NOT part of `ValidationSnapshot` (see
`validator/snapshots.py` module docstring and `DeviceContext`'s own
docstring) — each camera action opens its own `cv2.VideoCapture` at run
time, so this file keeps the pre-Phase-6 "open and immediately release" probe
inline rather than folding it into the shared snapshot.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ...actions import (
    FollowSampleAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    StartFollowingAction,
)
from ..execution_trace import ExecutionTrace
from ..models import Severity, emit_preflight

if TYPE_CHECKING:
    from ...scheduler_settings import GlobalFollowSettings
    from ..pre_validator import PreCheckResult

_DEVICE = "camera"

_CALIBRATION_PATH = (
    Path(__file__).parent.parent.parent.parent / "interactive_camera" / "calibration.json"
)


def check_camera(
    trace: ExecutionTrace,
    r: "PreCheckResult",
    global_follow: "GlobalFollowSettings | None" = None,
) -> None:
    flat_actions = [e.action for e in trace.flat]
    camera_actions = [
        a for a in flat_actions
        if isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction, FollowSampleAction))
    ]
    if not camera_actions:
        return

    # Check camera availability (open and immediately release)
    camera_indices: set[int] = set()
    for a in camera_actions:
        camera_indices.add(getattr(a, "camera_index", 0))

    try:
        import cv2
        for idx in camera_indices:
            cap = cv2.VideoCapture(idx)
            opened = cap.isOpened()
            cap.release()
            if not opened:
                emit_preflight(
                    r, "preflight.camera.index_not_openable",
                    f"Camera index {idx} could not be opened", device=_DEVICE,
                )
    except ImportError:
        emit_preflight(
            r, "preflight.camera.opencv_not_installed",
            "opencv-python not installed — camera checks skipped",
            device=_DEVICE, severity=Severity.WARNING,
        )

    # For following actions check calibration and reference image
    follow_actions = [
        a for a in flat_actions
        if isinstance(a, (StartFollowingAction, FollowSampleAction))
    ]
    if follow_actions:
        _check_calibration(r)

        for a in follow_actions:
            ref_path_str = getattr(a, "reference_path", None)
            if ref_path_str is None and global_follow is not None:
                ref_path_str = global_follow.reference_path
            if ref_path_str is None:
                emit_preflight(
                    r, "preflight.camera.follow_reference_not_configured",
                    f"{a.describe()}: no reference image configured — set one via "
                    "Global Settings > Follow Settings > Reference Image, "
                    "or specify reference_path on this step",
                    device=_DEVICE,
                )
                continue
            ref = Path(ref_path_str)
            if not ref.exists():
                emit_preflight(
                    r, "preflight.camera.follow_reference_not_found",
                    f"{a.describe()}: reference image not found: {ref} "
                    "(run Capture Now / Load from… again, or specify reference_path)",
                    device=_DEVICE,
                )


def _check_calibration(r: "PreCheckResult") -> None:
    if not _CALIBRATION_PATH.exists():
        emit_preflight(
            r, "preflight.camera.calibration_file_not_found",
            f"calibration.json not found at {_CALIBRATION_PATH} "
            "(run the calibration procedure in the Interactive Camera app first)",
            device=_DEVICE,
        )
        return
    try:
        data = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        emit_preflight(
            r, "preflight.camera.calibration_parse_error",
            f"calibration.json could not be parsed — {exc}", device=_DEVICE,
        )
        return
    if "matrix_inv" not in data:
        emit_preflight(
            r, "preflight.camera.calibration_missing_matrix_inv",
            "calibration.json has no 'matrix_inv' key — "
            "please re-run the calibration procedure in the Interactive Camera app",
            device=_DEVICE,
        )
