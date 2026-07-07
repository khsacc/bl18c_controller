"""Shared, hardware-independent helpers reused across the sub-apps.

Sub-packages here hold pure logic (no Qt, no I/O) so it can be imported by
any app without pulling in a widget toolkit — e.g. :mod:`utils.fitting`.
"""
