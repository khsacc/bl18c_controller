"""Gasket-outline sample following shared by Interactive Camera and the
Experimental Scheduler. Qt-independent.

The detector itself lives in ``gasket_tracking.py``. This module turns its
detections into stage corrections: it validates the annotated reference,
computes the rim centre that puts the marked sample on the X-ray beam,
averages detections over fresh frames, and runs bounded correction cycles.

Everything that depends on the host (camera frames, limited stage moves,
Ch3 autofocus, logging, stop requests) is passed in, so each app keeps its
own safety limits and motion ownership.

A follow configuration is a plain ``dict`` (see ``resolve_reference``) with,
in addition, the keys used by the correction cycle:

``M_inv``           2×2 calibration matrix (camera px → Ch4/Ch5 pulses)
``lim4`` / ``lim5`` per-attempt correction limit in pulses
``xy_max_retries``  XY re-corrections after the first one
``af_enabled``      run Ch3 autofocus in the cycle
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable

import numpy as np

from .gasket_tracking import (
    GasketEllipse, detect_gasket_hole, detect_gasket_hole_near, reference_digest,
    sample_offset,
)
from .gasket_units import (
    CALIBRATION_PATH, GASKET_BEAM_CHANNELS, displacement_um, distance_um,
    load_stage_um_per_pixel, nominal_um_per_pixel, stage_um_per_pixel,
)
from .xray_beam import check_xray_beam, load_xray_beam, read_microscope_pulses


DETECTION_FAILED = "runtime.gasket_detection_failed"
ALIGNMENT_FAILED = "runtime.gasket_alignment_failed"
BEAM_INVALID = "runtime.xray_beam_invalid"

# Frame coherence radius for averaging; describes detector noise and is
# independent of the operator's positioning tolerance.
COHERENCE_RADIUS_PX = 12.0


class GasketFollowError(RuntimeError):
    """A gasket-following failure with a stable ``code`` (see constants)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ reference

def resolve_reference(
    *, reference_path: str | Path, reference_frame: np.ndarray,
    ellipse: dict | None, digest: str | None, sample_point: dict | None,
    tolerance_um: float | None, tolerance_px: float | None = None,
    average_frames: int = 5, allow_uniform_scale: bool = False,
    matrix_inv=None,
) -> dict:
    """Validate an annotated reference photo and return a gasket config.

    Raises ``RuntimeError`` with an operator-facing message. The X-ray beam
    is checked only against the reference image size here; ``gasket_target``
    re-reads it, with the live microscope position, at every measurement.
    ``tolerance_px`` is only used by old saved settings without a µm value.
    """
    if tolerance_um is not None:
        try:
            tolerance_um = float(tolerance_um)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Gasket tolerance must be a positive finite µm distance") from exc
        if not math.isfinite(tolerance_um) or tolerance_um <= 0:
            raise RuntimeError("Gasket tolerance must be a positive finite µm distance")
    else:
        try:
            tolerance_px = float(tolerance_px)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Legacy gasket tolerance must be a positive finite pixel distance") from exc
        if not math.isfinite(tolerance_px) or tolerance_px <= 0:
            raise RuntimeError("Legacy gasket tolerance must be a positive finite pixel distance")
    try:
        um_matrix = (stage_um_per_pixel(matrix_inv) if matrix_inv is not None
                     else load_stage_um_per_pixel(CALIBRATION_PATH))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Cannot use camera calibration for gasket tracking: {exc}") from exc
    frames = average_frames
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 10:
        raise RuntimeError("Gasket average frame count must be between 1 and 10")
    if not ellipse or not digest:
        raise RuntimeError("Mark the gasket outline in Sample Positions before using gasket beta")
    if reference_digest(reference_path) != digest:
        raise RuntimeError("Gasket outline belongs to a different reference image; mark it again")
    if not sample_point:
        raise RuntimeError(
            "Mark the sample centre with Mark gasket outline before using gasket beta"
        )
    marked = GasketEllipse.from_dict(ellipse)
    try:
        detection = detect_gasket_hole(reference_frame, reference_frame, marked)
        centre = (detection.ellipse.cx, detection.ellipse.cy)
        offset = sample_offset(sample_point, centre)
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Gasket reference cannot be detected: {exc}") from exc
    try:
        check_xray_beam(load_xray_beam(), reference_frame.shape,
                        channels=GASKET_BEAM_CHANNELS)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return {
        "tracking_method": "gasket_beta",
        "reference_frame": reference_frame,
        "gasket_ellipse": ellipse,
        "gasket_allow_uniform_scale": allow_uniform_scale,
        "gasket_reference_centre": centre,
        "gasket_sample_offset": offset,
        "gasket_tolerance_px": tolerance_px,
        "gasket_tolerance_um": tolerance_um,
        "gasket_um_matrix": um_matrix,
        "gasket_average_frames": frames,
    }


