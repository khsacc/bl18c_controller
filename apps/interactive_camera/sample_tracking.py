"""Frame-comparison helpers shared by interactive_camera.py's Sample Tracking
tab and exp_scheduler's follow/autofocus loop. Qt-independent."""
import cv2
import numpy as np


def compute_xy_shift(ref: np.ndarray, current: np.ndarray) -> tuple[int, int]:
    """Template-match `current` against the central crop of `ref` and return
    the (dx, dy) pixel shift, or (0, 0) if the match confidence is too low."""
    ref_g = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    cur_g = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    h, w = ref_g.shape
    my, mx = h // 5, w // 5
    template = ref_g[my:h - my, mx:w - mx]
    result = cv2.matchTemplate(cur_g, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < 0.3:
        return 0, 0
    return max_loc[0] - mx, max_loc[1] - my


def compute_similarity(ref: np.ndarray, current: np.ndarray) -> float:
    """Template-match similarity of `current` against `ref` (TM_CCOEFF_NORMED,
    0-1 range; 1.0 = perfect match), resizing `current` to match `ref` first
    if their shapes differ."""
    ref_g = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    cur_g = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    if ref_g.shape != cur_g.shape:
        cur_g = cv2.resize(cur_g, (ref_g.shape[1], ref_g.shape[0]))
    result = cv2.matchTemplate(cur_g, ref_g, cv2.TM_CCOEFF_NORMED)
    return float(result[0, 0])


def clamp_xy_correction(
    proposed: int, per_attempt_limit: int, current_offset: int,
    total_min: int | None, total_max: int | None,
) -> int:
    """Limit a relative XY correction in pulses before issuing a stage move.

    ``current_offset`` and the total bounds use the same origin. A missing
    total bound leaves that direction unrestricted.
    """
    correction = max(-per_attempt_limit, min(per_attempt_limit, proposed))
    target_offset = current_offset + correction
    if total_min is not None:
        target_offset = max(total_min, target_offset)
    if total_max is not None:
        target_offset = min(total_max, target_offset)
    return target_offset - current_offset


def measure_xy_shift_strict(
    reference: np.ndarray, current: np.ndarray, *, minimum_score: float = 0.5,
) -> tuple[int, int, float]:
    """Return a verified central-template shift; fail before unsafe stage moves."""
    if reference.ndim != 3 or current.ndim != 3 or reference.shape != current.shape:
        raise ValueError("Live camera image size differs from the XRD reference image.")
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    cur_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    h, w = ref_gray.shape
    my, mx = h // 5, w // 5
    template = ref_gray[my:h-my, mx:w-mx]
    if min(template.shape) < 5 or float(template.std()) < 2:
        raise ValueError("XRD reference image has insufficient detail for alignment.")
    _, score, _, location = cv2.minMaxLoc(
        cv2.matchTemplate(cur_gray, template, cv2.TM_CCOEFF_NORMED)
    )
    if not np.isfinite(score) or score < minimum_score:
        raise ValueError(f"Image match confidence is too low ({score:.2f}).")
    return location[0] - mx, location[1] - my, float(score)
