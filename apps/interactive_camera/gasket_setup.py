"""Saved gasket-tracking setup of Interactive Camera's Gasket Detection tab.

``__localdata/gasket_tracking.json`` holds the reference photo path, gasket
outline, sample point, capture position and ruby positions (the keys used
by the Sample Positions dialog) plus the tab's settings. The Experimental
Scheduler can import it with ``sample_positions_result``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np

from .gasket_tracking import GasketEllipse, reference_digest
from .gasket_units import RUBY_ORIGIN_GASKET_CENTRE, RUBY_ORIGIN_XRD_POSITION

STATE_PATH = Path(__file__).parent / "__localdata" / "gasket_tracking.json"


def load_state(path: Path | None = None) -> dict:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class SamplePositionsResult:
    """The attributes a closed ``SamplePositionsDialog`` exposes."""
    reference_path: Path | None
    reference_frame: np.ndarray | None
    reference_changed: bool
    save_directory: Path
    xrd_position: tuple[int, int, int] | None
    ruby_positions: dict
    ruby_offset_origin: str
    gasket_ellipse: dict | None
    gasket_sample_point: dict | None
    gasket_sample_mode: str
    gasket_reference_digest: str | None
    gasket_reference_position: tuple[int, int, int] | None
    gasket_changed: bool = True
    # Validated tab settings present in the saved setup: tolerance_um,
    # average_frames, allow_uniform_scale.
    tracking_settings: dict = field(default_factory=dict)


def _tracking_settings(saved) -> dict:
    """Validate the saved tracking settings; raise ValueError if any is invalid."""
    if not isinstance(saved, dict):
        raise ValueError("Interactive Camera gasket settings are not readable.")
    result = {}
    if "tolerance_um" in saved:
        value = saved["tolerance_um"]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= 100):
            raise ValueError(f"Invalid gasket tolerance in Interactive Camera: {value!r} "
                             "(expected 0–100 µm).")
        result["tolerance_um"] = float(value)
    if "average_frames" in saved:
        value = saved["average_frames"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
            raise ValueError(f"Invalid average frame count in Interactive Camera: {value!r} "
                             "(expected 1–10).")
        result["average_frames"] = value
    if "allow_uniform_scale" in saved:
        value = saved["allow_uniform_scale"]
        if not isinstance(value, bool):
            raise ValueError(f"Invalid ellipse scaling setting in Interactive Camera: {value!r}.")
        result["allow_uniform_scale"] = value
    return result


def sample_positions_result(state: dict | None = None) -> SamplePositionsResult:
    """Interactive Camera's saved setup as a Sample Positions result.

    Raises ``ValueError`` when the setup cannot be used: no reference photo,
    an unreadable photo, an outline marked on different image bytes, no
    sample position, or an invalid saved value. Nothing is applied by this
    function, so a caller can validate everything before changing anything.
    """
    state = load_state() if state is None else state
    try:
        return _result(state)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"Interactive Camera gasket setup is not readable: {exc}") from exc


def _result(state: dict) -> SamplePositionsResult:
    path = state.get("reference_path")
    if path and not isinstance(path, str):
        raise ValueError(f"Invalid reference photo path in Interactive Camera: {path!r}.")
    if not path:
        raise ValueError("Interactive Camera has no gasket tracking reference photo.")
    path = Path(path)
    frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(f"Cannot read the Interactive Camera reference photo: {path}")
    if not state.get("gasket_ellipse") or not state.get("gasket_reference_digest"):
        raise ValueError("Mark the gasket outline in Interactive Camera first.")
    if reference_digest(path) != state["gasket_reference_digest"]:
        raise ValueError("The Interactive Camera gasket outline belongs to a different "
                         "reference image; mark it again.")
    if not state.get("gasket_sample_point"):
        raise ValueError("Mark the sample position in Interactive Camera first.")

    def position(key):
        value = state.get(key)
        if not value:
            return None
        if (not isinstance(value, (list, tuple)) or len(value) != 3
                or any(isinstance(v, bool) or not isinstance(v, int) for v in value)):
            raise ValueError(f"Invalid {key} in Interactive Camera: {value!r}.")
        return tuple(value)

    rubies = state.get("ruby_positions") or {}
    if not isinstance(rubies, dict) or any(
            not isinstance(values, dict)
            or set(values) != {"ch3", "ch4", "ch5"}
            or any(isinstance(v, bool) or not isinstance(v, int) for v in values.values())
            for values in rubies.values()):
        raise ValueError("Invalid ruby positions in Interactive Camera.")
    ellipse = GasketEllipse.from_dict(state["gasket_ellipse"]).to_dict()
    point = state["gasket_sample_point"]
    sample_point = {"x": float(point["x"]), "y": float(point["y"])}
    if not all(math.isfinite(v) for v in sample_point.values()):
        raise ValueError("Invalid sample position in Interactive Camera.")
    origin = state.get("ruby_offset_origin", RUBY_ORIGIN_GASKET_CENTRE)
    if origin not in (RUBY_ORIGIN_GASKET_CENTRE, RUBY_ORIGIN_XRD_POSITION):
        raise ValueError(f"Invalid ruby offset origin in Interactive Camera: {origin!r}.")
    sample_mode = state.get("gasket_sample_mode", "manual")
    if sample_mode not in ("manual", "gasket_centre"):
        raise ValueError(f"Invalid sample mode in Interactive Camera: {sample_mode!r}.")
    tracking_settings = _tracking_settings(state.get("settings", {}))

    return SamplePositionsResult(
        reference_path=path, reference_frame=frame, reference_changed=True,
        save_directory=path.parent,
        xrd_position=position("xrd_position") or position("gasket_reference_position"),
        ruby_positions={name: dict(values) for name, values in rubies.items()},
        ruby_offset_origin=origin,
        gasket_ellipse=ellipse,
        gasket_sample_point=sample_point,
        gasket_sample_mode=sample_mode,
        gasket_reference_digest=state["gasket_reference_digest"],
        gasket_reference_position=position("gasket_reference_position"),
        tracking_settings=tracking_settings,
    )
