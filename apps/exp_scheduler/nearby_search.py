"""Bounded pulse grids for the optional ruby-fluorescence nearby search."""
from __future__ import annotations

import math
from itertools import product
from typing import TYPE_CHECKING

from utils.stage.control_stage import PULSE_SCALE

if TYPE_CHECKING:
    from .actions import TakeSpectrumAction

MAX_SEARCH_POINTS = 512


def build_search_axes(action: "TakeSpectrumAction") -> tuple[tuple[int, tuple[int, ...]], ...]:
    """Return selected axes and offsets from the nominal ruby position.

    Each axis includes the nominal centre, both requested endpoints, and
    intermediate points no more than its own step apart. Rounding to whole
    motor pulses can merge nearby points, especially on Ch3/Ch4.
    """
    if not action.search_best_position_nearby:
        return ()
    channels = action.search_channels
    if not isinstance(channels, (tuple, list)) or not channels:
        raise ValueError("Nearby search needs at least one of Ch3, Ch4, Ch5")
    if (len(channels) > 3 or len(set(channels)) != len(channels)
            or any(type(ch) is not int or ch not in (3, 4, 5) for ch in channels)):
        raise ValueError("Nearby search channels must be unique Ch3, Ch4, Ch5 values")

    axes = []
    upper_count = 1
    for ch in sorted(channels):
        width = getattr(action, f"search_range_ch{ch}_um")
        step = getattr(action, f"search_step_ch{ch}_um")
        if (not isinstance(width, (int, float)) or isinstance(width, bool)
                or not math.isfinite(width) or width <= 0):
            raise ValueError(f"Ch{ch} nearby search range must be finite and positive")
        if (not isinstance(step, (int, float)) or isinstance(step, bool)
                or not math.isfinite(step) or step <= 0):
            raise ValueError(f"Ch{ch} nearby search step must be finite and positive")
        half = width / 2.0
        intervals = math.floor(half / step)
        upper_count *= 2 * intervals + 3
        if upper_count > MAX_SEARCH_POINTS:
            raise ValueError(f"Nearby search exceeds {MAX_SEARCH_POINTS} points")
        distances_um = [-half, 0.0, half]
        for index in range(1, intervals + 1):
            distances_um.extend((-index * step, index * step))
        pulse_offsets = tuple(sorted({int(round(value / PULSE_SCALE[ch])) for value in distances_um}))
        if len(pulse_offsets) < 2:
            raise ValueError(f"Ch{ch} search range is smaller than one motor pulse")
        axes.append((ch, pulse_offsets))
    return tuple(axes)


def search_positions(
    nominal: dict[int, int], axes: tuple[tuple[int, tuple[int, ...]], ...],
) -> list[dict[int, int]]:
    """Enumerate the full Cartesian grid in stable channel order."""
    return [
        {**nominal, **{ch: nominal[ch] + offset for (ch, _), offset in zip(axes, offsets)}}
        for offsets in product(*(offsets for _, offsets in axes))
    ]
