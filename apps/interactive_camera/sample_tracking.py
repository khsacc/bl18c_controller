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
