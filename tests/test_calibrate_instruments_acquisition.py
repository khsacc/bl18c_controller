"""Detector-calibration acquisition tests with no hardware access."""

import time

import numpy as np
from PyQt6 import QtWidgets

from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from apps.calibrate_instruments.calibrate_instruments_app import CalibrateInstrumentsWindow


class _Backend:
    def __init__(self, fail=False):
        self.exposure_ms = None
        self.timeout_ms = None
        self.fail = fail

    def _update_stored_exposure(self, ms):
        self.exposure_ms = ms

    def snap_triggered(self, timeout_ms=None):
        self.timeout_ms = timeout_ms
        if self.fail:
            raise RuntimeError("simulated detector failure")
        return np.array([[5, 7], [9, 11]], dtype=np.uint16)

    def snap(self, timeout_ms=None):
        raise AssertionError("Calibration must use triggered acquisition")


class _RadiconSettings:
    _apply_flip = RadiconWindow._apply_flip
    _dark_correct = RadiconWindow._dark_correct
    _begin_external_acquisition = RadiconWindow._begin_external_acquisition
    _end_external_acquisition = RadiconWindow._end_external_acquisition

    def __init__(self, dark_enabled):
        self._exp_spin = QtWidgets.QDoubleSpinBox()
        self._exp_spin.setRange(0.001, 60.0)
        self._exp_spin.setValue(60.0)
        self._flip_v_chk = QtWidgets.QCheckBox()
        self._flip_v_chk.setChecked(True)
        self._flip_h_chk = QtWidgets.QCheckBox()
        self._dark_correct_chk = QtWidgets.QCheckBox()
        self._dark_correct_chk.setChecked(dark_enabled)
        self._dark_img = np.full((2, 2), 2.0)
        self._external_acquisition = False
        self.controls_locked = False

    def _any_busy(self):
        return self._external_acquisition

    def _set_busy(self, busy):
        self.controls_locked = busy


def test_take_xrd_uses_radicon_exposure_and_dark_setting():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend()
    settings = _RadiconSettings(dark_enabled=True)
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    captured = []
    window._commit_position_image = (
        lambda row, image, ch9: captured.append(image.copy())
    )

    window._on_take_xrd(row)
    deadline = time.monotonic() + 5
    while settings._external_acquisition and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not settings._external_acquisition
    assert not settings.controls_locked
    assert backend.exposure_ms == 60_000
    assert backend.timeout_ms == 130_000
    assert len(captured) == 1
    np.testing.assert_array_equal(captured[0], [[7, 9], [3, 5]])
    window.close()


def test_take_xrd_releases_radicon_on_detector_error():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend(fail=True)
    settings = _RadiconSettings(dark_enabled=False)
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    messages = []
    original_critical = QtWidgets.QMessageBox.critical
    QtWidgets.QMessageBox.critical = lambda *args: messages.append(args[-1])
    try:
        window._on_take_xrd(row)
        deadline = time.monotonic() + 5
        while settings._external_acquisition and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    finally:
        QtWidgets.QMessageBox.critical = original_critical

    assert not settings._external_acquisition
    assert not settings.controls_locked
    assert row.position.image is None
    assert messages == ["simulated detector failure"]
    window.close()
