"""Diagnostic gasket-outline overlay shared by the Scheduler Monitor and
Interactive Camera's Gasket Detection tab. Qt-independent; never moves the
stage.

Colours (BGR): cyan marked reference outline, green detected outline and
centre, magenta sample point, red X-ray beam.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .gasket_tracking import (
    GasketDetection, GasketEllipse, detect_gasket_hole, detect_gasket_hole_near,
    sample_offset,
)
from .gasket_units import displacement_um, distance_um, nominal_um_per_pixel
from .xray_beam import draw_xray_beam

REFERENCE_COLOUR = (255, 255, 0)
DETECTED_COLOUR = (60, 255, 60)
SAMPLE_COLOUR = (255, 0, 255)
FAILED_COLOUR = (80, 80, 255)


@dataclass(frozen=True)
class OverlayReference:
    """Annotated reference used by the overlay."""
    frame: np.ndarray
    marked: GasketEllipse
    sample_offset: tuple[float, float] | None
    allow_uniform_scale: bool = False


def load_overlay_reference(
    reference_path: str | None, ellipse: dict | None,
    sample_point: dict | None = None, allow_uniform_scale: bool = False,
) -> OverlayReference | None:
    """Load the reference photo and mark, or None if they cannot be used."""
    if reference_path is None or ellipse is None:
        return None
    try:
        frame = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
        if frame is None:
            return None
        marked = GasketEllipse.from_dict(ellipse)
    except (ValueError, KeyError, TypeError, cv2.error):
        return None
    offset = None
    if sample_point:
        try:
            found = detect_gasket_hole(frame, frame, marked,
                                       allow_uniform_scale=allow_uniform_scale)
            offset = sample_offset(sample_point, (found.ellipse.cx, found.ellipse.cy))
        except (ValueError, KeyError, TypeError, cv2.error):
            pass
    return OverlayReference(frame, marked, offset, allow_uniform_scale)


def overlay_target(beam, offset: tuple[float, float] | None) -> tuple[float, float] | None:
    """Rim centre that puts the marked sample on the X-ray beam."""
    if beam is None or offset is None:
        return None
    return beam.x - offset[0], beam.y - offset[1]


def detect(
    frame: np.ndarray, reference: np.ndarray, marked: GasketEllipse,
    target: tuple[float, float] | None, allow_uniform_scale: bool,
) -> tuple[GasketDetection | None, str | None]:
    """One detection for display: (detection, None) or (None, error)."""
    try:
        return detect_gasket_hole_near(
            reference, frame, marked, (target, None),
            allow_uniform_scale=allow_uniform_scale), None
    except Exception as exc:
        return None, str(exc)


def describe_offset(
    detection: GasketDetection, target: tuple[float, float] | None,
    marked: GasketEllipse, um_matrix: np.ndarray | None,
) -> str:
    """Offset, score, coverage and residual of one detection."""
    if target is None:
        target = (marked.cx, marked.cy)
        what = "offset"
    else:
        what = "sample to X-ray beam (red)"
    offset_x = detection.ellipse.cx - target[0]
    offset_y = detection.ellipse.cy - target[1]
    offset_physical = residual_physical = ""
    if um_matrix is not None:
        ux, uy = displacement_um(um_matrix, offset_x, offset_y)
        residual_um = detection.residual_px * nominal_um_per_pixel(um_matrix)
        offset_physical = (f" / ({ux:+.1f}, {uy:+.1f}) µm "
                           f"(|offset|={distance_um(um_matrix, offset_x, offset_y):.1f} µm)")
        residual_physical = f" / ~{residual_um:.1f} µm"
    return (f"{what} ({offset_x:+.1f}, {offset_y:+.1f}) px{offset_physical}, "
            f"score {detection.score:.2f}, coverage {detection.coverage}/8, "
            f"residual {detection.residual_px:.1f} px{residual_physical}")


def describe_detection(detection: GasketDetection | None, error: str | None) -> str:
    """Short result of a live (idle) detection test."""
    if detection is None:
        return f"not detected ({error})"
    return (f"score {detection.score:.2f}, coverage {detection.coverage}/8, "
            f"residual {detection.residual_px:.1f} px")


def draw_overlay(
    image: np.ndarray, marked: GasketEllipse | None,
    detection: GasketDetection | None, offset: tuple[float, float] | None,
    beam, *, test_label: bool = False,
) -> None:
    """Draw the gasket overlay in place on a BGR display copy."""
    if marked is not None:
        cv2.ellipse(image, (round(marked.cx), round(marked.cy)),
                    (round(marked.rx), round(marked.ry)), 0, 0, 360, REFERENCE_COLOUR, 1)
    if detection is not None:
        e = detection.ellipse
        cv2.ellipse(image, (round(e.cx), round(e.cy)),
                    (round(e.rx), round(e.ry)), 0, 0, 360, DETECTED_COLOUR, 2)
        cv2.drawMarker(image, (round(e.cx), round(e.cy)), DETECTED_COLOUR,
                       cv2.MARKER_CROSS, 16, 2)
        if offset is not None:
            cv2.drawMarker(image, (round(e.cx + offset[0]), round(e.cy + offset[1])),
                           SAMPLE_COLOUR, cv2.MARKER_CROSS, 20, 2)
    if test_label:
        label = "Gasket edge: detected" if detection is not None else "Gasket edge: not detected"
        colour = DETECTED_COLOUR if detection is not None else FAILED_COLOUR
        cv2.putText(image, label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(image, label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, colour, 2, cv2.LINE_AA)
    if beam is not None:
        draw_xray_beam(image, beam.point)