# ------------------------------------------------------------------ measurement

def um_matrix(cfg: dict) -> np.ndarray:
    matrix = cfg.get("gasket_um_matrix")
    if matrix is not None:
        return matrix
    # Direct callers of the measurement helpers may provide only M_inv.
    if cfg.get("M_inv") is not None:
        return stage_um_per_pixel(cfg["M_inv"])
    return load_stage_um_per_pixel(CALIBRATION_PATH)


def offset_text(cfg: dict, dx: float, dy: float) -> str:
    ux, uy = displacement_um(um_matrix(cfg), dx, dy)
    return (f"({dx:+.1f}, {dy:+.1f}) px / "
            f"({ux:+.1f}, {uy:+.1f}) µm "
            f"(|offset|={math.hypot(dx, dy):.1f} px / {math.hypot(ux, uy):.1f} µm)")


def tolerance_text(cfg: dict) -> str:
    return (f"{cfg['gasket_tolerance_um']:.1f} µm"
            if cfg.get("gasket_tolerance_um") is not None
            else f"{cfg['gasket_tolerance_px']:.1f} px")


def accepted(cfg: dict, dx: float, dy: float) -> bool:
    """True when the averaged rim-centre offset is within the tolerance."""
    if cfg.get("gasket_tolerance_um") is not None:
        return distance_um(um_matrix(cfg), dx, dy) <= cfg["gasket_tolerance_um"]
    return math.hypot(dx, dy) <= cfg["gasket_tolerance_px"]


def gasket_target(cfg: dict, controller, log: Callable[[str], None]) -> tuple[float, float]:
    """Rim centre at which the marked sample sits on the X-ray beam.

    Reads the shared beam file and the live Ch6/Ch8 each time, so a
    re-registered beam is used at once and a moved microscope stops the
    alignment instead of steering to a stale pixel.
    """
    try:
        pulses = read_microscope_pulses(
            controller, require_stopped=False, channels=GASKET_BEAM_CHANNELS,
        )
        beam = check_xray_beam(load_xray_beam(), cfg["reference_frame"].shape,
                               pulses, channels=GASKET_BEAM_CHANNELS)
    except (RuntimeError, ValueError) as exc:
        raise GasketFollowError(BEAM_INVALID, str(exc)) from exc
    ox, oy = cfg["gasket_sample_offset"]
    target = (beam.x - ox, beam.y - oy)
    if cfg.get("gasket_beam") != beam.point:
        log(f"[GASKET] X-ray beam=({beam.x:.1f},{beam.y:.1f}) "
            f"sample offset=({ox:+.1f},{oy:+.1f}) target rim centre="
            f"({target[0]:.1f},{target[1]:.1f})")
    cfg["gasket_beam"] = beam.point
    cfg["gasket_target_centre"] = target
    return target


def measure_frame(
    cfg: dict, frame: np.ndarray, target: tuple[float, float],
    log: Callable[[str], None],
) -> tuple[float, float, float]:
    """Return the rim-centre offset from ``target`` and the match score."""
    # Search at the target first; the reference location covers a stage
    # that has not yet been moved since the reference was captured.
    marked = GasketEllipse.from_dict(cfg["gasket_ellipse"])
    try:
        detection = detect_gasket_hole_near(
            cfg["reference_frame"], frame, marked,
            (target, cfg.get("gasket_last_centre"), cfg["gasket_reference_centre"]),
            allow_uniform_scale=cfg.get("gasket_allow_uniform_scale", False),
        )
    except ValueError as exc:
        raise GasketFollowError(DETECTION_FAILED, str(exc)) from exc
    cfg["gasket_last_centre"] = (detection.ellipse.cx, detection.ellipse.cy)
    log(f"[GASKET] centre=({detection.ellipse.cx:.1f},{detection.ellipse.cy:.1f}) "
        f"score={detection.score:.3f} coverage={detection.coverage}/8 "
        f"residual={detection.residual_px:.1f}px "
        f"(~{detection.residual_px * nominal_um_per_pixel(um_matrix(cfg)):.1f}µm)")
    return (detection.ellipse.cx - target[0],
            detection.ellipse.cy - target[1], detection.score)


