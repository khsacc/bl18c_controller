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
import math
from pathlib import Path
from typing import TYPE_CHECKING

import cv2

from ...actions import (
    AlignToReferenceAction,
    FollowSampleAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    StartFollowingAction,
    TakeSpectrumAction,
)
from ...device_context import DeviceContext
from ...gasket_units import load_stage_um_per_pixel
from ...safety_rules import GASKET_BEAM_CHANNELS
from apps.interactive_camera.gasket_tracking import (
    GasketEllipse, detect_gasket_hole, reference_digest, sample_offset,
)
from apps.interactive_camera.xray_beam import check_xray_beam, load_xray_beam
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
    ctx: DeviceContext | None = None,
) -> None:
    flat_actions = [e.action for e in trace.flat]
    try:
        from utils.stage.control_stage_sim import PM16CControllerSim
        simulated = ctx is not None and isinstance(ctx.controller, PM16CControllerSim)
    except ImportError:
        simulated = False
    camera_actions = [
        a for a in flat_actions
        if isinstance(a, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction, FollowSampleAction,
                          AlignToReferenceAction))
        or isinstance(a, TakeSpectrumAction) and not simulated
    ]
    if not camera_actions:
        return

    # Check camera availability (open and immediately release)
    camera_indices: set[int] = set()
    for a in camera_actions:
        camera_indices.add(getattr(a, "camera_index", 0))
    if len(camera_indices) > 1:
        emit_preflight(
            r, "preflight.camera.multiple_indices",
            "One sequence must use a single camera index; ruby alignment uses camera 0",
            device=_DEVICE,
        )

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
        if isinstance(a, (StartFollowingAction, FollowSampleAction, AlignToReferenceAction))
        or isinstance(a, TakeSpectrumAction) and not simulated
    ]
    if follow_actions:
        _check_calibration(r)

        for a in follow_actions:
            ref_path_str = getattr(a, "reference_path", None)
            if ref_path_str is None and global_follow is not None:
                ref_path_str = global_follow.reference_path
            if ref_path_str is None:
                location = (
                    "Global Settings > Sample Positions > Set up sample positions "
                    "(or Reference Image > Load from…)"
                    if isinstance(a, TakeSpectrumAction)
                    else "Global Settings > Sample Positions > Set up sample positions "
                         "(or Reference Image > Load from…), "
                         "or specify reference_path on this step"
                )
                emit_preflight(
                    r, "preflight.camera.follow_reference_not_configured",
                    f"{a.describe()}: no reference image configured — set one via "
                    f"{location}",
                    device=_DEVICE,
                )
                continue
            ref = Path(ref_path_str)
            if not ref.exists():
                remedy = (
                    "capture or load the XRD reference again"
                    if isinstance(a, TakeSpectrumAction)
                    else "capture in Set up sample positions, load via Reference Image > "
                         "Load from…, or specify reference_path"
                )
                emit_preflight(
                    r, "preflight.camera.follow_reference_not_found",
                    f"{a.describe()}: reference image not found: {ref} "
                    f"({remedy})",
                    device=_DEVICE,
                )
                continue
            if (global_follow is not None
                    and global_follow.tracking_method == "gasket_beta"
                    and isinstance(a, (StartFollowingAction, FollowSampleAction,
                                       AlignToReferenceAction, TakeSpectrumAction))):
                try:
                    use_um = global_follow.gasket_tolerance_um is not None
                    tolerance = float(global_follow.gasket_tolerance_um if use_um
                                      else global_follow.gasket_tolerance_px)
                    if not math.isfinite(tolerance) or tolerance <= 0:
                        raise ValueError(f"must be a positive finite {'µm' if use_um else 'pixel'} distance")
                except (TypeError, ValueError) as exc:
                    emit_preflight(
                        r, "preflight.camera.gasket_tolerance_invalid",
                        f"Gasket Beta tolerance is invalid: {exc}", device=_DEVICE,
                    )
                    continue
                try:
                    load_stage_um_per_pixel(_CALIBRATION_PATH)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    emit_preflight(
                        r, "preflight.camera.calibration_invalid",
                        f"Gasket Beta needs a valid camera calibration: {exc}",
                        device=_DEVICE,
                    )
                    continue
                action_reference = getattr(a, "reference_path", None)
                if action_reference is not None and str(action_reference) != str(global_follow.reference_path):
                    emit_preflight(
                        r, "preflight.camera.gasket_reference_override",
                        "Gasket Beta requires the annotated Global Settings reference image",
                        device=_DEVICE,
                    )
                    continue
                try:
                    marked = GasketEllipse.from_dict(global_follow.gasket_ellipse)
                    if reference_digest(ref) != global_follow.gasket_reference_digest:
                        raise ValueError("gasket outline belongs to another reference image")
                    image = cv2.imread(str(ref), cv2.IMREAD_COLOR)
                    if image is None:
                        raise ValueError("reference image cannot be decoded")
                    detected = detect_gasket_hole(image, image, marked)
                    if not global_follow.gasket_sample_point:
                        raise ValueError("mark the sample centre with Mark gasket outline")
                    sample_offset(global_follow.gasket_sample_point,
                                  (detected.ellipse.cx, detected.ellipse.cy))
                except (ValueError, KeyError, TypeError, OSError, cv2.error) as exc:
                    emit_preflight(
                        r, "preflight.camera.gasket_outline_invalid",
                        f"Gasket Beta reference is not usable: {exc}", device=_DEVICE,
                    )
                    continue
                # Ch6/Ch8 are compared at run time, where the sequence may
                # have moved the microscope arm in and out.
                try:
                    check_xray_beam(load_xray_beam(), image.shape,
                                    channels=GASKET_BEAM_CHANNELS)
                except ValueError as exc:
                    emit_preflight(
                        r, "preflight.camera.xray_beam_invalid",
                        f"Gasket Beta: {exc}", device=_DEVICE,
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
