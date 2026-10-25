"""
Interactive Camera mark drawing and persistence, shared with other windows.

The Interactive Camera app stores its user-drawn marks (circle / rect / line /
cross) in ``__localdata/shapes.json`` and the laser-spot position in
``__localdata/laser_spot.json``. The Experimental Scheduler's monitor panel
reads the same files (via ``MarksFileCache``) and draws them with the same
helpers so both windows show identical overlays.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import cv2

LOCALDATA_DIR = Path(__file__).parent / "__localdata"
SHAPES_PATH = LOCALDATA_DIR / "shapes.json"
LASER_SPOT_PATH = LOCALDATA_DIR / "laser_spot.json"

MARK_COLOUR = (255, 255, 255)        # BGR — unselected mark
LASER_SPOT_COLOUR = (255, 255, 255)  # BGR — white


def draw_shape_on_frame(frame, shape: dict, color, thickness=None) -> None:
    if thickness is None:
        thickness = shape.get('thickness', 1.5)
    thickness = max(1, int(thickness))
    t = shape['type']
    if t == 'circle':
        if 'cx' in shape:
            cv2.circle(frame, (shape['cx'], shape['cy']), max(1, shape['r']), color, thickness)
        else:
            # preview uses x1/y1 as center, x2/y2 as edge point
            x1, y1, x2, y2 = shape['x1'], shape['y1'], shape['x2'], shape['y2']
            r = max(1, int(math.hypot(x2 - x1, y2 - y1)))
            cv2.circle(frame, (x1, y1), r, color, thickness)
    elif t == 'rect':
        x1 = min(shape['x1'], shape['x2'])
        y1 = min(shape['y1'], shape['y2'])
        x2 = max(shape['x1'], shape['x2'])
        y2 = max(shape['y1'], shape['y2'])
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)
    elif t == 'line':
        cv2.line(frame, (shape['x1'], shape['y1']), (shape['x2'], shape['y2']), color, thickness)
    elif t == 'cross':
        if 'cx' in shape:
            cx, cy, r = shape['cx'], shape['cy'], max(1, shape['r'])
        else:
            cx, cy = shape['x1'], shape['y1']
            r = max(1, int(math.hypot(shape['x2'] - cx, shape['y2'] - cy)))
        cv2.line(frame, (cx - r, cy), (cx + r, cy), color, thickness)
        cv2.line(frame, (cx, cy - r), (cx, cy + r), color, thickness)


def draw_laser_spot(frame, pos: tuple[int, int]) -> None:
    lx, ly = pos
    r = 8
    height, width = frame.shape[:2]
    x0, y0 = max(0, lx - r), max(0, ly - r)
    x1, y1 = min(width, lx + r + 1), min(height, ly + r + 1)
    if x0 >= x1 or y0 >= y1:
        return
    region = frame[y0:y1, x0:x1]
    overlay = region.copy()
    cv2.circle(overlay, (lx - x0, ly - y0), r, LASER_SPOT_COLOUR, 1)
    cv2.addWeighted(overlay, 0.7, region, 0.3, 0, dst=region)


def draw_saved_marks(frame, shapes: list, laser_spot: tuple[int, int] | None) -> None:
    """Draw persisted marks the way the Interactive Camera shows them with
    nothing selected. A malformed shape is skipped rather than aborting."""
    if laser_spot is not None:
        draw_laser_spot(frame, laser_spot)
    for shape in shapes:
        try:
            draw_shape_on_frame(frame, shape, MARK_COLOUR)
        except (KeyError, TypeError, ValueError, cv2.error):
            continue


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class MarksFileCache:
    """Reads shapes.json / laser_spot.json, re-reading only after a file
    changes on disk, so callers can poll it on every frame."""

    def __init__(self, shapes_path: Path = SHAPES_PATH,
                 laser_spot_path: Path = LASER_SPOT_PATH) -> None:
        self._shapes_path = Path(shapes_path)
        self._laser_spot_path = Path(laser_spot_path)
        self._shapes_mtime: float | None = None
        self._laser_mtime: float | None = None
        self._shapes: list = []
        self._laser_spot: tuple[int, int] | None = None

    def get(self) -> tuple[list, tuple[int, int] | None]:
        mtime = _mtime(self._shapes_path)
        if mtime != self._shapes_mtime:
            self._shapes_mtime = mtime
            try:
                shapes = json.loads(self._shapes_path.read_text(encoding="utf-8")).get('shapes', [])
                self._shapes = [s for s in shapes if isinstance(s, dict)]
            except (OSError, ValueError, AttributeError):
                self._shapes = []
        mtime = _mtime(self._laser_spot_path)
        if mtime != self._laser_mtime:
            self._laser_mtime = mtime
            try:
                data = json.loads(self._laser_spot_path.read_text(encoding="utf-8"))
                self._laser_spot = (int(data['x']), int(data['y']))
            except (OSError, ValueError, KeyError, TypeError):
                self._laser_spot = None
        return self._shapes, self._laser_spot