def live_measure_average(
    cfg: dict, purpose: str, *, controller,
    get_frame: Callable[[str], np.ndarray], log: Callable[[str], None],
) -> tuple[float, float, float]:
    """``measure_average`` aimed at the live beam target (for hosts without
    their own measurement wrappers, such as Interactive Camera)."""
    def target(c: dict) -> tuple[float, float]:
        return gasket_target(c, controller, log)

    return measure_average(
        cfg, purpose, get_frame=get_frame,
        measure=lambda c, frame: measure_frame(
            c, frame, c.get("gasket_target_centre") or target(c), log),
        log=log, refresh_target=target,
    )


def measure_average(
    cfg: dict, purpose: str, *,
    get_frame: Callable[[str], np.ndarray],
    measure: Callable[[dict, np.ndarray], tuple[float, float, float]],
    log: Callable[[str], None],
    refresh_target: Callable[[dict], object] | None = None,
) -> tuple[float, float, float]:
    """Average coherent detections from the configured fresh live frames.

    A moving rim must be detected before averaging: averaging the pixels
    would blur the edge. Require a majority of detections near their median
    so a transient bad fit cannot command a stage move or pass verification.
    ``measure`` failures whose ``code`` is ``DETECTION_FAILED`` count as
    missing frames; any other exception propagates. ``refresh_target``
    (normally ``gasket_target``) runs once first when the config has a
    sample offset, so every batch uses the current beam and microscope.
    """
    samples: list[tuple[float, float, float]] = []
    errors: list[str] = []
    frame_count = cfg.get("gasket_average_frames", 5)
    if (isinstance(frame_count, bool) or not isinstance(frame_count, int)
            or not 1 <= frame_count <= 10):
        raise GasketFollowError(
            DETECTION_FAILED, "Gasket average frame count must be between 1 and 10",
        )
    required = frame_count // 2 + 1
    if refresh_target is not None and "gasket_sample_offset" in cfg:
        refresh_target(cfg)
    for _ in range(frame_count):
        frame = get_frame(purpose)
        try:
            samples.append(measure(cfg, frame))
        except Exception as exc:
            if getattr(exc, "code", None) != DETECTION_FAILED:
                raise
            errors.append(str(exc))
    if len(samples) < required:
        raise GasketFollowError(
            DETECTION_FAILED,
            f"Only {len(samples)}/{frame_count} gasket frames were detected for {purpose}: "
            f"{errors[-1] if errors else 'insufficient valid frames'}",
        )
    xy = np.asarray([(dx, dy) for dx, dy, _score in samples], dtype=float)
    median = np.median(xy, axis=0)
    deviations = xy - median
    distances = np.linalg.norm(deviations, axis=1)
    matrix = um_matrix(cfg)
    um_distances = np.linalg.norm(deviations @ matrix.T, axis=1)
    if cfg.get("gasket_tolerance_um") is not None:
        inliers = um_distances <= COHERENCE_RADIUS_PX * nominal_um_per_pixel(matrix)
    else:
        inliers = distances <= COHERENCE_RADIUS_PX
    if int(np.count_nonzero(inliers)) < required:
        raise GasketFollowError(
            DETECTION_FAILED,
            f"Gasket centres varied too much across {frame_count} frames for {purpose} "
            f"({int(np.count_nonzero(inliers))} coherent frames; "
            f"spread up to {float(np.max(distances)):.1f} px / "
            f"{float(np.max(um_distances)):.1f} µm)",
        )
    centre = np.mean(xy[inliers], axis=0)
    scores = np.asarray([score for _dx, _dy, score in samples])
    score = float(np.mean(scores[inliers]))
    spread = float(np.max(distances[inliers]))
    spread_um = float(np.max(um_distances[inliers]))
    dx, dy = float(centre[0]), float(centre[1])
    log(f"[GASKET AVG] {purpose}: offset={offset_text(cfg, *centre)} "
        f"score={score:.3f} coherent={int(np.count_nonzero(inliers))}/{frame_count} "
        f"spread={spread:.1f}px / {spread_um:.1f}µm")
    return dx, dy, score


# ------------------------------------------------------------------ correction

@dataclass
class FollowHooks:
    """Host operations used by the correction cycle.

    measure_average(cfg, purpose) -> (dx, dy, score)
        averaged rim-centre offset from the target, in camera px.
    move_xy(proposed_ch4, proposed_ch5, lim4, lim5) -> (applied_ch4, applied_ch5)
        apply all host limits, move, wait for the stage and return the
        pulses actually moved.
    autofocus()
        Ch3 autofocus.
    aborted() -> bool
        a hard stop was requested; the cycle returns early.
    log(msg) / progress(msg)
        detailed log line / short status line.
    """
    measure_average: Callable[[dict, str], tuple[float, float, float]]
    move_xy: Callable[[int, int, int, int], tuple[int, int]]
    autofocus: Callable[[], None]
    aborted: Callable[[], bool]
    log: Callable[[str], None]
    progress: Callable[[str], None]


