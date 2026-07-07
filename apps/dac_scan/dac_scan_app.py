"""DAC Scan (Normal) window.

Thin, Ch4 (X) / Ch5 (Y) fixed specialization of the generic
``Free2DScanWindow`` (``apps/scan2d/free_2d_scan_app.py``). All scan, fit,
plotting, and log-saving logic lives in the base class; this module only
pins the channel pair, disables the channel pulldowns, and keeps the
existing window title and log directory (``dac_scan``) so on-disk logs and
saved sessions stay where users expect them.
"""
from __future__ import annotations

try:
    from apps.scan2d.free_2d_scan_app import Free2DScanWindow
    from apps.scan2d.free_2d_scan_backend import GpibReader
except ImportError:
    import os, sys
    _root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    sys.path.insert(0, _root)
    from apps.scan2d.free_2d_scan_app import Free2DScanWindow
    from apps.scan2d.free_2d_scan_backend import GpibReader


class DacScanWindow(Free2DScanWindow):
    """DAC Scan (Normal) — 2-D transmission mapping over Ch4 (X) / Ch5 (Y)."""

    def __init__(
        self,
        controller=None,
        gpib_reader: GpibReader | None = None,
        debug: bool = False,
        parent=None,
    ):
        super().__init__(
            controller=controller,
            gpib_reader=gpib_reader,
            debug=debug,
            parent=parent,
            default_ch_x=4,
            default_ch_y=5,
            allow_channel_change=False,
            log_key="dac_scan",
            window_title="DAC Scan (Normal)",
        )
