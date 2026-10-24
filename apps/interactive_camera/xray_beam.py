"""Persisted X-ray beam pixel shared by Interactive Camera and the scheduler.

``__localdata/xray_beam.json`` is the only copy of the registered beam
position. Interactive Camera writes it; every reader (Interactive Camera
itself, the Experimental Scheduler runner, Sample Positions and the monitor
panel) loads it through this module, so no window keeps a private value that
can drift from the others.

A beam pixel is meaningful only for the camera geometry it was registered
with. The frame size and the microscope Ch6/Ch7/Ch8 pulses are therefore
stored with it, and ``check_xray_beam`` rejects a beam whose geometry no
longer matches.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path

import cv2

LOCALDATA_DIR = Path(__file__).parent / "__localdata"
XRAY_BEAM_PATH = LOCALDATA_DIR / "xray_beam.json"
MICROSCOPE_CHANNELS = (6, 7, 8)
XRAY_BEAM_COLOUR = (0, 0, 255)       # BGR — red


@dataclass(frozen=True)
class XrayBeamPosition:
    x: float
    y: float
    frame_width: int
    frame_height: int
    microscope_pulses: dict[int, int]
    recorded_at: str = ""

    @property
    def point(self) -> tuple[float, float]:
        return self.x, self.y

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y,
            "frame_width": self.frame_width, "frame_height": self.frame_height,
            "microscope_pulses": {f"ch{ch}": int(v) for ch, v in self.microscope_pulses.items()},
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "XrayBeamPosition":
        x, y = float(data["x"]), float(data["y"])
        width, height = int(data["frame_width"]), int(data["frame_height"])
        pulses = {ch: int(data["microscope_pulses"][f"ch{ch}"]) for ch in MICROSCOPE_CHANNELS}
        if not (math.isfinite(x) and math.isfinite(y)) or width <= 0 or height <= 0:
            raise ValueError("X-ray beam position is not finite or has no frame size")
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("X-ray beam position lies outside its camera frame")
        return cls(x, y, width, height, pulses, str(data.get("recorded_at", "")))


def read_microscope_pulses(controller, *, require_stopped: bool = True,
                           channels: tuple[int, ...] = MICROSCOPE_CHANNELS) -> dict[int, int]:
    """Read the selected microscope axes (Ch6/Ch7/Ch8 by default).

    Registration needs a stopped stage. A runtime check may pass
    ``require_stopped=False`` because other axes (e.g. Ch11 oscillation) can
    move legitimately; a changed selected axis still fails the comparison.
    """
    if controller is None:
        raise RuntimeError("Stage controller is not connected")
    if require_stopped and controller.get_is_moving():
        raise RuntimeError("Wait for the stage to stop before using the X-ray beam position")
    pulses = {}
    for ch in channels:
        value = controller.get_ch_pos(ch)
        if value is None:
            raise RuntimeError(f"Ch{ch} position could not be read from the stage")
        pulses[ch] = int(value)
    return pulses


def make_xray_beam(x: float, y: float, frame_shape, controller) -> XrayBeamPosition:
    height, width = frame_shape[:2]
    return XrayBeamPosition(
        float(x), float(y), int(width), int(height), read_microscope_pulses(controller),
        datetime.now().isoformat(timespec="seconds"),
    )


def load_xray_beam(path: Path = XRAY_BEAM_PATH) -> XrayBeamPosition | None:
    """Return the registered beam, or None when absent or unreadable."""
    try:
        return XrayBeamPosition.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save_xray_beam(beam: XrayBeamPosition, path: Path = XRAY_BEAM_PATH) -> None:
    """Write atomically so a concurrent reader never sees a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(beam.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_xray_beam(path: Path = XRAY_BEAM_PATH) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def check_xray_beam(beam: XrayBeamPosition | None, frame_shape,
                    microscope_pulses: dict[int, int] | None = None, *,
                    channels: tuple[int, ...] = MICROSCOPE_CHANNELS) -> XrayBeamPosition:
    """Raise ValueError unless ``beam`` applies to this frame and selected axes."""
    if beam is None:
        raise ValueError(
            "X-ray beam position is not registered — set it in Interactive Camera "
            "(right-click → Remember … → X-ray beam position)"
        )
    height, width = frame_shape[:2]
    if (beam.frame_width, beam.frame_height) != (width, height):
        raise ValueError(
            f"X-ray beam position was registered on a {beam.frame_width}×{beam.frame_height} "
            f"image, not {width}×{height}; register it again"
        )
    if microscope_pulses is not None:
        moved = [f"Ch{ch} {beam.microscope_pulses[ch]}→{microscope_pulses[ch]}"
                 for ch in channels
                 if microscope_pulses.get(ch) != beam.microscope_pulses[ch]]
        if moved:
            raise ValueError(
                "The microscope has moved since the X-ray beam position was registered ("
                + ", ".join(moved) + "); return it or register the beam again"
            )
    return beam


def draw_xray_beam(frame, pos) -> None:
    bx, by = round(pos[0]), round(pos[1])
    cv2.drawMarker(frame, (bx, by), XRAY_BEAM_COLOUR, cv2.MARKER_TILTED_CROSS, 18, 2)
    cv2.circle(frame, (bx, by), 12, XRAY_BEAM_COLOUR, 1)


class XrayBeamFileCache:
    """Re-read the beam file only after it changes on disk."""

    def __init__(self, path: Path = XRAY_BEAM_PATH) -> None:
        self._path = Path(path)
        self._key = None
        self._beam: XrayBeamPosition | None = None

    def get(self) -> XrayBeamPosition | None:
        try:
            stat = self._path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        if key != self._key:
            self._key = key
            self._beam = load_xray_beam(self._path) if key is not None else None
        return self._beam
