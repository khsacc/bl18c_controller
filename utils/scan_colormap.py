"""Colour maps shared by the two-dimensional scan windows."""

import pyqtgraph as pg


SCAN_COLORMAP_NAMES = (
    "magma", "inferno", "plasma", "viridis",
    "black -> white", "white -> black",
)


def get_scan_colormap(name: str) -> pg.ColorMap:
    """Return a map whose colours run from the lowest to highest value."""
    if name == "black -> white":
        return pg.ColorMap([0.0, 1.0], [(0, 0, 0), (255, 255, 255)], name=name)
    if name == "white -> black":
        return pg.ColorMap([0.0, 1.0], [(255, 255, 255), (0, 0, 0)], name=name)
    return pg.colormap.get(name)
