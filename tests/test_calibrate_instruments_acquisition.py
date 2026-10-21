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

    def capture_triggered(self, ms, timeout_ms=None):
        self.exposure_ms = ms
        self.timeout_ms = timeout_ms
        if self.fail:
            raise RuntimeError("simulated detector failure")
        return np.array([[5, 7], [9, 11]], dtype=np.uint16)


class _RadiconSettings:
    _process_frame = RadiconWindow._process_frame
    _begin_external_acquisition = RadiconWindow._begin_external_acquisition
    _end_external_acquisition = RadiconWindow._end_external_acquisition

    def __init__(self, dark_enabled, defect_mode="none"):
        self._exp_spin = QtWidgets.QDoubleSpinBox()
        self._exp_spin.setRange(0.001, 60.0)
        self._exp_spin.setValue(60.0)
        self._flip_v_chk = QtWidgets.QCheckBox()
        self._flip_v_chk.setChecked(True)
        self._flip_h_chk = QtWidgets.QCheckBox()
        self._dark_correct_chk = QtWidgets.QCheckBox()
        self._dark_correct_chk.setChecked(dark_enabled)
        self._dark_img = np.full((2, 2), 2.0)
        self._defect_mode = defect_mode
        self._defect_mask = np.array([[True, False], [False, False]])
        self._defect_kernel_combo = QtWidgets.QComboBox()
        self._defect_kernel_combo.addItem("3 x 3")
        self._external_acquisition = False
        self.controls_locked = False

    def _current_defect_mode(self):
        return self._defect_mode

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


def test_take_xrd_uses_radicon_defect_setting():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend()
    settings = _RadiconSettings(dark_enabled=True, defect_mode="median")
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: settings,
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    window._on_take_xrd(row)
    deadline = time.monotonic() + 5
    while settings._external_acquisition and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert row.position.image is not None
    np.testing.assert_array_equal(row.position.image, [[7, 9], [7, 5]])
    window.close()


def test_take_xrd_rejects_mismatched_correction_mask():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    settings = _RadiconSettings(dark_enabled=False, defect_mode="median")
    settings._defect_mask = np.zeros((3, 3), dtype=bool)
    window = CalibrateInstrumentsWindow(
        backend=_Backend(), get_radicon_window=lambda: settings,
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

    assert row.position.image is None
    assert len(messages) == 1 and messages[0]
    assert not settings.controls_locked
    window.close()


def test_take_xrd_prompts_to_open_radicon_before_acquisition(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["test", "-platform", "offscreen"]
    )
    backend = _Backend()
    opened = []
    window = CalibrateInstrumentsWindow(
        backend=backend, get_radicon_window=lambda: None,
        open_radicon_window=lambda: opened.append(True),
    )
    window._ch9_timer.stop()
    row = window._rows[0]
    row.position.mgs_mm = 1.0
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        lambda *args: QtWidgets.QMessageBox.StandardButton.Yes,
    )

    window._on_take_xrd(row)

    assert opened == [True]
    assert backend.exposure_ms is None
    assert row.position.image is None
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