def _pulses(cfg: dict, dx: float, dy: float) -> tuple[int, int]:
    # Offsets keep fractional pixels; only the motor command is truncated.
    disp = -(np.asarray(cfg["M_inv"]) @ np.array([dx, dy]))
    return int(disp[0]), int(disp[1])


def correction_cycle(
    cfg: dict, cumulative: dict[int, int], hooks: FollowHooks, *,
    pre_focus: bool = False, post_focus: bool = True,
) -> None:
    """One correction cycle: optional Ch3 pre-focus, an XY correction from
    the averaged offset, bounded XY retries until the offset is within the
    tolerance, then optional Ch3 autofocus.

    The first measured offset is corrected whenever it maps to at least one
    whole pulse, even inside the tolerance; the tolerance only ends retries.
    ``cumulative`` ({4: ..., 5: ...}) is increased by the applied pulses.
    """
    lim4, lim5 = cfg["lim4"], cfg["lim5"]
    xy_max_retries = cfg["xy_max_retries"]
    if pre_focus and cfg["af_enabled"]:
        hooks.autofocus()

    dx, dy, score = hooks.measure_average(cfg, "follow_sample_position")
    hooks.progress(f"[follow] Gasket offset: {offset_text(cfg, dx, dy)}; score={score:.3f}")
    p4, p5 = _pulses(cfg, dx, dy)
    d4, d5 = hooks.move_xy(p4, p5, lim4, lim5)
    if d4 or d5:
        cumulative[4] += d4
        cumulative[5] += d5
        hooks.log(
            f"[FOLLOW] correction: Δ Ch4={d4:+d} Ch5={d5:+d} | "
            f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d} | "
            f"gasket score={score:.3f}"
        )
        hooks.progress(
            f"[follow] Δ Ch4={d4:+d} Ch5={d5:+d} | "
            f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d}"
        )
    else:
        hooks.log(
            f"[FOLLOW] no XY move: requested Ch4={p4:+d} Ch5={p5:+d} pulses; "
            "both applied corrections are zero after pulse quantisation and limits"
        )
    if hooks.aborted():
        return

    for retry in range(max(0, xy_max_retries)):
        if hooks.aborted():
            break
        dx, dy, score = hooks.measure_average(cfg, "follow XY retry")
        if accepted(cfg, dx, dy):
            break
        hooks.progress(
            f"[follow] Gasket offset {offset_text(cfg, dx, dy)} — "
            f"re-correcting XY (attempt {retry + 1}/{xy_max_retries})"
        )
        d4, d5 = hooks.move_xy(*_pulses(cfg, dx, dy), lim4, lim5)
        if d4 or d5:
            cumulative[4] += d4
            cumulative[5] += d5
            hooks.log(
                f"[FOLLOW] retry {retry + 1}: Δ Ch4={d4:+d} Ch5={d5:+d} | "
                f"cumul Ch4={cumulative[4]:+d} Ch5={cumulative[5]:+d}"
            )

    if hooks.aborted():
        return
    if post_focus and cfg["af_enabled"]:
        hooks.autofocus()


def align_and_verify(
    cfg: dict, cumulative: dict[int, int], hooks: FollowHooks, *,
    pre_focus: bool = False,
    verify_purpose: str, retry_purpose: str,
    cycle: Callable[..., None] | None = None,
) -> tuple[float, float, float] | None:
    """Run a correction cycle and verify the final offset.

    If autofocus moved the measured centre outside the tolerance, one more
    XY cycle runs without autofocus. Raises ``GasketFollowError``
    (``ALIGNMENT_FAILED``) if the offset is still too large. Returns None
    when ``hooks.aborted()`` became true. ``cycle`` overrides
    ``correction_cycle`` (same signature) for hosts that wrap it.
    """
    run_cycle = cycle or (lambda c, cum, **kw: correction_cycle(c, cum, hooks, **kw))
    run_cycle(cfg, cumulative, pre_focus=pre_focus)
    if hooks.aborted():
        return None
    dx, dy, score = hooks.measure_average(cfg, verify_purpose)
    if not accepted(cfg, dx, dy):
        run_cycle(cfg, cumulative, post_focus=False)
        if hooks.aborted():
            return None
        dx, dy, score = hooks.measure_average(cfg, retry_purpose)
    if not accepted(cfg, dx, dy):
        raise GasketFollowError(
            ALIGNMENT_FAILED,
            f"Gasket centre remains {offset_text(cfg, dx, dy)} "
            f"from reference (limit {tolerance_text(cfg)})",
        )
    return dx, dy, score
